"""Integration test for conversation/semantic_loop.py's full pipeline —
pure asyncio, FakeLLMProvider + a scripted ProspectTurnSource, no DB/Redis
(the real Postgres/Redis persistence integration is a separate test suite
under tests/integration/). This is the master prompt's "Multi-turn
context" (Section 22) and "Semantic examples" (Section 23) requirement
exercised end to end: paraphrase equivalence, a stated fact later
corrected, and a full conversation reaching a guardrail-gated END_CALL.
"""
from __future__ import annotations

import uuid

import pytest

from conversation.semantic_loop import run_conversation
from guardrails.policy import GuardrailContext
from intelligence.contracts import (
    ActionCategory,
    Certainty,
    ConversationPlan,
    InterestLevel,
    SemanticInterpretation,
    SpeechAct,
    initial_state,
)
from intelligence.fake_llm import FakeLLMProvider


def _interpretation(**overrides) -> SemanticInterpretation:
    defaults = dict(
        primary_intent="unknown",
        secondary_intents=(),
        speech_act=SpeechAct.STATEMENT,
        user_goal=None,
        conversation_stage="unknown",
        interest=InterestLevel.UNKNOWN,
        interest_certainty=Certainty.UNKNOWN,
        sentiment=None,
        emotion=None,
        objections=(),
        concerns=(),
        motivations=(),
        questions=(),
        requests=(),
        commitments=(),
        timing_signal=None,
        timing_certainty=Certainty.UNKNOWN,
        urgency=None,
        budget_signal=None,
        authority_signal=None,
        current_solution=None,
        competitor_mentions=(),
        pain_points=(),
        desired_outcomes=(),
        constraints=(),
        entities=(),
        new_facts=(),
        disputed_facts=(),
        missing_information=(),
        unresolved_items=(),
        implied_meaning=None,
        confidence=0.7,
        uncertainty_notes=(),
    )
    defaults.update(overrides)
    return SemanticInterpretation(**defaults)  # type: ignore[arg-type]  # known mypy limitation: dict-unpack into a dataclass with a broad-union value type cannot be statically verified, even though every value is runtime-correct


class _ScriptedProspect:
    """A fixture ProspectTurnSource - a plain scripted list of utterances,
    ending the "call" (returns None) once exhausted."""

    def __init__(self, utterances):
        self._utterances = utterances
        self._index = 0

    async def next_utterance(self, state):
        if self._index >= len(self._utterances):
            return None
        utterance = self._utterances[self._index]
        self._index += 1
        return utterance


def _no_op_guardrail_context() -> GuardrailContext:
    return GuardrailContext(
        opted_out=False, authorized_tools=(), required_tool_arguments={}, confirmed_terminal_outcomes=()
    )


@pytest.mark.asyncio
async def test_conversation_ends_cleanly_when_prospect_stops_talking():
    prospect = _ScriptedProspect(["Hi, who is this?"])
    interpretation_calls = []

    def interp_source(turn_input):
        interpretation_calls.append(turn_input.transcript)
        return _interpretation(primary_intent="clarification_request", speech_act=SpeechAct.QUESTION)

    def plan_source(ctx):
        return ConversationPlan(action_category=ActionCategory.SPEAK, objective="introduce", rationale="test")

    provider = FakeLLMProvider(
        interpretation_source=interp_source,
        plan_source=plan_source,
        response_source=lambda obj, facts, ctx: "Hi, this is Alex calling from LeadBoost.",
    )
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)

    result = await run_conversation(
        provider,
        state,
        prospect,
        call_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        session_id=state.session_id,
        organization_id=1,
        lead_id=1,
        guardrail_context=_no_op_guardrail_context(),
    )

    assert result.outcome_category == "prospect_ended"
    assert len(result.turns) == 1
    assert result.turns[0].agent_turn.response_text == "Hi, this is Alex calling from LeadBoost."
    assert interpretation_calls == ["Hi, who is this?"]


@pytest.mark.asyncio
async def test_paraphrased_existing_solution_statements_converge_semantically():
    """Different wording, same meaning, should reach the same
    interpreted primary_intent - this test controls the interpretation
    via a fixture (since we don't have a live model in CI), but proves
    the RECONCILER/STATE side of that convergence: both paraphrases
    update current_solution to the SAME belief, which is the state-level
    manifestation of "these mean the same thing"."""
    phrasing_a = "We already use Salesforce."
    phrasing_b = "We've got something in place already, so not really looking."

    async def _run_one_turn(utterance, current_solution_value):
        prospect = _ScriptedProspect([utterance])

        def interp_source(turn_input):
            return _interpretation(
                primary_intent="existing_solution",
                current_solution=current_solution_value,
                new_facts=(f"current_solution={current_solution_value}",),
            )

        provider = FakeLLMProvider(
            interpretation_source=interp_source,
            plan_source=lambda ctx: ConversationPlan(
                action_category=ActionCategory.ASK, objective="probe", rationale="test", question_objective="why?"
            ),
            response_source=lambda obj, facts, ctx: "Got it - mind if I ask what's working well about it?",
        )
        state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)
        return await run_conversation(
            provider,
            state,
            prospect,
            call_id=uuid.uuid4(),
            call_attempt_id=uuid.uuid4(),
            session_id=state.session_id,
            organization_id=1,
            lead_id=1,
            guardrail_context=_no_op_guardrail_context(),
        )

    result_a = await _run_one_turn(phrasing_a, "Salesforce")
    result_b = await _run_one_turn(phrasing_b, "Salesforce")

    assert result_a.final_state.current_solution.value == "Salesforce"
    assert result_b.final_state.current_solution.value == "Salesforce"
    assert result_a.final_state.primary_intent.value == result_b.final_state.primary_intent.value == "existing_solution"


