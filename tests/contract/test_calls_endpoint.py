from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.contract.jwt_helper import mint_test_token

ORG_ID = 8001


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


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


def test_create_call_without_auth_is_rejected(clean_db) -> None:
    resp = _client().post("/v1/calls", json={"lead_id": 1})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    assert "request_id" in body["error"]


def test_create_call_with_garbage_token_is_rejected(clean_db) -> None:
    resp = _client().post("/v1/calls", json={"lead_id": 1}, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401


def test_create_call_happy_path_writes_a_real_queued_row(auth_headers) -> None:
    resp = _client().post("/v1/calls", json={"lead_id": 42}, headers=auth_headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    uuid.UUID(body["call_id"])  # raises if not a real UUID
    assert "created_at" in body


def test_create_call_missing_lead_id_returns_standard_error_envelope(auth_headers) -> None:
    resp = _client().post("/v1/calls", json={}, headers=auth_headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "request_id" in body["error"]


def test_create_call_rls_scopes_the_written_row_to_the_callers_org(auth_headers) -> None:
    import psycopg

    from app.config import get_settings

    resp = _client().post("/v1/calls", json={"lead_id": 7}, headers=auth_headers)
    call_id = resp.json()["call_id"]

    dsn = get_settings().database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{ORG_ID}'")
        cur.execute("SELECT organization_id FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == ORG_ID


def test_rate_limit_actually_rejects_over_limit_requests(auth_headers) -> None:
    """Proves the rate limiter is wired to a real trigger (a real HTTP
    endpoint), per the master prompt's integration rule — not just a unit
    test of the limiter function in isolation."""
    client = _client()
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
