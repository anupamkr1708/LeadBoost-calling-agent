"""Context assembly: turns `ConversationState` + recent turns + the
business objective into a `PlanningContext` (docs/PHASE2_DESIGN.md
"Context management" / "Memory"). This is where the memory boundary from
the spec is actually implemented — not a generic memory framework, just
the three layers the Calling Agent specifically needs:

- SHORT-TERM: the last `max_recent_turns` lines of dialogue, verbatim.
- WORKING MEMORY: `ConversationState`'s CURRENT beliefs (objections,
  concerns, unresolved questions, current facts) — already compact,
  already structured, nothing to further summarize.
- LONGER-TERM: `ConversationState.fact_history` (superseded beliefs) is
  available for provenance/replay but is deliberately NOT included in
  what gets sent to the model on every turn — only current beliefs and
  a bounded recent-turns window are, so the amount of information sent
  per turn stays roughly constant regardless of how long the
  conversation runs, rather than growing with the full transcript.

No vector database, no generic retrieval — per the master prompt's
explicit "Phase 2 only needs the context architecture. Retrieval can be
expanded later."
"""
from __future__ import annotations

from intelligence.contracts import (
    ActionCategory,
    ConversationState,
    PlanningContext,
    SemanticInterpretation,
    TranscriptTurn,
)

DEFAULT_MAX_RECENT_TURNS = 8

DEFAULT_PERMITTED_ACTIONS: tuple[ActionCategory, ...] = (
    ActionCategory.SPEAK,
    ActionCategory.ASK,
    ActionCategory.WAIT,
    ActionCategory.END_CALL,
    # TOOL_CALL and TRANSFER are opt-in per call (permitted_tools/transfer
    # targets are business-configuration, not a Phase 2 default) — see
    # guardrails/policy.py's tool-authorization check for the enforcement
    # side of this.
)


def bound_recent_turns(
    turns: tuple[TranscriptTurn, ...], max_turns: int = DEFAULT_MAX_RECENT_TURNS
) -> tuple[TranscriptTurn, ...]:
    """The SHORT-TERM memory boundary: only the most recent `max_turns`
    lines of dialogue are ever sent verbatim — everything before that is
    only present in `ConversationState`'s structured beliefs (which don't
    grow with conversation length the way raw transcript does)."""
    if len(turns) <= max_turns:
        return turns
    return turns[-max_turns:]


def assemble_planning_context(
    state: ConversationState,
    latest_interpretation: SemanticInterpretation,
    recent_turns: tuple[TranscriptTurn, ...],
    *,
    permitted_actions: tuple[ActionCategory, ...] = DEFAULT_PERMITTED_ACTIONS,
    permitted_tools: tuple[str, ...] = (),
    max_recent_turns: int = DEFAULT_MAX_RECENT_TURNS,
) -> PlanningContext:
    return PlanningContext(
        state=state,
        latest_interpretation=latest_interpretation,
        recent_turns=bound_recent_turns(recent_turns, max_recent_turns),
        objective=state.objective,
        permitted_actions=permitted_actions,
        permitted_tools=permitted_tools,
    )
