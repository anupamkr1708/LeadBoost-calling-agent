"""Small benchmark harness for the Phase 1 worker runtime — NOT a pytest
test (it prints a report and is meant to be run manually / from CI as a
separate step), per the master prompt's "PERFORMANCE / LOAD TESTING"
requirement: measure throughput, p50/p95 latency, queue wait time, and
execution time at increasing concurrency, against real Postgres + real
Redis + the fake provider. No capacity number is claimed anywhere in
docs/PHASE1_DESIGN.md or PHASE1_IMPLEMENTATION_REPORT.md that wasn't
actually produced by running this.

Run with: .venv/bin/python scripts/benchmark.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

sys.path.insert(0, os.getcwd())

from app.config import get_settings  # noqa: E402
from orchestrator.call_service import CallService  # noqa: E402
from telephony.fake import FakeTelephonyProvider  # noqa: E402
from tests.integration.runtime_test_helpers import build_test_runtime  # noqa: E402

ORG_ID = 9999


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


def _reset() -> None:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE calls, call_attempts, conversation_sessions, call_attempt_events, "
            "call_idempotency_keys CASCADE"
        )
        cur.execute(
            "INSERT INTO organizations (id, plan_max_concurrent_calls) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET plan_max_concurrent_calls = EXCLUDED.plan_max_concurrent_calls",
            (ORG_ID, 10_000),  # org cap is not what's being measured here
        )


async def _run_one_load_level(n_calls: int, concurrency: int) -> dict[str, float]:
    _reset()
    settings = get_settings()
    # Mirrors app/main.py's composition-root fix (docs/PHASE1_IMPLEMENTATION_REPORT.md
    # §14/§16): size the to_thread executor to the runtime's own configured
    # capacity so the thread pool isn't a hidden bottleneck underneath the
    # very thing this benchmark is trying to measure.
    thread_pool = ThreadPoolExecutor(max_workers=max(concurrency + 2, 4))
    asyncio.get_running_loop().set_default_executor(thread_pool)

    runtime, queue, redis_client = build_test_runtime(
        redis_url=settings.redis_url.get_secret_value(),
        max_concurrent_calls=concurrency,
        queue_poll_interval_seconds=0.02,
    )
    # Deliberately fast (no artificial step delay) — this measures the
    # RUNTIME's own overhead (queue round trips, DB transactions, state
    # transitions), not an artificial provider delay.
    runtime._provider = FakeTelephonyProvider()  # noqa: SLF001 - test/benchmark-only override
    await runtime.start()

    service = CallService()
    submit_times: dict[uuid.UUID, float] = {}
    t_start = time.monotonic()

    async def _submit(i: int) -> uuid.UUID:
        created = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=i,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        submit_times[created.call_id] = time.monotonic()
        await queue.enqueue(str(created.first_attempt_id))
        return created.call_id

    call_ids = await asyncio.gather(*[_submit(i) for i in range(n_calls)])
    t_all_submitted = time.monotonic()

    async def _wait(call_id: uuid.UUID) -> float:
        while True:
            row = await asyncio.to_thread(_fetch_status, call_id)
            if row in ("completed", "failed"):
                return time.monotonic()
            await asyncio.sleep(0.01)

    completion_times = await asyncio.gather(*[_wait(cid) for cid in call_ids])
    t_end = time.monotonic()

    await runtime.stop(grace_period_seconds=5.0)
    await redis_client.aclose()  # type: ignore[attr-defined]  # redis-py stubs lag runtime here; aclose() exists
    thread_pool.shutdown(wait=True)

    latencies = [completion_times[i] - submit_times[call_ids[i]] for i in range(n_calls)]
    latencies.sort()

    def _pctile(p: float) -> float:
        idx = min(int(len(latencies) * p), len(latencies) - 1)
        return latencies[idx]

    wall_clock = t_end - t_start
    return {
        "n_calls": n_calls,
        "concurrency": concurrency,
        "wall_clock_seconds": wall_clock,
        "throughput_calls_per_sec": n_calls / wall_clock,
        "submit_phase_seconds": t_all_submitted - t_start,
        "p50_latency_seconds": _pctile(0.50),
        "p95_latency_seconds": _pctile(0.95),
        "max_latency_seconds": latencies[-1],
        "min_latency_seconds": latencies[0],
    }


def _fetch_status(call_id: uuid.UUID) -> str:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
        assert row is not None
        (status,) = row
        return str(status)


async def main() -> None:
    levels = [(10, 1), (10, 5), (25, 5), (25, 10), (50, 10), (50, 25)]
    results = []
    for n_calls, concurrency in levels:
        result = await _run_one_load_level(n_calls, concurrency)
        results.append(result)
        print(
            f"n={result['n_calls']:>4} concurrency={result['concurrency']:>3}  "
            f"throughput={result['throughput_calls_per_sec']:>7.1f}/s  "
            f"p50={result['p50_latency_seconds'] * 1000:>7.1f}ms  "
            f"p95={result['p95_latency_seconds'] * 1000:>7.1f}ms  "
            f"max={result['max_latency_seconds'] * 1000:>7.1f}ms  "
            f"wall={result['wall_clock_seconds']:.2f}s"
        )

    print("\n--- summary table (paste into PHASE1_IMPLEMENTATION_REPORT.md) ---")
    print("| n_calls | concurrency | throughput (calls/s) | p50 (ms) | p95 (ms) | max (ms) |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['n_calls']} | {r['concurrency']} | {r['throughput_calls_per_sec']:.1f} | "
            f"{r['p50_latency_seconds'] * 1000:.1f} | {r['p95_latency_seconds'] * 1000:.1f} | "
            f"{r['max_latency_seconds'] * 1000:.1f} |"
        )

    print(f"\nmean throughput across all levels: {statistics.mean(r['throughput_calls_per_sec'] for r in results):.1f} calls/s")


if __name__ == "__main__":
    asyncio.run(main())
