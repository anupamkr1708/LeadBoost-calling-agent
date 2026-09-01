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
    same minute bucket (a real bug I hit and fixed while building this).

    TRUNCATE is deliberately run via `database_migration_url` (falling back
    to `database_url` when unset, e.g. local single-role dev), never the
    app's own DATABASE_URL — this is test-administration, the same category
    of operation as running Alembic, not request-serving app traffic. The
    RLS-restricted `calling_agent_app` role (Phase 0 audit Finding 1's fix)
    is deliberately NOT granted TRUNCATE: that's a dangerous privilege with
    no legitimate use in request-serving code, and giving it out just to
    make this fixture convenient would reopen a smaller version of the same
    class of gap Finding 1 fixed."""
    import psycopg
    import redis

    migration_dsn = (app_settings.database_migration_url or app_settings.database_url).get_secret_value()
    dsn = migration_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE organizations, calls, call_transcript_turns, call_attempts, "
            "conversation_sessions, call_attempt_events, call_idempotency_keys, "
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


@pytest.fixture(scope="function")
def worker_role_dsn(app_settings) -> str:
    """DSN for the narrow, SELECT-only, cross-organization worker role
    (docs/PHASE1_DESIGN.md / Phase 1 audit) — used by tests that exercise
    the reaper/reconciliation sweeps' cross-org lookups directly."""
    return "postgresql://calling_agent_worker:worker_role_test_pw@localhost:5432/calling_agent_test"


@pytest.fixture(scope="function")
def seed_org():
    """Returns a callable that inserts (or updates) an organization row
    with a given plan_max_concurrent_calls, via the migration role (this is
    test setup, not app traffic) — several Phase 1 integration tests need
    an organization to exist before a Call can reference it."""
    import psycopg

    from app.config import get_settings

    def _seed(organization_id: int, plan_max_concurrent_calls: int = 5) -> None:
        settings = get_settings()
        migration_dsn = (settings.database_migration_url or settings.database_url).get_secret_value()
        dsn = migration_dsn.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (id, plan_max_concurrent_calls) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET plan_max_concurrent_calls = EXCLUDED.plan_max_concurrent_calls",
                (organization_id, plan_max_concurrent_calls),
            )

    return _seed
