"""THE full Phase 2 end-to-end integration test: real Postgres, real
Redis, a real WorkerRuntime with a conversation_engine_factory wired in,
FakeLLMProvider (no live API calls), a scripted ProspectTurnSource, and
real conversation_turns persistence -- proving admission through the
full semantic conversation to completion, through the actual production
object graph (docs/PHASE1_DESIGN.md "Very important testing principle":
don't fake the component under test -- here, the ONLY things faked are
the two genuine external I/O boundaries, telephony and the LLM).
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from app.config import get_settings
from conversation.persistence import persist_turn
from conversation.runtime import ConversationEngineConfig
from guardrails.policy import GuardrailContext
from intelligence.contracts import (
    ActionCategory,
    Certainty,
    ConversationPlan,
    InterestLevel,
    SemanticInterpretation,
    SpeechAct,
)
from intelligence.fake_llm import FakeLLMProvider
from orchestrator.call_service import CallService
from tests.integration.runtime_test_helpers import build_test_runtime

ORG_ID = 8101


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    seed_org(ORG_ID, plan_max_concurrent_calls=5)


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


def _interpretation(**overrides):
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
        confidence=0.8,
        uncertainty_notes=(),
    )
    defaults.update(overrides)
    return SemanticInterpretation(**defaults)


class _TwoTurnProspect:
    """A scripted prospect: interested, then agrees to a callback, then
    hangs up -- ending the conversation via a real guardrail-authorized
    END_CALL, not the prospect-hangup path, to prove that specific exit
    route through the full stack too."""

    def __init__(self):
        self._utterances = ["Sure, tell me more.", "Okay, call me back next week then."]
        self._index = 0

    async def next_utterance(self, state):
        if self._index >= len(self._utterances):
            return None
        u = self._utterances[self._index]
        self._index += 1
        return u


def _plan_source(ctx):
    # turn 1 (state.turn_count == 1, since reconcile() already incremented
    # it before the planner sees this context): ask a clarifying question.
    # turn 2: prospect agreed to a callback -> end the call with a
    # non-confirmation-required outcome.
    if ctx.state.turn_count == 1:
        return ConversationPlan(
            action_category=ActionCategory.ASK,
            objective="understand timing",
            rationale="prospect open to hearing more",
            question_objective="what would be a good time to reconnect?",
        )
    return ConversationPlan(
        action_category=ActionCategory.END_CALL,
        objective="end on agreed callback",
        rationale="prospect proposed a specific callback time",
        end_reason="callback_agreed",
        terminal_outcome="callback_requested",  # not in the confirmation-required set
    )


def _response_source(objective, facts, ctx):
    return "Great, I'll follow up then. Thanks for your time!"


@pytest.mark.asyncio
async def test_full_semantic_conversation_through_real_worker_runtime_and_persistence(app_settings):
    session_id_holder: dict[str, uuid.UUID] = {}

    def engine_factory(loaded, attempt_id, session_id):
        session_id_holder["session_id"] = session_id
        llm_provider = FakeLLMProvider(
            interpretation_source=lambda ti: _interpretation(
                primary_intent="interest", interest=InterestLevel.HIGH
            ),
            plan_source=_plan_source,
            response_source=_response_source,
        )

        async def persist_callback(prospect_utterance, outcome):
            await asyncio.to_thread(
                persist_turn, loaded.organization_id, attempt_id, session_id, prospect_utterance, outcome
            )

        return ConversationEngineConfig(
            llm_provider=llm_provider,
            prospect_source=_TwoTurnProspect(),
            guardrail_context=GuardrailContext(
                opted_out=False, authorized_tools=(), required_tool_arguments={}, confirmed_terminal_outcomes=()
            ),
            call_id=loaded.call_id,
            organization_id=loaded.organization_id,
            lead_id=loaded.lead_id,
            objective="book_meeting",
            session_id=session_id,
            persist_callback=persist_callback,
        )

    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        conversation_engine_factory=engine_factory,
    )
    await runtime.start()
    try:
        service = CallService()
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(created.first_attempt_id))

        deadline = asyncio.get_event_loop().time() + 10.0
        status = None
        disposition = None
        while asyncio.get_event_loop().time() < deadline:
            with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT status, disposition FROM calls WHERE id = %s", (created.call_id,))
                row = cur.fetchone()
                if row:
                    status, disposition = row
            if status in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)

        assert status == "completed", f"call did not complete via the real semantic path: status={status}"
        assert disposition == "callback_requested", f"expected the guardrail-authorized END_CALL outcome, got {disposition}"

        # And the actual semantic turn records exist, persisted through
        # the real DB path, with the real interpretation/plan/response
        # data -- not just the mechanical call-level outcome.
        session_id = session_id_holder["session_id"]
        with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT turn_number, transcript, response_text FROM conversation_turns "
                "WHERE session_id = %s ORDER BY turn_number",
                (session_id,),
            )
            turns = cur.fetchall()
        assert len(turns) == 2, f"expected 2 persisted semantic turns, got {turns}"
        assert turns[0][1] == "Sure, tell me more."
        assert turns[1][1] == "Okay, call me back next week then."
        assert all(t[2] == "Great, I'll follow up then. Thanks for your time!" for t in turns)
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()
