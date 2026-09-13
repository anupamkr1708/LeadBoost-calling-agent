"""The Semantic Interpreter: answers "what does the caller mean?"
(docs/PHASE2_DESIGN.md "Distinguish interpretation from decision"). This
module owns exactly two things: calling `LLMProvider.interpret` with a
well-formed `ConversationInput`, and deterministically validating/
clamping whatever comes back — it does NOT itself do any semantic
reasoning (that's the provider's job) and does NOT decide what happens
next (that's the planner's job).

Validation here is intentionally narrow and mechanical (clamp confidence
into [0,1], never fabricate a value the provider didn't return) — this is
the "LLM proposes, runtime validates" principle applied at the very first
step, not just at the action-authorization step guardrails handle later.
"""
from __future__ import annotations

from dataclasses import replace

from intelligence.contracts import ConversationInput, SemanticInterpretation
from intelligence.llm_provider import InterpretationResult, LLMProvider


def _clamp_confidence(interpretation: SemanticInterpretation) -> SemanticInterpretation:
    clamped = max(0.0, min(1.0, interpretation.confidence))
    if clamped == interpretation.confidence:
        return interpretation
    return replace(interpretation, confidence=clamped)


async def interpret_turn(provider: LLMProvider, turn_input: ConversationInput) -> InterpretationResult:
    """The one entry point. Returns the provider's `InterpretationResult`
    with its `SemanticInterpretation` deterministically sanitized —
    never re-derives or overrides the provider's semantic judgment
    itself, only enforces the contract's own invariants (e.g. confidence
    is a valid probability)."""
    result = await provider.interpret(turn_input)
    sanitized = _clamp_confidence(result.interpretation)
    if sanitized is result.interpretation:
        return result
    return InterpretationResult(interpretation=sanitized, meta=result.meta)
