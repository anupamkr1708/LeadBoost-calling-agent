"""State machines for Call, CallAttempt, and ConversationSession.

Single owner per machine: every transition in the runtime goes through
`CallStates.transition` / `CallAttemptStates.transition` /
`SessionStates.transition`, which raise `IllegalTransitionError` on an
illegal move. Nothing in this codebase does `call.status = "whatever"`
directly — see docs/PHASE1_DESIGN.md "State machines" for the full
transition diagrams and the reasoning for why there is no separate
CLAIMED state (that's Redis's `inflight` set, not a DB status) and no
separate QueueState class (an entry's state is which Redis structure it's
currently a member of; reifying that as a fourth parallel enum here would
be a second source of truth for a fact `orchestrator/queue.py` already
holds authoritatively).
"""
from __future__ import annotations

from dataclasses import dataclass


class IllegalTransitionError(ValueError):
    """Raised when code attempts a state transition that isn't in the
    legal transition table. This is a programming-error-class exception —
    it should never be caught and silently ignored; it means a call site
    has a bug."""

    def __init__(self, machine: str, current: str, target: str) -> None:
        self.machine = machine
        self.current = current
        self.target = target
        super().__init__(f"{machine}: illegal transition {current!r} -> {target!r}")


@dataclass(frozen=True)
class _Machine:
    """A named set of states plus a legal-transition table. `transition`
    is the ONLY way code in this repo should move an entity from one state
    to another — see module docstring."""

    name: str
    terminal_states: frozenset[str]
    legal_transitions: dict[str, frozenset[str]]

    def transition(self, current: str, target: str) -> str:
        if current in self.terminal_states:
            raise IllegalTransitionError(self.name, current, target)
        allowed = self.legal_transitions.get(current, frozenset())
        if target not in allowed:
            raise IllegalTransitionError(self.name, current, target)
        return target

    def is_terminal(self, state: str) -> bool:
        return state in self.terminal_states


class CallState:
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


CALL_STATES = _Machine(
    name="Call",
    terminal_states=frozenset({CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}),
    legal_transitions={
        # QUEUED = waiting for an attempt to run, including the gap between
        # a failed-but-retryable attempt and its successor. QUEUED -> FAILED
        # directly (not via IN_PROGRESS) is for the narrow case where an
        # attempt could be claimed but never legitimately started at all —
        # found during the Phase 1 hardening pass: a Call whose
        # organization_id has no corresponding `organizations` row is
        # discovered at claim time, before any attempt ever reaches
        # RUNNING, so routing it through IN_PROGRESS first would be
        # asserting something that never happened.
        CallState.QUEUED: frozenset({CallState.IN_PROGRESS, CallState.CANCELLED, CallState.FAILED}),
        # IN_PROGRESS = an attempt currently owns this call.
        CallState.IN_PROGRESS: frozenset(
            {CallState.QUEUED, CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}
        ),
    },
)


class CallAttemptState:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


CALL_ATTEMPT_STATES = _Machine(
    name="CallAttempt",
    terminal_states=frozenset(
        {CallAttemptState.COMPLETED, CallAttemptState.FAILED, CallAttemptState.INTERRUPTED}
    ),
    legal_transitions={
        # PENDING -> INTERRUPTED covers two distinct real cases, both
        # "this attempt never reached RUNNING and never will": a worker
        # crashing between claim and the RUNNING transition (the reaper's
        # original use), and — found during the Phase 1 hardening pass —
        # discovering the attempt's organization_id has no `organizations`
        # row at all, which is permanent, not transient, and must not be
        # conflated with "temporarily at capacity" (see
        # orchestrator/worker_runtime.py's _try_start_running docstring).
        CallAttemptState.PENDING: frozenset(
            {CallAttemptState.RUNNING, CallAttemptState.INTERRUPTED}
        ),
        CallAttemptState.RUNNING: frozenset(
            {CallAttemptState.COMPLETED, CallAttemptState.FAILED, CallAttemptState.INTERRUPTED}
        ),
    },
)


class SessionState:
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


SESSION_STATES = _Machine(
    name="ConversationSession",
    terminal_states=frozenset({SessionState.COMPLETED, SessionState.FAILED, SessionState.ABORTED}),
    legal_transitions={
        SessionState.STARTED: frozenset(
            {SessionState.RUNNING, SessionState.FAILED, SessionState.ABORTED}
        ),
        SessionState.RUNNING: frozenset(
            {SessionState.COMPLETED, SessionState.FAILED, SessionState.ABORTED}
        ),
    },
)
