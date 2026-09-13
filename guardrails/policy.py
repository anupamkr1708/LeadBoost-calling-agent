"""Deterministic action authorization — the ONE place a `ConversationPlan`
becomes (or fails to become) an executable `ConversationAction`
(docs/PHASE2_DESIGN.md "Guardrails" / master prompt §16). This module
contains ZERO model calls and ZERO semantic classification of raw text —
every check here is a structural/policy check over already-typed data
(`ConversationState`, `ConversationPlan`) that the interpreter and planner
already produced. "LLM proposes, runtime validates" — this is the
validation.

Every policy below is a documented, deterministic safety invariant, not a
semantic heuristic masquerading as one — see each check's docstring for
why it belongs here rather than being left to the model's judgment.
"""
from __future__ import annotations

from dataclasses import dataclass

from intelligence.contracts import (
    ActionCategory,
    ConversationAction,
    ConversationPlan,
    ConversationState,
    GuardrailResult,
    GuardrailVerdict,
)

POLICY_VERSION = "guardrails-v1"

# Stable policy identifiers — used in GuardrailResult.violated_policy for
# observability/eval (docs/PHASE2_DESIGN.md "Observability"), not prose.
POLICY_NO_ACTION_AFTER_OPT_OUT = "no_action_after_opt_out"
POLICY_ACTION_NOT_PERMITTED = "action_not_permitted"
POLICY_TOOL_NOT_AUTHORIZED = "tool_not_authorized"
POLICY_TOOL_MISSING_ARGUMENTS = "tool_missing_arguments"
POLICY_CANNOT_CLAIM_UNCONFIRMED_OUTCOME = "cannot_claim_unconfirmed_outcome"
POLICY_END_CALL_REQUIRES_REASON = "end_call_requires_reason"
POLICY_ASK_REQUIRES_OBJECTIVE = "ask_requires_objective"
POLICY_REPEATED_ACTION_WITHOUT_PROGRESS = "repeated_action_without_progress"


@dataclass(frozen=True)
class GuardrailContext:
    """What guardrails needs beyond the plan+state — deliberately narrow.
    `opted_out` and `tool_confirmed_results` are runtime-tracked facts
    (not semantic inferences) the conversation loop maintains alongside
    `ConversationState` — see conversation/semantic_loop.py."""

    opted_out: bool
    authorized_tools: tuple[str, ...]
    required_tool_arguments: dict[str, tuple[str, ...]]  # tool_name -> required arg names
    confirmed_terminal_outcomes: tuple[str, ...]  # outcomes a TOOL_RESULT has actually confirmed this call


# Outcomes that require a prior confirming tool result before the agent
# may claim them — "cannot claim a meeting is booked without tool
# confirmation" (master prompt §16), generalized to any outcome a real
# system action would need to back up, not hardcoded to "meeting" alone.
_OUTCOMES_REQUIRING_CONFIRMATION = frozenset({"meeting_booked", "callback_scheduled", "transfer_completed"})


def authorize(plan: ConversationPlan, state: ConversationState, context: GuardrailContext) -> GuardrailResult:
    """The one entry point. Every check below is a deterministic,
    independently-testable policy — see the individual `_check_*`
    functions for what each one is actually protecting against."""
    checks = (
        _check_not_after_opt_out,
        _check_action_permitted,
        _check_tool_authorization,
        _check_tool_arguments,
        _check_no_unconfirmed_terminal_claim,
        _check_end_call_has_reason,
        _check_ask_has_objective,
    )
    for check in checks:
        result = check(plan, state, context)
        if result is not None:
            return result
    return GuardrailResult(verdict=GuardrailVerdict.AUTHORIZED, reason="passed all policy checks")


def _check_not_after_opt_out(
    plan: ConversationPlan, _state: ConversationState, context: GuardrailContext
) -> GuardrailResult | None:
    """Safety invariant, not a semantic judgment: once the caller has
    explicitly opted out (detected once, upstream, and tracked as a plain
    boolean — not re-derived here from text), NOTHING but ending the call
    is permitted, regardless of what the planner proposes. This is
    deliberately unconditional — it does not defer to the planner's
    rationale."""
    if context.opted_out and plan.action_category != ActionCategory.END_CALL:
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason="caller has opted out; only END_CALL is permitted",
            violated_policy=POLICY_NO_ACTION_AFTER_OPT_OUT,
        )
    return None


