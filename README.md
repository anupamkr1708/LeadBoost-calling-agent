# LeadBoost Calling Agent

Multi-tenant, LLM-driven outbound calling agent, built as a separate
service integrating with LeadBoost. See `docs/SYSTEM_MAP.md` for the
Phase 0 baseline, `docs/PHASE0_AUDIT.md` for the audit that preceded Phase
1 (two real bugs found and fixed, both now regression-tested),
`docs/PHASE1_DESIGN.md` for the execution runtime's architecture,
`docs/PHASE1_IMPLEMENTATION_REPORT.md` for the original Phase 1 build, and
`docs/PHASE1_AUDIT_ADDENDUM.md` for the production-readiness hardening
pass that followed it — read the addendum for current status.

[![CI](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml)

> The badge above will render correctly once this repo is pushed to a real
> GitHub remote and the workflow has run at least once — it can't be
> verified from inside this build sandbox, which has no push access to
> GitHub. The CI workflow itself has been reproduced locally step-by-step
> (real Postgres, real Redis, the exact role/grant/migration sequence) —
> see `docs/PHASE0_AUDIT.md`'s "Verified functionality" section — but a
> green badge on a real Actions run is still unverified.

**Phase: 1 of 8, hardened.** Idempotent admission, an atomically-coordinated
Redis queue (including atomic lease recovery), a bounded-concurrency
worker runtime proven correct across two independent processes sharing
the same infrastructure, and a fake telephony boundary — with a
fail-closed guard preventing that fake boundary from ever silently
standing in for a real one in production. See `docs/PHASE1_AUDIT_ADDENDUM.md`
for the hardening pass (items A–J: reaper atomicity, dead configuration,
reconciliation safety, multi-process safety, state machine audit,
idempotency, tenant isolation, shutdown/crash recovery, configuration
parity, observability) and its verified-guarantees gate. No real
telephony and no semantic conversation intelligence yet (Phase 2+) —
Phase 2 has deliberately not been started.

## Local development setup

Requires Python 3.12, a local Postgres 16 with the `vector` and `pgcrypto`
extensions available, and Redis.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# fill in DATABASE_URL (the RLS-restricted calling_agent_app role in
# staging/production — see .env.example's comments), REDIS_URL, and
# generate a JWT keypair:
#   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt_private.pem
#   openssl rsa -pubout -in jwt_private.pem -out jwt_public.pem

.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Note: as of the hardening pass, setting `ENVIRONMENT=production` will
refuse to boot unconditionally — Phase 1 has no real telephony provider,
so production would otherwise silently simulate every call. See
`app/main.py`'s module-level guard and `docs/PHASE1_AUDIT_ADDENDUM.md`
item I.

## Running the test suite

```bash
cp .env.example .env.test   # then fill in test-specific values
.venv/bin/pytest tests/ -v
```

Test categories, matching the Phase Gate Protocol's required breakdown
(121 tests as of the Phase 1 hardening pass, up from Phase 0's 33):

- `tests/unit/` — pure logic, no DB/Redis: the fail-closed config loader
  (including the production telephony-provider guard), state machine
  transition validators, `RetryPolicy`'s backoff/retryability truth
  table, and `conversation.runtime` driven against the fake provider.
- `tests/layering/` — AST-based import-boundary enforcement, including
  Phase 1's dependency-direction rules (orchestrator → conversation →
  telephony, never the reverse; orchestrator stays framework-agnostic)
  and the hardening pass's symbol-level rule confining
  `storage.db.system_session` to `orchestrator/**` only.
- `tests/multitenant/` — Row-Level Security isolation, against real
  Postgres, connecting as the actual non-owner application role.
- `tests/contract/` — full HTTP request→response cycles through the real
  FastAPI app (with its lifespan actually running, so the worker runtime
  is live): auth, rate limiting, the standard error envelope, idempotent
  replay, and a call actually reaching `completed` through the real app.
- `tests/integration/` — real Postgres + real Redis + only the telephony
  provider faked: queue primitives (including 20-way concurrent claim
  *and* concurrent reaper-sweep exclusivity tests), idempotent admission
  under real concurrent transactions (including retry-after-completion),
  the full admission→completion path with explicit failure variants
  (duplicate request, provider failure→retry→success, worker crash
  recovery, a lease expiring under a still-alive worker, shutdown
  cancellation, capacity-unavailable, nonexistent-organization failure),
  a live-sampled concurrency bound test, tenant isolation through the
  runtime, reconciliation-race safety, and — the one category no
  single-process test can cover — two independent `WorkerRuntime`
  instances proven safe against the same shared Postgres and Redis.

## What this is not, yet

No real telephony (`telephony/fake.py` is the only provider implemented,
and a fail-closed guard prevents it from silently running in production)
and no semantic conversation intelligence — `conversation/runtime.py`
drives a deterministic script, never inspects transcript content, and
contains no keyword/regex/sentiment logic anywhere. See
`docs/PHASE1_IMPLEMENTATION_REPORT.md` §16–17 for the original build's
honestly-labelled list of what's built vs. deferred, and
`docs/PHASE1_AUDIT_ADDENDUM.md`'s "NOT VERIFIED / DEFERRED" and "Remaining
limitations" sections for the current, post-hardening state.
