"""The Responder: answers "how should the authorized action be expressed
naturally?" (docs/PHASE2_DESIGN.md "Response generation" / master prompt
§17). Deliberately the LAST step, operating on an already-GUARDRAIL-
AUTHORIZED `ConversationAction` — planning ("what to accomplish") and
wording ("how to say it") are two different questions, and conflating
them back into one prompt is exactly what the spec warns against.

Grounding is enforced structurally, not by instruction alone: this
module's entry point takes only the action's own objective and an
explicit `grounded_facts` tuple the CALLER selects (from
`ConversationState.facts`, the LeadBoost context, or a tool result) —
never the full `ConversationState` or raw transcript. The responder
cannot invent a fact it wasn't handed, because it was never given
anything to invent FROM beyond what's explicitly passed in.

No hardcoded response templates per intent/action — matching the "use
structured objectives + model generation" requirement directly: this
module has no `if action_category == ASK: return f"Can you tell me
about {topic}?"` anywhere. WAIT is the one action category with no
generation at all (silence has no wording), handled by never calling the
provider for it, not by a template.
"""
from __future__ import annotations

from intelligence.contracts import ActionCategory, ConversationAction, PlanningContext
from intelligence.llm_provider import LLMProvider, ResponseResult


async def generate_response(
    provider: LLMProvider,
    action: ConversationAction,
    grounded_facts: tuple[str, ...],
    context: PlanningContext,
) -> ResponseResult | None:
    """Returns `None` for action categories with no natural-language
    realization (WAIT — and TOOL_CALL/TRANSFER, which are executed, not
    spoken, at least until their own result becomes something to SPEAK
    about on a later turn) — a `None` return is a real, valid outcome
    here, not an error, matching the rest of this codebase's "unknown/
    absent is representable, not synthesized" posture.
    """
    if action.action_category in (ActionCategory.WAIT, ActionCategory.TOOL_CALL, ActionCategory.TRANSFER):
        return None
    if action.action_category == ActionCategory.ASK:
        # guardrails/policy.py's _check_ask_has_objective already
        # guarantees this is non-empty for any AUTHORIZED ASK action —
        # asserting it here documents that dependency rather than
        # silently trusting it.
        assert action.question_objective, "ASK action reached the responder without a question_objective"
        objective = action.question_objective
    else:
        objective = action.objective
    return await provider.generate_response(objective, grounded_facts, context)
