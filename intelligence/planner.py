"""The Planner: answers "given the situation and the objective, what
should happen next?" (docs/PHASE2_DESIGN.md "Planner" / master prompt
§13-14). Like `intelligence/interpreter.py`, this module owns exactly
two things — calling `LLMProvider.plan` with a well-formed
`PlanningContext`, and deterministically validating what comes back — it
does NOT itself decide the action (that's the model's job, informed by
context this module assembles no further reasoning over) and does NOT
authorize the action (that's guardrails' job, downstream).

No static funnel anywhere in this file. There is no
`if stage == DISCOVERY: ASK_PAIN_POINT` — the planner receives the full
`PlanningContext` (state, latest interpretation, recent turns, objective,
permitted actions/tools) and asks the model for a proposal; this module's
own code only validates the SHAPE of what comes back (is the action
category one we actually offered, are the category-specific fields
present), never which action is "correct" for a given stage — that
judgment call is entirely the model's, exactly as the spec requires.
"""
from __future__ import annotations

from intelligence.contracts import ActionCategory, ConversationPlan, PlanningContext
from intelligence.llm_provider import LLMProvider, PlanningResult


class PlannerContractViolation(ValueError):
    """Raised when the provider proposes something that violates the
    PLANNING contract itself (e.g. an action category that wasn't
    offered) — distinct from a guardrail REJECTION, which is a policy
    decision about an otherwise-well-formed plan. A contract violation
    means the provider didn't follow the interface it was given, which is
    a bug/provider-quality issue, not a business policy outcome."""


def _validate_plan_shape(plan: ConversationPlan, context: PlanningContext) -> None:
    if plan.action_category not in context.permitted_actions:
        raise PlannerContractViolation(
            f"planner proposed {plan.action_category!r}, which was not in "
            f"the permitted actions offered: {context.permitted_actions}"
        )
    if plan.action_category == ActionCategory.TOOL_CALL:
        if plan.tool_name is None:
            raise PlannerContractViolation("TOOL_CALL plan is missing tool_name")
        if plan.tool_name not in context.permitted_tools:
            raise PlannerContractViolation(
                f"planner proposed tool {plan.tool_name!r}, which was not in "
                f"the permitted tools offered: {context.permitted_tools}"
            )


async def propose_next_action(provider: LLMProvider, context: PlanningContext) -> PlanningResult:
    """The one entry point. Raises `PlannerContractViolation` if the
    provider's proposal doesn't respect the offered action/tool
    vocabulary — this is checked here (fail fast, close to the source)
    rather than left for guardrails to reject less specifically later.
    Everything else about whether the CHOSEN action is a good idea is
    left entirely to guardrails and, ultimately, to the model's own
    reasoning — this function does not second-guess the choice itself.
    """
    result = await provider.plan(context)
    _validate_plan_shape(result.plan, context)
    return result
