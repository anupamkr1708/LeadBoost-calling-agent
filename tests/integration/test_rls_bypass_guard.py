"""Regression test for Phase 0 audit Finding 1 (docs/PHASE0_AUDIT.md): the
app must refuse to serve traffic in staging/production if its own DB
connection turns out to be a role RLS cannot restrict (superuser or
BYPASSRLS), because FORCE ROW LEVEL SECURITY is a no-op for such roles.

Uses a REAL Postgres connection (the same `calling_agent` superuser role CI
already stands up for migrations) — not a mock of the check — because the
thing being proven is that `pg_roles.rolsuper` is read from the actual
connected role, not from configuration the app could get out of sync with
reality.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from app.config import Settings


@pytest.fixture()
def _superuser_database_url(app_settings: Settings) -> str:
    migration_url = app_settings.database_migration_url
    assert migration_url is not None, "test .env.test must set DATABASE_MIGRATION_URL"
    value: str = migration_url.get_secret_value()
    return value


def test_refuses_to_serve_in_production_when_connected_as_superuser(monkeypatch, _superuser_database_url):
    from sqlalchemy import text

    import storage.db as db
    from app.config import ConfigError, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DATABASE_URL", _superuser_database_url)
    get_settings.cache_clear()
    db.reset_engine_for_tests()
    try:
        with pytest.raises(ConfigError, match="bypasses Row-Level Security"), db.get_session() as session:
            session.execute(text("SELECT 1"))
    finally:
        db.reset_engine_for_tests()
        get_settings.cache_clear()


def test_allows_serving_when_connected_as_the_restricted_app_role(app_settings):
    """Sanity check the other direction: the real app-role DSN (what
    DATABASE_URL is actually set to for every other test in this suite)
    must NOT trip the guard, or every other test would already be failing."""
    from sqlalchemy import text

    import storage.db as db

    db.reset_engine_for_tests()
    try:
        with db.get_session() as session:
            row = session.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            is_super, bypasses_rls = row.fetchone()
            assert not is_super
            assert not bypasses_rls
    finally:
        db.reset_engine_for_tests()
