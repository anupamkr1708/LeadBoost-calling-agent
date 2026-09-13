"""Unit tests for intelligence/planner.py — pure, uses FakeLLMProvider,
no DB/Redis. Proves the planner validates the SHAPE of what the provider
proposes (permitted actions/tools) without ever second-guessing WHICH
action was chosen — that judgment is entirely the model's."""
from __future__ import annotations

import uuid

import pytest

from intelligence.context import assemble_planning_context
from intelligence.contracts import (
    ActionCategory,
    ConversationPlan,
    InterestLevel,
    SemanticInterpretation,
    SpeechAct,
    initial_state,
)
from intelligence.fake_llm import FakeLLMProvider
from intelligence.planner import PlannerContractViolation, propose_next_action

STATE = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)


def _interpretation(**overrides) -> SemanticInterpretation:
    defaults = dict(
        primary_intent="interest",
        secondary_intents=(),
        speech_act=SpeechAct.STATEMENT,
        user_goal=None,
        conversation_stage="evaluation",
        interest=InterestLevel.CONDITIONAL,
        interest_certainty="moderate",
        sentiment=None,
        emotion=None,
        objections=(),
        concerns=(),
        motivations=(),
        questions=(),
        requests=(),
        commitments=(),
        timing_signal=None,
        timing_certainty="unknown",
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
        confidence=0.8,
        uncertainty_notes=(),
    )
    defaults.update(overrides)
    return SemanticInterpretation(**defaults)  # type: ignore[arg-type]  # known mypy limitation: dict-unpack into a dataclass with a broad-union value type cannot be statically verified, even though every value is runtime-correct


@pytest.mark.asyncio
async def test_proposes_a_permitted_action():
    context = assemble_planning_context(
        STATE, _interpretation(), recent_turns=(), permitted_actions=(ActionCategory.ASK, ActionCategory.SPEAK)
    )
    provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(
            action_category=ActionCategory.ASK,
            objective="clarify migration concern",
            rationale="prospect mentioned switching would be painful",
            question_objective="what specifically worries you about switching?",
        )
    )
    result = await propose_next_action(provider, context)
    assert result.plan.action_category == ActionCategory.ASK


@pytest.mark.asyncio
async def test_rejects_action_not_in_permitted_set():
    context = assemble_planning_context(
        STATE, _interpretation(), recent_turns=(), permitted_actions=(ActionCategory.SPEAK,)
    )
    provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(
            action_category=ActionCategory.END_CALL,  # not offered
            objective="end",
            rationale="test",
            end_reason="test",
        )
    )
    with pytest.raises(PlannerContractViolation):
        await propose_next_action(provider, context)


@pytest.mark.asyncio
async def test_rejects_tool_call_with_unoffered_tool():
    context = assemble_planning_context(
        STATE,
        _interpretation(),
        recent_turns=(),
        permitted_actions=(ActionCategory.TOOL_CALL,),
        permitted_tools=("send_email",),
    )
    provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(
            action_category=ActionCategory.TOOL_CALL,
            objective="book",
            rationale="test",
            tool_name="book_meeting",  # not in permitted_tools
        )
    )
    with pytest.raises(PlannerContractViolation):
        await propose_next_action(provider, context)


@pytest.mark.asyncio
async def test_accepts_tool_call_with_offered_tool():
    context = assemble_planning_context(
        STATE,
        _interpretation(),
        recent_turns=(),
        permitted_actions=(ActionCategory.TOOL_CALL,),
        permitted_tools=("send_email",),
    )
    provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(
            action_category=ActionCategory.TOOL_CALL,
            objective="follow up",
            rationale="test",
            tool_name="send_email",
            tool_arguments={"to": "prospect@example.com"},
        )
    )
    result = await propose_next_action(provider, context)
    assert result.plan.tool_name == "send_email"


@pytest.mark.asyncio
async def test_planner_does_not_second_guess_which_action_was_chosen():
    """The planner validates SHAPE, never WHICH action — proven by two
    equally-valid-shaped plans from two different fixtures both passing
    unmodified, with no code path in planner.py that could prefer one."""
    context = assemble_planning_context(
        STATE, _interpretation(), recent_turns=(), permitted_actions=(ActionCategory.SPEAK, ActionCategory.WAIT)
    )
    speak_provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(action_category=ActionCategory.SPEAK, objective="a", rationale="x")
    )
    wait_provider = FakeLLMProvider(
        plan_source=lambda ctx: ConversationPlan(action_category=ActionCategory.WAIT, objective="b", rationale="y")
    )
    r1 = await propose_next_action(speak_provider, context)
    r2 = await propose_next_action(wait_provider, context)
    assert r1.plan.action_category == ActionCategory.SPEAK
    assert r2.plan.action_category == ActionCategory.WAIT
