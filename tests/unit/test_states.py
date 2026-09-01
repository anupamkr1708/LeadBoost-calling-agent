"""Pure unit tests for orchestrator/states.py — no DB, no Redis. Every
legal transition in docs/PHASE1_DESIGN.md's diagrams is exercised, plus a
representative sample of illegal ones (not exhaustive over all N^2 pairs,
which would just be restating the transition table back at itself)."""
from __future__ import annotations

import pytest

from orchestrator.states import (
    CALL_ATTEMPT_STATES,
    CALL_STATES,
    SESSION_STATES,
    CallAttemptState,
    CallState,
    IllegalTransitionError,
    SessionState,
)


class TestCallStateMachine:
    def test_queued_to_in_progress_is_legal(self):
        assert CALL_STATES.transition(CallState.QUEUED, CallState.IN_PROGRESS) == CallState.IN_PROGRESS

    def test_queued_to_cancelled_is_legal(self):
        assert CALL_STATES.transition(CallState.QUEUED, CallState.CANCELLED) == CallState.CANCELLED

    def test_in_progress_back_to_queued_is_legal_for_retry_gap(self):
        assert CALL_STATES.transition(CallState.IN_PROGRESS, CallState.QUEUED) == CallState.QUEUED

    @pytest.mark.parametrize("terminal", [CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED])
    def test_in_progress_to_each_terminal_state_is_legal(self, terminal):
        assert CALL_STATES.transition(CallState.IN_PROGRESS, terminal) == terminal

    def test_queued_to_completed_is_illegal_must_go_through_in_progress(self):
        with pytest.raises(IllegalTransitionError):
            CALL_STATES.transition(CallState.QUEUED, CallState.COMPLETED)

    @pytest.mark.parametrize("terminal", [CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED])
    def test_terminal_states_accept_no_further_transitions(self, terminal):
        with pytest.raises(IllegalTransitionError):
            CALL_STATES.transition(terminal, CallState.QUEUED)

    def test_is_terminal(self):
        assert CALL_STATES.is_terminal(CallState.COMPLETED)
        assert not CALL_STATES.is_terminal(CallState.QUEUED)


class TestCallAttemptStateMachine:
    def test_pending_to_running_is_legal(self):
        assert (
            CALL_ATTEMPT_STATES.transition(CallAttemptState.PENDING, CallAttemptState.RUNNING)
            == CallAttemptState.RUNNING
        )

    def test_pending_to_interrupted_is_legal_crash_before_running(self):
        assert (
            CALL_ATTEMPT_STATES.transition(CallAttemptState.PENDING, CallAttemptState.INTERRUPTED)
            == CallAttemptState.INTERRUPTED
        )

    @pytest.mark.parametrize(
        "terminal", [CallAttemptState.COMPLETED, CallAttemptState.FAILED, CallAttemptState.INTERRUPTED]
    )
    def test_running_to_each_terminal_state_is_legal(self, terminal):
        assert CALL_ATTEMPT_STATES.transition(CallAttemptState.RUNNING, terminal) == terminal

    def test_pending_to_completed_is_illegal_must_go_through_running(self):
        with pytest.raises(IllegalTransitionError):
            CALL_ATTEMPT_STATES.transition(CallAttemptState.PENDING, CallAttemptState.COMPLETED)

    def test_completed_accepts_no_further_transitions_retries_are_new_rows(self):
        with pytest.raises(IllegalTransitionError):
            CALL_ATTEMPT_STATES.transition(CallAttemptState.COMPLETED, CallAttemptState.PENDING)

    def test_there_is_no_claimed_state_by_design(self):
        # docs/PHASE1_DESIGN.md: "claimed but not running" is Redis's
        # inflight set, not a DB status — asserting this negatively so a
        # future change that adds one gets caught by a failing test, not
        # silently drifting from the design doc.
        assert not hasattr(CallAttemptState, "CLAIMED")


class TestSessionStateMachine:
    def test_started_to_running_is_legal(self):
        assert SESSION_STATES.transition(SessionState.STARTED, SessionState.RUNNING) == SessionState.RUNNING

    @pytest.mark.parametrize("terminal", [SessionState.COMPLETED, SessionState.FAILED, SessionState.ABORTED])
    def test_running_to_each_terminal_state_is_legal(self, terminal):
        assert SESSION_STATES.transition(SessionState.RUNNING, terminal) == terminal

    def test_started_can_go_directly_to_aborted_immediate_crash(self):
        assert SESSION_STATES.transition(SessionState.STARTED, SessionState.ABORTED) == SessionState.ABORTED

    def test_started_to_completed_is_illegal_must_go_through_running(self):
        with pytest.raises(IllegalTransitionError):
            SESSION_STATES.transition(SessionState.STARTED, SessionState.COMPLETED)
