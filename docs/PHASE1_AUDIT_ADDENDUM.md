# Phase 1 Audit Addendum — Production-Readiness Hardening

A second, deeper audit of the already-shipped Phase 1 runtime
(`docs/PHASE1_IMPLEMENTATION_REPORT.md`), performed against the specific
gate items A–J below. Not a rewrite — no architecture changed, no new
Kafka/Celery/CQRS/microservices, exactly as scoped. Every item was
inspected against the real execution graph first; only real, reproduced
defects were fixed. 121 tests now pass (up from 109), and one of the new
tests caught a genuine bug this same pass introduced and then fixed (a
missing `call_attempts.session_id` write-back — see item A).

## A. Redis lease expiry / reaper — FIXED

**Real defect, reproduced:** `sweep_expired_leases` did `ZRANGEBYSCORE`
(read) then `ZREM`+`HDEL` (remove) as two separate round trips. Two
concurrent reapers (two processes, or a race within tight polling) could
both read the same expired member before either removed it, both proceed
to "recover" it independently. `ux_attempts_one_running_per_call` already
prevented the worst case (actual duplicate concurrent execution), but it
could still produce duplicate `CallAttempt` rows and wasted work — a real
queue-semantics defect, not just theoretical.

**Fix:** a second Lua script (`_SWEEP_EXPIRED_SCRIPT_SOURCE`,
`orchestrator/queue.py`), same technique as the existing claim script —
read and remove in one atomic server-side operation. Proven exclusive
under 20 concurrent `sweep_expired_leases()` calls racing one expired
lease (`tests/integration/test_queue.py::test_sweep_expired_leases_is_exclusive_across_concurrent_reapers`).

**A second, deeper bug this same investigation surfaced:** while adding a
DB-level guard (`WHERE status = 'running'`) to the reaper's recovery
UPDATE as defense-in-depth, a test asserting the recovered attempt's
*session* state (not just the attempt's) found that
`call_attempts.session_id` was **never being written** anywhere —
`_try_start_running` created the `ConversationSession` row but never
wrote its id back onto the attempt. This silently broke the reaper's
"abort this attempt's session too" branch since its `row.session_id` was
always `NULL`. Fixed (`_try_start_running` now writes it back in the same
transaction) and covered by
`tests/integration/test_worker_runtime_e2e.py::test_lease_expiring_mid_execution_does_not_corrupt_state_when_worker_finishes_late`,
which also proves the related, previously-unaddressed race: a lease can
legitimately expire while a worker is still alive and finishing (not
crashed) — the reaper wins, the original worker's late completion is
detected as superseded (`_FinalizeOutcome(action="superseded")`) and
does not overwrite the reaper's already-committed state. A DB-level
`WHERE status = 'running'` guard, added to both `_finalize_attempt` and
`_recover_expired_attempt`'s writes (and to the `calls` table updates in
`_decide_retry_and_persist`), is what actually closes this — not
elaborate lease-renewal machinery, per the explicit scope constraint.

Also added: `ux_attempts_call_id_attempt_number`, a real unique index —
cheap, additional defense-in-depth so two attempt rows can never share an
attempt_number regardless of any future bug in either recovery path.

## B. `queue_claim_batch_size` — WIRED THROUGH

Was declared in `Settings` but silently ignored (`_worker_slot_loop`
hardcoded `batch_size=1`) — genuinely dead configuration. Wired through:
a slot now claims up to `queue_claim_batch_size` items in one Redis round
trip and processes them sequentially (still one execution at a time per
slot — batching reduces round-trips under backlog, it does not increase
concurrency). Proven with `max_concurrent_calls=1,
queue_claim_batch_size=3`: 3 calls all complete, all stamped with the
same single slot's `worker_id`
(`test_queue_claim_batch_size_is_actually_wired_through`).

## C. Reconciliation — PROVEN SAFE, NO CODE CHANGE NEEDED

