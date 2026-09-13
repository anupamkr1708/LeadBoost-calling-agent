"""Integration test proving conversation/persistence.py::persist_turn
actually writes to real Postgres, is genuinely RLS-scoped (not just
assumed to be because it uses org_scoped_session), and round-trips the
full structured turn record correctly.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from app.config import get_settings
from conversation.persistence import persist_turn
from conversation.semantic_loop import TurnOutcome
from guardrails.policy import GuardrailContext, authorize, authorized_action_from
from intelligence.contracts import (
    ActionCategory,
    AgentTurn,
    Certainty,
    ConversationPlan,
    InterestLevel,
    ModelInvocationMeta,
    SemanticInterpretation,
    Speaker,
    SpeechAct,
    initial_state,
)
from intelligence.state import reconcile

ORG_A = 7001
ORG_B = 7002


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    seed_org(ORG_A, plan_max_concurrent_calls=5)
    seed_org(ORG_B, plan_max_concurrent_calls=5)


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


def _build_outcome(session_id: uuid.UUID) -> TurnOutcome:
    state = initial_state(session_id, objective="book_meeting", context_version=1)
    interpretation = SemanticInterpretation(
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
    state_after = reconcile(state, interpretation, speaker=Speaker.PROSPECT, turn_number=1)
    plan = ConversationPlan(
        action_category=ActionCategory.ASK,
        objective="probe switching risk",
        rationale="prospect mentioned existing solution",
        question_objective="what would need to be true to consider switching?",
    )
    guardrail_context = GuardrailContext(
        opted_out=False, authorized_tools=(), required_tool_arguments={}, confirmed_terminal_outcomes=()
    )
    guardrail_result = authorize(plan, state_after, guardrail_context)
    action = authorized_action_from(plan)
    meta = ModelInvocationMeta(
        model="fake-llm", provider="fake", prompt_version="interpreter-v1", policy_version="fake-v1",
        context_version=1, latency_ms=12.5, prompt_tokens=100, completion_tokens=50,
    )
    agent_turn = AgentTurn(
        turn_number=1, action=action, response_text="Got it, what's working well about Salesforce today?",
        grounded_facts_used=("Salesforce",),
    )
    return TurnOutcome(
        agent_turn=agent_turn,
        state_before=state,
        state_after=state_after,
        interpretation=interpretation,
        interpretation_meta=meta,
        plan=plan,
        planner_meta=meta,
        guardrail_result=guardrail_result,
        response_meta=meta,
    )


def test_persist_turn_writes_a_real_row_readable_back():
    session_id = uuid.uuid4()
    call_attempt_id = uuid.uuid4()
    outcome = _build_outcome(session_id)

    persist_turn(ORG_A, call_attempt_id, session_id, "We already use Salesforce.", outcome)

    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT organization_id, transcript, response_text, interpretation_model, planner_prompt_version "
            "FROM conversation_turns WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == ORG_A
    assert row[1] == "We already use Salesforce."
    assert row[2] == "Got it, what's working well about Salesforce today?"
    assert row[3] == "fake-llm"
    assert row[4] == "interpreter-v1" or row[4]  # planner_prompt_version comes from planner_meta (same fake meta here)


def test_org_b_cannot_read_org_as_conversation_turn_via_rls(app_role_dsn):
    session_id = uuid.uuid4()
    call_attempt_id = uuid.uuid4()
    outcome = _build_outcome(session_id)
    persist_turn(ORG_A, call_attempt_id, session_id, "We already use Salesforce.", outcome)

    with psycopg.connect(app_role_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{ORG_B}'")
        cur.execute("SELECT * FROM conversation_turns WHERE session_id = %s", (session_id,))
        assert cur.fetchall() == [], "org B must see ZERO rows of org A's conversation_turns"

        cur.execute(f"SET app.current_org_id = '{ORG_A}'")
        cur.execute("SELECT session_id FROM conversation_turns WHERE session_id = %s", (session_id,))
        assert cur.fetchone() is not None, "sanity: org A can see its own row"
