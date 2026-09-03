"""Integration tests for orchestrator/call_service.py against REAL Postgres.
The concurrency test here is one of the master prompt's explicitly
required authoritative tests: 10 concurrent identical requests must
produce exactly one logical Call, correct under real transaction races,
not just in the common sequential case (docs/PHASE1_DESIGN.md
"Idempotency")."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from orchestrator.call_service import CallService

ORG_ID = 6001


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    seed_org(ORG_ID, plan_max_concurrent_calls=5)


def test_create_call_without_idempotency_key_always_creates_new():
    service = CallService()
    a = service.create_call(
        organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=None
    )
    b = service.create_call(
        organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=None
    )
    assert a.call_id != b.call_id
    assert a.is_new and b.is_new


def test_create_call_with_same_idempotency_key_sequentially_returns_same_call():
    service = CallService()
    key = str(uuid.uuid4())
    a = service.create_call(
        organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=key
    )
    b = service.create_call(
        organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=key
    )
    assert a.call_id == b.call_id
    assert a.is_new is True
    assert b.is_new is False
    assert b.first_attempt_id is None  # replay must not signal "enqueue a new attempt"


def test_different_idempotency_keys_create_different_calls():
    service = CallService()
    a = service.create_call(
        organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=str(uuid.uuid4())
    )
    b = service.create_call(
        organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=str(uuid.uuid4())
    )
    assert a.call_id != b.call_id


@pytest.mark.asyncio
async def test_retry_with_same_idempotency_key_after_call_already_completed_returns_completed_call(app_settings):
    """Phase 1 hardening item F's explicit second half: idempotency isn't
    just about the admission race — a caller retrying (e.g. after a
    network timeout on their end, even though the original request
    actually succeeded) with the same key AFTER the call has already run
    to completion must get back the SAME, now-completed Call — not a new
    one, and not one that gets re-executed."""
    from tests.integration.runtime_test_helpers import build_test_runtime

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
        assert first.is_new
        await queue.enqueue(str(first.first_attempt_id))

        import psycopg

        from app.config import get_settings

        settings = get_settings()
        migration_url = settings.database_migration_url or settings.database_url
        dsn = migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")

        deadline = asyncio.get_event_loop().time() + 5.0
        status = None
        while asyncio.get_event_loop().time() < deadline:
            with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT status FROM calls WHERE id = %s", (first.call_id,))
                (status,) = cur.fetchone()
            if status == "completed":
                break
            await asyncio.sleep(0.02)
        assert status == "completed"

        # The "retry" — same org, same idempotency key, sent again well
        # after the original actually finished.
        retry = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_ID,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=key,
        )
        assert retry.call_id == first.call_id
        assert retry.is_new is False
        assert retry.status == "completed"
        assert retry.first_attempt_id is None  # must not signal "enqueue a new attempt"

        # And durably: still exactly one CallAttempt row — the retry must
        # not have caused a second execution.
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM call_attempts WHERE call_id = %s", (first.call_id,))
            (attempt_count,) = cur.fetchone()
            assert attempt_count == 1
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_ten_concurrent_identical_requests_create_exactly_one_call():
    """THE authoritative idempotency test: real concurrent Postgres
    transactions racing on the same idempotency key, not a sequential
    simulation of concurrency."""
    service = CallService()
    key = str(uuid.uuid4())

    def _create():
        return service.create_call(
            organization_id=ORG_ID, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=key
        )

    results = await asyncio.gather(*[asyncio.to_thread(_create) for _ in range(10)])

    call_ids = {r.call_id for r in results}
    assert len(call_ids) == 1, f"expected exactly 1 unique call_id, got {len(call_ids)}: {call_ids}"

    new_flags = [r.is_new for r in results]
    assert sum(new_flags) == 1, f"expected exactly 1 winner (is_new=True), got {sum(new_flags)}"

    # And durably: exactly one row in Postgres, not just one value observed
    # in-process — a bug that rolled back the loser's ledger insert but not
    # their orphaned `calls` row would pass the above and fail this.
    import psycopg

    from app.config import get_settings

    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    dsn = migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM calls WHERE organization_id = %s AND idempotency_key = %s", (ORG_ID, key)
        )
        (count,) = cur.fetchone()
        assert count == 1, f"expected exactly 1 durable Call row, found {count}"

        cur.execute(
            "SELECT count(*) FROM call_attempts WHERE organization_id = %s AND lead_id = 1 AND call_id = %s",
            (ORG_ID, list(call_ids)[0]),
        )
        (attempt_count,) = cur.fetchone()
        assert attempt_count == 1, (
            f"expected exactly 1 CallAttempt for the winning Call, found {attempt_count} "
            "— a loser that wasn't cleaned up correctly could create an orphaned attempt too"
        )
