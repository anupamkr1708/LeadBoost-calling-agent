"""The Worker Runtime: owns execution capacity and drives claimed attempts
through `conversation.runtime` to completion. See docs/PHASE1_DESIGN.md
"Concurrency / worker acquisition", "Worker-crash recovery", and
"Shutdown" for the full reasoning; this module implements exactly that
design.

Concurrency model: `max_concurrent_calls` background asyncio tasks
("slots"), each processing at most one claimed attempt at a time before
looping back to claim again. That IS the global bound — no separate
`asyncio.Semaphore` is layered on top, because N slot tasks each running
sequentially already guarantees at most N concurrently-RUNNING attempts;
a semaphore here would be a second mechanism enforcing the same fact the
task count already enforces.

Postgres access in this module is synchronous (storage.db is sync
SQLAlchemy, unchanged from Phase 0 — see docs/PHASE1_DESIGN.md
"Composition root" for why this wasn't rewritten to async) and is always
offloaded via `asyncio.to_thread` so it doesn't block the event loop the
other slots/reaper/reconciliation tasks share. Every `asyncio.to_thread`
helper below RETURNS its result rather than writing to `self` — a
worker-thread callback mutating shared instance state would be a real
data race across concurrently-running slots, not just an awkward API.
Redis access is native async (`orchestrator.queue.Queue` wraps
`redis.asyncio`) and happens only on the asyncio side of that boundary.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from conversation.runtime import ExecutionResult, execute_call_attempt
from orchestrator.failures import RetryPolicy
from orchestrator.queue import Queue
from orchestrator.states import (
    CALL_ATTEMPT_STATES,
    CALL_STATES,
    SESSION_STATES,
    CallAttemptState,
    CallState,
    SessionState,
)
from storage.db import org_scoped_session, system_session
from telephony.contracts import CallAttemptContext, FailureCategory, TelephonyProvider

# structlog, not stdlib logging — matching app/main.py's configuration
# (Phase 1 hardening item J). Before this fix, this module used a plain
# `logging.getLogger(__name__)` with no handler configured anywhere in the
# app (only structlog is set up, and its default logger factory doesn't
# feed stdlib logging), meaning INFO-level logs here would never have been
# emitted at all, and even the existing WARNING/ERROR calls had no
# structured fields — a real, previously-invisible observability gap, not
# just a style inconsistency.
logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _AttemptContext:
    organization_id: int
    call_id: uuid.UUID
    attempt_number: int
    lead_id: int


@dataclass(frozen=True)
class _FinalizeOutcome:
    action: Literal["completed", "terminal_failure", "retry_scheduled", "superseded"]
    next_attempt_id: uuid.UUID | None = None
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class _ReaperOutcome:
    action: Literal["requeue_same", "requeue_new", "none"]
    attempt_id: uuid.UUID | None = None
    delay_seconds: float = 0.0


class WorkerRuntime:
    def __init__(
        self,
        *,
        instance_id: str,
        queue: Queue,
        provider: TelephonyProvider,
        retry_policy: RetryPolicy,
        max_concurrent_calls: int,
        queue_lease_seconds: float,
        queue_poll_interval_seconds: float,
        provider_operation_timeout_seconds: float,
        queue_claim_batch_size: int = 1,
    ) -> None:
        self._instance_id = instance_id
        self._queue = queue
        self._provider = provider
        self._retry_policy = retry_policy
        self._max_concurrent_calls = max_concurrent_calls
        self._queue_lease_seconds = queue_lease_seconds
        self._queue_poll_interval_seconds = queue_poll_interval_seconds
        self._provider_operation_timeout_seconds = provider_operation_timeout_seconds
        self._queue_claim_batch_size = queue_claim_batch_size
        self._stopping = False
        self._tasks: list[asyncio.Task[None]] = []

    # --- lifecycle: this runtime is the SOLE owner of these background
    # tasks (docs/PHASE1_DESIGN.md "Background work" requirement) — nothing
    # else starts, cancels, or awaits them. ---

    async def start(self) -> None:
        for slot in range(self._max_concurrent_calls):
            worker_id = f"{self._instance_id}:{slot}"
            self._tasks.append(asyncio.create_task(self._worker_slot_loop(worker_id), name=f"worker-slot-{slot}"))
        self._tasks.append(asyncio.create_task(self._reaper_loop(), name="reaper"))
        self._tasks.append(asyncio.create_task(self._reconciliation_loop(), name="reconciliation"))

    async def stop(self, grace_period_seconds: float) -> None:
        """See docs/PHASE1_DESIGN.md "Shutdown": stop taking new work,
        give in-flight work `grace_period_seconds` to finish naturally,
        then cancel whatever's left. A cancelled in-flight attempt is left
        RUNNING in Postgres on purpose — the next process's reaper finds
        its Redis lease expired and INTERRUPTs+retries it correctly."""
        self._stopping = True
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=grace_period_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
        self._tasks = []

    # --- worker slots ---

    async def _worker_slot_loop(self, worker_id: str) -> None:
        while not self._stopping:
            try:
                claimed = await self._queue.claim(
                    worker_id, self._queue_lease_seconds, batch_size=self._queue_claim_batch_size
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("queue_claim_failed", worker_id=worker_id)
                await asyncio.sleep(self._queue_poll_interval_seconds)
                continue
            if not claimed:
                await asyncio.sleep(self._queue_poll_interval_seconds)
                continue
            # A slot is still ONE execution capacity slot — batching here
            # only reduces Redis round-trips when there's a backlog
            # (claim() fetches up to queue_claim_batch_size items in one
            # call instead of needing one claim() per item); it does NOT
            # mean this slot runs them concurrently. Each claimed item is
            # still processed fully, one at a time, before the next — the
            # global concurrency bound (docs/PHASE1_DESIGN.md "Concurrency
            # / worker acquisition") is unaffected by this setting.
            for attempt_id_str in claimed:
                if self._stopping:
                    # Mid-batch shutdown: whatever's left in `claimed` was
                    # already atomically removed from `ready` and is sitting
                    # in `inflight` with a real lease — leaving it there
                    # (rather than trying to somehow "return" it) is exactly
                    # the same, already-correct recovery path a crash mid-
                    # batch would hit: the reaper reclaims it once its
                    # lease expires (docs/PHASE1_DESIGN.md "Shutdown").
                    break
                try:
                    await self._execute_claimed_attempt(worker_id, attempt_id_str)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # An unhandled exception here (a transient DB blip, a bug)
                    # must NOT be allowed to kill this slot's task permanently
                    # — WorkerRuntime.start() spawns each slot exactly once and
                    # has no supervisor to restart a dead one, so an unguarded
                    # crash here would be a slow, silent capacity leak: each
                    # crash permanently loses one slot until eventually none
                    # are left processing work at all. Logging and continuing
                    # the loop is what keeps this a transient blip instead.
                    logger.exception(
                        "unhandled_error_executing_claimed_attempt",
                        worker_id=worker_id,
                        attempt_id=attempt_id_str,
                    )

    async def _execute_claimed_attempt(self, worker_id: str, attempt_id_str: str) -> None:
        attempt_id = uuid.UUID(attempt_id_str)
        loaded = await asyncio.to_thread(self._load_attempt_context, attempt_id)
        if loaded is None:
            # Attempt vanished or is already non-PENDING (a completion and
            # a stale claim raced) — nothing to run, just drop it.
            await self._queue.ack(attempt_id_str)
            return

        logger.info(
            "attempt_claimed",
            worker_id=worker_id,
            attempt_id=attempt_id_str,
            call_id=str(loaded.call_id),
            organization_id=loaded.organization_id,
            attempt_number=loaded.attempt_number,
        )

        session_id = await asyncio.to_thread(self._try_start_running, loaded, attempt_id, worker_id)
        if session_id is None:
            # Already transitioned out of PENDING by something else
            # (defense-in-depth beyond the Redis claim) — trust whatever's
            # authoritative now.
            await self._queue.ack(attempt_id_str)
            return
        if session_id == "missing_org":
            # Permanent, not transient: the organization_id this attempt
            # references has no `organizations` row at all. Unlike
            # capacity (which will free up), this will never resolve on
            # its own — fail the attempt and the Call outright rather than
            # looping forever (the bug this fixed — see
            # _try_start_running's docstring). No RUNNING transition ever
            # happened, so this goes straight to a terminal write, not
            # through _finalize_attempt's RUNNING-gated path.
            logger.error(
                "attempt_failed_missing_organization",
                worker_id=worker_id,
                attempt_id=attempt_id_str,
                call_id=str(loaded.call_id),
                organization_id=loaded.organization_id,
            )
            await asyncio.to_thread(self._fail_attempt_missing_org, loaded, attempt_id)
            await self._queue.ack(attempt_id_str)
            return
        if session_id == "capacity_blocked":
            # Org at its plan_max_concurrent_calls cap right now. The call
            # WAITS, it doesn't disappear: release the claim back to ready
            # with a short delay so this org's other queued work doesn't
            # hot-spin this slot.
            logger.info(
                "attempt_capacity_blocked",
                worker_id=worker_id,
                attempt_id=attempt_id_str,
                call_id=str(loaded.call_id),
                organization_id=loaded.organization_id,
            )
            await self._queue.fail_and_reschedule(
                attempt_id_str, attempt_id_str, ready_at=time.time() + self._queue_poll_interval_seconds
            )
            return

        logger.info(
            "attempt_started",
            worker_id=worker_id,
            attempt_id=attempt_id_str,
            call_id=str(loaded.call_id),
            organization_id=loaded.organization_id,
            session_id=str(session_id),
        )

        context = CallAttemptContext(call_attempt_id=attempt_id, attempt_number=loaded.attempt_number)
        result = await execute_call_attempt(context, self._provider, self._provider_operation_timeout_seconds)
        outcome = await asyncio.to_thread(self._finalize_attempt, loaded, attempt_id, session_id, result)
        logger.info(
            "attempt_finalized",
            worker_id=worker_id,
            attempt_id=attempt_id_str,
            call_id=str(loaded.call_id),
            organization_id=loaded.organization_id,
            outcome=outcome.action,
            disposition=result.disposition,
            failure_category=result.failure_category.value if result.failure_category else None,
        )
        if outcome.action == "retry_scheduled":
            assert outcome.next_attempt_id is not None
            logger.info(
                "retry_scheduled",
                call_id=str(loaded.call_id),
                previous_attempt_id=attempt_id_str,
                next_attempt_id=str(outcome.next_attempt_id),
                organization_id=loaded.organization_id,
                delay_seconds=outcome.delay_seconds,
            )
            await self._queue.fail_and_reschedule(
                attempt_id_str, str(outcome.next_attempt_id), ready_at=time.time() + outcome.delay_seconds
            )
        else:
            await self._queue.ack(attempt_id_str)

    @staticmethod
    def _fail_attempt_missing_org(loaded: _AttemptContext, attempt_id: uuid.UUID) -> None:
        """The fix for the bug documented in `_try_start_running`'s
        docstring: an attempt whose organization_id has no `organizations`
        row is a permanent failure, discovered before the attempt ever
        reached RUNNING — so this goes PENDING -> INTERRUPTED (not
        RUNNING -> FAILED, which would be asserting the attempt actually
        ran) and QUEUED -> FAILED directly on the Call. No retry is
        scheduled; retrying wouldn't help; the org still won't exist."""
        with org_scoped_session(loaded.organization_id) as session:
            CALL_ATTEMPT_STATES.transition(CallAttemptState.PENDING, CallAttemptState.INTERRUPTED)
            session.execute(
                text(
                    "UPDATE call_attempts SET status = :status, ended_at = now(), "
                    "failure_category = :fc, failure_detail = :fd "
                    "WHERE id = :id AND status = 'pending'"
                ),
                {
                    "status": CallAttemptState.INTERRUPTED,
                    "fc": FailureCategory.VALIDATION.value,
                    "fd": f"organization_id={loaded.organization_id} has no organizations row",
                    "id": attempt_id,
                },
            )
            CALL_STATES.transition(CallState.QUEUED, CallState.FAILED)
            session.execute(
                text(
                    "UPDATE calls SET status = :status, ended_at = now(), disposition = :disposition "
                    "WHERE id = :call_id AND status = 'queued'"
                ),
                {
                    "status": CallState.FAILED,
                    "disposition": "organization_not_found",
                    "call_id": loaded.call_id,
                },
            )

    # --- synchronous DB helpers (run via asyncio.to_thread; each RETURNS
    # its result rather than mutating `self` — see module docstring) ---

    @staticmethod
    def _load_attempt_context(attempt_id: uuid.UUID) -> _AttemptContext | None:
        # Cross-org by necessity: we only have a bare attempt_id from the
        # Redis queue and don't know its org yet — see storage.db.system_session.
        with system_session() as session:
            row = session.execute(
                text(
                    "SELECT organization_id, call_id, attempt_number, lead_id, status "
                    "FROM call_attempts WHERE id = :id"
                ),
                {"id": attempt_id},
            ).fetchone()
        if row is None or row.status != CallAttemptState.PENDING:
            return None
        return _AttemptContext(
            organization_id=row.organization_id,
            call_id=row.call_id,
            attempt_number=row.attempt_number,
            lead_id=row.lead_id,
        )

    @staticmethod
    def _try_start_running(
        loaded: _AttemptContext, attempt_id: uuid.UUID, worker_id: str
    ) -> uuid.UUID | Literal["capacity_blocked", "missing_org"] | None:
        """Returns the new ConversationSession id on success,
        `"capacity_blocked"` if the org exists but is at its concurrency
        cap (attempt left PENDING, caller reschedules), `"missing_org"` if
        the organization_id referenced by this attempt has no
        `organizations` row at all (a data-consistency problem, not a
        transient one — see below), or `None` if the attempt was no
        longer PENDING (nothing to do).

        `"missing_org"` is a real bug this Phase 1 hardening pass found by
        actually reading the runtime's own logs (Item J), not by reasoning
        about the code in the abstract: before this fix, a nonexistent org
        (`cap_row is None`) made `cap` default to 0, and `running >= cap`
        (0 >= 0) was then ALWAYS true — indistinguishable from "temporarily
        at capacity", so the attempt was released back to `ready` and
        reclaimed forever, silently, never completing and never failing.
        Treating "org doesn't exist" as its own outcome (permanent,
        non-retryable) instead of conflating it with "org is busy right
        now" (transient, worth waiting for) is the actual fix.

        The org row is locked with SELECT ... FOR UPDATE first so the
        running-attempt count read below is serialized against any other
        concurrent "start an attempt for this org" transaction — without
        that lock, two concurrent transactions could both read a
        just-under-capacity count and both proceed, over-provisioning the
        org's soft concurrency cap by a small amount. This does NOT
        protect the hard "same call never runs twice" invariant — that's
        `ux_attempts_one_running_per_call`, a real unique index, unrelated
        to this lock.
        """
        with org_scoped_session(loaded.organization_id) as session:
            cap_row = session.execute(
                text("SELECT plan_max_concurrent_calls FROM organizations WHERE id = :org FOR UPDATE"),
                {"org": loaded.organization_id},
            ).fetchone()
            if cap_row is None:
                return "missing_org"
            cap = cap_row.plan_max_concurrent_calls
            running_row = session.execute(
                text("SELECT count(*) AS n FROM call_attempts WHERE organization_id = :org AND status = 'running'"),
                {"org": loaded.organization_id},
            ).fetchone()
            running = running_row.n if running_row is not None else 0
            if running >= cap:
                return "capacity_blocked"

            CALL_ATTEMPT_STATES.transition(CallAttemptState.PENDING, CallAttemptState.RUNNING)
            updated = session.execute(
                text(
                    "UPDATE call_attempts SET status = 'running', worker_id = :worker_id, started_at = now() "
                    "WHERE id = :id AND status = 'pending' RETURNING id"
                ),
                {"worker_id": worker_id, "id": attempt_id},
            ).fetchone()
            if updated is None:
                return None

            SESSION_STATES.transition(SessionState.STARTED, SessionState.RUNNING)
            session_row = session.execute(
                text(
                    "INSERT INTO conversation_sessions (organization_id, call_attempt_id, worker_id, state) "
                    "VALUES (:org, :attempt_id, :worker_id, :state) RETURNING id"
                ),
                {
                    "org": loaded.organization_id,
                    "attempt_id": attempt_id,
                    "worker_id": worker_id,
                    "state": SessionState.RUNNING,
                },
            ).fetchone()
            assert session_row is not None

            # Bug caught during the Phase 1 hardening pass: this write-back
            # was missing entirely, meaning call_attempts.session_id was
            # NEVER populated — silently breaking the reaper's "abort this
            # attempt's session too" logic in _recover_expired_attempt
            # below (row.session_id was always None, so that whole branch
            # was dead code). Caught by a test asserting the session's
            # actual state after a reaper recovery, not just the attempt's.
            session.execute(
                text("UPDATE call_attempts SET session_id = :session_id WHERE id = :id"),
                {"session_id": session_row.id, "id": attempt_id},
            )

            CALL_STATES.transition(CallState.QUEUED, CallState.IN_PROGRESS)
            session.execute(
                text("UPDATE calls SET status = :state WHERE id = :call_id AND status = 'queued'"),
                {"state": CallState.IN_PROGRESS, "call_id": loaded.call_id},
            )
            return uuid.UUID(str(session_row.id))

    def _finalize_attempt(
        self,
        loaded: _AttemptContext,
        attempt_id: uuid.UUID,
        session_id: uuid.UUID,
        result: ExecutionResult,
    ) -> _FinalizeOutcome:
        with org_scoped_session(loaded.organization_id) as session:
            for sequence_number, event in enumerate(result.events, start=1):
                session.execute(
                    text(
                        "INSERT INTO call_attempt_events "
                        "(organization_id, call_attempt_id, sequence_number, event_type, detail) "
                        "VALUES (:org, :attempt_id, :seq, :event_type, :detail)"
                    ),
                    {
                        "org": loaded.organization_id,
                        "attempt_id": attempt_id,
                        "seq": sequence_number,
                        "event_type": event.event_type,
                        "detail": event.detail,
                    },
                )

            # The attempt-status UPDATE (guarded, WHERE status='running')
            # happens FIRST and gates everything after it — this is the
            # one statement that determines whether this worker still
            # "owns" the attempt by the time it's ready to finalize.
            # ConversationSession and Call are only touched AFTER
            # confirming that ownership, specifically so a worker that
            # loses a race against the reaper (see below) can't still go
            # on to overwrite the reaper's already-committed
            # ConversationSession/Call state with a stale result — an
            # ordering bug this exact fix closed during the Phase 1
            # hardening pass (the session update used to run before this
            # guard existed, racing the reaper on that row even after the
            # attempt/call race was fixed).
            attempt_terminal = (
                CallAttemptState.COMPLETED if result.outcome == "completed" else CallAttemptState.FAILED
            )
            CALL_ATTEMPT_STATES.transition(CallAttemptState.RUNNING, attempt_terminal)
            failure_category = result.failure_category.value if result.failure_category else None
            updated = session.execute(
                text(
                    "UPDATE call_attempts SET status = :status, ended_at = now(), "
                    "failure_category = :fc, failure_detail = :fd "
                    "WHERE id = :id AND status = 'running' RETURNING id"
                ),
                {"status": attempt_terminal, "fc": failure_category, "fd": result.disposition, "id": attempt_id},
            ).fetchone()
            if updated is None:
                # The attempt is no longer RUNNING — the reaper got there
                # first (its lease expired while this worker was still
                # genuinely alive and finishing, not crashed: a real
                # possibility once execution can legitimately take longer
                # than queue_lease_seconds, e.g. a slower real provider in
                # a later phase — see docs/PHASE1_DESIGN.md and the Phase 1
                # hardening pass). The reaper already marked it INTERRUPTED,
                # aborted its ConversationSession, and scheduled its own
                # retry; this worker's result is now moot. The events above
                # are still inserted (a true historical record of what this
                # attempt actually did, even though the reaper superseded
                # its outcome), but nothing else is touched from here.
                return _FinalizeOutcome(action="superseded")

            session_terminal = SessionState.COMPLETED if result.outcome == "completed" else SessionState.FAILED
            SESSION_STATES.transition(SessionState.RUNNING, session_terminal)
            session.execute(
                text("UPDATE conversation_sessions SET state = :state, ended_at = now() WHERE id = :id"),
                {"state": session_terminal, "id": session_id},
            )

            if result.outcome == "completed":
                CALL_STATES.transition(CallState.IN_PROGRESS, CallState.COMPLETED)
                completed = session.execute(
                    text(
                        "UPDATE calls SET status = :status, ended_at = now(), disposition = :disposition "
                        "WHERE id = :call_id AND status = 'in_progress' RETURNING id"
                    ),
                    {"status": CallState.COMPLETED, "disposition": result.disposition, "call_id": loaded.call_id},
                ).fetchone()
                if completed is None:
                    logger.warning(
                        "call_not_in_progress_when_completing",
                        call_id=str(loaded.call_id),
                        organization_id=loaded.organization_id,
                        reason="expected exactly one caller to reach this point per attempt",
                    )
                return _FinalizeOutcome(action="completed")

            assert result.failure_category is not None
            return self._decide_retry_and_persist(session, loaded, result.failure_category, result.disposition)

    def _decide_retry_and_persist(
        self,
        session: Session,
        loaded: _AttemptContext,
        failure_category: FailureCategory,
        disposition: str,
    ) -> _FinalizeOutcome:
        decision = self._retry_policy.decide(failure_category, loaded.attempt_number)
        if decision.should_retry:
            next_row = session.execute(
                text(
                    "INSERT INTO call_attempts "
                    "(lead_id, organization_id, attempt_number, status, scheduled_at, call_id) "
                    "VALUES (:lead_id, :org, :attempt_number, 'pending', "
                    "now() + make_interval(secs => :delay), :call_id) RETURNING id"
                ),
                {
                    "lead_id": loaded.lead_id,
                    "org": loaded.organization_id,
                    "attempt_number": loaded.attempt_number + 1,
                    "delay": decision.delay_seconds,
                    "call_id": loaded.call_id,
                },
            ).fetchone()
            assert next_row is not None
            CALL_STATES.transition(CallState.IN_PROGRESS, CallState.QUEUED)
            requeued = session.execute(
                text("UPDATE calls SET status = :status WHERE id = :call_id AND status = 'in_progress' RETURNING id"),
                {"status": CallState.QUEUED, "call_id": loaded.call_id},
            ).fetchone()
            if requeued is None:
                # Defense-in-depth: by construction, only one of
                # _finalize_attempt / _recover_expired_attempt should ever
                # reach this function for a given attempt (both gate entry
                # on winning their own attempt-level status guard first —
                # see the comments on both call sites). If this still
                # fires, something upstream has a bug; log it loudly rather
                # than silently leaving the Call in whatever state it was.
                logger.warning(
                    "call_not_in_progress_when_scheduling_retry",
                    call_id=str(loaded.call_id),
                    organization_id=loaded.organization_id,
                    reason="expected exactly one caller to reach this point per attempt",
                )
            return _FinalizeOutcome(
                action="retry_scheduled", next_attempt_id=next_row.id, delay_seconds=decision.delay_seconds
            )

        CALL_STATES.transition(CallState.IN_PROGRESS, CallState.FAILED)
        failed = session.execute(
            text(
                "UPDATE calls SET status = :status, ended_at = now(), disposition = :disposition "
                "WHERE id = :call_id AND status = 'in_progress' RETURNING id"
            ),
            {"status": CallState.FAILED, "disposition": disposition, "call_id": loaded.call_id},
        ).fetchone()
        if failed is None:
            logger.warning(
                "call_not_in_progress_when_failing_terminally",
                call_id=str(loaded.call_id),
                organization_id=loaded.organization_id,
                reason="expected exactly one caller to reach this point per attempt",
            )
        return _FinalizeOutcome(action="terminal_failure")

    # --- reaper: recovers attempts whose worker crashed mid-execution ---

    async def _reaper_loop(self) -> None:
        while not self._stopping:
            try:
                expired = await self._queue.sweep_expired_leases()
                for attempt_id_str in expired:
                    outcome = await asyncio.to_thread(self._recover_expired_attempt, uuid.UUID(attempt_id_str))
                    if outcome.action != "none":
                        logger.info(
                            "attempt_recovered_by_reaper",
                            attempt_id=attempt_id_str,
                            action=outcome.action,
                            requeued_attempt_id=str(outcome.attempt_id) if outcome.attempt_id else None,
                            delay_seconds=outcome.delay_seconds,
                        )
                    if outcome.action == "requeue_same":
                        await self._queue.enqueue(attempt_id_str, ready_at=time.time())
                    elif outcome.action == "requeue_new":
                        assert outcome.attempt_id is not None
                        await self._queue.enqueue(str(outcome.attempt_id), ready_at=time.time() + outcome.delay_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reaper_sweep_failed")
            await asyncio.sleep(self._queue_poll_interval_seconds)

    def _recover_expired_attempt(self, attempt_id: uuid.UUID) -> _ReaperOutcome:
        # Same reasoning as _load_attempt_context: the reaper only has a
        # bare attempt_id from Redis's inflight set, org unknown yet.
        with system_session() as session:
            row = session.execute(
                text(
                    "SELECT organization_id, call_id, attempt_number, lead_id, status, session_id "
                    "FROM call_attempts WHERE id = :id"
                ),
                {"id": attempt_id},
            ).fetchone()
        if row is None:
            return _ReaperOutcome(action="none")
        if row.status == CallAttemptState.PENDING:
            # Crashed before the RUNNING transition ever committed — no
            # durable state to undo, just get the SAME attempt back in
            # front of a worker.
            return _ReaperOutcome(action="requeue_same", attempt_id=attempt_id)
        if row.status != CallAttemptState.RUNNING:
            return _ReaperOutcome(action="none")  # already terminal — a completion won the race

        loaded = _AttemptContext(
            organization_id=row.organization_id,
            call_id=row.call_id,
            attempt_number=row.attempt_number,
            lead_id=row.lead_id,
        )
        with org_scoped_session(row.organization_id) as session:
            CALL_ATTEMPT_STATES.transition(CallAttemptState.RUNNING, CallAttemptState.INTERRUPTED)
            updated = session.execute(
                text(
                    "UPDATE call_attempts SET status = :status, ended_at = now(), "
                    "failure_category = :fc, failure_detail = :fd "
                    "WHERE id = :id AND status = 'running' RETURNING id"
                ),
                {
                    "status": CallAttemptState.INTERRUPTED,
                    "fc": FailureCategory.TRANSIENT_INFRA.value,
                    "fd": "worker crashed: reaper detected an expired lease with the attempt still RUNNING",
                    "id": attempt_id,
                },
            ).fetchone()
            if updated is None:
                # Defense-in-depth beyond the atomic Redis sweep
                # (orchestrator/queue.py's _SWEEP_EXPIRED_SCRIPT_SOURCE):
                # something else already moved this attempt out of RUNNING
                # between our SELECT above and this UPDATE (e.g. it
                # completed normally in that exact window) — trust
                # whatever's authoritative now and do nothing further,
                # rather than blindly overwriting a real completion.
                return _ReaperOutcome(action="none")
            if row.session_id is not None:
                SESSION_STATES.transition(SessionState.RUNNING, SessionState.ABORTED)
                session.execute(
                    text("UPDATE conversation_sessions SET state = :state, ended_at = now() WHERE id = :id"),
                    {"state": SessionState.ABORTED, "id": row.session_id},
                )

            outcome = self._decide_retry_and_persist(
                session, loaded, FailureCategory.TRANSIENT_INFRA, "worker_crash"
            )
        if outcome.action == "retry_scheduled":
            assert outcome.next_attempt_id is not None
            return _ReaperOutcome(
                action="requeue_new", attempt_id=outcome.next_attempt_id, delay_seconds=outcome.delay_seconds
            )
        return _ReaperOutcome(action="none")

    # --- reconciliation: recovers the commit-then-enqueue failure window ---

    async def _reconciliation_loop(self) -> None:
        """See docs/PHASE1_DESIGN.md "Idempotency": if the process dies
        between committing a Call/first CallAttempt and pushing it into
        Redis, the attempt exists durably in Postgres but nothing ever
        enqueued it. This sweep finds PENDING attempts whose scheduled_at
        has passed and are absent from the queue, and re-enqueues them —
        Postgres is the source of truth, Redis is reconciled from it."""
        while not self._stopping:
            try:
                candidates = await asyncio.to_thread(self._find_unenqueued_pending_attempts)
                for attempt_id in candidates:
                    if not await self._queue.is_queued(str(attempt_id)):
                        logger.info("reconciliation_reenqueued_attempt", attempt_id=str(attempt_id))
                        await self._queue.enqueue(str(attempt_id), ready_at=time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reconciliation_sweep_failed")
            await asyncio.sleep(self._queue_poll_interval_seconds)

    @staticmethod
    def _find_unenqueued_pending_attempts(limit: int = 100) -> list[uuid.UUID]:
        # Genuinely cross-org by design: this sweep scans ALL organizations'
        # pending attempts for ones the queue never learned about.
        with system_session() as session:
            rows = session.execute(
                text(
                    "SELECT id FROM call_attempts WHERE status = 'pending' AND scheduled_at <= now() "
                    "ORDER BY scheduled_at ASC LIMIT :limit"
                ),
                {"limit": limit},
            ).fetchall()
        return [row.id for row in rows]
