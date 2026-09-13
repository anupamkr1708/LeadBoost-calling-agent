# LeadBoost Calling Agent

Multi-tenant, LLM-driven outbound calling agent, built as a separate
service integrating with LeadBoost. See `docs/SYSTEM_MAP.md` for the
Phase 0 baseline, `docs/PHASE0_AUDIT.md` for the audit that preceded Phase
1, `docs/PHASE1_DESIGN.md`/`docs/PHASE1_IMPLEMENTATION_REPORT.md` for the
execution runtime, `docs/PHASE1_AUDIT_ADDENDUM.md` for the Phase 1
production-readiness hardening pass, and `docs/PHASE2_DESIGN.md` /
`docs/PHASE2_AUDIT.md` for semantic conversation intelligence — read the
Phase 2 audit for current status.

[![CI](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/REPLACE_WITH_ACTUAL_ORG/calling-agent/actions/workflows/ci.yml)

> The badge above will render correctly once this repo is pushed to a real
> GitHub remote and the workflow has run at least once — it can't be
> verified from inside this build sandbox, which has no push access to
> GitHub. The CI workflow itself has been reproduced locally step-by-step
> (real Postgres, real Redis, the exact role/grant/migration sequence) —
> see `docs/PHASE0_AUDIT.md`'s "Verified functionality" section — but a
> green badge on a real Actions run is still unverified.

**Phase: 2 of 8.** Genuine semantic conversation intelligence —
interpretation, state reconciliation with provenance, context-driven
planning (no static funnel), deterministic guardrails, and grounded
response generation — built entirely inside the unchanged, hardened
Phase 1 execution runtime. Proven end-to-end through a real
`WorkerRuntime`, real Postgres, and real Redis, with only the two genuine
external boundaries (telephony, the LLM) faked. `guardrails/policy.py`
contains zero model calls; `intelligence/fake_llm.py` is mechanically
proven free of keyword/heuristic logic, not just by convention. See
`docs/PHASE2_AUDIT.md` for the full report, what's verified, and what's
explicitly deferred (real speech is Phase 3 — see that report's final
section for the exact next step). No real telephony yet.

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
# GROQ_API_KEY is only needed for conversation/llm_client.py's real
# provider and eval/live_smoke.py — everything else uses
# intelligence/fake_llm.py and runs without it.

.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Note: setting `ENVIRONMENT=production` still refuses to boot
unconditionally (Phase 1 hardening's fail-closed guard) — no real
telephony provider exists yet, so production would otherwise silently
simulate every call. See `app/main.py`'s module-level guard.

## Running the test suite

```bash
cp .env.example .env.test   # then fill in test-specific values
.venv/bin/pytest tests/ -v
```

Test categories (181 tests as of Phase 2, up from Phase 1's 121):

- `tests/unit/` — pure logic, no DB/Redis: config, state machines,
  `RetryPolicy`, `conversation.runtime` against the fake telephony
  provider, plus Phase 2's semantic contracts/reconciler, guardrails,
  planner, responder, fake-LLM self-check, full pipeline via
  `FakeLLMProvider`, replay, and the always-on evaluation suite.
- `tests/layering/` — AST-based import-boundary enforcement: Phase 1's
  rules plus Phase 2's `intelligence`/`guardrails` bans on importing
  `storage`, `orchestrator`, `telephony`, or `fastapi`.
- `tests/multitenant/` — Row-Level Security isolation against real
  Postgres.
- `tests/contract/` — full HTTP request→response cycles through the real
  running app.
- `tests/integration/` — real Postgres + real Redis: Phase 1's full
  hardened suite (queue atomicity, idempotency, multi-process safety,
  shutdown/crash recovery, tenant isolation) plus Phase 2's
  `conversation_turns` persistence/RLS tests and a full end-to-end
  semantic conversation through a real `WorkerRuntime`.

`eval/live_smoke.py` (3 tests against the real Groq API) is intentionally
excluded from `pytest tests/` — run explicitly with a real `GROQ_API_KEY`
set: `GROQ_API_KEY=... pytest eval/live_smoke.py -v`.

## What this is not, yet

No real telephony, no STT/TTS — `telephony/fake.py` is still the only
provider, and a fail-closed guard prevents it from silently running in
production. No tool execution (`guardrails/policy.py`'s tool checks are
real and tested, but nothing calls a real tool yet). Phase 2's semantic
engine is not wired into the default production composition root — see
`docs/PHASE2_AUDIT.md` section O for why that's deliberate, not an
oversight, and section P for the exact Phase 3 plan.