The DB→Redis reconciliation sweep's `is_queued()` then `enqueue()` shape
is check-then-act, not atomic. Investigated whether this can cause
duplicate execution: it cannot, for two independent, already-existing
reasons — (1) Redis ZSET membership is inherently idempotent (re-adding
an existing member updates its score, never creates a second entry), and
(2) every claim path re-checks the attempt's actual Postgres status via
`_load_attempt_context` and refuses anything that isn't `PENDING`. Proven
directly rather than left as an assertion:
`test_reenqueueing_an_already_completed_attempt_does_not_reexecute_it`
(re-adds an already-terminal attempt_id, confirms it's claimed-and-dropped,
not re-run) and
`test_concurrent_duplicate_enqueues_of_the_same_pending_attempt_execute_it_once`
(10 concurrent enqueues of the same pending attempt, exactly one
execution).

## D. Multi-process safety — NEWLY PROVEN

No prior test ran two `WorkerRuntime` instances against shared
infrastructure — per the master prompt, that can't be inferred from
single-process tests. `tests/integration/test_reconciliation_and_multiprocess.py`
adds two real multi-process tests:

- `test_two_worker_runtime_instances_share_infrastructure_safely`: two
  independent runtimes (different `instance_id`, different poll cadence),
  same Postgres, same Redis. An org capped at 1 concurrent call, hit from
  both processes at once — the shared `organizations` row's `FOR UPDATE`
  lock holds the cap globally (proven: sampled peak ≤ 1 across the whole
  run), and no call anywhere gets more than one attempt.
- `test_two_worker_runtimes_reaper_recovery_does_not_duplicate_across_processes`:
  one runtime claims and starts an attempt, then is killed with zero
  grace period (simulating a crashed process) while a second, independent
  runtime is the only thing left running. Its reaper recovers the orphan
  exactly once.

## E. State machine audit — GUARDS ADDED, ONE NEW TEST

Traced every status-mutating statement in the repo (`grep` confirmed all
of them live in `orchestrator/worker_runtime.py` and `call_service.py`,
matching the "single owner" design). Found the `calls` table's UPDATE
statements in `_decide_retry_and_persist` and `_finalize_attempt` had no
`WHERE status = '...'` guard — relying entirely on the *caller* already
having exclusively won the attempt-level race before reaching them. Added
matching guards (with a `logger.warning` if one ever fires unexpectedly)
for real defense-in-depth, consistent with "do not rely only on
application-level locks."

New direct test,
`test_terminal_call_state_cannot_be_mutated_by_a_second_finalize_call`:
calls `WorkerRuntime._finalize_attempt` a second time for an
already-completed attempt and asserts the Call's row is byte-for-byte
unchanged — isolating the Call-level guard from every other protection
layer (Redis exclusivity, attempt-level guards) to prove it holds on its
own.

**A new legal transition was added, not invented ad hoc:** `CallState`
had no path from `QUEUED` directly to `FAILED` — every terminal failure
was modeled as having gone through `IN_PROGRESS` first. Investigating the
missing-org bug (item J below) surfaced a real case where that's false: a
Call can be discovered un-runnable *before* any attempt ever reaches
`RUNNING`. Added `QUEUED → FAILED` as a documented, legal transition
(`orchestrator/states.py`) rather than routing around the state machine
or asserting something that didn't happen.

## F. Idempotency — RE-VERIFIED, ONE GAP CLOSED

The 10-concurrent-request test already existed and was re-run (still
passes). The explicitly-requested second case — retrying with the same
key *after* the original request already completed — had no test.
Added: `test_retry_with_same_idempotency_key_after_call_already_completed_returns_completed_call`,
which runs a call to real completion, then retries admission with the
same key and asserts the same, already-completed Call comes back with no
new attempt created.

## G. Tenant isolation — RE-VERIFIED, ONE ARCHITECTURAL RULE ADDED

Application-session and worker/system-lookup paths were already
separately tested (`tests/multitenant/`, `tests/integration/test_tenant_isolation_runtime.py`).
Added one more permanent guarantee: a new AST-level layering rule
(`app/layers.py`'s `symbol_confinement`, enforced by
`tests/layering/test_import_boundaries.py::test_symbol_confinement`)
restricts `storage.db.system_session` — the narrow cross-organization
role — to `orchestrator/**` only. `api/**`, which is always scoped to one
authenticated caller's org, cannot import it at all; this is checked by
parsing imports, not by convention.

