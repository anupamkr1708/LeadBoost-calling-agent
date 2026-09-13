"""The semantic turn pipeline (docs/PHASE2_DESIGN.md "Target turn
pipeline" / master prompt §4, §10): ConversationInput -> interpret ->
reconcile -> plan -> authorize -> respond -> AgentTurn. This module owns
ORCHESTRATION ONLY — every actual decision (what something means, what
changed, what to do, whether it's allowed, how to phrase it) is made by
`intelligence/interpreter.py`, `state.py`, `planner.py`,
`guardrails/policy.py`, and `responder.py` respectively. This file just
calls them in order and threads state through.

`run_conversation` is the multi-turn loop that
`conversation/runtime.py::execute_call_attempt` invokes once a call
reaches CONNECTED (docs/PHASE2_DESIGN.md "Production execution
integration") — it owns turn-counting and the max-turns safety bound
(itself a deterministic guardrail, not a semantic one: an unbounded loop
is an infrastructure risk regardless of how good the model is), but still
delegates every conversational decision downstream.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from guardrails.policy import GuardrailContext, authorize, authorized_action_from
from intelligence.context import DEFAULT_PERMITTED_ACTIONS, assemble_planning_context
from intelligence.contracts import (
    ActionCategory,
    AgentTurn,
    ConversationInput,
    ConversationPlan,
    ConversationState,
    GuardrailResult,
    ModelInvocationMeta,
    SemanticInterpretation,
    Speaker,
    TranscriptTurn,
)
from intelligence.interpreter import interpret_turn
from intelligence.llm_provider import LLMProvider
from intelligence.observability import log_turn
from intelligence.planner import propose_next_action
from intelligence.responder import generate_response
from intelligence.state import reconcile, record_action_taken

# Deterministic safety bound, not a semantic decision — an unbounded
# conversation loop is an infrastructure risk (a stuck/adversarial
# exchange could run forever) regardless of how good the model is. This
# mirrors Phase 1's own posture on bounded resources (queue_lease_seconds,
# max_concurrent_calls): a plain, configurable ceiling, not "smart" logic.
DEFAULT_MAX_TURNS = 30


@dataclass(frozen=True)
class TurnOutcome:
    """One turn's complete result — everything `intelligence/observability.py`
    logs and everything a replay harness needs to reconstruct the turn
    later, in one place."""

    agent_turn: AgentTurn
    state_before: ConversationState
    state_after: ConversationState
    interpretation: SemanticInterpretation
    interpretation_meta: ModelInvocationMeta
    plan: ConversationPlan
    planner_meta: ModelInvocationMeta
    guardrail_result: GuardrailResult
    response_meta: ModelInvocationMeta | None


class ProspectTurnSource(Protocol):
    """Supplies the next prospect utterance, given the conversation so
    far — the text-conversation analogue of a real speech pipeline's STT
    output (docs/PHASE2_DESIGN.md "Phase 2 scope": text-only, no STT/TTS
    yet). Returns `None` when the prospect has nothing more to say (e.g.
    they hung up) — a real, valid outcome, not an error. Implemented by
    fixture-based sources for tests/eval; a real speech-derived source is
    a later phase's concern, same boundary pattern as
    `telephony.contracts.TelephonyProvider`."""

    async def next_utterance(self, state: ConversationState) -> str | None: ...


class TurnPersistCallback(Protocol):
    """Injected by the caller (`conversation/runtime.py`, wrapping
    `conversation/persistence.py::persist_turn` in `asyncio.to_thread`
    since Phase 1's DB layer is synchronous) — kept OPTIONAL and
    injectable rather than hardcoded so `run_conversation` stays testable
    with pure asyncio and no database at all
    (tests/unit/test_semantic_loop_pipeline.py), matching this codebase's
    established DI philosophy (docs/PHASE1_DESIGN.md "Composition root")."""

    async def __call__(self, prospect_utterance: str, outcome: TurnOutcome) -> None: ...


@dataclass(frozen=True)
class ConversationLoopResult:
    outcome_category: str  # e.g. "meeting_booked", "not_interested", "opted_out", "prospect_ended", "max_turns_reached"
    turns: tuple[TurnOutcome, ...]
    final_state: ConversationState


def _blocked_wait_plan(reason: str) -> ConversationPlan:
    """A guardrail rejection becomes a safe, silent WAIT rather than
    crashing the turn or executing the rejected action — a real guardrail
    rejection is an expected, routine outcome (the model proposed
    something not currently allowed), not a system failure."""
    return ConversationPlan(action_category=ActionCategory.WAIT, objective="blocked by guardrails", rationale=reason)


async def run_turn(
    provider: LLMProvider,
    state: ConversationState,
    prospect_utterance: str,
    *,
    call_attempt_id: uuid.UUID,
    organization_id: int,
    lead_id: int,
    recent_turns: tuple[TranscriptTurn, ...],
    guardrail_context: GuardrailContext,
    permitted_actions: tuple[ActionCategory, ...] = DEFAULT_PERMITTED_ACTIONS,
    permitted_tools: tuple[str, ...] = (),
) -> TurnOutcome:
    """Runs exactly ONE turn of the pipeline: interpret the prospect's
    utterance, reconcile it into state, plan the agent's next action,
    authorize it, and generate the response. Does not decide when the
    conversation ends — that's `run_conversation`'s job, reading
    `agent_turn.action.action_category == END_CALL` off this result.
    """
    turn_number = state.turn_count + 1
    turn_input = ConversationInput(
        session_id=state.session_id,
        call_attempt_id=call_attempt_id,
        organization_id=organization_id,
        lead_id=lead_id,
        turn_number=turn_number,
        speaker=Speaker.PROSPECT,
        transcript=prospect_utterance,
        prior_state=state,
        recent_turns=recent_turns,
        objective=state.objective,
        context_version=state.context_version,
    )

    interpretation_result = await interpret_turn(provider, turn_input)
    state_after_interpretation = reconcile(
        state, interpretation_result.interpretation, speaker=Speaker.PROSPECT, turn_number=turn_number
    )

    planning_context = assemble_planning_context(
        state_after_interpretation,
        interpretation_result.interpretation,
        recent_turns,
        permitted_actions=permitted_actions,
        permitted_tools=permitted_tools,
    )
    planning_result = await propose_next_action(provider, planning_context)

    guardrail_result = authorize(planning_result.plan, state_after_interpretation, guardrail_context)
    effective_plan = planning_result.plan if guardrail_result.verdict.value == "authorized" else _blocked_wait_plan(
        guardrail_result.reason
    )
    action = authorized_action_from(effective_plan)

    grounded_facts = tuple(
        belief.value
        for belief in state_after_interpretation.facts
        if belief.status.value == "current" and belief.value
    )
    response_result = await generate_response(provider, action, grounded_facts, planning_context)

    final_state = record_action_taken(state_after_interpretation, action.action_category.value)

    agent_turn = AgentTurn(
        turn_number=turn_number,
        action=action,
        response_text=response_result.response_text if response_result else None,
        grounded_facts_used=grounded_facts,
    )

    return TurnOutcome(
        agent_turn=agent_turn,
        state_before=state,
        state_after=final_state,
        interpretation=interpretation_result.interpretation,
        interpretation_meta=interpretation_result.meta,
        plan=planning_result.plan,
        planner_meta=planning_result.meta,
        guardrail_result=guardrail_result,
        response_meta=response_result.meta if response_result else None,
    )


async def run_conversation(
    provider: LLMProvider,
    initial_state: ConversationState,
    prospect_source: ProspectTurnSource,
    *,
    call_id: uuid.UUID,
    call_attempt_id: uuid.UUID,
    session_id: uuid.UUID,
    organization_id: int,
    lead_id: int,
    guardrail_context: GuardrailContext,
    permitted_actions: tuple[ActionCategory, ...] = DEFAULT_PERMITTED_ACTIONS,
    permitted_tools: tuple[str, ...] = (),
    max_turns: int = DEFAULT_MAX_TURNS,
    persist_callback: TurnPersistCallback | None = None,
) -> ConversationLoopResult:
    """The multi-turn loop `conversation/runtime.py` invokes once
    CONNECTED. Terminates on: the agent choosing END_CALL (guardrail-
    authorized), the prospect source returning `None` (they hung up), or
    `max_turns` (the deterministic safety bound — see module docstring).
    `persist_callback`, if given, is awaited once per turn with the
    prospect's utterance and the full `TurnOutcome` — see
    `TurnPersistCallback`'s docstring for why this is injected rather
    than a hardcoded DB call.
    """
    state = initial_state
    recent_turns: tuple[TranscriptTurn, ...] = ()
    turns: list[TurnOutcome] = []

    while state.turn_count < max_turns:
        prospect_utterance = await prospect_source.next_utterance(state)
        if prospect_utterance is None:
            return ConversationLoopResult(outcome_category="prospect_ended", turns=tuple(turns), final_state=state)

        recent_turns = (*recent_turns, TranscriptTurn(Speaker.PROSPECT, prospect_utterance, state.turn_count + 1))

        outcome = await run_turn(
            provider,
            state,
            prospect_utterance,
            call_attempt_id=call_attempt_id,
            organization_id=organization_id,
            lead_id=lead_id,
            recent_turns=recent_turns,
            guardrail_context=guardrail_context,
            permitted_actions=permitted_actions,
            permitted_tools=permitted_tools,
        )
        turns.append(outcome)
        state = outcome.state_after

        log_turn(
            call_id=call_id,
            attempt_id=call_attempt_id,
            session_id=session_id,
            turn_number=outcome.agent_turn.turn_number,
            state_before=outcome.state_before,
            transcript=prospect_utterance,
            interpretation=outcome.interpretation,
            interpretation_meta=outcome.interpretation_meta,
            state_after=outcome.state_after,
            planner_rationale=outcome.agent_turn.action.rationale,
            planner_meta=outcome.planner_meta,
            guardrail_result=outcome.guardrail_result,
            final_action=outcome.agent_turn.action,
            response_text=outcome.agent_turn.response_text,
            response_meta=outcome.response_meta,
        )

        if outcome.agent_turn.response_text:
            recent_turns = (
                *recent_turns,
                TranscriptTurn(Speaker.AGENT, outcome.agent_turn.response_text, outcome.agent_turn.turn_number),
            )

        if persist_callback is not None:
            await persist_callback(prospect_utterance, outcome)

        if outcome.agent_turn.action.action_category == ActionCategory.END_CALL:
            outcome_category = outcome.agent_turn.action.terminal_outcome or "end_call"
            return ConversationLoopResult(outcome_category=outcome_category, turns=tuple(turns), final_state=state)

    return ConversationLoopResult(outcome_category="max_turns_reached", turns=tuple(turns), final_state=state)
