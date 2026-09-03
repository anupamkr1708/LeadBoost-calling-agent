"""Regression test for Phase 1 hardening item I: the composition root
(app/main.py) must refuse to start in `production` because Phase 1 has no
real telephony provider — starting anyway would silently simulate every
call. See app/main.py's module-level guard for the full reasoning.

This test reloads `app.main` with production-shaped settings to exercise
the REAL module-level code path (not a re-implementation of the check),
then restores the module to the test-environment state other test files
in this session depend on — `app.main` is a singleton module object
shared across the whole pytest process, so leaving it reloaded with
production settings would break every other test that imports it after
this one runs.
"""
from __future__ import annotations

import importlib
import os

import pytest


def _clear_settings_cache() -> None:
    from app.config import get_settings

    get_settings.cache_clear()


def test_refuses_to_start_in_production_with_only_the_fake_provider_available(monkeypatch, app_settings):
    # Import FIRST, before mutating the environment: this guarantees
    # app.main is loaded successfully at least once under the normal test
    # environment, whether this is the first time it's ever been imported
    # in this process (test file run in isolation) or it's already sitting
    # in sys.modules from an earlier test file (full suite run) — either
    # way, from this point on `importlib.reload` is what re-executes its
    # module body, which is where the production-only guard actually needs
    # to be exercised, not on a bare `import` statement racing against
    # whatever env happened to be set at that exact moment.
    import app.main as app_main_module

    assert app_main_module.app is not None  # sanity: it imported cleanly

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", app_settings.database_url.get_secret_value())
    monkeypatch.setenv("REDIS_URL", app_settings.redis_url.get_secret_value())
    monkeypatch.setenv("JWT_PRIVATE_KEY", app_settings.jwt_private_key.get_secret_value())
    monkeypatch.setenv("JWT_PUBLIC_KEY", app_settings.jwt_public_key.get_secret_value())
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", app_settings.webhook_hmac_secret.get_secret_value())
    # Valid enough to pass every OTHER production-mode check in
    # app/config.py (non-placeholder vendor keys, TLS required) so this
    # test is isolated to specifically the new provider guard, not
    # incidentally blocked by an unrelated, already-tested production
    # requirement.
    monkeypatch.setenv("EXOTEL_API_KEY", "xk_live_9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_live_3c2b1a0f9e8d7c6b5a4938271605f4e3")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_live_7e6d5c4b3a29180f7e6d5c4b3a29180f")
    monkeypatch.setenv("REQUIRE_TLS_DB", "true")
    monkeypatch.setenv("REQUIRE_TLS_REDIS", "true")
    _clear_settings_cache()

    try:
        with pytest.raises(Exception) as exc_info:  # noqa: PT011 - ConfigError specifically, see assertion below
            importlib.reload(app_main_module)
        assert "no real telephony provider" in str(exc_info.value)
        assert "production" in str(exc_info.value).lower()
    finally:
        # The reload above raised partway through executing the module
        # body, leaving app.main in a broken state in sys.modules —
        # restore it explicitly (env-var/cache resets alone don't
        # retroactively fix an already-broken module object), since every
        # OTHER test file in this session imports the real app.main and
        # needs it usable. In a `finally` so this cleanup runs even if one
        # of the assertions above fails.
        os.environ["ENVIRONMENT"] = "test"
        _clear_settings_cache()
        importlib.reload(app_main_module)
        assert app_main_module.app is not None