## H. Shutdown / crash recovery — NEWLY PROVEN AT THE WORKER LEVEL

`conversation/runtime.py`'s cancellation-propagation property (a genuine
`asyncio.CancelledError` is never caught and converted into a normal
result) was already unit-tested in isolation. Added
`test_shutdown_cancellation_is_not_treated_as_a_retryable_provider_failure`,
which exercises the SAME property through the real `WorkerRuntime.stop()`
path: a slot mid-execution is forcibly cancelled with zero grace period,
the attempt is confirmed to remain `RUNNING` in Postgres immediately
after (not corrupted into some other status), and a fresh runtime's
ordinary reaper — the exact same mechanism a real crash uses — recovers
it correctly. Proves shutdown-cancellation and a genuine crash share one
recovery path, not two that could silently diverge.

## I. Configuration parity — ONE REAL GAP FOUND AND CLOSED

Phase 0 already refuses to boot in production with placeholder vendor API
keys (`app/config.py`). That check is necessary but was not sufficient:
`app/main.py`'s composition root unconditionally constructs
`FakeTelephonyProvider()` regardless of those credentials, because no
real adapter exists yet. A deployer could set fully real Exotel/Deepgram
credentials in production and the service would still silently simulate
every call. Fixed: `app/main.py` now refuses to start at all when
`ENVIRONMENT=production`, with an explicit, temporary reason in the error
message (remove this specific guard in the same change that wires in a
real `TelephonyProvider`). Proven by
`tests/unit/test_production_provider_guard.py`, which reloads `app.main`
under production-shaped settings and asserts the real module-level guard
fires — not a re-implementation of the check.

## J. Observability — REAL GAP FOUND AND CLOSED, WHICH THEN FOUND A SECOND BUG

`orchestrator/worker_runtime.py` used stdlib `logging`, not `structlog`
(the only logging configured anywhere in the app, in `app/main.py`).
Since nothing configures a stdlib logging handler, this meant **every
log call in the worker runtime — including the pre-existing
`logger.exception`/`logger.warning` error-path calls — was effectively
going nowhere**, not just missing structured fields. Fixed: switched to
`structlog.get_logger(__name__)`, matching the rest of the app, and added
INFO-level structured events at every lifecycle point requested
(`call_admitted` in `api/endpoints/calls.py`, `attempt_claimed`,
`attempt_started`, `attempt_capacity_blocked`, `attempt_finalized`,
`retry_scheduled`, `attempt_recovered_by_reaper`,
`reconciliation_reenqueued_attempt`), each carrying `call_id`,
`attempt_id`, `organization_id`, `worker_id`/`session_id` as available,
and the originating HTTP `request_id` at the one point it's actually
available (admission) — a plain trace/correlation approach, not a
distributed tracing system.

