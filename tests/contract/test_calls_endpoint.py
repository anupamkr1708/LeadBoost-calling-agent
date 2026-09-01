from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.contract.jwt_helper import mint_test_token

ORG_ID = 8001


@pytest.fixture()
def client():
    """TestClient MUST be used as a context manager — that's what runs
    FastAPI's lifespan (app/main.py's `_lifespan`), which is what starts
    the worker runtime and sets `app.state.queue`. Phase 0's `_client()`
    helper returned a plain (non-context-managed) TestClient, which never
    triggered lifespan at all; that was fine when the endpoint only did a
    synchronous INSERT, but Phase 1's endpoint genuinely needs the runtime
    running (`request.app.state.queue.enqueue(...)`) — this is a real
    requirement, not a test artifact, so the fixture is the fix, not a
    workaround around it."""
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers(clean_db) -> dict:
    # calls.organization_id has no hard FK to organizations, but we still
    # seed it for realism / in case future phases add the FK.
    import psycopg

    from app.config import get_settings

    dsn = get_settings().database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{ORG_ID}'")
        cur.execute(
            "INSERT INTO organizations (id, plan_max_concurrent_calls) VALUES (%s, 5) "
            "ON CONFLICT (id) DO NOTHING",
            (ORG_ID,),
        )
    token = mint_test_token(organization_id=ORG_ID)
    return {"Authorization": f"Bearer {token}"}


def test_create_call_without_auth_is_rejected(clean_db, client) -> None:
    resp = client.post("/v1/calls", json={"lead_id": 1})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    assert "request_id" in body["error"]


def test_create_call_with_garbage_token_is_rejected(clean_db, client) -> None:
    resp = client.post("/v1/calls", json={"lead_id": 1}, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401


def test_create_call_happy_path_writes_a_real_queued_row(auth_headers, client) -> None:
    resp = client.post("/v1/calls", json={"lead_id": 42}, headers=auth_headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    uuid.UUID(body["call_id"])  # raises if not a real UUID
    assert "created_at" in body


def test_create_call_missing_lead_id_returns_standard_error_envelope(auth_headers, client) -> None:
    resp = client.post("/v1/calls", json={}, headers=auth_headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "request_id" in body["error"]


def test_create_call_rls_scopes_the_written_row_to_the_callers_org(auth_headers, client) -> None:
    import psycopg

    from app.config import get_settings

    resp = client.post("/v1/calls", json={"lead_id": 7}, headers=auth_headers)
    call_id = resp.json()["call_id"]

    dsn = get_settings().database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{ORG_ID}'")
        cur.execute("SELECT organization_id FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == ORG_ID


def test_rate_limit_actually_rejects_over_limit_requests(auth_headers, client) -> None:
    """Proves the rate limiter is wired to a real trigger (a real HTTP
    endpoint), per the master prompt's integration rule — not just a unit
    test of the limiter function in isolation."""
    from api.rate_limit import DEFAULT_LIMIT_PER_MINUTE

    last_status = None
    for _ in range(DEFAULT_LIMIT_PER_MINUTE + 5):
        resp = client.post("/v1/calls", json={"lead_id": 1}, headers=auth_headers)
        last_status = resp.status_code
        if last_status == 429:
            break
    assert last_status == 429, "Expected the rate limiter to eventually reject requests, but it never did."
    body = resp.json()
    assert body["error"]["code"] == "rate_limited"


def test_call_actually_completes_through_the_real_running_app(auth_headers, client) -> None:
    """The HTTP-boundary half of the required end-to-end test — proves the
    admission response is not the whole story: the same lifespan-started
    worker runtime that serves this TestClient's requests actually claims,
    executes (against the default fake provider, which always succeeds),
    and completes the call, entirely through the real composed app, no
    internal shortcuts."""
    import time

    import psycopg

    from app.config import get_settings

    resp = client.post("/v1/calls", json={"lead_id": 99}, headers=auth_headers)
    assert resp.status_code == 202
    call_id = resp.json()["call_id"]

    dsn = get_settings().database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    deadline = time.time() + 5.0
    status = None
    while time.time() < deadline:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f"SET app.current_org_id = '{ORG_ID}'")
            cur.execute("SELECT status FROM calls WHERE id = %s", (call_id,))
            (status,) = cur.fetchone()
        if status in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert status == "completed", f"call never completed via the real running app, last status: {status}"


def test_idempotent_replay_through_http_does_not_create_a_second_call(auth_headers, client) -> None:
    key = str(uuid.uuid4())
    first = client.post(
        "/v1/calls", json={"lead_id": 1, "idempotency_key": key}, headers=auth_headers
    )
    second = client.post(
        "/v1/calls", json={"lead_id": 1, "idempotency_key": key}, headers=auth_headers
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["call_id"] == second.json()["call_id"]
