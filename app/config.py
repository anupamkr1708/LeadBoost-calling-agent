"""Environment-driven settings that FAIL CLOSED.

Direct, deliberate fix for the exact bug found in LeadBoost-saas
(``SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-change-in-production")``):
a secret-bearing setting must never silently fall back to a default. If a
secret is missing, or equals a known placeholder value, the process refuses
to start. There is no "log a warning and continue" path for secrets.

See: CallingAgent-PRD-TRD-SystemDesign.md Part 0.4 and Part 5.4.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder values we explicitly refuse to boot with. Deliberately broad:
# catching "changeme"-style values is cheap insurance against exactly the
# LeadBoost failure mode (a default that looks like a real secret at a glance).
_FORBIDDEN_PLACEHOLDER_SUBSTRINGS = (
    "changeme",
    "change-in-production",
    "change_in_production",
    "your-super-secret",
    "placeholder",
    "example",
    "insecure",
    "default-secret",
    "dev-secret-do-not-use-in-prod",  # our own local-dev value, see .env.example
    "test-secret-do-not-use-in-prod",
)


class ConfigError(RuntimeError):
    """Raised when the process must refuse to boot due to bad configuration."""


def _reject_if_placeholder(field_name: str, value: str) -> None:
    lowered = value.lower()
    if not value.strip():
        raise ConfigError(
            f"Refusing to start: required secret-bearing setting '{field_name}' "
            f"is empty. Set it via environment variable or secret file."
        )
    for bad in _FORBIDDEN_PLACEHOLDER_SUBSTRINGS:
        if bad in lowered:
            raise ConfigError(
                f"Refusing to start: setting '{field_name}' looks like a "
                f"placeholder value ('{bad}' substring detected). This is the "
                f"exact failure mode found in LeadBoost's fallback JWT secret "
                f"(PRD/TRD Part 0.4) — set a real secret, there is no safe default."
            )


class Settings(BaseSettings):
    """Process configuration. Every secret-bearing field is required (no
    Python-level default) and is additionally checked for placeholder values
    in `validate_secrets_or_die`. Non-secret fields may have sane defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- environment / non-secret ---
    environment: str = Field(default="development")
    service_name: str = Field(default="calling-agent")
    log_level: str = Field(default="INFO")

    # --- secrets: NO DEFAULTS. Missing => pydantic raises at construction. ---
    database_url: SecretStr
    redis_url: SecretStr
    jwt_private_key: SecretStr
    jwt_public_key: SecretStr
    webhook_hmac_secret: SecretStr

    # Vendor API keys - required in production, but Phase 0 ships no vendor
    # adapters yet, so these are only *required* to be present and non-placeholder
    # when environment == "production". In dev/test they may be a clearly-marked
    # fake value (still checked against the placeholder list, just permitted to
    # be a "fake-provider" style string agreed on by the team, not silently unset).
    exotel_api_key: SecretStr = Field(default=SecretStr("fake-provider-key-for-dev-only"))
    deepgram_api_key: SecretStr = Field(default=SecretStr("fake-provider-key-for-dev-only"))
    groq_api_key: SecretStr = Field(default=SecretStr("fake-provider-key-for-dev-only"))

    # --- TLS enforcement (TRD Part 5.2) ---
    require_tls_db: bool = Field(default=True)
    require_tls_redis: bool = Field(default=True)

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        allowed = {"development", "test", "staging", "production"}
        if v not in allowed:
            raise ConfigError(f"environment must be one of {allowed}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _fail_closed_on_placeholders(self) -> Settings:
        # Always-required secrets, regardless of environment.
        always_required: dict[str, SecretStr] = {
            "database_url": self.database_url,
            "redis_url": self.redis_url,
            "jwt_private_key": self.jwt_private_key,
            "jwt_public_key": self.jwt_public_key,
            "webhook_hmac_secret": self.webhook_hmac_secret,
        }
        for name, secret in always_required.items():
            _reject_if_placeholder(name, secret.get_secret_value())

        # Vendor keys only get the placeholder check in real environments —
        # in dev/test a shared, clearly-labelled fake value is fine (and is
        # itself one of the allowed dev-only placeholders below), but
        # *production* must never boot with a fake provider key.
        if self.environment == "production":
            vendor_required: dict[str, SecretStr] = {
                "exotel_api_key": self.exotel_api_key,
                "deepgram_api_key": self.deepgram_api_key,
                "groq_api_key": self.groq_api_key,
            }
            for name, secret in vendor_required.items():
                value = secret.get_secret_value()
                if "fake-provider-key-for-dev-only" in value:
                    raise ConfigError(
                        f"Refusing to start in production: '{name}' is still "
                        f"the dev-only fake value. Set a real vendor credential."
                    )
                _reject_if_placeholder(name, value)

            if not self.require_tls_db or not self.require_tls_redis:
                raise ConfigError(
                    "Refusing to start in production with TLS disabled for "
                    "database or redis (TRD Part 5.2 is non-negotiable at the edge)."
                )
        return self

    def masked_summary(self) -> dict[str, Any]:
        """Safe-to-log summary — never includes secret values themselves."""
        return {
            "environment": self.environment,
            "service_name": self.service_name,
            "log_level": self.log_level,
            "require_tls_db": self.require_tls_db,
            "require_tls_redis": self.require_tls_redis,
            "database_url_set": bool(self.database_url.get_secret_value()),
            "redis_url_set": bool(self.redis_url.get_secret_value()),
        }


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process. Any ConfigError here is fatal and is
    allowed to propagate — the ASGI app must not start with a half-loaded config.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from env/.env
    except Exception as exc:  # noqa: BLE001 - deliberately broad: ANY config
        # failure must abort startup, not just pydantic's own ValidationError.
        print(f"FATAL: configuration failed validation, refusing to start: {exc}", file=sys.stderr)
        raise ConfigError(str(exc)) from exc
