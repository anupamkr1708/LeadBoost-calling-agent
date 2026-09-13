"""Unit tests for conversation/replay.py -- proves replay is a genuine,
deterministic reproduction (same recorded outputs in, same TurnOutcome
out), and that it's actually useful as a regression tool: a guardrail
policy change is DETECTABLE by re-running the same recorded turn.
"""
from __future__ import annotations

import uuid

import pytest

from conversation.replay import TurnRecord, replay_turn
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


def _interpretation(**overrides):
    defaults = dict(
        primary_intent="existing_solution",
        secondary_intents=(),
        speech_act=SpeechAct.STATEMENT,
        user_goal=None,
        conversation_stage="evaluation",
        interest=InterestLevel.CONDITIONAL,
        interest_certainty=Certainty.MODERATE,
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
        current_solution="Salesforce",
        competitor_mentions=(),
        pain_points=(),
        desired_outcomes=(),
        constraints=(),
        entities=(),
        new_facts=("current_solution=Salesforce",),
        disputed_facts=(),
        missing_information=(),
        unresolved_items=(),
        implied_meaning=None,
        confidence=0.85,
        uncertainty_notes=(),
    )
    defaults.update(overrides)
    return SemanticInterpretation(**defaults)  # type: ignore[arg-type]  # known mypy limitation: dict-unpack into a dataclass with a broad-union value type cannot be statically verified, even though every value is runtime-correct


def _record(**plan_overrides) -> TurnRecord:
    plan_defaults = dict(
        action_category=ActionCategory.END_CALL,
        objective="end",
        rationale="prospect agreed",
        end_reason="meeting booked",
        terminal_outcome="meeting_booked",
    )
    plan_defaults.update(plan_overrides)
    return TurnRecord(
        state_before=initial_state(uuid.uuid4(), objective="book_meeting", context_version=1),
        prospect_utterance="We already use Salesforce.",
        recorded_interpretation=_interpretation(),
        recorded_plan=ConversationPlan(**plan_defaults),  # type: ignore[arg-type]
        recorded_response_text="Understood, thanks!",
        guardrail_context=GuardrailContext(
            opted_out=False, authorized_tools=(), required_tool_arguments={}, confirmed_terminal_outcomes=()
        ),
    )


@pytest.mark.asyncio
async def test_replay_is_deterministic_same_inputs_same_outcome():
    record = _record()
    outcome_1 = await replay_turn(record, call_attempt_id=uuid.uuid4(), organization_id=1, lead_id=1)
    outcome_2 = await replay_turn(record, call_attempt_id=uuid.uuid4(), organization_id=1, lead_id=1)

    assert outcome_1.interpretation == outcome_2.interpretation
    assert outcome_1.plan == outcome_2.plan
    assert outcome_1.guardrail_result.verdict == outcome_2.guardrail_result.verdict
    assert outcome_1.agent_turn.action.action_category == outcome_2.agent_turn.action.action_category


@pytest.mark.asyncio
async def test_replay_reproduces_the_recorded_interpretation_exactly():
    record = _record()
    outcome = await replay_turn(record, call_attempt_id=uuid.uuid4(), organization_id=1, lead_id=1)
    assert outcome.interpretation == record.recorded_interpretation
    assert outcome.state_after.current_solution.value == "Salesforce"


@pytest.mark.asyncio
async def test_replay_detects_a_guardrail_policy_regression():
    """The actual regression-testing use case: a recorded turn that was
    ORIGINALLY authorized (a meeting_booked claim WITH tool confirmation
    at the time it was recorded) is replayed with a guardrail_context
    that no longer has that confirmation -- simulating "we recorded this
    turn when it was correctly authorized, but a later policy/config
    change would now reject it". Replay surfaces this directly, without
    needing to re-invoke a model."""
    record = _record()
    # Recorded with confirmation present -- this is what the turn looked
    # like when it originally ran.
    originally_authorized_context = GuardrailContext(
        opted_out=False,
        authorized_tools=(),
        required_tool_arguments={},
        confirmed_terminal_outcomes=("meeting_booked",),
    )
    record_as_originally_run = TurnRecord(
        state_before=record.state_before,
        prospect_utterance=record.prospect_utterance,
        recorded_interpretation=record.recorded_interpretation,
        recorded_plan=record.recorded_plan,
        recorded_response_text=record.recorded_response_text,
        guardrail_context=originally_authorized_context,
    )
    original_outcome = await replay_turn(
        record_as_originally_run, call_attempt_id=uuid.uuid4(), organization_id=1, lead_id=1
    )
    assert original_outcome.guardrail_result.verdict.value == "authorized"

    # Replaying the SAME recorded model output against today's context
    # (no confirmation) surfaces the regression immediately.
    regressed_outcome = await replay_turn(record, call_attempt_id=uuid.uuid4(), organization_id=1, lead_id=1)
    assert regressed_outcome.guardrail_result.verdict.value == "rejected"
    assert regressed_outcome.agent_turn.action.action_category == ActionCategory.WAIT