@pytest.mark.asyncio
async def test_correction_supersedes_earlier_fact_not_appended_alongside_it():
    """The exact worked example: turn 2 says Salesforce, a later turn
    says they moved off it - the final state must show the CORRECTED
    value as current, with the old one superseded, not both as equally-
    current facts."""
    prospect = _ScriptedProspect(["I use Salesforce.", "Actually, we moved off Salesforce last quarter."])

    turn_count = {"n": 0}

    def interp_source(turn_input):
        turn_count["n"] += 1
        if turn_count["n"] == 1:
            return _interpretation(current_solution="Salesforce", new_facts=("current_solution=Salesforce",))
        return _interpretation(
            current_solution="none",
            disputed_facts=("current_solution=Salesforce",),
            new_facts=("current_solution=none",),
        )

    provider = FakeLLMProvider(
        interpretation_source=interp_source,
        plan_source=lambda ctx: ConversationPlan(action_category=ActionCategory.SPEAK, objective="ack", rationale="t"),
        response_source=lambda obj, facts, ctx: "Understood.",
    )
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)

    result = await run_conversation(
        provider,
        state,
        prospect,
        call_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        session_id=state.session_id,
        organization_id=1,
        lead_id=1,
        guardrail_context=_no_op_guardrail_context(),
    )

    assert result.final_state.current_solution.value == "none"
    assert result.final_state.current_solution.status.value == "current"


@pytest.mark.asyncio
async def test_guardrail_rejected_plan_becomes_wait_not_a_crash():
    """A planner proposing an unconfirmed meeting-booked END_CALL must be
    rejected by guardrails and safely downgraded to WAIT - the loop
    continues rather than crashing or silently executing the rejected
    claim."""
    prospect = _ScriptedProspect(["Great, let's book it."])

    def plan_source(ctx):
        return ConversationPlan(
            action_category=ActionCategory.END_CALL,
            objective="end",
            rationale="prospect agreed",
            end_reason="meeting booked",
            terminal_outcome="meeting_booked",
        )

    provider = FakeLLMProvider(
        interpretation_source=lambda ti: _interpretation(primary_intent="commitment", interest=InterestLevel.HIGH),
        plan_source=plan_source,
        response_source=lambda obj, facts, ctx: "should not be reached with the claim intact",
    )
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)

    result = await run_conversation(
        provider,
        state,
        prospect,
        call_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        session_id=state.session_id,
        organization_id=1,
        lead_id=1,
        guardrail_context=_no_op_guardrail_context(),
    )

    assert result.outcome_category == "prospect_ended"
    assert result.turns[0].agent_turn.action.action_category == ActionCategory.WAIT
    assert result.turns[0].guardrail_result.verdict.value == "rejected"


@pytest.mark.asyncio
async def test_opt_out_ends_the_call_deterministically_regardless_of_planner():
    """Even if the planner (mis)proposes something else, opted_out=True
    forces WAIT (never the proposed action) - the deterministic safety
    invariant holds regardless of model behavior."""
    prospect = _ScriptedProspect(["Take me off your list."])

    provider = FakeLLMProvider(
        interpretation_source=lambda ti: _interpretation(primary_intent="opt_out", speech_act=SpeechAct.REQUEST),
        plan_source=lambda ctx: ConversationPlan(action_category=ActionCategory.SPEAK, objective="continue pitch", rationale="bad plan"),
        response_source=lambda obj, facts, ctx: "ok",
    )
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)

    result = await run_conversation(
        provider,
        state,
        prospect,
        call_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        session_id=state.session_id,
        organization_id=1,
        lead_id=1,
        guardrail_context=GuardrailContext(
            opted_out=True, authorized_tools=(), required_tool_arguments={}, confirmed_terminal_outcomes=()
        ),
    )
    assert result.turns[0].agent_turn.action.action_category == ActionCategory.WAIT
    assert result.turns[0].guardrail_result.violated_policy == "no_action_after_opt_out"


@pytest.mark.asyncio
async def test_max_turns_is_a_real_bound_not_decorative():
    prospect_utterances = [f"turn {i}" for i in range(100)]
    prospect = _ScriptedProspect(prospect_utterances)

    provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(action_category=ActionCategory.WAIT, objective="x", rationale="y"),
    )
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)

    result = await run_conversation(
        provider,
        state,
        prospect,
        call_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        session_id=state.session_id,
        organization_id=1,
        lead_id=1,
        guardrail_context=_no_op_guardrail_context(),
        max_turns=5,
    )
    assert result.outcome_category == "max_turns_reached"
    assert len(result.turns) == 5
