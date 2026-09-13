"""The Conversation Runtime: a small, deterministic executor that drives a
single `CallAttempt` against a `TelephonyProvider` and returns an
`ExecutionResult`. This module owns ZERO storage/persistence — it is a
pure function of (context, provider, timeout, ...) to a result plus an
ordered in-memory event list; `orchestrator/worker_runtime.py` is what
persists that result, keeping this module trivially unit-testable without
a database and keeping the storage-access rule in app/layers.py
uncomplicated (storage.db.get_session is the only session factory, and
nothing here needs one).

Phase 2 fills the extension point named in the module's original Phase 1
docstring (see docs/PHASE1_DESIGN.md "Phase 2 extension point"): once the
provider reaches CONNECTED, if a `ConversationEngineConfig` is supplied,
this module hands off to `conversation/semantic_loop.py::run_conversation`
for the actual multi-turn semantic exchange, then maps its outcome back
onto Phase 1's `ExecutionResult` vocabulary. When `conversation_engine` is
`None` (the default, and the only thing every existing Phase 1 test still
passes), behavior is BYTE-IDENTICAL to Phase 1 — nothing about admission,
idempotency, the queue, worker lifecycle, tenant isolation, retry policy,
state machines, or the provider boundary changed to make this possible,
exactly as required.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from telephony.contracts import CallAttemptContext, FailureCategory, ProviderEventType, TelephonyProvider

if TYPE_CHECKING:
    from conversation.semantic_loop import ProspectTurnSource, TurnPersistCallback
    from guardrails.policy import GuardrailContext
    from intelligence.contracts import ActionCategory, ConversationState
    from intelligence.llm_provider import LLMProvider

# Provider events that end the call, mapped to whether they represent a
# successful execution (COMPLETED) or not. This is the ONE place execution
# outcome is decided from a provider event type — not scattered
# if/elif chains at call sites.
_TERMINAL_EVENT_TYPES = frozenset(
    {
        ProviderEventType.COMPLETED,
        ProviderEventType.BUSY,
        ProviderEventType.NO_ANSWER,
        ProviderEventType.FAILED,
        ProviderEventType.CANCELLED,
    }
)


@dataclass(frozen=True)
class ConversationEvent:
    """One ordered event recorded during execution. `orchestrator` assigns
    sequence numbers when persisting these — this module just guarantees
    list order matches wall-clock/causal order."""

    event_type: str
    detail: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    outcome: Literal["completed", "failed"]
    disposition: str
    failure_category: FailureCategory | None
    events: tuple[ConversationEvent, ...]


@dataclass(frozen=True)
class ConversationEngineConfig:
    """Everything needed to run a Phase 2 semantic conversation once a
    call connects — bundled into one object rather than exploding
    `execute_call_attempt`'s signature with a dozen new parameters.
    Optional and injected: `None` (the default everywhere) means "Phase 1
    behavior, no semantic layer" — see module docstring.
    """

    llm_provider: LLMProvider
    prospect_source: ProspectTurnSource
    guardrail_context: GuardrailContext
    call_id: uuid.UUID
    organization_id: int
    lead_id: int
    objective: str
    permitted_actions: tuple[ActionCategory, ...] | None = None
    permitted_tools: tuple[str, ...] = ()
    max_turns: int = 30
    persist_callback: TurnPersistCallback | None = None
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)


async def _run_semantic_conversation(
    context: CallAttemptContext, engine: ConversationEngineConfig
) -> tuple[str, ConversationState]:
    """Isolated so `execute_call_attempt` doesn't need to know
    `intelligence`/`guardrails` internals beyond this one call — imports
    are deferred to inside this function specifically so a plain Phase 1
    call (`conversation_engine=None`) never even imports the Phase 2
    packages, keeping the "byte-identical when unconfigured" claim true
    at the import level too, not just the behavioral one."""
    from conversation.semantic_loop import run_conversation
    from intelligence.context import DEFAULT_PERMITTED_ACTIONS
    from intelligence.contracts import initial_state

    state = initial_state(engine.session_id, engine.objective, context_version=1)
    result = await run_conversation(
        engine.llm_provider,
        state,
        engine.prospect_source,
        call_id=engine.call_id,
        call_attempt_id=context.call_attempt_id,
        session_id=engine.session_id,
        organization_id=engine.organization_id,
        lead_id=engine.lead_id,
        guardrail_context=engine.guardrail_context,
        permitted_actions=engine.permitted_actions or DEFAULT_PERMITTED_ACTIONS,
        permitted_tools=engine.permitted_tools,
        max_turns=engine.max_turns,
        persist_callback=engine.persist_callback,
    )
    return result.outcome_category, result.final_state


async def execute_call_attempt(
    context: CallAttemptContext,
    provider: TelephonyProvider,
    timeout_seconds: float,
    conversation_engine: ConversationEngineConfig | None = None,
) -> ExecutionResult:
    """Drives one attempt to completion. Deliberately does NOT catch
    `asyncio.CancelledError` from a genuine outer cancellation (worker
    shutdown) — that must propagate so the caller's shutdown logic
    (docs/PHASE1_DESIGN.md "Shutdown") sees it and leaves the attempt
    RUNNING in Postgres for the reaper to recover, rather than this
    function silently converting a shutdown into a fabricated "failed"
    result. This applies equally to the Phase 2 semantic conversation
    below — a shutdown cancellation during a live conversation gets the
    exact same treatment as one during the deterministic telephony wait,
    by construction (the semantic loop is awaited inside the SAME
    `asyncio.timeout` block, so cancellation propagates through it
    unchanged).

    Once CONNECTED, if `conversation_engine` is provided, hands off to
    `conversation/semantic_loop.py` for the actual multi-turn exchange
    instead of continuing to iterate the provider's own event stream —
    Phase 2's scope (docs/PHASE2_DESIGN.md) doesn't yet need to signal a
    real hang-up back to the provider once the semantic conversation ends
    (that's a real-telephony-adapter concern, a later phase's problem,
    documented as a known limitation, not silently glossed over).
    """
    events: list[ConversationEvent] = [ConversationEvent(event_type="session_started")]
    try:
        async with asyncio.timeout(timeout_seconds):
            async for provider_event in provider.place_call(context):
                events.append(
                    ConversationEvent(event_type=provider_event.type.value, detail=provider_event.detail)
                )
                if provider_event.type is ProviderEventType.CONNECTED and conversation_engine is not None:
                    events.append(ConversationEvent(event_type="conversation_started"))
                    try:
                        outcome_category, _final_state = await _run_semantic_conversation(
                            context, conversation_engine
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # A model/provider/parsing hiccup during the
                        # conversation is treated as transient
                        # infrastructure, not a permanent execution
                        # failure — it flows through the SAME retry
                        # policy Phase 1 already has for any other
                        # transient failure (orchestrator/failures.py),
                        # no new failure-handling machinery for Phase 2.
                        events.append(ConversationEvent(event_type="conversation_error", detail=str(exc)))
                        events.append(ConversationEvent(event_type="session_ended"))
                        return ExecutionResult(
                            outcome="failed",
                            disposition="conversation_error",
                            failure_category=FailureCategory.TRANSIENT_INFRA,
                            events=tuple(events),
                        )
                    events.append(ConversationEvent(event_type="conversation_ended", detail=outcome_category))
                    events.append(ConversationEvent(event_type="session_ended"))
                    return ExecutionResult(
                        outcome="completed",
                        disposition=outcome_category,
                        failure_category=None,
                        events=tuple(events),
                    )
                if provider_event.type in _TERMINAL_EVENT_TYPES:
                    events.append(ConversationEvent(event_type="session_ended"))
                    if provider_event.type is ProviderEventType.COMPLETED:
                        return ExecutionResult(
                            outcome="completed",
                            disposition=provider_event.type.value,
                            failure_category=None,
                            events=tuple(events),
                        )
                    return ExecutionResult(
                        outcome="failed",
                        disposition=provider_event.type.value,
                        failure_category=provider_event.failure_category or FailureCategory.PROVIDER,
                        events=tuple(events),
                    )
    except TimeoutError:
        events.append(ConversationEvent(event_type="timeout"))
        events.append(ConversationEvent(event_type="session_ended"))
        return ExecutionResult(
            outcome="failed",
            disposition="timeout",
            failure_category=FailureCategory.TIMEOUT,
            events=tuple(events),
        )

    # The provider's async generator ended without yielding a terminal
    # event — a contract violation by the provider (see
    # telephony/contracts.py: "ending in exactly one terminal event"), not
    # a scenario the runtime should silently paper over.
    events.append(ConversationEvent(event_type="session_ended"))
    return ExecutionResult(
        outcome="failed",
        disposition="provider_contract_violation",
        failure_category=FailureCategory.PERMANENT_EXECUTION,
        events=tuple(events),
    )
