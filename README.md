# LeadBoost Calling Agent

Multi-tenant, LLM-driven outbound calling agent, built as a separate
service integrating with LeadBoost. See `docs/SYSTEM_MAP.md` for exactly
what is and isn't built, and `docs/ARCHITECTURE_DECISIONS.md` for why.

[![CI](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml)

> The badge above will render correctly once this repo is pushed to a real
> GitHub remote and the workflow has run at least once — it can't be
> verified from inside this build sandbox, which has no push access to
> GitHub. See `docs/SYSTEM_MAP.md`'s gap statement for the full list of
> what is/isn't independently verified.

**Phase: 0 of 8** (schema + contracts, no calls yet — see the roadmap
document's Part H for the full phase list).

## Local development setup

Requires Python 3.12, a local Postgres 16 with the `vector` and `pgcrypto`
extensions available, and Redis.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# fill in DATABASE_URL, REDIS_URL, and generate a JWT keypair:
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

Test categories, matching the Phase Gate Protocol's required breakdown:

- `tests/unit/` — pure logic, mocked externals (currently: the fail-closed
  config loader).
- `tests/layering/` — AST-based import-boundary enforcement.
- `tests/multitenant/` — Row-Level Security isolation, against real
  Postgres, connecting as the actual non-owner application role.
- `tests/contract/` — full HTTP request→response cycles through the real
  FastAPI app: auth, rate limiting, the standard error envelope.
- `tests/integration/` — currently empty; nothing exists yet that needs
  one beyond what the categories above already cover with real Postgres/
  Redis. Will gain real content starting Phase 1.

## What this is not, yet

No calls are placed. `POST /v1/calls` validates, authenticates, rate-limits,
and writes a `queued` row — nothing dispatches it anywhere. See
`docs/SYSTEM_MAP.md` for the full, honestly-labelled breakdown of what's
✅ wired-and-invoked vs. 🟡 built-but-unverified vs. 🔴 not built at all.
