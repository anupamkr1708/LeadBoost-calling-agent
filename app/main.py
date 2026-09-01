"""Composition root. Per app/layers.py, nothing else may import this module
(no cycles back into the entrypoint) — everything else gets wired in HERE.

Startup behavior is deliberately fail-closed: `get_settings()` is called at
import time (via api.auth / storage.db importing app.config, and directly
below), so a bad config aborts the process before it ever binds a port,
per the master prompt's non-negotiable rule #4.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from api.endpoints import calls, health
from api.errors import register_exception_handlers
from app.config import get_settings

# Fail closed BEFORE constructing the FastAPI app at all.
_settings = get_settings()

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("service_starting", **_settings.masked_summary())
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="LeadBoost Calling Agent",
        version="0.1.0-phase0",
        description=(
            "Multi-tenant LLM-driven outbound calling agent. Phase 0: "
            "schema + contracts + fail-closed config + RLS. No calls are "
            "actually placed yet — see docs/SYSTEM_MAP.md."
        ),
        lifespan=_lifespan,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(calls.router)
    return app


app = create_app()
