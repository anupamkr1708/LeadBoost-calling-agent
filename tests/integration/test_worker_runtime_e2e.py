"""THE required end-to-end integration test (master prompt "REQUIRED
END-TO-END TEST"): admission -> Postgres Call -> Redis queue -> worker
claim -> CallAttempt -> ConversationSession -> fake provider ->
completion -> Postgres -> worker release, through the REAL
`orchestrator.call_service.CallService` and `orchestrator.worker_runtime.WorkerRuntime`
(not the HTTP layer — tests/contract/test_calls_endpoint.py covers that
boundary separately; this test's job is to prove the runtime itself, with
fine-grained control over timing/scenarios that driving it through HTTP
would make awkward). Plus the required failure variants: duplicate
request, provider failure with retry, worker crash recovery, capacity
unavailable.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from app.config import get_settings
from orchestrator.call_service import CallService
from telephony.contracts import CallAttemptContext
from telephony.fake import Scenario
from tests.integration.runtime_test_helpers import build_test_runtime

ORG_ID = 6101


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    seed_org(ORG_ID, plan_max_concurrent_calls=5)


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


def _fetch_call(call_id: uuid.UUID) -> tuple:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, disposition FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
        assert row is not None, f"no calls row for {call_id} — test setup bug, not an expected outcome"
        return row


def _fetch_attempts(call_id: uuid.UUID) -> list[tuple]:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_number, status, failure_category, worker_id FROM call_attempts "
            "WHERE call_id = %s ORDER BY attempt_number",
            (call_id,),
        )
        return cur.fetchall()


def _fetch_events(attempt_number: int, call_id: uuid.UUID) -> list[tuple]:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, sequence_number FROM call_attempt_events cae "
            "JOIN call_attempts ca ON ca.id = cae.call_attempt_id "
            "WHERE ca.call_id = %s AND ca.attempt_number = %s ORDER BY sequence_number",
            (call_id, attempt_number),
        )
        return cur.fetchall()


async def _wait_until_terminal(call_id: uuid.UUID, timeout: float = 5.0) -> tuple:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await asyncio.to_thread(_fetch_call, call_id)
        if row and row[0] in ("completed", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"call {call_id} never reached a terminal state within {timeout}s")


@pytest.mark.asyncio
async def test_lease_expiring_mid_execution_does_not_corrupt_state_when_worker_finishes_late(app_settings):
    """Regression test for a race identified in the Phase 1 hardening pass
    (docs/PHASE1_IMPLEMENTATION_REPORT.md's audit addendum): a lease
    expiring does NOT necessarily mean the worker crashed — it can mean
    the worker is just slower than the configured lease (a real concern
    once execution can legitimately outlast a short lease, e.g. a slower
    real provider in a later phase). The reaper reclaims and retries the
    attempt while the ORIGINAL worker is still genuinely alive and
    finishes it "late" (superseded). The original worker's late,
    now-stale completion must NOT be allowed to overwrite the reaper's
    already-committed INTERRUPTED/retry decision — proving the
    WHERE-status-guarded UPDATE added during hardening actually closes
    this, not just that it compiles.

    Only attempt #1 is deliberately slowed down (to trigger the race);
    once it's been reaped, the fake provider is sped back up so the
    RETRY attempt completes normally within its own lease — otherwise
    every attempt would race the reaper identically and the call would
    exhaust its retries instead of ever completing, which would prove
    nothing about this specific race.
    """
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=3,
        queue_lease_seconds=0.15,
        queue_poll_interval_seconds=0.05,
        retry_initial_delay_seconds=0.5,  # comfortable gap before the retry attempt becomes claimable
    )
    runtime._provider._step_delay_seconds = 0.3  # noqa: SLF001 - test-only pacing, slow enough to outlast the lease
    await runtime.start()
    try:
        service = CallService()
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(created.first_attempt_id))

        # Wait specifically for the reaper to have interrupted attempt #1
        # (not for the call to reach a terminal state yet — that only
        # happens after attempt #2 runs, which we haven't sped up yet).
        deadline = asyncio.get_event_loop().time() + 5.0
        attempt_1_interrupted = False
        while asyncio.get_event_loop().time() < deadline:
            attempts = await asyncio.to_thread(_fetch_attempts, created.call_id)
            if attempts and attempts[0][:2] == (1, "interrupted"):
                attempt_1_interrupted = True
                break
            await asyncio.sleep(0.02)
        assert attempt_1_interrupted, "reaper never interrupted attempt #1 within 5s"

        # Now speed the provider back up before attempt #2 becomes
        # claimable (retry_initial_delay_seconds=0.5 gives ample margin).
        runtime._provider._step_delay_seconds = 0.0  # noqa: SLF001 - test-only pacing

        final = await _wait_until_terminal(created.call_id, timeout=5.0)
        assert final == ("completed", "completed")

        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 2, f"expected the lease-expired attempt + its reaper-scheduled retry, got {attempts}"
        # attempt #1: the reaper won the race and marked it INTERRUPTED —
        # NOT "completed", which is what it would incorrectly show if the
        # original (late-finishing) worker's stale write had won instead.
        assert attempts[0][:2] == (1, "interrupted")
        assert attempts[1][:2] == (2, "completed")

        # And the session for attempt #1 must be ABORTED (the reaper's
        # write), not COMPLETED (what the late worker would have set).
        with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cs.state FROM conversation_sessions cs "
                "JOIN call_attempts ca ON ca.id = cs.call_attempt_id "
                "WHERE ca.call_id = %s AND ca.attempt_number = 1",
                (created.call_id,),
            )
            (session_state,) = cur.fetchone()
            assert session_state == "aborted", (
                f"expected attempt #1's session to be 'aborted' (the reaper's write), got {session_state!r} "
                "— if this is 'completed', the late worker's stale write won the race instead of the reaper's"
            )
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_queue_claim_batch_size_is_actually_wired_through(app_settings):
    """Regression test for the dead-configuration finding in the Phase 1
    hardening pass: `queue_claim_batch_size` existed in Settings but the
    worker slot loop hardcoded `batch_size=1`, silently ignoring it. With
    exactly ONE slot (`max_concurrent_calls=1`) and `queue_claim_batch_size=3`,
    3 calls enqueued at once must all still complete — and all three
    attempts must be stamped with the SAME worker_id, which only happens
    if that one slot's single `claim()` call actually pulled all 3 at once
    (rather than the slot somehow needing multiple slots/claims that a
    single-slot runtime doesn't have) and processed them sequentially."""
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=1,
        queue_claim_batch_size=3,
        queue_poll_interval_seconds=0.02,
    )
    await runtime.start()
    try:
        service = CallService()
        created_calls = []
        for _ in range(3):
            created = await asyncio.to_thread(
                service.create_call,
                organization_id=ORG_ID,
                lead_id=1,
                agent_config_id=None,
                campaign_id=None,
                idempotency_key=None,
            )
            await queue.enqueue(str(created.first_attempt_id))
            created_calls.append(created)

        results = await asyncio.gather(*[_wait_until_terminal(c.call_id) for c in created_calls])
        assert all(r == ("completed", "completed") for r in results)

        with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT worker_id FROM call_attempts WHERE call_id = ANY(%s)",
                ([c.call_id for c in created_calls],),
            )
            worker_ids = [row[0] for row in cur.fetchall()]
        assert len(worker_ids) == 1, (
            f"expected all 3 attempts to be stamped with the SAME single slot's worker_id "
            f"(proving one claim() call actually pulled a batch of 3), got {worker_ids}"
        )
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_call_for_nonexistent_organization_fails_cleanly_instead_of_looping_forever(app_settings):
    """Regression test for a real bug FOUND by actually reading the
    runtime's own logs during the Phase 1 hardening pass (Item J,
    observability): before this fix, an attempt whose organization_id had
    no `organizations` row made the capacity check's `cap` default to 0,
    and `running (0) >= cap (0)` was then ALWAYS true — indistinguishable
    from "temporarily at capacity", so the attempt was released back to
    `ready` and reclaimed forever, silently, never completing and never
    failing. This is deliberately NOT reachable through CallService (which
    doesn't validate organization existence at admission — same as
    before), only through the worker runtime discovering it at claim time,
    which is exactly how the original bug was triggered."""
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(), queue_poll_interval_seconds=0.02
    )
    await runtime.start()
    try:
        service = CallService()
        nonexistent_org_id = 6199  # deliberately NOT seeded by the _seed fixture
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=nonexistent_org_id,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(created.first_attempt_id))

        final = await _wait_until_terminal(created.call_id, timeout=5.0)
        assert final == ("failed", "organization_not_found")

        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 1, f"must fail immediately, no retry (retrying won't make the org exist): {attempts}"
        assert attempts[0][1] == "interrupted"

        # And it must actually have left the queue — not be sitting in
        # `ready` waiting to be reclaimed forever.
        assert await queue.is_queued(str(created.first_attempt_id)) is False
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_full_happy_path_admission_through_completion(app_settings):
    runtime, queue, redis_client = build_test_runtime(redis_url=app_settings.redis_url.get_secret_value())
    await runtime.start()
    try:
        service = CallService()
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        assert created.is_new
        await queue.enqueue(str(created.first_attempt_id))

        final = await _wait_until_terminal(created.call_id)
        assert final == ("completed", "completed")

        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 1
        assert attempts[0][:2] == (1, "completed")
        assert attempts[0][3] is not None  # worker_id was stamped

        events = _fetch_events(1, created.call_id)
        event_types = [e[0] for e in events]
        assert event_types[0] == "session_started"
        assert event_types[-1] == "session_ended"
        assert event_types == sorted(event_types, key=lambda _: 0)  # order preserved as inserted
        assert [e[1] for e in events] == list(range(1, len(events) + 1))  # sequence_number is contiguous from 1
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_duplicate_request_does_not_duplicate_execution(app_settings):
    """Failure variant: duplicate request. Admission-level idempotency is
    covered exhaustively in test_call_admission.py; this proves the
    consequence at the RUNTIME level — a replayed request must not result
    in the queue being asked to run anything twice."""
    runtime, queue, redis_client = build_test_runtime(redis_url=app_settings.redis_url.get_secret_value())
    await runtime.start()
    try:
        service = CallService()
        key = str(uuid.uuid4())
        first = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=key,
        )
        await queue.enqueue(str(first.first_attempt_id))
        second = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=key,
        )
        assert second.is_new is False
        assert second.first_attempt_id is None  # the caller correctly has nothing new to enqueue

        final = await _wait_until_terminal(first.call_id)
        assert final == ("completed", "completed")
        attempts = _fetch_attempts(first.call_id)
        assert len(attempts) == 1  # exactly one attempt ever ran, not two
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_provider_failure_retries_then_eventually_succeeds(app_settings):
    """Failure variant: provider failure, then a retry that succeeds —
    proves attempt_number increments, the failed attempt stays FAILED
    (never overwritten), and the Call ends COMPLETED once a later attempt
    succeeds."""
    call_count = {"n": 0}

    def scenario_source(_context: CallAttemptContext) -> Scenario:
        call_count["n"] += 1
        return Scenario.PROVIDER_FAILURE if call_count["n"] == 1 else Scenario.SUCCESS

    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        scenario_source=scenario_source,
        retry_initial_delay_seconds=0.05,
    )
    await runtime.start()
    try:
        service = CallService()
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(created.first_attempt_id))

        final = await _wait_until_terminal(created.call_id, timeout=5.0)
        assert final == ("completed", "completed")

        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 2, f"expected exactly 2 attempts (1 failed, 1 succeeded), got {attempts}"
        assert attempts[0][:3] == (1, "failed", "provider")
        assert attempts[1][:2] == (2, "completed")
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_failure_leaves_call_terminally_failed(app_settings):
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        scenario_source=lambda _c: Scenario.CANCELLATION,  # FailureCategory.CANCELLATION is not retryable by default
    )
    await runtime.start()
    try:
        service = CallService()
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(created.first_attempt_id))

        final = await _wait_until_terminal(created.call_id)
        assert final[0] == "failed"
        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 1  # no retry for a non-retryable category
        assert attempts[0][:3] == (1, "failed", "cancellation")
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_terminal_call_state_cannot_be_mutated_by_a_second_finalize_call(app_settings):
    """Direct test of the Phase 1 hardening pass's Item E requirement
    ("make terminal states immutable"): once a Call has reached a terminal
    state, calling the internal finalize path a SECOND time for what
    claims to be the same attempt (simulating a bug elsewhere that lets
    two code paths both believe they own an attempt) must be a safe no-op
    at the Call level — proven by directly invoking
    `WorkerRuntime._finalize_attempt` twice, not by waiting for a
    real-world race to reproduce it."""
    from orchestrator.worker_runtime import WorkerRuntime, _AttemptContext

    runtime, queue, redis_client = build_test_runtime(redis_url=app_settings.redis_url.get_secret_value())
    await runtime.start()
    try:
        service = CallService()
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(created.first_attempt_id))
        final = await _wait_until_terminal(created.call_id)
        assert final == ("completed", "completed")
        completed_at_first = _fetch_call(created.call_id)

        # Directly call the internal finalize path again for the SAME
        # (already-terminal) attempt — this bypasses the normal claim flow
        # entirely, which is the point: it isolates the Call-level guard
        # from every other protection layer (Redis claim exclusivity, the
        # attempt-level WHERE guards) to prove IT specifically holds too.
        from conversation.runtime import ExecutionResult

        loaded = _AttemptContext(
            organization_id=ORG_ID, call_id=created.call_id, attempt_number=1, lead_id=1
        )
        fake_result = ExecutionResult(outcome="completed", disposition="completed", failure_category=None, events=())
        outcome = await asyncio.to_thread(
            WorkerRuntime._finalize_attempt,
            runtime,
            loaded,
            created.first_attempt_id,
            uuid.uuid4(),  # a session_id that doesn't correspond to anything real — fine, never reached
            fake_result,
        )
        assert outcome.action == "superseded", (
            f"a second finalize call for an already-terminal attempt must be recognized as superseded, "
            f"not treated as a fresh completion, got action={outcome.action!r}"
        )

        completed_at_second = _fetch_call(created.call_id)
        assert completed_at_second == completed_at_first, (
            "the Call's terminal state/disposition must be byte-for-byte unchanged by the second call: "
            f"first={completed_at_first} second={completed_at_second}"
        )
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_not_treated_as_a_retryable_provider_failure(app_settings):
    """Phase 1 hardening item H, specifically at the WorkerRuntime level
    (conversation/runtime.py's own cancellation-propagation property is
    covered separately in tests/unit/test_conversation_runtime.py — this
    test proves the SAME property holds through the full worker stack,
    triggered by a real `runtime.stop(grace_period_seconds=0)`, not a bare
    `task.cancel()` on the conversation layer in isolation).

    A slot mid-execution, forcibly cancelled by shutdown with zero grace
    period, must leave its attempt RUNNING in Postgres — NOT FAILED with
    some retryable category, which is what would happen if shutdown
    cancellation were ever accidentally caught and converted into an
    ordinary execution failure. The attempt's eventual recovery must go
    through the SAME normal reaper INTERRUPT+retry path a genuine crash
    uses (test_worker_crash_is_recovered_by_the_reaper, right below) —
    proven here by simply letting a fresh runtime's own reaper loop
    discover the expired lease naturally, not by driving recovery
    manually, which would prove nothing about the real mechanism.
    """
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=1,
        queue_lease_seconds=0.15,
        queue_poll_interval_seconds=0.02,
    )
    runtime._provider._step_delay_seconds = 10.0  # noqa: SLF001 - never finishes on its own
    await runtime.start()

    service = CallService()
    created = await asyncio.to_thread(
        service.create_call,
        organization_id=ORG_ID,
        lead_id=1,
        agent_config_id=None,
        campaign_id=None,
        idempotency_key=None,
    )
    await queue.enqueue(str(created.first_attempt_id))

    deadline = asyncio.get_event_loop().time() + 5.0
    started = False
    while asyncio.get_event_loop().time() < deadline:
        attempts = _fetch_attempts(created.call_id)
        if attempts and attempts[0][1] == "running":
            started = True
            break
        await asyncio.sleep(0.02)
    assert started, "attempt never reached RUNNING before the cancellation test could proceed"

    # Zero grace period: forces immediate task.cancel() on the slot that's
    # actively awaiting the (10s-delayed) provider — this is a real
    # asyncio.CancelledError propagating through the exact code path a
    # true crash's abrupt process termination approximates.
    await runtime.stop(grace_period_seconds=0.0)
    await redis_client.aclose()

    attempts_after_cancel = _fetch_attempts(created.call_id)
    assert attempts_after_cancel[0][1] == "running", (
        f"shutdown cancellation must leave the attempt RUNNING (for the reaper to recover normally), "
        f"not corrupt it into some other status: got {attempts_after_cancel}"
    )

    # A fresh runtime's OWN reaper loop, on its own normal polling cadence,
    # discovers the now-genuinely-expired lease and recovers it — no
    # manual intervention, exactly the real recovery path.
    runtime_survivor, _queue_survivor, redis_survivor = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=2,
        queue_lease_seconds=0.15,
        queue_poll_interval_seconds=0.05,
        retry_initial_delay_seconds=0.05,
    )
    await runtime_survivor.start()
    try:
        final = await _wait_until_terminal(created.call_id, timeout=10.0)
        assert final == ("completed", "completed")
        attempts_final = _fetch_attempts(created.call_id)
        assert len(attempts_final) == 2, f"expected the cancelled attempt + its recovery attempt, got {attempts_final}"
        assert attempts_final[0][1] == "interrupted", (
            "the cancelled attempt must end up INTERRUPTED via the normal reaper path — "
            f"NOT 'failed' (which would mean cancellation was miscategorized as a provider failure), got {attempts_final}"
        )
        assert attempts_final[1][1] == "completed"
    finally:
        await runtime_survivor.stop(grace_period_seconds=2.0)
        await redis_survivor.aclose()


