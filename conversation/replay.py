"""Deterministic replay (docs/PHASE2_DESIGN.md "Replay" / master prompt
Section 29): given a recorded turn's inputs and the model outputs it
actually got, re-run the SAME deterministic pipeline (reconciliation,
planning validation, guardrails, response dispatch) against a
FakeLLMProvider seeded to return those EXACT recorded outputs, and get
back an identical TurnOutcome. This is what lets you change
intelligence/state.py's reconciliation logic or guardrails/policy.py's
rules and immediately see whether historical turns would now resolve
differently -- a regression harness for the deterministic parts, using
real historical model outputs as fixed input, without needing to
re-invoke a live model at all.

Deliberately NOT event sourcing: this is a snapshot-per-turn replay
(TurnRecord mirrors exactly what conversation/persistence.py already
persists to conversation_turns), not a system that reconstructs state by
replaying every event since the beginning of time. Master prompt Section
29: "Do not build a large event-sourcing system. A simple replayable
representation is enough" -- this is exactly that.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from conversation.semantic_loop import TurnOutcome, run_turn
from guardrails.policy import GuardrailContext
from intelligence.contracts import ConversationPlan, ConversationState, SemanticInterpretation
from intelligence.fake_llm import FakeLLMProvider


@dataclass(frozen=True)
class TurnRecord:
    """Everything needed to replay ONE turn -- the recorded model outputs
    plus the inputs they were originally produced from. Field names
    mirror `conversation_turns` columns / `TurnOutcome` fields directly
    (docs/PHASE2_DESIGN.md "Observability"), not a separate schema, so a
    row fetched from that table can be turned into a TurnRecord with no
    translation layer beyond deserializing the stored JSON.
    """

    state_before: ConversationState
    prospect_utterance: str
    recorded_interpretation: SemanticInterpretation
    recorded_plan: ConversationPlan
    recorded_response_text: str | None
    guardrail_context: GuardrailContext


def to_fixture_provider(record: TurnRecord) -> FakeLLMProvider:
    """Builds a FakeLLMProvider that deterministically returns EXACTLY
    the recorded interpretation/plan/response, regardless of what it's
    asked -- this is what makes re-running run_turn against it a true
    replay rather than a fresh (and possibly different) model call."""
    return FakeLLMProvider(
        interpretation_source=lambda _turn_input: record.recorded_interpretation,
        plan_source=lambda _planning_context: record.recorded_plan,
        response_source=lambda _obj, _facts, _ctx: record.recorded_response_text or "",
    )


async def replay_turn(
    record: TurnRecord, call_attempt_id: uuid.UUID, organization_id: int, lead_id: int
) -> TurnOutcome:
    """Re-runs the turn pipeline against the recorded model outputs.
    Returns a fresh TurnOutcome -- comparing it to the ORIGINAL persisted
    outcome (e.g. state_after, guardrail_result) is the actual regression
    check; this function only reproduces the run, it doesn't itself
    decide "did this replay match" (that's the caller's job, since what
    counts as a meaningful difference depends on what changed -- comparing
    everything byte-for-byte after a deliberate prompt/policy change would
    trivially "fail" on fields that weren't meant to stay the same)."""
    provider = to_fixture_provider(record)
    return await run_turn(
        provider,
        record.state_before,
        record.prospect_utterance,
        call_attempt_id=call_attempt_id,
        organization_id=organization_id,
        lead_id=lead_id,
        recent_turns=(),
        guardrail_context=record.guardrail_context,
    )
