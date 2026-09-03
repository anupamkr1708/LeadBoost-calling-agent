# Phase 0 Audit

Performed by re-reading every first-party file in the tarball and by actually
standing up Postgres 16 + pgvector + Redis 7 in the audit sandbox, running
the exact steps `.github/workflows/ci.yml` claims to run (role creation,
`alembic upgrade head`, all four test suites, ruff, `mypy --strict`), rather
than taking `docs/SYSTEM_MAP.md`'s prior claims on faith. Every finding below
is either a direct code reading or a reproduced command with its real output.

## Method note on the two real bugs found

Both bugs below survived the previous audit because every existing test
imports `app.main` (or connects directly with a role a test fixture
constructs), and the previous CI run was **never actually executed on GitHub
Actions** — `docs/SYSTEM_MAP.md` says so itself ("no push access... never
watched running green"). Locally, the previous author worked around the
missing `.env` by having one present in their shell, which papered over bug
#2, and no test ever asked "which role is `DATABASE_URL` for?", which is
exactly what let bug #1 through. Neither is a criticism of test *quantity* —
29/29 passed both then and now — it's that both bugs live in the gap between
"the tests as written pass" and "the thing they're implicitly assumed to
prove is true."

## Finding 1 (CRITICAL — fixed): the running application never actually gets RLS protection

**Claim under test:** `docs/SYSTEM_MAP.md` marks "Multi-tenant RLS isolation"
and "DB session factory + RLS-scoped session" both ✅, citing
`tests/multitenant/test_rls_isolation.py` (5/5 passing) as proof.

**What's actually true:** the RLS *policies* are correct — `ALTER TABLE ...
FORCE ROW LEVEL SECURITY` plus per-table `USING (organization_id =
current_setting('app.current_org_id', true)::int)` is exactly right, and I
reproduced all 5 tests passing. But every one of those tests connects
directly via the `app_role_dsn` fixture, i.e. as `calling_agent_app` — a
role the fixture constructs *for the test*, not the role the running FastAPI
service uses. `storage/db.py`, `api/endpoints/calls.py`, and
`alembic/env.py` all read one single setting, `Settings.database_url`, for
everything: migrations, the app's own connection pool, and (implicitly)
whatever role ends up owning the tables.

In `.github/workflows/ci.yml`, `DATABASE_URL` for the actual running app is
`postgresql+psycopg://calling_agent:ci_only_pw@...` — `calling_agent` is the
Postgres service container's `POSTGRES_USER`, which the official Postgres
Docker image (and `pgvector/pgvector`, which is built on it) always grants
`SUPERUSER`. I confirmed this both from Docker's own documentation
("POSTGRES_USER – Specifies a user with superuser privileges") and directly
against a locally-built equivalent role:

```
      rolname      | rolsuper | rolbypassrls
-------------------+----------+--------------
 calling_agent     | t        | f
 calling_agent_app | f        | f
```

**Why `FORCE ROW LEVEL SECURITY` doesn't save this:** FORCE makes RLS apply
to the table *owner* when the owner is an ordinary role. It does **not**
apply to superusers or `BYPASSRLS` roles — Postgres exempts them
unconditionally, by design, and this cannot be forced. So the one thing
Phase 0 built specifically to make tenant isolation hold "regardless of what
the application code does or forgets to do" (the `org_scoped_session`
docstring's own words) is connected to the database as a role for which RLS
is architecturally a no-op.

**Proof, not just reasoning** — reproduced against the real schema, using
the exact role and connection string the app is actually configured with,
with no `app.current_org_id` set at all (the literal "app forgot to scope
the query" failure mode `test_no_org_context_set_returns_zero_rows_not_all_rows`
exists to rule out for `calling_agent_app`):

```sql
-- connected as calling_agent (== DATABASE_URL's role, no SET at all)
SELECT organization_id, lead_id, status FROM calls WHERE organization_id IN (7001,7002);

 organization_id | lead_id | status
------------------+---------+--------
             7001 |       1 | queued
             7002 |       2 | queued
```

Both tenants' rows, unscoped, through the exact identity the live service
uses. Phase 0's only current endpoint (`POST /v1/calls`) happens not to
expose this today because it only ever does a single `INSERT` with an
explicit `organization_id` bound from the JWT — so there's no *live* leak in
Phase 0's actual surface area. But Phase 1 is exactly the part of the system
that starts doing cross-row reads (queue claims, worker dispatch, retries,
"is this call already running") — the class of code where "someone forgot a
WHERE clause" is a realistic future bug, which is precisely the failure mode
RLS-with-FORCE exists to catch. Shipping Phase 1 on top of this would mean
building the whole execution runtime with its safety net silently disabled.

Contributing factor: `tests/contract/test_calls_endpoint.py::test_create_call_rls_scopes_the_written_row_to_the_callers_org`
is misleadingly named. It asserts the inserted row's `organization_id`
column equals the value the app was given — a correctness check on the
INSERT statement, not an RLS check — and it queries using the same
non-restricted role the app itself uses, so it would pass identically with
RLS turned off entirely. It gave real, specific, false confidence.

**Classification:** RLS policies/schema = **A** (correct). Application
wiring to actually benefit from them = **C** (implemented incorrectly).
**Risk: HIGH — fixed in this pass** (see "Phase 0 fixes" below), because
Phase 1 is exactly the part of the system where this stops being latent.

## Finding 2 (fixed): CI's own migration step cannot run as written

`Settings` (`app/config.py`) loads from `env_file=".env"` — a fixed
filename, not `.env.test`. `tests/conftest.py` works around this by loading
`.env.test`'s values into `os.environ` directly at import time, *before*
pytest ever imports application code. But `.github/workflows/ci.yml`'s
"Run baseline Alembic migration against ephemeral Postgres" step runs
`alembic upgrade head` directly from the shell, after a step that writes
only `.env.test` — never `.env`, and never exports the values either.
`alembic/env.py` calls `get_settings()` at import time with no such
workaround. I reproduced this exactly:

```
$ rm -f .env && alembic upgrade head
FATAL: configuration failed validation, refusing to start: 5 validation errors for Settings
database_url
  Field required [type=missing, input_value={}, input_type=dict]
...
app.config.ConfigError: 5 validation errors for Settings
```

This is consistent with `docs/SYSTEM_MAP.md`'s own admission that CI was
"written... every step's command run and confirmed to pass **locally**"
but never actually watched green on GitHub Actions — it wasn't caught
because whoever ran these steps locally always had a real `.env` sitting in
the repo root already. It's a real, mechanical CI bug, not a design flaw.

**Classification: B (implemented but incomplete) — fixed in this pass.**

## Verified functionality (reproduced, not assumed)

I ran every command `docs/SYSTEM_MAP.md` claims was run, against a real
local Postgres 16 (+ pgvector 0.6.0) and Redis 7, using the exact CI role
setup:

| Check | Result |
|---|---|
| `alembic upgrade head` against real Postgres | Clean, one revision, no errors |
| `pytest tests/unit -v` | 11 passed (SYSTEM_MAP says 10 — harmless doc drift, not a defect) |
| `pytest tests/layering -v` | 3 passed |
| `pytest tests/contract -v` | 10 passed |
| `pytest tests/multitenant -v` | 5 passed |
| `ruff check .` | Clean |
| `mypy --strict app api storage` | Clean, 15 files |

29/29 tests genuinely pass, and the schema, migration, config fail-closed
behavior, JWT auth, rate limiter, and error envelope are all real and
correctly built — Findings 1 and 2 are specific, narrow defects, not a
reason to distrust the rest of Phase 0's claims. I did not have a real
GitHub remote either, so the CI *workflow file* itself (post-fix) is
reproduced-locally-verified the same way the previous pass was, with the
same honestly-stated caveat.

## Per-area classification

| Area | Class | Note |
|---|---|---|
| Multi-tenancy (schema/RLS policies) | A | Correct FORCE RLS, correct predicate, correct fail-closed-to-zero-rows behavior, proven against the restricted role |
| Multi-tenancy (app wiring to the restricted role) | C → fixed | Finding 1 |
| Authentication (JWT, RS256, required claims) | A | No fallback secret; verified end-to-end via contract tests |
| Authorization (org-scoped session, RLS as backstop) | C → fixed | Same as Finding 1 — the "backstop" wasn't actually behind the app |
| Rate limiting | A | Real Redis round-trip (`INCR`+`EXPIRE`), keyed per-org (not global), atomic under concurrent workers since `INCR` is a single Redis command |
| Database schema/constraints/indexes | A | Composite PKs for partitioning reasoned correctly; index choices match the stated query patterns (org+status, org+created_at) |
| Idempotency (`calls.idempotency_key`) | D (seam only, as designed) | Correctly *not* DB-unique on a partitioned table; correctly deferred to Phase 1's dispatch-time enforcement — this was never claimed done, so it's not a defect, it's Phase 1's actual job (see PHASE1_DESIGN.md) |
| Redis usage (rate limiter only) | A | Nothing beyond rate limiting exists yet, and nothing wrongly duplicates Postgres-owned truth |
| CI pipeline | B → fixed | Finding 2 |
| Composition root / layering / architecture tests | A | AST-based, genuinely scans >10 modules (guarded against a vacuous scan), all 3 tests reproduced passing |
| `orchestrator/`, `conversation/`, `telephony/*`, `retrieval/`, `guardrails/`, `eval/` | D (seam only, as designed) | Empty packages, correctly not claimed as more; this is Phase 1+'s job |
| Docker build | Not independently re-verified | Same sandbox network constraint as the original build (no Docker Hub egress here either); Dockerfile itself is fine on inspection |

## Phase 0 fixes made in this pass

Both fixes are scoped exactly to Findings 1 and 2 — nothing else in Phase 0
was touched, per the "don't rewrite for style" policy.

1. **`app/config.py`** — added an optional `database_migration_url`. When
   set, Alembic uses it for DDL and the app's own engine uses
   `database_url` for request traffic; when unset, both fall back to
   `database_url` (so a single-role local dev setup keeps working
   unchanged — this is additive, not a breaking change to local dev).
2. **`storage/db.py`** — the app's engine now fails closed at first
   connection in `staging`/`production`: it queries its own connected
   role's `rolsuper`/`rolbypassrls` and refuses to serve traffic if either
   is true. This turns "the app is accidentally running as a role RLS
   can't restrict" from a silent gap back into exactly the kind of
   boot-time refusal this codebase already uses for secrets — it can't
   recur unnoticed. `development`/`test` are exempt so the existing
   single-role CI/local setup keeps working (see `.env.example` for the
   documented real-deployment shape).
3. **`.github/workflows/ci.yml`** — `.env.test` is now also written to
   `.env` (Finding 2). The app-facing steps (contract, multitenant, and
   the new Phase 1 integration/concurrency/tenant-isolation suites) now
   run against `calling_agent_app`'s DSN, not the bootstrap superuser, so
   CI actually exercises the role the deployed service is meant to use;
   `MIGRATION_DATABASE_URL` (the superuser role) is used only for the
   `alembic upgrade head` step.
4. **`.env.example`** — documented both variables and stated explicitly
   that `DATABASE_URL` in staging/production must be the restricted
   `calling_agent_app` DSN, with `DATABASE_MIGRATION_URL` reserved for the
   role that runs Alembic.
5. **`tests/unit/test_config.py`** — added coverage for the new setting's
   fallback behavior (only-`database_url`-set case) and explicit-override
   case.

No other Phase 0 file was modified. Everything in "Per-area classification"
marked A was left exactly as it was.

## Deferred, unchanged from Phase 0's own stated gaps

Monthly partition rollover job, LeadBoost-side migration application,
eval harness, Docker Hub image-pull verification — all still correctly 🔴/🟡
per `docs/SYSTEM_MAP.md`'s original reasoning, none of it changed by this
pass, none of it blocks Phase 1.
