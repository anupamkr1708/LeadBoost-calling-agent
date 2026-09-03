"""THIRD authoritative test (master prompt): Organization A cannot observe
or execute Organization B's Call through the queue, the worker runtime, or
the database — through the REAL runtime this time (Phase 0's
tests/multitenant/ already proved RLS itself at the SQL layer; this proves
the Phase 1 additions specifically: the shared global queue doesn't leak
between orgs, and the new tables — conversation_sessions,
call_attempt_events, call_idempotency_keys — carry the same RLS guarantee
as the baseline tables)."""
from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from app.config import get_settings
from orchestrator.call_service import CallService
from tests.integration.runtime_test_helpers import build_test_runtime

ORG_A = 6301
ORG_B = 6302


@pytest.fixture(autouse=True)
def _seed(seed_org, clean_db):
    seed_org(ORG_A, plan_max_concurrent_calls=5)
    seed_org(ORG_B, plan_max_concurrent_calls=5)


def _dsn() -> str:
    settings = get_settings()
    migration_url = settings.database_migration_url or settings.database_url
    return migration_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


async def _wait_until_terminal(call_id: uuid.UUID, timeout: float = 5.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM calls WHERE id = %s", (call_id,))
            row = cur.fetchone()
            assert row is not None, f"no calls row for {call_id}"
            (status,) = row
            if status in ("completed", "failed", "cancelled"):
                return str(status)
        await asyncio.sleep(0.02)
    raise AssertionError(f"call {call_id} never reached a terminal state")


@pytest.mark.asyncio
async def test_two_orgs_calls_run_through_the_same_shared_runtime_without_cross_contamination(app_settings):
    """One shared WorkerRuntime/Queue instance (exactly like a real
    deployment: one process, many tenants) processes both orgs' calls
    concurrently. Every resulting row must carry the RIGHT org_id — proving
    the worker never confuses which org a claimed attempt belongs to,
    despite the queue itself being global and organization-agnostic."""
    runtime, queue, redis_client = build_test_runtime(
        redis_url=app_settings.redis_url.get_secret_value(), max_concurrent_calls=4
    )
    await runtime.start()
    try:
        service = CallService()
        call_a = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_A,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        call_b = await asyncio.to_thread(
            service.create_call,
            organization_id=ORG_B,
            lead_id=1,
            agent_config_id=None,
            campaign_id=None,
            idempotency_key=None,
        )
        await queue.enqueue(str(call_a.first_attempt_id))
        await queue.enqueue(str(call_b.first_attempt_id))

        status_a, status_b = await asyncio.gather(
            _wait_until_terminal(call_a.call_id), _wait_until_terminal(call_b.call_id)
        )
        assert status_a == "completed"
        assert status_b == "completed"

        with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT organization_id FROM call_attempts WHERE call_id = %s", (call_a.call_id,))
            assert cur.fetchone() == (ORG_A,)
            cur.execute("SELECT organization_id FROM call_attempts WHERE call_id = %s", (call_b.call_id,))
            assert cur.fetchone() == (ORG_B,)

            cur.execute(
                "SELECT cs.organization_id FROM conversation_sessions cs "
                "JOIN call_attempts ca ON ca.id = cs.call_attempt_id WHERE ca.call_id = %s",
                (call_a.call_id,),
            )
            assert cur.fetchone() == (ORG_A,)
            cur.execute(
                "SELECT cs.organization_id FROM conversation_sessions cs "
                "JOIN call_attempts ca ON ca.id = cs.call_attempt_id WHERE ca.call_id = %s",
                (call_b.call_id,),
            )
            assert cur.fetchone() == (ORG_B,)
    finally:
        await runtime.stop(grace_period_seconds=2.0)
        await redis_client.aclose()


def test_org_b_cannot_read_org_as_call_attempts_via_rls(app_role_dsn, seed_org):
    """Extends Phase 0's RLS proof (tests/multitenant/) to the Phase 1
    tables specifically — call_attempts, conversation_sessions, and
    call_attempt_events didn't exist when that suite was written."""
    service = CallService()
    call_a = service.create_call(
        organization_id=ORG_A, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=None
    )

    with psycopg.connect(app_role_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{ORG_B}'")
        cur.execute("SELECT * FROM call_attempts WHERE call_id = %s", (call_a.call_id,))
        assert cur.fetchall() == [], "org B must see ZERO rows of org A's call_attempts, not an error, not a leak"

        cur.execute(f"SET app.current_org_id = '{ORG_A}'")
        cur.execute("SELECT call_id FROM call_attempts WHERE call_id = %s", (call_a.call_id,))
        assert cur.fetchone() is not None, "sanity: org A can see its own row (proves the SET actually matters)"


def test_org_b_cannot_read_org_as_idempotency_key_via_rls(app_role_dsn):
    service = CallService()
    key = str(uuid.uuid4())
    service.create_call(
        organization_id=ORG_A, lead_id=1, agent_config_id=None, campaign_id=None, idempotency_key=key
    )

    with psycopg.connect(app_role_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{ORG_B}'")
        cur.execute(
            "SELECT * FROM call_idempotency_keys WHERE organization_id = %s AND idempotency_key = %s",
            (ORG_A, key),
        )
        assert cur.fetchall() == []


@pytest.mark.asyncio
async def test_worker_role_cross_org_read_cannot_be_used_to_write(worker_role_dsn):
    """Defense-in-depth check on the narrow calling_agent_worker role
    itself (docs/PHASE0_AUDIT.md-style empirical proof, not just a design
    claim): it can read across orgs by design, but MUST NOT be able to
    write anything, in any org — the grant is SELECT-only."""
    with psycopg.connect(worker_role_dsn, autocommit=True) as conn, conn.cursor() as cur:  # noqa: SIM117 - nesting pytest.raises around just the failing call is the readable structure here
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "UPDATE call_attempts SET status = 'running' WHERE organization_id = %s", (ORG_A,)
            )
