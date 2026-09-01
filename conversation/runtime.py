"""The Conversation Runtime: a small, deterministic executor that drives a
single `CallAttempt` against a `TelephonyProvider` and returns an
`ExecutionResult`. This module owns ZERO storage/persistence — it is a
pure function of (context, provider, timeout) to a result plus an ordered
in-memory event list; `orchestrator/worker_runtime.py` is what persists
that result, keeping this module trivially unit-testable without a
database and keeping the storage-access rule in app/layers.py
uncomplicated (storage.db.get_session is the only session factory, and
nothing here needs one).

NOT semantic. Nothing in this module inspects transcript content — Phase 1
has no transcript content, because there's no LLM yet. Phase 2 replaces
only the inside of `execute_call_attempt` (see docs/PHASE1_DESIGN.md
"Phase 2 extension point"); everything that calls this function is
unaffected by that swap.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from telephony.contracts import CallAttemptContext, FailureCategory, ProviderEventType, TelephonyProvider

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


async def execute_call_attempt(
    context: CallAttemptContext,
    provider: TelephonyProvider,
    timeout_seconds: float,
) -> ExecutionResult:
    """Drives one attempt to completion. Deliberately does NOT catch
    `asyncio.CancelledError` from a genuine outer cancellation (worker
    shutdown) — that must propagate so the caller's shutdown logic
    (docs/PHASE1_DESIGN.md "Shutdown") sees it and leaves the attempt
    RUNNING in Postgres for the reaper to recover, rather than this
    function silently converting a shutdown into a fabricated "failed"
    result."""
    events: list[ConversationEvent] = [ConversationEvent(event_type="session_started")]
    try:
        async with asyncio.timeout(timeout_seconds):
            async for provider_event in provider.place_call(context):
                events.append(
                    ConversationEvent(event_type=provider_event.type.value, detail=provider_event.detail)
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
