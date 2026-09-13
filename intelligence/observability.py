"""Observability for semantic turns (docs/PHASE2_DESIGN.md
"Observability" / master prompt §28). One function, called once per turn
by `conversation/semantic_loop.py`, logging everything needed to answer
"why did the agent say that?" without a bespoke tracing framework —
structlog, matching every other module in this repo
(`orchestrator/worker_runtime.py`'s Phase 1 hardening pass established
this as the house style; see docs/PHASE1_AUDIT_ADDENDUM.md item J for why
stdlib `logging` is the wrong choice here).

Deliberately excluded from the log payload: raw transcript text beyond
what's needed to answer "why", and anything from `LLMTurnRequest`/
`LLMTurnResponse` that isn't already captured in the typed metadata
(`ModelInvocationMeta`) — no API keys, no raw prompt text logged at INFO
level (that level of detail belongs in replay fixtures, which are
explicit, versioned test data, not production logs).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict

import structlog

from intelligence.contracts import (
    ConversationAction,
    ConversationState,
    GuardrailResult,
    ModelInvocationMeta,
    SemanticInterpretation,
)

logger = structlog.get_logger(__name__)


def log_turn(
    *,
    call_id: uuid.UUID,
    attempt_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_number: int,
    state_before: ConversationState,
    transcript: str,
    interpretation: SemanticInterpretation,
    interpretation_meta: ModelInvocationMeta,
    state_after: ConversationState,
    planner_rationale: str,
    planner_meta: ModelInvocationMeta,
    guardrail_result: GuardrailResult,
    final_action: ConversationAction,
    response_text: str | None,
    response_meta: ModelInvocationMeta | None,
) -> None:
    """The single observability entry point for one full turn pipeline
    pass — every field master prompt §28 asks for, one call, one
    structured event, correlatable by call_id/attempt_id/session_id/turn
    (docs/PHASE2_DESIGN.md "Observability")."""
    logger.info(
        "semantic_turn_completed",
        call_id=str(call_id),
        attempt_id=str(attempt_id),
        session_id=str(session_id),
        turn_number=turn_number,
        state_before_summary=_state_summary(state_before),
        transcript=transcript,
        interpretation_primary_intent=interpretation.primary_intent,
        interpretation_interest=interpretation.interest.value,
        interpretation_confidence=interpretation.confidence,
        interpretation_model=interpretation_meta.model,
        interpretation_provider=interpretation_meta.provider,
        interpretation_prompt_version=interpretation_meta.prompt_version,
        interpretation_policy_version=interpretation_meta.policy_version,
        interpretation_context_version=interpretation_meta.context_version,
        interpretation_latency_ms=interpretation_meta.latency_ms,
        interpretation_prompt_tokens=interpretation_meta.prompt_tokens,
        interpretation_completion_tokens=interpretation_meta.completion_tokens,
        state_after_summary=_state_summary(state_after),
        planner_action_category=final_action.action_category.value,
        planner_rationale=planner_rationale,
        planner_model=planner_meta.model,
        planner_prompt_version=planner_meta.prompt_version,
        planner_latency_ms=planner_meta.latency_ms,
        guardrail_verdict=guardrail_result.verdict.value,
        guardrail_reason=guardrail_result.reason,
        guardrail_violated_policy=guardrail_result.violated_policy,
        final_action_category=final_action.action_category.value,
        final_action_objective=final_action.objective,
        response_text=response_text,
        response_model=response_meta.model if response_meta else None,
        response_latency_ms=response_meta.latency_ms if response_meta else None,
    )


def _state_summary(state: ConversationState) -> dict[str, object]:
    """A compact, redaction-friendly summary for logs — current beliefs
    only (mirrors `intelligence/prompts.py`'s prompt-context summary, kept
    separate because a log line and a prompt payload have different
    evolution reasons even though they look similar today)."""
    return {
        "stage": state.stage.value,
        "primary_intent": state.primary_intent.value,
        "interest": state.interest.value.value if state.interest.value else None,
        "turn_count": state.turn_count,
        "objection_count": sum(1 for b in state.objections if b.status.value == "current"),
        "fact_count": sum(1 for b in state.facts if b.status.value == "current"),
        "unresolved_question_count": len(state.unresolved_questions),
    }


def guardrail_result_as_dict(result: GuardrailResult) -> dict[str, object]:
    """For replay/eval fixtures that need the full structured result, not
    just what's logged — kept separate from `log_turn` so replay doesn't
    depend on parsing log lines."""
    return asdict(result)
