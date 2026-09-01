# System Map — Phase 0

Legend (matches the reference repo's convention, per the master prompt):
✅ wired-and-invoked · 🔵 fake-verified · 🟡 code-ready-untested · 🔴 not built

An item is only ✅ if (a) it's imported/called by the composition root or a
real API handler or a real scheduled job, (b) an integration test exercises
it through the real entrypoint, and (c) for infrastructure, a test proves it
actually changes system behavior under the condition it's meant to handle —
per the master prompt's Integration rule. Nothing below is marked ✅ that I
did not personally watch pass in this sandbox.

| Component | Status | Evidence |
|---|---|---|
| Fail-closed config loader (`app/config.py`) | ✅ | `tests/unit/test_config.py` (10 tests): boots with valid config, refuses to boot on missing secret, refuses on 5 placeholder patterns, refuses on fake vendor key in production, refuses on TLS-disabled in production, rejects unknown environment value. All passing. |
| Layer graph + AST import-boundary checker (`app/layers.py`, `tests/layering/`) | ✅ | 3 tests passing, confirmed to actually scan >10 first-party modules (not a silently-empty scan). |
| SQLAlchemy models (`storage/models.py`) | ✅ | Loaded successfully by Alembic; migration built from them ran clean against real Postgres. |
| Baseline Alembic migration (8 tables, partitions, RLS, grants) | ✅ | `alembic upgrade head` ran successfully against real local Postgres 16; verified via direct `psql` inspection: partitions exist (`calls_2026_08`, `calls_2026_09`, etc.), `relrowsecurity`/`relforcerowsecurity` both `true` on every tenant table, HNSW index present on `knowledge_chunks.embedding`, `calling_agent_app` role confirmed to have no UPDATE/DELETE grant on `audit_log`. |
| DB session factory + RLS-scoped session (`storage/db.py`) | ✅ | Exercised end-to-end by every contract test that hits `POST /v1/calls` — real `SET LOCAL app.current_org_id`, real commit/rollback. |
| Multi-tenant RLS isolation (TRD Part 6.4) | ✅ | `tests/multitenant/test_rls_isolation.py`, 5/5 passing, connecting as the non-owner `calling_agent_app` role: bare unfiltered `SELECT` only returns the caller's org; missing org context returns **zero** rows (fail-closed, not fail-open); cross-tenant `INSERT` is rejected by `WITH CHECK`; `audit_log` UPDATE is rejected at the grant level. |
| FastAPI composition root (`app/main.py`) | ✅ | Constructed and exercised by every contract test via `TestClient`; fails closed at import time if config is bad (calls `get_settings()` before building the app). |
| `/live`, `/ready`, `/health` endpoints | ✅ | `tests/contract/test_health_endpoints.py`, 4/4 passing; `/ready` does real round-trips to Postgres and Redis, not a hardcoded 200. |
| `POST /v1/calls` (stub — writes a queued row, does NOT dispatch a call) | ✅ for what it claims to be | `tests/contract/test_calls_endpoint.py`, 6/6 passing: rejects missing/garbage auth, writes a real row scoped by RLS to the caller's org, returns the standard error envelope on validation failure. Explicitly NOT ✅ as "a working call-placement endpoint" — it isn't one yet, by design (Phase 0 = "no calls yet"). |
| JWT bearer auth (`api/auth.py`) | ✅ | Exercised by every contract test on the calls endpoint; verified RS256 signature checking, expiry, and required-claims enforcement, all via real HTTP requests through the real app, not by calling the function directly. |
| Redis-backed rate limiter (`api/rate_limit.py`) | ✅ | `test_rate_limit_actually_rejects_over_limit_requests` drives 65 real HTTP requests through the real endpoint and confirms a real 429 with the standard error envelope — proven through the live trigger, not a unit test of the limiter function alone. |
| Standard error envelope (`api/errors.py`) | ✅ | Confirmed present on 401 (auth), 422 (validation), and 429 (rate limit) responses via contract tests. |
| Ruff lint | ✅ | Clean (`ruff check .`), confirmed after fixing all findings (no suppressions except two explicitly-justified `# noqa` on false positives: FastAPI's `Depends()`-as-default idiom, and a deliberately-separate `elif`/`if` in the AST walker). |
| `mypy --strict` (app/, api/, storage/) | ✅ | Clean, confirmed after fixing 11 real mechanical findings (missing return types, missing generic type args). |
| `mypy` (whole repo incl. tests) | ✅ | Clean, 39 source files. |
| `detect-secrets` scan + reviewed baseline | ✅ | 12 findings, each manually inspected and confirmed false-positive (template placeholders, a documented local-test-only password, deliberately-fake values used to test the placeholder-rejection logic itself) and recorded in `.secrets.baseline`; re-running against the baseline shows zero new findings. |
| `pip-audit` dependency scan | ✅ | Clean — no known vulnerabilities in the pinned dependency set. |
| GitHub Actions CI workflow (`.github/workflows/ci.yml`) | 🟡 | Written, implementing all 9 required pipeline steps, and every step's underlying command was run and confirmed to pass **locally** in this sandbox (except the final Docker build — see below). Not yet 🟢/✅ because I have no push access to a real GitHub remote to watch the actual Actions run go green; that's a genuine, stated gap, not a skipped requirement. |
| Docker build | 🟡 | `Dockerfile` + `.dockerignore` written; `docker build` was actually invoked in this sandbox (Docker installs and the daemon runs here) and got as far as `Step 1/13 : FROM python:3.12-slim`, then failed because this sandbox's network allowlist blocks `registry-1.docker.io`. Not ✅: I did not watch a complete build succeed. Would very likely succeed unmodified on a real GitHub Actions runner (unrestricted internet) — but "very likely" is not "verified," so this stays 🟡. |
| LeadBoost-side additive migration | 🟡 | Written as a standalone, ready-to-apply migration file (`docs/leadboost_additive_migration/`) — not applied to the real `LeadBoost-saas` repo (no push access), and therefore never run against that repo's actual schema. Correctly 🟡, not ✅. |
| Orchestrator, conversation engine, telephony adapters, retrieval, guardrails | 🔴 | Not built. Phase 0 scope is explicitly "schema + contracts, no calls yet" (roadmap Part H). Empty packages with `__init__.py` exist as placeholders for the layering graph to reference; there is no logic inside them. |
| Monthly partition rollover job | 🔴 | Not built. Two partitions (current month + next month) were created by the baseline migration so the service can take writes immediately, but nothing rolls a third partition forward when month three arrives. Flagged here deliberately, per the Integration rule ("don't build a subsystem without its trigger, or don't build it yet") — I chose "don't build it yet" rather than half-building a scheduler with no real cron trigger behind it. |
| Eval harness (`eval/call_scenarios/`) | 🔴 | Not built — nothing to evaluate yet (no LLM-driven layer exists). |

## Honest gap statement (Phase 0)

What's real: a fail-closed config loader that I personally watched refuse
to boot on 5 different placeholder patterns and on missing secrets; a
full 8-table schema with correct Postgres-partitioning-aware composite
keys, running against a real local Postgres 16 instance; Row-Level
Security that I proved holds even for the table-owning role (FORCE RLS)
and fails closed (missing org context → zero rows, not all rows) rather
than failing open; and a thin but genuinely end-to-end API surface (auth →
rate limit → RLS-scoped write → standard error envelope) exercised by 29
passing tests across four categories, rerun twice to confirm determinism.

What's explicitly not real yet, stated plainly rather than papered over:
there is no orchestrator, no telephony adapter, no LLM integration, and
`POST /v1/calls` writes a row and stops — it does not call anyone. CI is
written and its steps individually verified locally, but never watched
running green on actual GitHub infrastructure, because this build has no
push access to a real remote. The Docker image's base-layer pull could not
be verified in this specific sandbox due to a network policy blocking
Docker Hub, though the Dockerfile itself, the daemon, and the build
command all demonstrably work otherwise. The LeadBoost-side schema
additions exist only as a standalone patch file, not as a change actually
applied to that repository. And there is no monthly-partition-rollover
job — a known, deliberately deferred piece of operational automation
rather than a forgotten one.

Per the Phase Gate Protocol: none of the 🟡/🔴 items above are being
claimed as ✅. This phase is gated as **PASSED for its own stated scope**
(schema + contracts + fail-closed config + RLS + CI definition), with the
🟡/🔴 items above carried forward explicitly as Phase 1+ work, not hidden.
