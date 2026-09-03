"""The Redis-backed queue coordination layer.

Two sorted sets + one hash under `calling:queue:*` (see
docs/PHASE1_DESIGN.md "Queue (Redis)" for the full design rationale):

- `ready`    (ZSET attempt_id -> ready_at epoch)     work eligible now/later
- `inflight` (ZSET attempt_id -> lease_expiry epoch)  claimed, executing
- `owner`    (HASH attempt_id -> worker_id)           observability

An attempt's "queue state" is which of these structures it's currently a
member of — there is deliberately no separate Python QueueState enum
duplicating that fact (see orchestrator/states.py's module docstring).

Redis holds none of the Call/CallAttempt business data itself, only bare
attempt ids — every consumer looks the attempt up in Postgres through an
org_scoped_session, which is what keeps this layer tenant-isolation-safe
without the queue needing to know anything about organizations at all.

No busy loop: `queue_poll_interval_seconds` (app/config.py) governs how
often idle callers re-check `ready` and how often the reaper re-checks
`inflight` — Redis's sorted sets have no blocking "wait for score <= now"
primitive, so a bounded, configurable poll is the honest minimum here,
not a magic `sleep(1)`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

_CLAIM_SCRIPT_SOURCE = """
local ready_key = KEYS[1]
local inflight_key = KEYS[2]
local owner_key = KEYS[3]
local now = tonumber(ARGV[1])
local lease_seconds = tonumber(ARGV[2])
local batch_size = tonumber(ARGV[3])
local worker_id = ARGV[4]

local candidates = redis.call('ZRANGEBYSCORE', ready_key, '-inf', now, 'LIMIT', 0, batch_size)
local claimed = {}
local lease_expiry = now + lease_seconds
for _, member in ipairs(candidates) do
    -- ZREM on a specific member is atomic and single-threaded server-side
    -- (this whole script runs atomically), so exactly one caller across
    -- any number of concurrent claim() calls ever succeeds in removing a
    -- given member — that IS the atomic claim, not a separate lock.
    local removed = redis.call('ZREM', ready_key, member)
    if removed == 1 then
        redis.call('ZADD', inflight_key, lease_expiry, member)
        redis.call('HSET', owner_key, member, worker_id)
        table.insert(claimed, member)
    end
end
return claimed
"""

# Mirrors _CLAIM_SCRIPT_SOURCE's exact pattern for the same reason: reading
# expired members with ZRANGEBYSCORE and THEN removing them in a separate
# round trip (the original implementation) is a check-then-act race — two
# concurrent reapers (two processes, per docs/PHASE1_DESIGN.md's
# multi-process readiness) can both read the same expired member before
# either has removed it, and both then proceed to "recover" it
# independently. Postgres's ux_attempts_one_running_per_call and the
# WHERE-status guards in orchestrator/worker_runtime.py's recovery path
# make the WORST case of that race non-catastrophic (no duplicate
# concurrent execution), but it can still produce duplicate CallAttempt
# rows and wasted work — a real queue-semantics defect, not just a
# theoretical one. Folding the read and the removal into one atomic script
# (same technique as claim, above) closes it at the source: exactly one
# caller, across any number of concurrent reapers, ever gets a given
# expired member back from this script.
_SWEEP_EXPIRED_SCRIPT_SOURCE = """
local inflight_key = KEYS[1]
local owner_key = KEYS[2]
local cutoff = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

local candidates = redis.call('ZRANGEBYSCORE', inflight_key, '-inf', cutoff, 'LIMIT', 0, limit)
local reclaimed = {}
for _, member in ipairs(candidates) do
    local removed = redis.call('ZREM', inflight_key, member)
    if removed == 1 then
        redis.call('HDEL', owner_key, member)
        table.insert(reclaimed, member)
    end
end
return reclaimed
"""


@dataclass(frozen=True)
class QueueKeys:
    ready: str = "calling:queue:ready"
    inflight: str = "calling:queue:inflight"
    owner: str = "calling:queue:owner"


class Queue:
    def __init__(self, redis_client: Redis[bytes], keys: QueueKeys | None = None) -> None:
        self._redis = redis_client
        self._keys = keys or QueueKeys()
        # redis-py's Script wrapper handles EVALSHA with automatic
        # fallback to EVAL (and re-registering) if the script has been
        # flushed from Redis's script cache — no manual SHA bookkeeping.
        self._claim_script: AsyncScript = redis_client.register_script(_CLAIM_SCRIPT_SOURCE)
        self._sweep_script: AsyncScript = redis_client.register_script(_SWEEP_EXPIRED_SCRIPT_SOURCE)

    async def enqueue(self, attempt_id: str, ready_at: float | None = None) -> None:
        score = ready_at if ready_at is not None else time.time()
        await self._redis.zadd(self._keys.ready, {attempt_id: score})

    async def claim(self, worker_id: str, lease_seconds: float, batch_size: int = 1) -> list[str]:
        result = await self._claim_script(
            keys=[self._keys.ready, self._keys.inflight, self._keys.owner],
            args=[time.time(), lease_seconds, batch_size, worker_id],
        )
        return [member.decode() if isinstance(member, bytes) else member for member in result]

    async def ack(self, attempt_id: str) -> None:
        """Successful completion or terminal (non-retryable) failure: the
        attempt leaves the queue entirely."""
        await self._redis.zrem(self._keys.inflight, attempt_id)
        await self._redis.hdel(self._keys.owner, attempt_id)

    async def fail_and_reschedule(self, attempt_id: str, next_attempt_id: str, ready_at: float) -> None:
        """Retryable failure: the FAILED attempt leaves inflight, and a
        NEW attempt id (never the same id — retries are new CallAttempt
        rows, docs/PHASE1_DESIGN.md "Domain model") is scheduled into
        ready."""
        await self.ack(attempt_id)
        await self.enqueue(next_attempt_id, ready_at)

    async def sweep_expired_leases(self, now: float | None = None, limit: int = 1000) -> list[str]:
        """Called by the reaper. Atomically identifies AND removes expired
        inflight members in one Redis-side script (see
        `_SWEEP_EXPIRED_SCRIPT_SOURCE`'s comment for why this replaced an
        earlier read-then-remove version) — the caller decides what
        happened to each returned attempt (docs/PHASE1_DESIGN.md "Queue
        (Redis)" / "Worker-crash recovery") by checking Postgres, which is
        the durable truth this Redis bookkeeping is only ever a
        coordination layer over. Because the removal is part of the same
        atomic operation as the read, a given expired attempt_id is
        returned to AT MOST ONE caller, even under concurrent reapers
        (verified by tests/integration/test_reaper_race.py)."""
        cutoff = now if now is not None else time.time()
        result = await self._sweep_script(keys=[self._keys.inflight, self._keys.owner], args=[cutoff, limit])
        return [member.decode() if isinstance(member, bytes) else member for member in result]

    async def is_queued(self, attempt_id: str) -> bool:
        """True if the attempt is currently in `ready` or `inflight` —
        used by the reconciliation sweep to detect a Call whose enqueue
        step never happened (docs/PHASE1_DESIGN.md "Idempotency", the
        commit-then-enqueue failure window)."""
        in_ready = await self._redis.zscore(self._keys.ready, attempt_id)
        if in_ready is not None:
            return True
        in_inflight = await self._redis.zscore(self._keys.inflight, attempt_id)
        return in_inflight is not None

    async def owner_of(self, attempt_id: str) -> str | None:
        result = await self._redis.hget(self._keys.owner, attempt_id)
        if result is None:
            return None
        return result.decode() if isinstance(result, bytes) else result
