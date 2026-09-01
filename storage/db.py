"""Database engine + session factory.

Per app/layers.py rule 3: this is the ONLY module that constructs a
SQLAlchemy engine or raw Session. Everything else calls `get_session` (plain,
for org-agnostic/system operations) or `org_scoped_session` (for anything
touching tenant data — sets `app.current_org_id` via `SET LOCAL` so RLS
policies apply, TRD Part 4.4).
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url.get_secret_value()
        connect_args: dict[str, str] = {}
        if settings.require_tls_db and settings.environment != "test":
            # sslmode is a libpq/psycopg connect arg, not part of the SQLAlchemy URL
            # by default here — enforced explicitly so it can never be silently
            # dropped (TRD Part 5.2).
            connect_args["sslmode"] = "require"
        _engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    return _engine


def _get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """A plain session with no org context set. Only for system-level
    operations (migrations, admin tooling, cross-tenant jobs like the
    session reaper) that have a legitimate reason to see all rows — RLS is
    enabled with FORCE on every tenant table, so even this session sees
    nothing tenant-scoped unless it uses the bypass role (never the app role)."""
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


def reset_engine_for_tests() -> None:
    """Test-only: forces re-creation of the engine/sessionmaker, needed
    because get_settings() is lru_cached and tests swap DATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
