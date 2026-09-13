"""Isolated live-LLM smoke tests (docs/PHASE2_DESIGN.md "Live LLM smoke"
/ master prompt Section 41: "Do not let live API tests become the
ordinary CI test suite"). Every test here is skipped unless a real
GROQ_API_KEY is present in the environment -- never required for normal
deterministic CI, and never run as part of `pytest tests/`. Run
explicitly with:

    GROQ_API_KEY=... pytest eval/live_smoke.py -v

These exercise conversation/llm_client.py::GroqLLMProvider against the
REAL Groq API -- the one place in this entire test suite that makes a
live model call. They check STRUCTURAL properties (does a request
succeed, does the response parse into the typed contract, is latency/
token usage populated) rather than semantic correctness -- semantic
quality is eval/scenarios.py's job (deterministic, fixture-driven,
always-on), not this file's.
"""
from __future__ import annotations

import os
import uuid

import pytest

from conversation.llm_client import GroqLLMProvider
from intelligence.context import DEFAULT_PERMITTED_ACTIONS, assemble_planning_context
from intelligence.contracts import ConversationInput, Speaker, initial_state

pytestmark = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"),
    reason="live LLM smoke tests require a real GROQ_API_KEY and are never part of ordinary CI",
)


@pytest.fixture()
def provider() -> GroqLLMProvider:
    return GroqLLMProvider(api_key=os.environ["GROQ_API_KEY"])


@pytest.mark.asyncio
async def test_interpret_returns_a_well_formed_interpretation(provider):
    turn_input = ConversationInput(
        session_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        organization_id=1,
        lead_id=1,
        turn_number=1,
        speaker=Speaker.PROSPECT,
        transcript="We already use Salesforce and are pretty happy with it.",
        prior_state=None,
        recent_turns=(),
        objective="book_meeting",
        context_version=1,
    )
    result = await provider.interpret(turn_input)
    assert result.interpretation.primary_intent
    assert 0.0 <= result.interpretation.confidence <= 1.0
    assert result.meta.latency_ms > 0
    assert result.meta.provider == "groq"


@pytest.mark.asyncio
async def test_plan_returns_an_action_from_the_permitted_set(provider):
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)
    turn_input = ConversationInput(
        session_id=state.session_id,
        call_attempt_id=uuid.uuid4(),
        organization_id=1,
        lead_id=1,
        turn_number=1,
        speaker=Speaker.PROSPECT,
        transcript="Sure, tell me more.",
        prior_state=state,
        recent_turns=(),
        objective="book_meeting",
        context_version=1,
    )
    interpretation_result = await provider.interpret(turn_input)
    context = assemble_planning_context(
        state, interpretation_result.interpretation, recent_turns=(), permitted_actions=DEFAULT_PERMITTED_ACTIONS
    )
    plan_result = await provider.plan(context)
    assert plan_result.plan.action_category in DEFAULT_PERMITTED_ACTIONS
    assert plan_result.plan.rationale


@pytest.mark.asyncio
async def test_generate_response_produces_non_empty_grounded_text(provider):
    state = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)
    context = assemble_planning_context(
        state,
        (await provider.interpret(
            ConversationInput(
                session_id=state.session_id,
                call_attempt_id=uuid.uuid4(),
                organization_id=1,
                lead_id=1,
                turn_number=1,
                speaker=Speaker.PROSPECT,
                transcript="What does your product do?",
                prior_state=state,
                recent_turns=(),
                objective="book_meeting",
                context_version=1,
            )
        )).interpretation,
        recent_turns=(),
    )
    result = await provider.generate_response(
        "explain the product briefly", ("helps sales teams book more qualified meetings",), context
    )
    assert result.response_text.strip()
    assert result.meta.latency_ms > 0
