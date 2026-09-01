# Architecture Decisions — Phase 0

This document records every deliberate deviation from the two source
documents' literal text, every judgment call made where they left something
open, and every explicit "not yet" from the anti-overengineering list (TRD
Part 8). Each entry states the decision and the one-sentence reasoning
required by the master prompt's ROLE section.

## Decisions made where the source documents left something open

**1. `organizations` is a thin local mirror, not a cross-service soft
reference everywhere.** The roadmap's Part E.2 says `Call.organization_id`
is "FK by reference, not FK constraint (cross-service)", but the TRD's Part
4.6 SQL literally writes `agent_configs.organization_id REFERENCES
organizations(id)`, which requires a local `organizations` table to exist.
Decision: maintain a small, read-mostly local mirror (`id`,
`plan_max_concurrent_calls`, `plan_max_call_minutes_per_month`,
`compliance_profile`, `synced_at`), synced from LeadBoost by the
integration layer (Phase 5, not built yet) — LeadBoost stays the source of
truth for org identity/billing, but `agent_configs`, `knowledge_chunks`,
and `usage_records` get a real DB-enforced FK to *this* table, since those
three genuinely need referential integrity against organization existence
in a way that `calls`/`call_attempts` (which reference `lead_id` too, a
LeadBoost concept with no local mirror) don't. `calls`/`call_attempts`
keep `organization_id` as an indexed, non-FK column, matching the
roadmap's explicit instruction for those specific tables.

**2. `calls` and `call_transcript_turns` use composite primary keys
`(id, created_at)` / `(id, occurred_at)`, not a bare `id` PK.** Postgres
requires a RANGE-partitioned table's PK/UNIQUE constraints to include the
partition key — this isn't a style choice, it's a hard Postgres
requirement neither source document spells out at the DDL level.
`call_transcript_turns` additionally carries a denormalized
`call_created_at` column so it can hold a real composite FK into `calls`
rather than a soft reference, because it's the single highest-volume,
tightest-coupled child table and integrity there is worth the extra
column.

**3. `calls.idempotency_key` is NOT a DB-level unique constraint.**
Postgres can't enforce global uniqueness on a partitioned table without
including the partition key, which wouldn't be a real uniqueness guarantee
anyway (two rows in different months could reuse a key). This isn't a gap:
TRD Part 3.5 already specifies idempotency is enforced via a Redis
SETNX-style check *before* insert. The column here is a plain indexed
lookup/audit field; real enforcement lands in Phase 1 when calls are
actually dispatched through Redis.

**4. `call_attempts.call_id` is a soft reference (no FK).** A scheduled
attempt can exist before the call it becomes has a row at all — a hard FK
doesn't fit that lifecycle, unlike `call_transcript_turns` which is always
written after its parent call already exists.

**5. Every tenant table gets its own `organization_id` column, even where
it could be inferred through a join** (e.g. `call_transcript_turns` could
theoretically infer org from `calls`). This directly implements the master
prompt's non-negotiable rule #2 and is what makes direct RLS policies
possible on every table without a join in the policy itself.

**6. The LeadBoost-side additive migration was NOT applied to the actual
`LeadBoost-saas` repo.** I have no push/PR access to someone else's
GitHub repository. Instead, Phase 0 produces the additive schema as a
standalone, ready-to-apply migration file under
`docs/leadboost_additive_migration/`, for whoever owns that repo to apply.
This is a scope decision stated up front, not a silently skipped
requirement.

**7. `mypy --strict` is enforced on `app/`, `api/`, `storage/` (the
packages with real logic so far); a looser `mypy` pass covers the whole
repo including tests.** Phase 0 has no `orchestrator/`, `conversation/`,
`retrieval/`, `guardrails/`, or `telephony/*` code yet beyond empty
packages — there's nothing there to hold to strict mode yet. As each
phase adds real code to those packages, they get added to the `--strict`
target list, not left permanently looser.

## Deliberate deviations from the reference repo (`Calling-Agent-`)

- **Not ported**: its keyword-matching conversation engine, its
  single-process/fixed-worker-count fleet model, and its habit of building
  a subsystem with no live trigger wired to it (Queue Runtime, Health
  Manager, Recovery Executor problem). Phase 0 has no orchestrator yet at
  all, deliberately — see the SYSTEM_MAP gap statement.
- **Adopted**: its AST-based layering/import-boundary technique
  (`app/layers.py` + `tests/layering/test_import_boundaries.py`), its
  evidence-labelled system map convention, and its discipline of pacing
  fake adapters realistically before trusting a benchmark number (not yet
  applicable — no adapters exist yet to benchmark).

## Explicit "not yet" list (TRD Part 8), reaffirmed for Phase 0

No Kubernetes, no Kafka/RabbitMQ, no knowledge graph, no multi-region, no
mTLS mesh, no microservice-per-concern split. Concretely in Phase 0: one
FastAPI process, one Postgres instance (partitioned tables, not sharded),
Redis used only for rate limiting so far (idempotency/session-state wiring
comes with the orchestrator in Phase 1), no message queue — the stub
`POST /v1/calls` writes directly to Postgres, it does not publish to
anything, because there's nothing downstream yet to consume it.

## Known trade-offs specific to the sandbox this was built in

- **No Docker Hub access.** This build sandbox's network allowlist does
  not include `registry-1.docker.io`, so `docker build` fails at the base
  image pull step (`python:3.12-slim`) even though Docker itself installs
  and runs here, and the Dockerfile itself was written and the daemon
  confirmed functional. The CI workflow (`.github/workflows/ci.yml`) would
  pull that same image successfully on a real GitHub Actions runner, which
  has unrestricted internet access — this is a constraint of where Phase 0
  was built, not of the Dockerfile or the CI definition.
- **"Ephemeral containers" for integration tests are real local Postgres
  16 + Redis instances installed via `apt`, not literal Docker containers**
  (same root cause as above — no registry access to pull `postgres:16` /
  `redis:7` images locally). The CI workflow uses real GitHub Actions
  service containers instead, which is the more correct version of this
  same discipline; what ran locally during this build is a reasonable
  local approximation, not a mock — every integration/RLS/contract test
  hit a real running Postgres and Redis process, with real SQL and real
  RLS policies enforced by the database engine itself.
