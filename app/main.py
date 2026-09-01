"""Composition root. Per app/layers.py, nothing else may import this module
(no cycles back into the entrypoint) — everything else gets wired in HERE.

Startup behavior is deliberately fail-closed: `get_settings()` is called at
import time (via api.auth / storage.db importing app.config, and directly
below), so a bad config aborts the process before it ever binds a port,
per the master prompt's non-negotiable rule #4.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis

from api.endpoints import calls, health
from api.errors import register_exception_handlers
from app.config import get_settings
from orchestrator.failures import RetryPolicy
from orchestrator.queue import Queue
from orchestrator.worker_runtime import WorkerRuntime
from storage.db import close_engines
from telephony.fake import FakeTelephonyProvider

# Fail closed BEFORE constructing the FastAPI app at all.
_settings = get_settings()

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("service_starting", **_settings.masked_summary())

    # Every Postgres call in this runtime is synchronous SQLAlchemy
    # (Phase 0's DB layer, deliberately not rewritten to async — see
    # docs/PHASE1_DESIGN.md "Composition root") offloaded via
    # asyncio.to_thread. Python's DEFAULT to_thread executor caps at
    # min(32, cpu_count+4) threads — on a small/single-core deployment
    # that can be well under max_concurrent_calls, silently turning the
    # thread pool itself into the concurrency bottleneck instead of
    # anything this runtime actually controls (measured and diagnosed in
    # docs/PHASE1_IMPLEMENTATION_REPORT.md §14). A dedicated executor sized
    # to the runtime's own configured capacity removes that hidden ceiling.
    thread_pool = ThreadPoolExecutor(
        max_workers=max(_settings.max_concurrent_calls + 2, 4),
        thread_name_prefix="calling-agent-db",
    )
    asyncio.get_running_loop().set_default_executor(thread_pool)

    # Everything below is constructed ONCE, here, and injected into
    # whatever needs it — nothing below this module constructs a Redis
    # client, an Engine, or a TelephonyProvider itself
    # (docs/PHASE1_DESIGN.md "Composition root"). Phase 1 ships with only
    # the fake telephony provider; a real adapter is a later phase's
    # one-line swap here, not a change to orchestrator/ or conversation/.
    redis_client: Redis[bytes] = Redis.from_url(_settings.redis_url.get_secret_value())
    queue = Queue(redis_client)
    provider = FakeTelephonyProvider()
    retry_policy = RetryPolicy(
        max_attempts=_settings.retry_max_attempts,
        initial_delay_seconds=_settings.retry_initial_delay_seconds,
        backoff_multiplier=_settings.retry_backoff_multiplier,
        max_delay_seconds=_settings.retry_max_delay_seconds,
        jitter_fraction=_settings.retry_jitter_fraction,
    )
    runtime = WorkerRuntime(
        instance_id=uuid.uuid4().hex[:8],
        queue=queue,
        provider=provider,
        retry_policy=retry_policy,
        max_concurrent_calls=_settings.max_concurrent_calls,
        queue_lease_seconds=_settings.queue_lease_seconds,
        queue_poll_interval_seconds=_settings.queue_poll_interval_seconds,
        provider_operation_timeout_seconds=_settings.provider_operation_timeout_seconds,
    )

    app.state.queue = queue
    app.state.worker_runtime = runtime

    await runtime.start()
    logger.info(
        "worker_runtime_started",
        max_concurrent_calls=_settings.max_concurrent_calls,
        db_thread_pool_size=thread_pool._max_workers,  # noqa: SLF001 - logging only, no behavior depends on this
    )
    try:
        yield
    finally:
        logger.info("service_stopping", grace_period_seconds=_settings.shutdown_grace_period_seconds)
        await runtime.stop(_settings.shutdown_grace_period_seconds)
        await redis_client.aclose()  # type: ignore[attr-defined]  # redis-py stubs lag runtime here; aclose() exists
        close_engines()
        thread_pool.shutdown(wait=True)


def create_app() -> FastAPI:
    app = FastAPI(
        title="LeadBoost Calling Agent",
        version="0.2.0-phase1",
        description=(
            "Multi-tenant outbound calling agent execution runtime. Phase "
            "1: idempotent admission, Redis-backed queue, bounded-"
            "concurrency worker runtime, fake telephony boundary. See "
            "docs/PHASE1_DESIGN.md. No real telephony or semantic "
            "conversation intelligence yet — those are later phases."
        ),
        lifespan=_lifespan,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(calls.router)
    return app


app = create_app()
