# LeadBoost Calling Agent

Multi-tenant, LLM-driven outbound calling agent, built as a separate
service integrating with LeadBoost. See `docs/SYSTEM_MAP.md` for the
Phase 0 baseline, `docs/PHASE0_AUDIT.md` for the audit that preceded Phase
1 (two real bugs found and fixed, both now regression-tested),
`docs/PHASE1_DESIGN.md` for the execution runtime's architecture, and
`docs/PHASE1_IMPLEMENTATION_REPORT.md` for what actually got built,
including real (not estimated) test counts and benchmark numbers.

[![CI](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml)

> The badge above will render correctly once this repo is pushed to a real
> GitHub remote and the workflow has run at least once — it can't be
> verified from inside this build sandbox, which has no push access to
> GitHub. The CI workflow itself has been reproduced locally step-by-step
> (real Postgres, real Redis, the exact role/grant/migration sequence) —
> see `docs/PHASE0_AUDIT.md`'s "Verified functionality" section — but a
> green badge on a real Actions run is still unverified.

**Phase: 1 of 8** — idempotent admission, a Redis-backed queue, a
bounded-concurrency worker runtime, and a fake telephony boundary. Calls
are genuinely queued, claimed, executed (against the fake provider), and
completed end-to-end — see `docs/PHASE1_DESIGN.md` for the architecture
and `docs/PHASE1_IMPLEMENTATION_REPORT.md` for what's proven vs. deferred.
No real telephony and no semantic conversation intelligence yet (Phase 2+).

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

## Running the test suite

```bash
cp .env.example .env.test   # then fill in test-specific values
.venv/bin/pytest tests/ -v
```

Test categories, matching the Phase Gate Protocol's required breakdown
(109 tests as of Phase 1, up from Phase 0's 33):

- `tests/unit/` — pure logic, no DB/Redis: the fail-closed config loader,
  state machine transition validators, `RetryPolicy`'s backoff/retryability
  truth table, and `conversation.runtime` driven against the fake provider.
- `tests/layering/` — AST-based import-boundary enforcement, including
  Phase 1's dependency-direction rules (orchestrator → conversation →
  telephony, never the reverse; orchestrator stays framework-agnostic).
- `tests/multitenant/` — Row-Level Security isolation, against real
  Postgres, connecting as the actual non-owner application role.
- `tests/contract/` — full HTTP request→response cycles through the real
  FastAPI app (with its lifespan actually running, so the worker runtime
  is live): auth, rate limiting, the standard error envelope, idempotent
  replay, and a call actually reaching `completed` through the real app.
- `tests/integration/` — real Postgres + real Redis + only the telephony
  provider faked: queue primitives (including a 20-way concurrent claim
  exclusivity test), idempotent admission under real concurrent
  transactions, the full admission→completion path with explicit failure
  variants (duplicate request, provider failure→retry→success, worker
  crash recovery, capacity-unavailable), a live-sampled concurrency bound
  test, and tenant isolation through the runtime.

## What this is not, yet

No real telephony (`telephony/fake.py` is the only provider implemented)
and no semantic conversation intelligence — `conversation/runtime.py`
drives a deterministic script, never inspects transcript content, and
contains no keyword/regex/sentiment logic anywhere. See
`docs/PHASE1_IMPLEMENTATION_REPORT.md` §16–17 for the full, honestly-labelled
list of what's built vs. deferred vs. genuinely limited (including a
diagnosed-then-tested-and-revised performance finding — see §14).
