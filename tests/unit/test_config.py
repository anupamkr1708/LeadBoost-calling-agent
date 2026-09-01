from __future__ import annotations

import os

import pytest


def _clear_settings_cache() -> None:
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test in this file controls its own env from scratch."""
    _clear_settings_cache()  # BEFORE, not just after — a prior test file
    # (e.g. contract tests) may have already populated the lru_cache with
    # environment=test settings; without clearing first, this test would
    # silently read that stale cached value instead of what it just set.
    for key in list(os.environ):
        if key in {
            "ENVIRONMENT", "DATABASE_URL", "REDIS_URL", "JWT_PRIVATE_KEY",
            "JWT_PUBLIC_KEY", "WEBHOOK_HMAC_SECRET", "EXOTEL_API_KEY",
            "DEEPGRAM_API_KEY", "GROQ_API_KEY", "REQUIRE_TLS_DB", "REQUIRE_TLS_REDIS",
        }:
            monkeypatch.delenv(key, raising=False)
    yield
    _clear_settings_cache()


def _set_minimal_valid_env(monkeypatch, **overrides):
    values = {
        "ENVIRONMENT": "development",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nMIIfakebutlonger\n-----END PRIVATE KEY-----",
        "JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nMIIfakebutlonger\n-----END PUBLIC KEY-----",
        "WEBHOOK_HMAC_SECRET": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    }
    values.update(overrides)
    for k, v in values.items():
        monkeypatch.setenv(k, v)


def test_boots_with_valid_config(monkeypatch, tmp_path):
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # no .env file here, pure env vars
    from app.config import get_settings

    settings = get_settings()
    assert settings.environment == "development"
    assert settings.masked_summary()["database_url_set"] is True


def test_refuses_to_boot_with_missing_secret(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from app.config import ConfigError, get_settings

    with pytest.raises(ConfigError):
        get_settings()


@pytest.mark.parametrize(
    "placeholder",
    [
        "your-super-secret-key-change-in-production",
        "changeme",
        "CHANGEME123",
        "placeholder-secret",
        "insecure-default",
    ],
)
def test_refuses_to_boot_with_placeholder_secret(monkeypatch, tmp_path, placeholder):
    _set_minimal_valid_env(monkeypatch, WEBHOOK_HMAC_SECRET=placeholder)
    monkeypatch.chdir(tmp_path)
    from app.config import ConfigError, get_settings

    with pytest.raises(ConfigError):
        get_settings()


def test_refuses_to_boot_in_production_with_fake_vendor_key(monkeypatch, tmp_path):
    _set_minimal_valid_env(monkeypatch, ENVIRONMENT="production")
    monkeypatch.chdir(tmp_path)
    from app.config import ConfigError, get_settings

    with pytest.raises(ConfigError):
        get_settings()  # exotel/deepgram/groq keys default to the dev fake value


def test_boots_in_production_with_real_looking_vendor_keys(monkeypatch, tmp_path):
    _set_minimal_valid_env(
        monkeypatch,
        ENVIRONMENT="production",
        EXOTEL_API_KEY="sk_live_realkey123456",
        DEEPGRAM_API_KEY="dg_live_realkey123456",
        GROQ_API_KEY="gsk_live_realkey123456",
        REQUIRE_TLS_DB="true",
        REQUIRE_TLS_REDIS="true",
    )
    monkeypatch.chdir(tmp_path)
    from app.config import get_settings

    settings = get_settings()
    assert settings.environment == "production"


def test_refuses_to_boot_in_production_with_tls_disabled(monkeypatch, tmp_path):
    _set_minimal_valid_env(
        monkeypatch,
        ENVIRONMENT="production",
        EXOTEL_API_KEY="sk_live_realkey123456",
        DEEPGRAM_API_KEY="dg_live_realkey123456",
        GROQ_API_KEY="gsk_live_realkey123456",
        REQUIRE_TLS_DB="false",
    )
    monkeypatch.chdir(tmp_path)
    from app.config import ConfigError, get_settings

    with pytest.raises(ConfigError):
        get_settings()


def test_rejects_unknown_environment_value(monkeypatch, tmp_path):
    _set_minimal_valid_env(monkeypatch, ENVIRONMENT="production-ish")
    monkeypatch.chdir(tmp_path)
    from pydantic import ValidationError

    from app.config import ConfigError, get_settings

    with pytest.raises((ConfigError, ValidationError)):
        get_settings()
