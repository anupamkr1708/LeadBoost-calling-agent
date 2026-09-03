"""SECOND authoritative test (master prompt): N call requests against M
configured worker capacity must never result in more than M concurrently
RUNNING attempts, and must never result in the same call executing twice.
Uses a real Postgres + real Redis + the fake provider slowed down just
enough (`step_delay_seconds`) to give a concurrent sampler time to observe
the system mid-flight — this is what makes "never exceeds M" an actually
observed property, not an inference from the final state.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from app.config import get_settings
from orchestrator.call_service import CallService
from tests.integration.runtime_test_helpers import build_test_runtime

ORG_ID = 6201
N_CALLS = 12
M_CAPACITY = 3


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    # Plan cap set well above M_CAPACITY so the ORG-level cap
    # (docs/PHASE1_DESIGN.md's second concurrency dimension) is not what's
    # being tested here — this test isolates the GLOBAL process-wide bound.
    seed_org(ORG_ID, plan_max_concurrent_calls=N_CALLS)


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


def _count_running() -> int:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM call_attempts WHERE status = 'running'")
        row = cur.fetchone()
        assert row is not None
        (n,) = row
        return int(n)


async def _sample_max_running(stop_event: asyncio.Event, interval: float = 0.02) -> int:
    peak = 0
    while not stop_event.is_set():
        peak = max(peak, await asyncio.to_thread(_count_running))
        await asyncio.sleep(interval)
    peak = max(peak, await asyncio.to_thread(_count_running))
    return peak


@pytest.mark.asyncio
async def test_active_running_never_exceeds_configured_capacity(app_settings):
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(),
        max_concurrent_calls=M_CAPACITY,
        queue_poll_interval_seconds=0.02,
    )
    # step_delay_seconds keeps each simulated call "in flight" long enough
    # for the sampler below to catch the system under real concurrent load.
    runtime._provider._step_delay_seconds = 0.15  # noqa: SLF001 - test-only tuning of the fake provider's pacing

    await runtime.start()
    stop_event = asyncio.Event()
    sampler = asyncio.ensure_future(_sample_max_running(stop_event))
    try:
        service = CallService()
        created_calls = []
        for _ in range(N_CALLS):
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

        async def _wait(call_id: uuid.UUID) -> tuple:
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                    cur.execute("SELECT status FROM calls WHERE id = %s", (call_id,))
                    row = cur.fetchone()
                    assert row is not None
                    (status,) = row
                    if status in ("completed", "failed"):
                        return (status,)
                await asyncio.sleep(0.05)
            raise AssertionError(f"call {call_id} never finished")

        results = await asyncio.gather(*[_wait(c.call_id) for c in created_calls])
        assert all(r == ("completed",) for r in results)
    finally:
        stop_event.set()
        peak = await sampler
        await runtime.stop(grace_period_seconds=3.0)
        await redis_client.aclose()

    assert peak <= M_CAPACITY, f"observed {peak} concurrently RUNNING attempts, configured capacity was {M_CAPACITY}"
    assert peak > 0, "sampler never observed any RUNNING attempt at all — test is not exercising real concurrency"

    # No call ever executed twice: exactly one attempt per call, all completed.
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT call_id, count(*) FROM call_attempts WHERE organization_id = %s GROUP BY call_id HAVING count(*) > 1",
            (ORG_ID,),
        )
        duplicated = cur.fetchall()
        assert not duplicated, f"calls with more than one attempt (should be none, no failures in this test): {duplicated}"
