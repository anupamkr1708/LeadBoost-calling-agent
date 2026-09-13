"""Unit tests for intelligence/responder.py — pure, uses FakeLLMProvider.
Proves: (1) WAIT/TOOL_CALL/TRANSFER never call the provider at all (no
wording to generate), (2) SPEAK/ASK do, (3) only the explicitly-passed
grounded_facts and objective ever reach the provider — never the full
state or transcript."""
from __future__ import annotations

import uuid

import pytest

from intelligence.context import assemble_planning_context
from intelligence.contracts import (
    ActionCategory,
    Certainty,
    ConversationAction,
    InterestLevel,
    SemanticInterpretation,
    SpeechAct,
    initial_state,
)
from intelligence.fake_llm import FakeLLMProvider
from intelligence.responder import generate_response

STATE = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)


def _interpretation() -> SemanticInterpretation:
    return SemanticInterpretation(
        primary_intent="interest",
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


CONTEXT = assemble_planning_context(STATE, _interpretation(), recent_turns=())


@pytest.mark.parametrize("category", [ActionCategory.WAIT, ActionCategory.TOOL_CALL, ActionCategory.TRANSFER])
@pytest.mark.asyncio
async def test_non_verbal_actions_never_call_the_provider(category):
    calls = []

    def _source(objective, facts, ctx):
        calls.append(objective)
        return "should never be called"

    provider = FakeLLMProvider(response_source=_source)
    action = ConversationAction(action_category=category, objective="x", rationale="test")
    result = await generate_response(provider, action, grounded_facts=(), context=CONTEXT)
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_speak_action_calls_the_provider_with_its_objective():
    seen = {}

    def _source(objective, facts, ctx):
        seen["objective"] = objective
        seen["facts"] = facts
        return "Great, glad to hear it!"

    provider = FakeLLMProvider(response_source=_source)
    action = ConversationAction(action_category=ActionCategory.SPEAK, objective="acknowledge interest", rationale="x")
    result = await generate_response(provider, action, grounded_facts=("plan_supports_up_to_500_seats",), context=CONTEXT)
    assert result is not None
    assert result.response_text == "Great, glad to hear it!"
    assert seen["objective"] == "acknowledge interest"
    assert seen["facts"] == ("plan_supports_up_to_500_seats",)


@pytest.mark.asyncio
async def test_ask_action_uses_question_objective_not_generic_objective():
    seen = {}

    def _source(objective, facts, ctx):
        seen["objective"] = objective
        return "What worries you about switching?"

    provider = FakeLLMProvider(response_source=_source)
    action = ConversationAction(
        action_category=ActionCategory.ASK,
        objective="reduce switching-risk uncertainty",
        rationale="x",
        question_objective="what specifically worries you about switching?",
    )
    result = await generate_response(provider, action, grounded_facts=(), context=CONTEXT)
    assert result is not None
    assert seen["objective"] == "what specifically worries you about switching?"


@pytest.mark.asyncio
async def test_only_explicitly_grounded_facts_reach_the_provider():
    """Regression guard for the grounding requirement: even though STATE
    has no facts here, this proves the mechanism — the responder passes
    through EXACTLY what's given, nothing derived from ConversationState
    itself, which the responder was never even given a reference to."""
    seen = {}

    def _source(objective, facts, ctx):
        seen["facts"] = facts
        return "ok"

    provider = FakeLLMProvider(response_source=_source)
    action = ConversationAction(action_category=ActionCategory.SPEAK, objective="x", rationale="x")
    await generate_response(provider, action, grounded_facts=("only_this_fact",), context=CONTEXT)
    assert seen["facts"] == ("only_this_fact",)
