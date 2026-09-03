"""Shared helper for Phase 1 integration tests: builds a real
`orchestrator.queue.Queue` (real Redis) and `orchestrator.worker_runtime.WorkerRuntime`
(real Postgres via storage.db, which reads DATABASE_URL/DATABASE_WORKER_URL
from the already-loaded test env) with an injectable
`telephony.fake.FakeTelephonyProvider` scenario source — this is what lets
integration tests exercise the REAL runtime/queue/state-machine code with
only the external I/O boundary faked, per docs/PHASE1_DESIGN.md "Very
important testing principle": don't fake the component under test.
"""
from __future__ import annotations

import uuid

from redis.asyncio import Redis

from orchestrator.failures import RetryPolicy
from orchestrator.queue import Queue
from orchestrator.worker_runtime import WorkerRuntime
from telephony.fake import FakeTelephonyProvider, ScenarioSource


def build_test_runtime(
    *,
    redis_url: str,
    scenario_source: ScenarioSource | None = None,
    max_concurrent_calls: int = 3,
    queue_lease_seconds: float = 2.0,
    queue_poll_interval_seconds: float = 0.05,
    provider_operation_timeout_seconds: float = 2.0,
    retry_max_attempts: int = 3,
    retry_initial_delay_seconds: float = 0.1,
    queue_claim_batch_size: int = 1,
) -> tuple[WorkerRuntime, Queue, Redis]:
    """Fast, test-tuned timings (sub-second polling/leases) so integration
    tests don't need to sleep for the production defaults (0.5s poll,
    45s lease) — the mechanism under test is identical, only the clock is
    compressed."""
    redis_client: Redis = Redis.from_url(redis_url)
    queue = Queue(redis_client)
    provider = FakeTelephonyProvider(scenario_source=scenario_source) if scenario_source else FakeTelephonyProvider()
    retry_policy = RetryPolicy(
        max_attempts=retry_max_attempts,
        initial_delay_seconds=retry_initial_delay_seconds,
        backoff_multiplier=2.0,
        max_delay_seconds=5.0,
        jitter_fraction=0.0,
    )
    runtime = WorkerRuntime(
        instance_id=uuid.uuid4().hex[:8],
        queue=queue,
        provider=provider,
        retry_policy=retry_policy,
        max_concurrent_calls=max_concurrent_calls,
        queue_lease_seconds=queue_lease_seconds,
        queue_poll_interval_seconds=queue_poll_interval_seconds,
        provider_operation_timeout_seconds=provider_operation_timeout_seconds,
        queue_claim_batch_size=queue_claim_batch_size,
    )
    return runtime, queue, redis_client
