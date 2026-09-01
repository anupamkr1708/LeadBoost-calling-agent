"""Redis-backed fixed-window rate limiter, per organization_id.

Deliberately simple (fixed-window, not sliding/token-bucket) for Phase 0 —
per the master prompt's anti-overengineering rule, a more sophisticated
limiter is a "not yet" until there's evidence fixed-window causes real
problems (bursty edge-of-window traffic). This one is at least REAL: it
round-trips to Redis and actually rejects over-limit requests, which is
what the Phase Gate Protocol's integration rule requires — no in-memory
stub pretending to be a rate limiter.
"""
from __future__ import annotations

import redis

from api.auth import AuthContext
from api.errors import RateLimitError
from app.config import get_settings

_redis_client: redis.Redis[str] | None = None

DEFAULT_LIMIT_PER_MINUTE = 60


def _get_redis() -> redis.Redis[str]:
    global _redis_client
    if _redis_client is None:
        url = get_settings().redis_url.get_secret_value()
        _redis_client = redis.Redis.from_url(url, decode_responses=True)
    return _redis_client


def reset_redis_client_for_tests() -> None:
    global _redis_client
    _redis_client = None


def enforce_rate_limit(auth: AuthContext, limit_per_minute: int = DEFAULT_LIMIT_PER_MINUTE) -> None:
    r = _get_redis()
    key = f"ratelimit:org:{auth.organization_id}:{_current_minute_bucket()}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, 65)  # a little over a minute, so a slow clock doesn't undercount
    if count > limit_per_minute:
        raise RateLimitError(f"Organization {auth.organization_id} exceeded {limit_per_minute} requests/minute.")


def _current_minute_bucket() -> int:
    import time

    return int(time.time() // 60)
