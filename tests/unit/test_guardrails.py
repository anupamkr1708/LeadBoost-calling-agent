"""Unit tests for guardrails/policy.py — pure, no DB/Redis/LLM. Each
policy check is exercised both for rejection and for the corresponding
authorized case, per master prompt §16's explicit list of required
safety invariants."""
from __future__ import annotations

import uuid

from guardrails.policy import (
    POLICY_ASK_REQUIRES_OBJECTIVE,
    POLICY_CANNOT_CLAIM_UNCONFIRMED_OUTCOME,
    POLICY_END_CALL_REQUIRES_REASON,
    POLICY_NO_ACTION_AFTER_OPT_OUT,
    POLICY_TOOL_MISSING_ARGUMENTS,
    POLICY_TOOL_NOT_AUTHORIZED,
    GuardrailContext,
    authorize,
    authorized_action_from,
)
from intelligence.contracts import ActionCategory, ConversationPlan, GuardrailVerdict, initial_state

STATE = initial_state(uuid.uuid4(), objective="book_meeting", context_version=1)


def _context(**overrides) -> GuardrailContext:
    defaults = dict(
        opted_out=False,
        authorized_tools=(),
        required_tool_arguments={},
        confirmed_terminal_outcomes=(),
    )
    defaults.update(overrides)
    return GuardrailContext(**defaults)  # type: ignore[arg-type]  # same known mypy limitation as SemanticInterpretation's builder above


def _speak_plan(**overrides) -> ConversationPlan:
    defaults = dict(action_category=ActionCategory.SPEAK, objective="acknowledge", rationale="test")
    defaults.update(overrides)
    return ConversationPlan(**defaults)  # type: ignore[arg-type]


class TestOptOut:
    def test_rejects_any_non_end_call_action_after_opt_out(self):
        result = authorize(_speak_plan(), STATE, _context(opted_out=True))
        assert result.verdict == GuardrailVerdict.REJECTED
        assert result.violated_policy == POLICY_NO_ACTION_AFTER_OPT_OUT

    def test_permits_end_call_after_opt_out(self):
        plan = ConversationPlan(
            action_category=ActionCategory.END_CALL,
            objective="end",
            rationale="caller opted out",
            end_reason="explicit_opt_out",
        )
        result = authorize(plan, STATE, _context(opted_out=True))
        assert result.verdict == GuardrailVerdict.AUTHORIZED

    def test_permits_normal_speak_when_not_opted_out(self):
        result = authorize(_speak_plan(), STATE, _context(opted_out=False))
        assert result.verdict == GuardrailVerdict.AUTHORIZED


class TestToolAuthorization:
    def test_rejects_unauthorized_tool(self):
        plan = ConversationPlan(
            action_category=ActionCategory.TOOL_CALL,
            objective="book",
            rationale="test",
            tool_name="book_meeting",
            tool_arguments={"date": "2026-01-01"},
        )
        result = authorize(plan, STATE, _context(authorized_tools=("send_email",)))
        assert result.verdict == GuardrailVerdict.REJECTED
        assert result.violated_policy == POLICY_TOOL_NOT_AUTHORIZED

    def test_permits_authorized_tool_with_required_arguments(self):
        plan = ConversationPlan(
            action_category=ActionCategory.TOOL_CALL,
            objective="book",
            rationale="test",
            tool_name="book_meeting",
            tool_arguments={"date": "2026-01-01", "attendee_email": "a@b.com"},
        )
        result = authorize(
            plan,
            STATE,
            _context(
                authorized_tools=("book_meeting",),
                required_tool_arguments={"book_meeting": ("date", "attendee_email")},
            ),
        )
        assert result.verdict == GuardrailVerdict.AUTHORIZED

    def test_rejects_missing_required_arguments(self):
        plan = ConversationPlan(
            action_category=ActionCategory.TOOL_CALL,
            objective="book",
            rationale="test",
            tool_name="book_meeting",
            tool_arguments={"date": "2026-01-01"},  # missing attendee_email
        )
        result = authorize(
            plan,
            STATE,
            _context(
                authorized_tools=("book_meeting",),
                required_tool_arguments={"book_meeting": ("date", "attendee_email")},
            ),
        )
        assert result.verdict == GuardrailVerdict.REJECTED
        assert result.violated_policy == POLICY_TOOL_MISSING_ARGUMENTS