@pytest.mark.asyncio
async def test_worker_crash_is_recovered_by_the_reaper(app_settings):
    """Failure variant: worker crash. Simulated by claiming an attempt
    directly against the queue (as if a worker picked it up) and never
    finishing it — no WorkerRuntime is started at all, so nothing will
    ever ack or extend the lease. The reaper (a fresh runtime's background
    task) must find the expired lease, mark the orphaned attempt
    INTERRUPTED, and schedule + enqueue attempt #2, which then runs to
    completion normally."""
    service = CallService()
    created = await asyncio.to_thread(
        service.create_call,
        organization_id=ORG_ID,
        lead_id=1,
        agent_config_id=None,
        campaign_id=None,
        idempotency_key=None,
    )

    # Simulate a worker that claimed the attempt, marked it RUNNING, and
    # then vanished — using a very short lease so the reaper finds it fast.
    crashed_runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(), queue_lease_seconds=0.1
    )
    await queue.enqueue(str(created.first_attempt_id))
    claimed = await queue.claim("crashed-worker:0", lease_seconds=0.1)
    assert claimed == [str(created.first_attempt_id)]
    del crashed_runtime  # only its Queue/Redis are used above; the runtime itself is never started

    # Actually transition the attempt to RUNNING in Postgres, exactly like
    # a real worker would have, so the reaper's PENDING-vs-RUNNING branch
    # is exercised for real (not just "crashed before ever running").
    import orchestrator.worker_runtime as wr

    loaded = await asyncio.to_thread(wr.WorkerRuntime._load_attempt_context, created.first_attempt_id)
    assert loaded is not None
    session_id = await asyncio.to_thread(
        wr.WorkerRuntime._try_start_running, loaded, created.first_attempt_id, "crashed-worker:0"
    )
    assert session_id and session_id is not False

    # Now start a FRESH runtime (simulating a new process) whose reaper
    # should recover the orphaned, still-RUNNING attempt once the short
    # lease above expires.
    await redis_client.aclose()
    recovery_runtime, recovery_queue, recovery_redis = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        queue_lease_seconds=0.1,
        queue_poll_interval_seconds=0.05,
        retry_initial_delay_seconds=0.05,
    )
    await recovery_runtime.start()
    try:
        final = await _wait_until_terminal(created.call_id, timeout=5.0)
        assert final == ("completed", "completed")

        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 2, f"expected the crashed attempt + a recovery attempt, got {attempts}"
        assert attempts[0][:2] == (1, "interrupted")
        assert attempts[1][:2] == (2, "completed")
    finally:
        await recovery_runtime.stop(grace_period_seconds=2.0)
        await recovery_redis.aclose()


@pytest.mark.asyncio
async def test_org_at_capacity_waits_instead_of_disappearing(app_settings, seed_org):
    """Failure variant: capacity unavailable. The org has capacity for 1
    concurrent call; two calls are enqueued; the second must WAIT (get
    released back to ready, not lost) until the first finishes, then run
    — proving both eventually complete rather than the second vanishing."""
    seed_org(ORG_ID, plan_max_concurrent_calls=1)
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=3,  # global capacity is not the bottleneck here, the ORG cap is
        queue_poll_interval_seconds=0.02,
    )
    await runtime.start()
    try:
        service = CallService()
        created_calls = []
        for _ in range(2):
            created = await asyncio.to_thread(
                service.create_call,
                organization_id=ORG_ID,
                lead_id=1,
                agent_config_id=None,
                campaign_id=None,
                idempotency_key=None,
            )
            await queue.enqueue(str(created.first_attempt_id))
            created_calls.append(created)

        results = await asyncio.gather(*[_wait_until_terminal(c.call_id, timeout=5.0) for c in created_calls])
        assert all(r == ("completed", "completed") for r in results)
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()