def _check_action_permitted(
    plan: ConversationPlan, state: ConversationState, _context: GuardrailContext
) -> GuardrailResult | None:
    """The planner can only propose from the SAME closed
    `ActionCategory` vocabulary guardrails validates against — this check
    exists for the case where a plan was constructed by something other
    than `intelligence/planner.py` (e.g. a malformed fixture in a test,
    or a future caller), so the invariant holds regardless of the
    plan's origin, not just when the real planner is well-behaved."""
    # PlanningContext.permitted_actions isn't threaded through
    # GuardrailContext by design — guardrails checks against the
    # STATE-independent closed vocabulary (ActionCategory itself), while
    # "was this action offered to the planner this turn" is a planning
    # concern, not a safety one. A planner proposing an action it wasn't
    # offered is a planner bug (unit-tested in test_planner.py), not a
    # guardrail violation category — see docs/PHASE2_DESIGN.md.
    if not isinstance(plan.action_category, ActionCategory):  # pragma: no cover - type-system invariant
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason=f"{plan.action_category!r} is not a recognized action category",
            violated_policy=POLICY_ACTION_NOT_PERMITTED,
        )
    return None


def _check_tool_authorization(
    plan: ConversationPlan, _state: ConversationState, context: GuardrailContext
) -> GuardrailResult | None:
    """"cannot execute an unauthorized tool" (master prompt §16) —
    a plain membership check against the call's configured tool list,
    never inferred from the plan's own rationale text."""
    if plan.action_category != ActionCategory.TOOL_CALL:
        return None
    if plan.tool_name is None or plan.tool_name not in context.authorized_tools:
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason=f"tool {plan.tool_name!r} is not in this call's authorized tool list",
            violated_policy=POLICY_TOOL_NOT_AUTHORIZED,
        )
    return None


def _check_tool_arguments(
    plan: ConversationPlan, _state: ConversationState, context: GuardrailContext
) -> GuardrailResult | None:
    """"cannot execute a tool with invalid arguments" (master prompt §16)
    — checks presence of every REQUIRED argument name for this tool.
    Deliberately does not validate argument VALUES (that's the tool
    adapter's job when it actually executes, a later-phase concern) —
    this is the structural gate, not full schema validation."""
    if plan.action_category != ActionCategory.TOOL_CALL or plan.tool_name is None:
        return None
    required = context.required_tool_arguments.get(plan.tool_name, ())
    provided = set((plan.tool_arguments or {}).keys())
    missing = [name for name in required if name not in provided]
    if missing:
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason=f"tool {plan.tool_name!r} is missing required arguments: {missing}",
            violated_policy=POLICY_TOOL_MISSING_ARGUMENTS,
        )
    return None


def _check_no_unconfirmed_terminal_claim(
    plan: ConversationPlan, _state: ConversationState, context: GuardrailContext
) -> GuardrailResult | None:
    """"cannot claim a meeting is booked without tool confirmation"
    (master prompt §16), generalized: any `terminal_outcome` in
    `_OUTCOMES_REQUIRING_CONFIRMATION` may only be claimed on END_CALL if
    a matching entry already exists in
    `context.confirmed_terminal_outcomes` — populated only by an actual
    TOOL_CALL result the runtime observed, never by the planner's own
    say-so."""
    if plan.action_category != ActionCategory.END_CALL or plan.terminal_outcome is None:
        return None
    if (
        plan.terminal_outcome in _OUTCOMES_REQUIRING_CONFIRMATION
        and plan.terminal_outcome not in context.confirmed_terminal_outcomes
    ):
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason=f"terminal_outcome={plan.terminal_outcome!r} claimed without a confirming tool result",
            violated_policy=POLICY_CANNOT_CLAIM_UNCONFIRMED_OUTCOME,
        )
    return None


def _check_end_call_has_reason(
    plan: ConversationPlan, _state: ConversationState, _context: GuardrailContext
) -> GuardrailResult | None:
    """Structural completeness, not semantic judgment: an END_CALL with
    no reason is unauditable — observability (master prompt §28) needs
    "why did the agent end the call?" answerable from the action itself."""
    if plan.action_category == ActionCategory.END_CALL and not plan.end_reason:
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason="END_CALL requires a non-empty end_reason",
            violated_policy=POLICY_END_CALL_REQUIRES_REASON,
        )
    return None


def _check_ask_has_objective(
    plan: ConversationPlan, _state: ConversationState, _context: GuardrailContext
) -> GuardrailResult | None:
    """Same structural-completeness reasoning as END_CALL: an ASK with no
    question_objective can't be validated as purposeful (vs. the planner
    just filling in the action category) and can't be explained later."""
    if plan.action_category == ActionCategory.ASK and not plan.question_objective:
        return GuardrailResult(
            verdict=GuardrailVerdict.REJECTED,
            reason="ASK requires a non-empty question_objective",
            violated_policy=POLICY_ASK_REQUIRES_OBJECTIVE,
        )
    return None


def authorized_action_from(plan: ConversationPlan) -> ConversationAction:
    """Only ever call this after `authorize()` returned AUTHORIZED — kept
    as a separate explicit step (not folded into `authorize`) so the
    "plan -> action" conversion is visible at call sites, matching
    `ConversationAction`'s own docstring: guardrails authorize or reject,
    they don't rewrite."""
    return ConversationAction.from_plan(plan)
