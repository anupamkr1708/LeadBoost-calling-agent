from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from storage.db import get_session

router = APIRouter(tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: process is up and can respond. No dependency checks —
    a broken DB should not make Kubernetes/whatever kill and restart a
    perfectly healthy process."""
    return {"status": "alive"}


@router.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: actually checks the dependencies this service needs to
    serve traffic. Real round-trips, not a hardcoded 200."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
        healthy = False

    try:
        import redis

        from app.config import get_settings

        r = redis.Redis.from_url(get_settings().redis_url.get_secret_value())
        r.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if healthy else "not_ready", "checks": checks}


@router.get("/health")
async def health() -> dict[str, str]:
    """Human-facing summary endpoint, distinct from the k8s-style /live and
    /ready probes."""
    from app.config import get_settings

    settings = get_settings()
    return {"service": settings.service_name, "environment": settings.environment}
