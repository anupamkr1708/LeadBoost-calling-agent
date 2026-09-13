"""Persists `conversation/semantic_loop.py::TurnOutcome` records to the
durable `conversation_turns` table (docs/PHASE2_DESIGN.md "Observability").
This is a THIN persistence adapter — it serializes already-computed,
already-typed data; it makes no decisions of its own. Kept in
`conversation/` (not `intelligence/`) because it touches `storage.db`,
and `intelligence/*` must remain free of any DB dependency
(docs/PHASE2_DESIGN.md "Architecture tests": "interpreter does not
mutate database").

Uses `org_scoped_session` (never `system_session`) — by the time a
semantic turn runs, the worker has already resolved and is operating
within a specific organization's session (Phase 1's `_try_start_running`
already succeeded), so there is no cross-org lookup problem here, unlike
the reaper's bare-attempt-id case.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from sqlalchemy import text

from conversation.semantic_loop import TurnOutcome
from storage.db import org_scoped_session


def _json(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def persist_turn(
    organization_id: int,
    call_attempt_id: uuid.UUID,
    session_id: uuid.UUID,
    prospect_utterance: str,
    outcome: TurnOutcome,
) -> None:
    """Writes one turn. `prospect_utterance` is passed explicitly by the
    caller (it's the input to `run_turn`, not part of `TurnOutcome`
    itself) rather than reconstructed from anything else on the outcome.

    Raises on a genuine DB failure (foreign-key violation, connection
    error) rather than swallowing it — a turn that silently fails to
    persist would be an observability/replay gap no one would ever
    notice, which is worse than a loud failure the caller
    (`conversation/runtime.py`) can route through the same retry/failure
    handling Phase 1 already has for any other transient DB error.
    """
    with org_scoped_session(organization_id) as session:
        session.execute(
            text(
                """
                INSERT INTO conversation_turns (
                    organization_id, call_attempt_id, session_id, turn_number, speaker, transcript,
                    state_before, state_after, interpretation, plan, guardrail_result, final_action,
                    response_text, interpretation_model, interpretation_provider,
                    interpretation_prompt_version, interpretation_policy_version,
                    interpretation_context_version, interpretation_latency_ms,
                    planner_model, planner_prompt_version, planner_latency_ms,
                    response_model, response_latency_ms
                ) VALUES (
                    :organization_id, :call_attempt_id, :session_id, :turn_number, :speaker, :transcript,
                    :state_before, :state_after, :interpretation, :plan, :guardrail_result, :final_action,
                    :response_text, :interpretation_model, :interpretation_provider,
                    :interpretation_prompt_version, :interpretation_policy_version,
                    :interpretation_context_version, :interpretation_latency_ms,
                    :planner_model, :planner_prompt_version, :planner_latency_ms,
                    :response_model, :response_latency_ms
                )
                """
            ),
            {
                "organization_id": organization_id,
                "call_attempt_id": call_attempt_id,
                "session_id": session_id,
                "turn_number": outcome.agent_turn.turn_number,
                "speaker": "prospect",
                "transcript": prospect_utterance,
                "state_before": _json(asdict(outcome.state_before)),
                "state_after": _json(asdict(outcome.state_after)),
                "interpretation": _json(asdict(outcome.interpretation)),
                "plan": _json(asdict(outcome.plan)),
                "guardrail_result": _json(asdict(outcome.guardrail_result)),
                "final_action": _json(asdict(outcome.agent_turn.action)),
                "response_text": outcome.agent_turn.response_text,
                "interpretation_model": outcome.interpretation_meta.model,
                "interpretation_provider": outcome.interpretation_meta.provider,
                "interpretation_prompt_version": outcome.interpretation_meta.prompt_version,
                "interpretation_policy_version": outcome.interpretation_meta.policy_version,
                "interpretation_context_version": outcome.interpretation_meta.context_version,
                "interpretation_latency_ms": outcome.interpretation_meta.latency_ms,
                "planner_model": outcome.planner_meta.model,
                "planner_prompt_version": outcome.planner_meta.prompt_version,
                "planner_latency_ms": outcome.planner_meta.latency_ms,
                "response_model": outcome.response_meta.model if outcome.response_meta else None,
                "response_latency_ms": outcome.response_meta.latency_ms if outcome.response_meta else None,
            },
        )
