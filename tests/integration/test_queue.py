"""Integration tests for orchestrator/queue.py against a REAL Redis — the
Lua claim script's atomicity is exactly the thing that can't be proven
against a mock (see docs/PHASE1_DESIGN.md "Queue (Redis)")."""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from orchestrator.queue import Queue


@pytest_asyncio.fixture()
async def queue(app_settings, clean_db):
    redis_client: Redis = Redis.from_url(app_settings.redis_url.get_secret_value())
    q = Queue(redis_client)
    yield q
    await redis_client.aclose()


@pytest.mark.asyncio
async def test_enqueue_then_claim_returns_the_attempt(queue):
    await queue.enqueue("attempt-1")
    claimed = await queue.claim("worker-a", lease_seconds=10)
    assert claimed == ["attempt-1"]


@pytest.mark.asyncio
async def test_future_ready_at_is_not_claimable_yet(queue):
    await queue.enqueue("attempt-future", ready_at=time.time() + 60)
    claimed = await queue.claim("worker-a", lease_seconds=10)
    assert claimed == []


@pytest.mark.asyncio
async def test_claim_is_exclusive_across_concurrent_callers(queue):
    """The core correctness property: N concurrent claim() calls for the
    same single ready item must yield exactly ONE winner, never zero
    winners and never two — this is what makes the Lua script an atomic
    claim rather than a check-then-act race (docs/PHASE1_DESIGN.md)."""
    await queue.enqueue("attempt-contested")
    results = await asyncio.gather(*[queue.claim(f"worker-{i}", lease_seconds=10) for i in range(20)])
    winners = [r for r in results if r]
    assert len(winners) == 1
    assert winners[0] == ["attempt-contested"]

    total_claimed = sum(len(r) for r in results)
    assert total_claimed == 1


@pytest.mark.asyncio
async def test_ack_removes_from_inflight_and_owner(queue):
    await queue.enqueue("attempt-2")
    await queue.claim("worker-a", lease_seconds=10)
    assert await queue.owner_of("attempt-2") == "worker-a"
    await queue.ack("attempt-2")
    assert await queue.owner_of("attempt-2") is None
    assert await queue.is_queued("attempt-2") is False


@pytest.mark.asyncio
async def test_fail_and_reschedule_moves_to_a_new_id_in_ready(queue):
    await queue.enqueue("attempt-3")
    await queue.claim("worker-a", lease_seconds=10)
    await queue.fail_and_reschedule("attempt-3", "attempt-3-retry", ready_at=time.time())

    assert await queue.is_queued("attempt-3") is False
    claimed = await queue.claim("worker-b", lease_seconds=10)
    assert claimed == ["attempt-3-retry"]


@pytest.mark.asyncio
async def test_sweep_expired_leases_returns_and_clears_only_expired(queue):
    await queue.enqueue("attempt-expiring")
    await queue.enqueue("attempt-fresh")
    await queue.claim("worker-a", lease_seconds=0.01)  # claims attempt-expiring (FIFO by ready_at)
    await asyncio.sleep(0.05)
    await queue.claim("worker-b", lease_seconds=100)  # claims attempt-fresh, long lease

    expired = await queue.sweep_expired_leases()
    assert expired == ["attempt-expiring"]
    assert await queue.owner_of("attempt-expiring") is None
    # the fresh one must be untouched
    assert await queue.owner_of("attempt-fresh") == "worker-b"


@pytest.mark.asyncio
async def test_batch_claim_respects_batch_size(queue):
    for i in range(5):
        await queue.enqueue(f"attempt-batch-{i}")
    claimed = await queue.claim("worker-a", lease_seconds=10, batch_size=3)
    assert len(claimed) == 3
    remaining = await queue.claim("worker-b", lease_seconds=10, batch_size=10)
    assert len(remaining) == 2
