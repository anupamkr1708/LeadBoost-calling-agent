"""Database engine + session factory.

Per app/layers.py rule 3: this is the ONLY module that constructs a
SQLAlchemy engine or raw Session. Everything else calls `get_session`
(plain, for org-agnostic/system operations — see its docstring for what
that DOES and DOES NOT include), `org_scoped_session` (for anything
touching tenant data — sets `app.current_org_id` via `SET LOCAL` so RLS
policies apply, TRD Part 4.4), or `system_session` (for cross-organization
system reads the worker runtime needs — see its docstring).
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import ConfigError, Settings, get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_worker_engine: Engine | None = None
_WorkerSessionLocal: sessionmaker[Session] | None = None

# environments where the app is genuinely expected to share a single
# superuser role with migrations (local dev, CI's ephemeral Postgres
# container) — see docs/PHASE0_AUDIT.md Finding 1. staging/production are
# the environments this guard exists to protect.
_RLS_BYPASS_CHECK_EXEMPT_ENVIRONMENTS = {"development", "test"}


def _refuse_if_connection_bypasses_rls(dbapi_connection: object, _connection_record: object) -> None:
    """Fail closed the moment a connection turns out to be a role RLS
    cannot restrict (superuser or BYPASSRLS) outside dev/test. Applied to
    BOTH the app engine and the worker engine below — the worker role is
    supposed to be narrowly SELECT-only, not superuser/BYPASSRLS, so this
    check is real defense-in-depth for it too, not just for `database_url`.

    This exists because FORCE ROW LEVEL SECURITY, which every tenant table
    in the baseline migration sets, is a genuine no-op for superusers and
    BYPASSRLS roles — Postgres exempts them unconditionally, and this
    cannot be overridden by FORCE. That's exactly the gap Phase 0 audit
    Finding 1 found: the app was configured to connect as such a role,
    silently disabling the tenant-isolation backstop the schema was built
    for. Checking at connect time, rather than trusting deployment config,
    means this specific class of bug turns into an immediate boot-time
    refusal instead of a silent gap — the same fail-closed posture
    app/config.py already applies to secrets.
    """
    settings = get_settings()
    if settings.environment in _RLS_BYPASS_CHECK_EXEMPT_ENVIRONMENTS:
        return
    with dbapi_connection.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
    is_super, bypasses_rls = bool(row[0]), bool(row[1])
    if is_super or bypasses_rls:
        raise ConfigError(
            "Refusing to serve traffic: this database connection is a "
            "role that bypasses Row-Level Security "
            f"(rolsuper={is_super}, rolbypassrls={bypasses_rls}) in "
            f"environment={settings.environment!r}. Neither DATABASE_URL "
            "nor DATABASE_WORKER_URL may be a superuser/BYPASSRLS role in "
            "staging/production — see docs/PHASE0_AUDIT.md Finding 1. Use "
            "DATABASE_MIGRATION_URL for the one role that's allowed to be "
            "elevated (Alembic only)."
        )


def _build_engine(url: str, settings: Settings) -> Engine:
    connect_args: dict[str, str] = {}
    if settings.require_tls_db and settings.environment != "test":
        # sslmode is a libpq/psycopg connect arg, not part of the SQLAlchemy URL
        # by default here — enforced explicitly so it can never be silently
        # dropped (TRD Part 5.2).
        connect_args["sslmode"] = "require"
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    event.listen(engine, "connect", _refuse_if_connection_bypasses_rls)
    return engine


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = _build_engine(settings.database_url.get_secret_value(), settings)
    return _engine


def _get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _SessionLocal


def _get_worker_engine() -> Engine:
    global _worker_engine
    if _worker_engine is None:
        settings = get_settings()
        url = (settings.database_worker_url or settings.database_url).get_secret_value()
        _worker_engine = _build_engine(url, settings)
    return _worker_engine


def _get_worker_sessionmaker() -> sessionmaker[Session]:
    global _WorkerSessionLocal
    if _WorkerSessionLocal is None:
        _WorkerSessionLocal = sessionmaker(bind=_get_worker_engine(), expire_on_commit=False)
    return _WorkerSessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """A plain session, on the SAME role/engine as `org_scoped_session`,
    with no org context set. RLS is enabled with FORCE on every tenant
    table, so this session sees NOTHING from a tenant table in
    staging/production — it exists for genuinely org-agnostic operations
    (health checks, non-tenant-table reads) only. For cross-organization
    reads of tenant tables (the worker runtime's reaper/reconciliation
    sweeps), use `system_session` instead — see its docstring for why
    those are a different, narrower role rather than this one."""
    session = _get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def org_scoped_session(organization_id: int) -> Generator[Session, None, None]:
    """A session with `app.current_org_id` set via SET LOCAL for the
    duration of this transaction, so RLS policies enforce tenant isolation
    at the database level regardless of what the application code does or
    forgets to do (TRD Part 4.4). This is the session every request-handling
    code path must use.
    """
    if not isinstance(organization_id, int) or organization_id <= 0:
        raise ValueError(
            f"org_scoped_session requires a positive integer organization_id, "
            f"got {organization_id!r} — refusing to open an ambiguous session "
            f"rather than silently defaulting to 'no org filter'."
        )
    session = _get_sessionmaker()()
    try:
        # SET LOCAL does not support query parameters ($1) in Postgres — it's
        # parsed like a DDL-ish statement, not a normal parameterized query.
        # Safe to interpolate directly here because we've already validated
        # organization_id is a positive int (see the check above), never
        # raw user input.
        session.execute(text(f"SET LOCAL app.current_org_id = '{organization_id}'"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_session() -> Generator[Session, None, None]:
    """A session on the `database_worker_url` role (`calling_agent_worker`
    in the baseline+phase1 migrations) — narrowly SELECT-only, on exactly
    the tables that need cross-organization system visibility
    (`call_attempts`, `conversation_sessions`), granted via a dedicated
    `system_worker_cross_org_read` RLS policy rather than any RLS bypass
    (see docs/PHASE1_DESIGN.md "Concurrency / worker acquisition" and the
    Phase 1 migration).

    This exists because the worker runtime routinely needs to answer "what
    organization does this bare attempt_id (claimed off the GLOBAL Redis
    queue) belong to?" BEFORE it can open an `org_scoped_session` for
    that org — a genuine chicken-and-egg RLS cannot itself resolve. This
    session must NEVER be used for writes; the SELECT-only grant means an
    attempted write would fail loudly at the database level, not silently
    at the application level, if a future call site got this wrong.
    """
    session = _get_worker_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def close_engines() -> None:
    """Disposes both engines' connection pools. Called from
    `app/main.py`'s lifespan shutdown (docs/PHASE1_DESIGN.md "Shutdown") —
    an engine holding open Postgres connections past process exit is
    exactly the kind of untracked resource the master prompt's "RESOURCE
    LEAKS" review calls out; this is that review's fix, not just its
    finding."""
    global _engine, _SessionLocal, _worker_engine, _WorkerSessionLocal
    if _engine is not None:
        _engine.dispose()
    if _worker_engine is not None:
        _worker_engine.dispose()
    _engine = None
    _SessionLocal = None
    _worker_engine = None
    _WorkerSessionLocal = None


def reset_engine_for_tests() -> None:
    """Test-only: forces re-creation of the engine/sessionmaker, needed
    because get_settings() is lru_cached and tests swap DATABASE_URL. Same
    underlying operation as `close_engines()` — separate name because the
    call sites mean different things (test isolation vs. real shutdown),
    even though the code is identical."""
    close_engines()
