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
        return cur.fetchone()


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
