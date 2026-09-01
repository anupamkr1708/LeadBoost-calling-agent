from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_live_endpoint_returns_200() -> None:
    resp = _client().get("/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_ready_endpoint_checks_real_dependencies() -> None:
    resp = _client().get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


def test_health_endpoint_reports_environment() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["environment"] == "test"


def test_request_id_header_is_always_present() -> None:
    resp = _client().get("/live")
    assert "x-request-id" in resp.headers
