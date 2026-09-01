from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Loaded at MODULE IMPORT time, not inside a fixture — pytest imports
# conftest.py before it imports any test module, but fixtures only run
# after collection. Contract tests import `app.main`, which calls
# get_settings() at import time (deliberately, for fail-closed startup) —
# so the env must already be populated before that import happens, which
# means this cannot be fixture-based.
os.environ.setdefault("ENVIRONMENT", "test")
from dotenv import dotenv_values  # noqa: E402

_test_env_values = dotenv_values(REPO_ROOT / ".env.test")
for _key, _val in _test_env_values.items():
    if _val is not None:
        os.environ[_key] = _val


@pytest.fixture(scope="session", autouse=True)
def _load_test_env() -> None:
    # Env is already loaded at module scope above; this fixture just clears
    # the lru_cache in case an earlier test/process cached settings first.
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(scope="session")
def app_settings():
    from app.config import get_settings

    return get_settings()


@pytest.fixture(scope="function")
def clean_db(app_settings):
    """Truncates every tenant table AND flushes Redis before each test that
    requests this fixture, so tests don't leak state into each other —
    this matters concretely for the rate limiter, which is Redis-backed and
    would otherwise carry over request counts between test runs within the
    same minute bucket (a real bug I hit and fixed while building this)."""
    import psycopg
    import redis

    dsn = app_settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE organizations, calls, call_transcript_turns, call_attempts, "
            "agent_configs, knowledge_chunks, usage_records, audit_log CASCADE"
        )

    r = redis.Redis.from_url(app_settings.redis_url.get_secret_value())
    r.flushdb()
    yield


@pytest.fixture(scope="function")
def app_role_dsn(app_settings) -> str:
    """DSN for the RLS-bound, non-owner application role — the role that
    actually matters for proving RLS works, since the table owner bypasses
    RLS by default."""
    return "postgresql://calling_agent_app:app_role_test_pw@localhost:5432/calling_agent_test"