class TestUnconfirmedTerminalClaims:
    def test_rejects_meeting_booked_claim_without_tool_confirmation(self):
        plan = ConversationPlan(
            action_category=ActionCategory.END_CALL,
            objective="end",
            rationale="test",
            end_reason="meeting scheduled",
            terminal_outcome="meeting_booked",
        )
        result = authorize(plan, STATE, _context())
        assert result.verdict == GuardrailVerdict.REJECTED
        assert result.violated_policy == POLICY_CANNOT_CLAIM_UNCONFIRMED_OUTCOME

    def test_permits_meeting_booked_claim_with_tool_confirmation(self):
        plan = ConversationPlan(
            action_category=ActionCategory.END_CALL,
            objective="end",
            rationale="test",
            end_reason="meeting scheduled",
            terminal_outcome="meeting_booked",
        )
        result = authorize(plan, STATE, _context(confirmed_terminal_outcomes=("meeting_booked",)))
        assert result.verdict == GuardrailVerdict.AUTHORIZED

    def test_permits_unconfirmed_non_sensitive_terminal_outcome(self):
        """Outcomes NOT in the confirmation-required set (e.g.
        'not_interested') don't need a tool result — only ones that claim
        a real-world system action happened do."""
        plan = ConversationPlan(
            action_category=ActionCategory.END_CALL,
            objective="end",
            rationale="test",
            end_reason="explicit disinterest",
            terminal_outcome="not_interested",
        )
        result = authorize(plan, STATE, _context())
        assert result.verdict == GuardrailVerdict.AUTHORIZED


class TestStructuralCompleteness:
    def test_rejects_end_call_without_reason(self):
        plan = ConversationPlan(action_category=ActionCategory.END_CALL, objective="end", rationale="test")
        result = authorize(plan, STATE, _context())
        assert result.verdict == GuardrailVerdict.REJECTED
        assert result.violated_policy == POLICY_END_CALL_REQUIRES_REASON

    def test_rejects_ask_without_question_objective(self):
        plan = ConversationPlan(action_category=ActionCategory.ASK, objective="clarify", rationale="test")
        result = authorize(plan, STATE, _context())
        assert result.verdict == GuardrailVerdict.REJECTED
        assert result.violated_policy == POLICY_ASK_REQUIRES_OBJECTIVE

    def test_permits_well_formed_ask(self):
        plan = ConversationPlan(
            action_category=ActionCategory.ASK,
            objective="clarify migration concern",
            rationale="test",
            question_objective="what specifically worries you about switching?",
        )
        result = authorize(plan, STATE, _context())
        assert result.verdict == GuardrailVerdict.AUTHORIZED


class TestAuthorizedActionConversion:
    def test_authorized_action_from_preserves_all_fields(self):
        plan = _speak_plan(objective="acknowledge interest", rationale="prospect expressed interest")
        action = authorized_action_from(plan)
        assert action.action_category == plan.action_category
        assert action.objective == plan.objective
        assert action.rationale == plan.rationale


class TestPolicyOrderingIsDeterministic:
    def test_opt_out_check_wins_over_every_other_violation(self):
        """A plan that would ALSO fail other checks (e.g. an unauthorized
        tool call) must still be rejected specifically for opt-out when
        opted_out is true — proving check ordering is deterministic, not
        incidentally whichever check happens to run first."""
        plan = ConversationPlan(
            action_category=ActionCategory.TOOL_CALL,
            objective="book",
            rationale="test",
            tool_name="unauthorized_tool",
        )
        result = authorize(plan, STATE, _context(opted_out=True))
        assert result.violated_policy == POLICY_NO_ACTION_AFTER_OPT_OUT