**Making these logs real, then actually reading them, is what found item
I's sibling bug**: running a manual end-to-end request through the real
app and watching the logs showed `attempt_claimed` firing repeatedly for
the same attempt, same worker. Traced to `_try_start_running`: when an
attempt's `organization_id` had no `organizations` row, the capacity
check's `cap` silently defaulted to `0`, and `running (0) >= cap (0)` was
then **always** true — indistinguishable from "temporarily at capacity",
so the attempt was released back to the queue and reclaimed forever,
silently, never completing and never failing. This was a real,
previously-invisible bug that code review alone had not caught. Fixed:
`_try_start_running` now returns a distinct `"missing_org"` outcome,
handled by immediately failing the attempt and Call
(`_fail_attempt_missing_org`, `FailureCategory.VALIDATION`, no retry —
retrying can't make the org exist). Re-ran the exact manual repro after
the fix to confirm empirically, not just re-read the code: one claim, one
clear error log, one terminal failure. Covered by
`test_call_for_nonexistent_organization_fails_cleanly_instead_of_looping_forever`.

## Code changes summary

Real correctness defects only, per the constraint against test-driven
production changes:

1. `orchestrator/queue.py` — atomic sweep script (A)
2. `orchestrator/worker_runtime.py` — `session_id` write-back (A);
   `WHERE status=...` guards on attempt and call UPDATEs (A, E);
   `queue_claim_batch_size` wiring (B); `"missing_org"` outcome + terminal
   failure path (J); structlog conversion + correlation fields (J)
3. `orchestrator/states.py` — `QUEUED → FAILED` legal transition (E, J)
4. `app/main.py` — production fail-closed guard (I)
5. `api/endpoints/calls.py` — `call_admitted` correlation log (J)
6. `app/layers.py` — `system_session` symbol-confinement rule (G)
7. New migration `b3d4e5f6a7c8` — `ux_attempts_call_id_attempt_number` (A)

## VERIFIED

- **Idempotency** — concurrent-request race (pre-existing) and
  retry-after-completion (new), both against real Postgres transactions.
- **Tenant isolation** — RLS on every tenant table including the Phase 1
  additions, the narrow worker role's read-only cross-org access, and now
  an enforced architectural rule restricting who may use it.
- **Bounded concurrency** — global (task-count), per-org (row-locked),
  and now proven across two real, independent processes, not inferred
  from one.
- **Retry semantics** — category-driven, policy-owned, exponential
  backoff with jitter; proven end-to-end including the specific case of a
  failure discovered before any attempt could legitimately start.
- **Lease/reaper correctness** — atomic sweep (fixed), proven exclusive
  under 20-way concurrent reaping, proven correct when a lease expires
  under a worker that's still genuinely alive, proven correct across two
  processes.
- **Reconciliation** — proven safe under both of its plausible race
  outcomes (re-enqueueing something terminal; concurrent duplicate
  enqueues of something still pending).
- **Shutdown/recovery** — proven that forced shutdown-cancellation and a
  genuine crash converge on the exact same recovery mechanism, not two
  that could diverge.
- **State machine invariants** — every transition site traced to a single
  owner; terminal-state immutability proven directly, not just implied by
  other tests passing; the one new legal transition is documented, not
  silently added.
- **Provider isolation** — unchanged from Phase 1 (Protocol-based,
  `telephony/fake.py` the only implementation), now with a fail-closed
  guard preventing it from silently standing in for a real provider in
  production.
- **Production/test graph parity** — the composition root, the real
  `WorkerRuntime`, the real `Queue`, and (as of this pass) the real
  logging pipeline are what every integration test exercises; nothing
  about the object graph differs between test and production beyond
  timing constants and the (now guarded) provider choice.

## NOT VERIFIED / DEFERRED

Unchanged from Phase 1 — explicitly out of scope for this pass:

- Semantic conversation intelligence
- LLM reasoning
- STT/TTS
- Real telephony
- LeadBoost production integration

## Remaining limitations

- The lease-vs-slow-execution tension (documented in
  `docs/PHASE1_DESIGN.md`) is now handled correctly when it occurs
  (proven in item A), but `queue_lease_seconds` is still a single fixed
  value, not workload-aware. Acceptable for Phase 1's fake, instant
  provider; worth revisiting once a real, variable-latency provider
  exists — deliberately not solved with heartbeat/lease-renewal machinery
  now, per the explicit scope constraint.
- `CallService.create_call` still does not validate that
  `organization_id` corresponds to an existing `organizations` row at
  admission time — the fix in this pass makes the *consequence* of that
  (a call for a nonexistent org) fail cleanly instead of looping forever,
  but the call is still accepted at `POST /v1/calls` before that's
  discovered. Adding admission-time validation would be a reasonable
  follow-up, not done here to keep this pass to actual correctness
  defects rather than expanding validation scope.
- Multi-process testing (item D) covers two processes with a real Redis
  and Postgres; it does not simulate network partition between a process
  and Redis/Postgres specifically, only process death. Believed to
  degrade the same way (lease expiry → reaper recovery) but not
  separately proven.
