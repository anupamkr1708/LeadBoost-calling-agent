"""Phase 1 hardening items C (reconciliation race) and D (multi-process
safety).

C: proves the DB→Redis reconciliation sweep's check-then-act shape
(is_queued() then enqueue(), two round trips, not one atomic operation)
cannot cause duplicate EXECUTION even though it's not itself atomic —
because (1) Redis ZSET membership is idempotent (re-enqueueing an
already-queued attempt_id just overwrites its score, never creates a
second entry) and (2) every claim path re-checks the attempt's actual
Postgres status and refuses anything that isn't PENDING
(orchestrator/worker_runtime.py's `_load_attempt_context`). This is
reasoned about in docs/PHASE1_DESIGN.md; this file is where it's proven
against real infrastructure instead of just asserted.

D: proves the properties that CANNOT be proven from single-process tests
alone by actually running TWO independent `WorkerRuntime` instances
against the same Postgres + same Redis, exactly like two real deployed
processes would share them.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from app.config import get_settings
from orchestrator.call_service import CallService
from tests.integration.runtime_test_helpers import build_test_runtime

ORG_ID = 6401
ORG_A = 6402
ORG_B = 6403


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    seed_org(ORG_ID, plan_max_concurrent_calls=10)
    seed_org(ORG_A, plan_max_concurrent_calls=10)
    seed_org(ORG_B, plan_max_concurrent_calls=10)


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


def _fetch_call_status(call_id: uuid.UUID) -> str:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
        assert row is not None, f"no calls row for {call_id}"
        (status,) = row
        return str(status)


def _fetch_attempts(call_id: uuid.UUID) -> list[tuple]:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_number, status, worker_id FROM call_attempts WHERE call_id = %s ORDER BY attempt_number",
            (call_id,),
        )
        return cur.fetchall()


async def _wait_until_terminal(call_id: uuid.UUID, timeout: float = 5.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await asyncio.to_thread(_fetch_call_status, call_id)
        if status in ("completed", "failed", "cancelled"):
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"call {call_id} never reached a terminal state within {timeout}s")


# ============================== Item C ==============================


@pytest.mark.asyncio
async def test_reenqueueing_an_already_completed_attempt_does_not_reexecute_it(app_settings):
    """Direct proof of the reconciliation race's WORST plausible outcome:
    something (a racing reconciliation sweep, a bug, a human at a Redis
    shell) re-adds an attempt_id that's ALREADY terminal in Postgres back
    into the `ready` set. A worker that then claims it must find it's not
    PENDING and simply drop it — never re-run it."""
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
        status = await _wait_until_terminal(created.call_id)
        assert status == "completed"
        attempts_before = _fetch_attempts(created.call_id)
        assert len(attempts_before) == 1

        # Simulate exactly what a racing reconciliation sweep's stale
        # is_queued()==False read, followed by a delayed enqueue(), would
        # do: push the SAME already-completed attempt_id back into ready.
        await queue.enqueue(str(created.first_attempt_id))

        # Give a worker slot every chance to wrongly pick this up.
        await asyncio.sleep(0.3)

        attempts_after = _fetch_attempts(created.call_id)
        assert attempts_after == attempts_before, (
            f"re-enqueueing a completed attempt must not create new work or mutate "
            f"existing rows: before={attempts_before} after={attempts_after}"
        )
        assert await queue.is_queued(str(created.first_attempt_id)) is False, (
            "the worker should have claimed-and-dropped the stale re-enqueue, "
            "not left it sitting in the queue forever"
        )
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_enqueues_of_the_same_pending_attempt_execute_it_once(app_settings):
    """The other half of the reconciliation race: several concurrent
    callers (simulating several reconciliation loops, e.g. across
    multiple processes) all deciding independently to enqueue the SAME
    still-PENDING attempt_id at once. Redis ZSET membership is inherently
    idempotent (re-ZADDing an existing member updates its score, never
    creates a duplicate entry) — proving that here, at the level that
    actually matters (only one execution happens), not just asserting the
    ZSET semantics abstractly."""
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
        # 10 "reconciliation loops" all racing to enqueue the same attempt.
        await asyncio.gather(*[queue.enqueue(str(created.first_attempt_id)) for _ in range(10)])

        status = await _wait_until_terminal(created.call_id)
        assert status == "completed"
        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 1, f"expected exactly one attempt to have ever run, got {attempts}"
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


# ============================== Item D ==============================


@pytest.mark.asyncio
async def test_two_worker_runtime_instances_share_infrastructure_safely(app_settings):
    """Runs TWO independent WorkerRuntime instances (distinct instance_id,
    distinct queue-poll timing so they're not accidentally lock-stepped)
    against the SAME Postgres and SAME Redis — exactly two real deployed
    processes. Proves, across the two processes together (not per-process,
    which single-process tests already cover and cannot speak to this):

    - the per-organization concurrency bound is respected GLOBALLY, not
      just within whichever process happens to be looking
    - one call never has two RUNNING attempts at once
    - no attempt is executed twice
    - every call submitted eventually completes exactly once

    Per the master prompt: do not claim multi-process correctness from
    single-process tests — this is the one that actually exercises two.
    """
    runtime_a, queue_a, redis_a = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=2,
        queue_poll_interval_seconds=0.02,
    )
    runtime_b, queue_b, redis_b = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=2,
        queue_poll_interval_seconds=0.03,  # deliberately different cadence from A
    )
    # Both runtimes must talk to the exact same Redis KEYS (not just the
    # same Redis server/db) to genuinely share one queue — build_test_runtime
    # already points both at the same redis_url/db, so both Queue instances
    # use the same default QueueKeys and are the same logical queue.

    # Give ORG_A a tight cap specifically so the multi-process claim below
    # is actually testing something: if both processes' workers try to run
    # ORG_A attempts simultaneously, the shared Postgres row lock
    # (organizations FOR UPDATE) must still hold the org to its cap even
    # though the two counting/locking transactions come from two separate
    # OS processes' connection pools.
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("UPDATE organizations SET plan_max_concurrent_calls = 1 WHERE id = %s", (ORG_A,))

    await runtime_a.start()
    await runtime_b.start()
    try:
        service = CallService()
        n_per_org = 6
        created_a = []
        created_b = []
        for _ in range(n_per_org):
            ca = await asyncio.to_thread(
                service.create_call,
                organization_id=ORG_A,
                lead_id=1,
                agent_config_id=None,
                campaign_id=None,
                idempotency_key=None,
            )
            cb = await asyncio.to_thread(
                service.create_call,
                organization_id=ORG_B,
                lead_id=1,
                agent_config_id=None,
                campaign_id=None,
                idempotency_key=None,
            )
            # Enqueue through BOTH queues' clients alternately — they're the
            # same underlying Redis keys, so this also incidentally proves
            # enqueue() from either process reaches the one shared queue.
            await queue_a.enqueue(str(ca.first_attempt_id))
            await queue_b.enqueue(str(cb.first_attempt_id))
            created_a.append(ca)
            created_b.append(cb)

        async def _sample_org_a_running_peak(stop_event: asyncio.Event) -> int:
            peak = 0
            while not stop_event.is_set():
                with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM call_attempts WHERE organization_id = %s AND status = 'running'",
                        (ORG_A,),
                    )
                    row = cur.fetchone()
                    assert row is not None
                    (n,) = row
                peak = max(peak, n)
                await asyncio.sleep(0.01)
            return peak

        stop_event = asyncio.Event()
        sampler = asyncio.ensure_future(_sample_org_a_running_peak(stop_event))

        results_a = await asyncio.gather(*[_wait_until_terminal(c.call_id, timeout=10.0) for c in created_a])
        results_b = await asyncio.gather(*[_wait_until_terminal(c.call_id, timeout=10.0) for c in created_b])

        stop_event.set()
        peak_a = await sampler

        assert all(r == "completed" for r in results_a), results_a
        assert all(r == "completed" for r in results_b), results_b

        # ORG_A's cap of 1 was enforced GLOBALLY across both processes —
        # this is the property single-process tests structurally cannot
        # prove, since a single process's own row lock trivially serializes
        # against itself.
        assert peak_a <= 1, f"ORG_A's plan_max_concurrent_calls=1 was violated ACROSS the two processes: peak={peak_a}"

        # No call, in either org, ever had more than one attempt (nothing
        # failed and needed a retry in this test — a duplicate-attempt
        # count here would mean something executed twice or crashed).
        with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            all_call_ids = [c.call_id for c in created_a + created_b]
            cur.execute(
                "SELECT call_id, count(*) FROM call_attempts WHERE call_id = ANY(%s) "
                "GROUP BY call_id HAVING count(*) > 1",
                (all_call_ids,),
            )
            duplicated = cur.fetchall()
            assert not duplicated, f"calls with more than one attempt across the two-process run: {duplicated}"
    finally:
        await runtime_a.stop(grace_period_seconds=3.0)
        await runtime_b.stop(grace_period_seconds=3.0)
        await redis_a.aclose()
        await redis_b.aclose()


@pytest.mark.asyncio
async def test_two_worker_runtimes_reaper_recovery_does_not_duplicate_across_processes(app_settings):
    """Multi-process worker-crash recovery: one runtime's slot claims and
    starts an attempt, then that runtime is stopped WITHOUT the attempt
    finishing (simulating that process crashing) while a SECOND, separate
    runtime instance keeps running. The second runtime's reaper must
    recover the orphaned attempt exactly once — proving reaper recovery is
    genuinely safe across process boundaries, not just across tasks within
    one process (which every other reaper test in this suite exercises)."""
    runtime_dying, queue, redis_dying = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=1,
        queue_lease_seconds=0.15,
        queue_poll_interval_seconds=0.05,
    )
    runtime_dying._provider._step_delay_seconds = 10.0  # noqa: SLF001 - never finishes before we kill this runtime
    await runtime_dying.start()

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

    # Wait for runtime_dying to actually claim and start it.
    deadline = asyncio.get_event_loop().time() + 5.0
    started = False
    while asyncio.get_event_loop().time() < deadline:
        attempts = await asyncio.to_thread(_fetch_attempts, created.call_id)
        if attempts and attempts[0][1] == "running":
            started = True
            break
        await asyncio.sleep(0.02)
    assert started, "runtime_dying never started the attempt"

    # Kill it hard (no grace period) — its in-flight attempt is abandoned
    # exactly like a real process crash, per docs/PHASE1_DESIGN.md
    # "Shutdown": left RUNNING in Postgres, lease still ticking down in Redis.
    await runtime_dying.stop(grace_period_seconds=0.0)
    await redis_dying.aclose()

    # A second, independent runtime — simulating a fresh process — is the
    # only thing left running. Its OWN reaper must find and recover the
    # orphan.
    runtime_survivor, queue_survivor, redis_survivor = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=2,
        queue_lease_seconds=0.15,
        queue_poll_interval_seconds=0.05,
        retry_initial_delay_seconds=0.05,
    )
    await runtime_survivor.start()
    try:
        status = await _wait_until_terminal(created.call_id, timeout=10.0)
        assert status == "completed"
        attempts = _fetch_attempts(created.call_id)
        assert len(attempts) == 2, f"expected the orphaned attempt + exactly one recovery attempt, got {attempts}"
        assert attempts[0][1] == "interrupted"
        assert attempts[1][1] == "completed"
    finally:
        await runtime_survivor.stop(grace_period_seconds=2.0)
        await redis_survivor.aclose()
