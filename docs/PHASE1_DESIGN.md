# Phase 1 Design — Execution Runtime

Scope: turn `POST /v1/calls` from "writes a queued row and stops" into a
real, bounded-concurrency, idempotent, retry-aware execution runtime ending
at a fake telephony boundary. No LLM, no keyword logic, no real telephony —
see `docs/PHASE0_AUDIT.md` for everything this builds on.

## New packages, and why they land where they do

Phase 0 already declared the intended shape via empty placeholder packages
and `app/layers.py`'s vendor-confinement comments. Phase 1 fills them in
rather than inventing a new layout:

- **`orchestrator/`** — the admission service, the Redis-backed queue, the
  worker runtime, retry policy, and the state-machine transition owners.
  This is the "Queue Runtime" + "Worker Runtime" + "Call Service" boxes in
  the target architecture.
- **`telephony/contracts.py`** — the `TelephonyProvider` Protocol and its
  outcome/event types. **`telephony/fake.py`** — the configurable fake
  adapter. Neither imports a vendor SDK, so neither needs to live under
  `telephony/exotel/` or `telephony/deepgram/` (those stay empty, reserved
  for real adapters in a later phase); `app/layers.py`'s existing
  vendor-confinement rules are untouched.
- **`conversation/runtime.py`** — the "Conversation Runtime" box: a small,
  deterministic executor that owns a `ConversationSession`, drives it
  through a scripted turn sequence against a `TelephonyProvider`, and
  returns a result. No semantic interpretation of any kind lives here —
  that's Phase 2's job, and this module is the clean seam for it (see
  "Phase 2 extension point" below).

No package gets a `queue_context.py` / `queue_state.py` / `queue_guard.py`
style split — `orchestrator/queue.py` is one cohesive module because
enqueue/claim/ack/fail are one responsibility (queue coordination), not
five.

## Domain model

Extends, doesn't replace, Phase 0's `storage/models.py`:

- **`Call`** (existing, unchanged) — the logical request. `status` becomes
  a real state machine (see below) instead of an unvalidated string.
- **`CallAttempt`** (existing, extended) — one execution attempt. Adds
  `session_id`, `worker_id`, `started_at`, `ended_at`, `failure_category`,
  `failure_detail`, `updated_at`. Retries are new rows
  (`attempt_number` + 1), never overwrites of a terminal row.
- **`ConversationSession`** (new) — the live execution session, distinct
  from the logical `Call`. Owns `state`, `started_at`, `ended_at`.
- **`CallAttemptEvent`** (new) — an ordered, durable record of what
  happened during an attempt (`sequence_number` per attempt, not
  wall-clock — two events can share a timestamp). This is the "durable
  execution record plus ordered events" the master prompt asks for; it is
  deliberately not full event sourcing (no replay, no CQRS) — it's an
  audit trail read by observability and by the retry decision, nothing
  replays state from it.
- **`CallIdempotencyKey`** (new) — see "Idempotency" below.
- **Worker** — deliberately **not** a durable table. A worker is execution
  capacity: an in-process asyncio task holding a semaphore slot, identified
  by `f"{instance_id}:{slot_index}"`. Its only durable footprint is the
  `worker_id` string stamped onto whichever `CallAttempt`/`ConversationSession`
  it's running, for correlation — giving it its own table with a lifecycle
  would duplicate what Redis leases + those stamped columns already prove,
  for no behavior anything would actually use.
- **QueueEntry** — similarly not a Python class with its own file. An
  entry's state *is* which Redis structure it's currently a member of
  (`ready` vs `inflight` vs neither); reifying that as a third
  parallel state enum would be a second source of truth for a fact Redis
  already holds. `orchestrator/queue.py`'s docstring is the "state machine"
  documentation for this one.

## State machines

Single owner per machine: a `Transition` validator function that raises on
an illegal move, called from exactly one place (`CallService` for `Call`,
`WorkerRuntime`/the reaper for `CallAttempt` and `ConversationSession`) —
`orchestrator/states.py`.

```
CallState:        QUEUED ⇄ IN_PROGRESS → COMPLETED | FAILED | CANCELLED
                   QUEUED → CANCELLED
  (QUEUED = waiting for an attempt to run, including between retries;
   IN_PROGRESS = an attempt currently owns it; all three right-hand
   states are terminal.)

CallAttemptState:  PENDING → RUNNING → COMPLETED | FAILED
                   PENDING → INTERRUPTED   (reaper: crashed before RUNNING)
                   RUNNING → INTERRUPTED   (reaper: crashed during RUNNING)
  (No CLAIMED state: "claimed but not yet running" is Redis's inflight
   set, not a DB status — seeded above under "Worker".)

ConversationSessionState: STARTED → RUNNING → COMPLETED | FAILED | ABORTED
```

## Idempotency

`POST /v1/calls` with the same `idempotency_key` for the same org must
create exactly one `Call`, correct under concurrency, backed by a real
constraint — not a Redis-only guarantee (Redis is coordination/ephemeral by
this system's own stated design principle; a guarantee that only holds
until the next `maxmemory` eviction isn't a guarantee).

`calls.idempotency_key` can't carry a real global unique constraint — it's
on a table partitioned by `created_at` (Phase 0 audit already confirmed
this is correct, not a gap). So Phase 1 adds one small, **non-partitioned**
table that is the actual enforcement point:

```sql
CREATE TABLE call_idempotency_keys (
    organization_id INTEGER NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    call_id UUID NOT NULL,
    call_created_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, idempotency_key),
    FOREIGN KEY (call_id, call_created_at) REFERENCES calls (id, created_at)
);
```

`CallService.create_call` does, in **one Postgres transaction**:
1. If `idempotency_key` given: `INSERT ... ON CONFLICT (organization_id,
   idempotency_key) DO NOTHING`. If 0 rows affected, someone already won —
   `SELECT` the existing `call_id`/`call_created_at`, load and return that
   `Call` (not a new one), transaction ends here.
2. Otherwise: insert the `Call` row, insert the ledger row referencing it
   (same transaction — one commits, both do, or neither does), insert the
   first `CallAttempt` (`PENDING`, `scheduled_at = now()`).
3. Commit.

This is correct under concurrency because the ledger's primary key makes
step 1 atomic at the database level regardless of how many requests race —
tested directly (`tests/integration/test_call_admission.py`,
10 concurrent identical requests → 1 `Call`).

**Enqueue happens after commit, not inside the transaction** — pushing to
Redis can't be rolled back by a Postgres abort, so it must not be able to
partially participate in one. This creates one honestly-documented gap: if
the process dies between the commit and the Redis push, the `Call`/first
`CallAttempt` exist durably but nothing enqueued them. **Recovery:** the
same periodic sweep the worker runtime already runs for lease expiry
(`orchestrator/worker_runtime.py`'s reconciliation task) also scans for
`PENDING` attempts whose `scheduled_at` has passed and are absent from
Redis's `ready`/`inflight` sets, and re-enqueues them. No distributed
transaction is invented; the gap is closed by making Postgres the source of
truth and Redis reconcilable from it, per the stated design principle.

## Queue (Redis)

Two sorted sets, one hash, under `calling:queue:*` (global, not
per-org — the queue coordinates ordering; org isolation is enforced by
every consumer looking up the attempt in Postgres through an
`org_scoped_session`, and by `WorkerRuntime`'s per-org capacity check before
it ever touches call data):

| Key | Type | Member → score/value | Purpose | TTL |
|---|---|---|---|---|
| `calling:queue:ready` | ZSET | `attempt_id` → `ready_at` (epoch) | work eligible now or in the future | none (durable-ish; reconciled from Postgres) |
| `calling:queue:inflight` | ZSET | `attempt_id` → `lease_expiry` (epoch) | claimed, being executed | none (swept by the reaper) |
| `calling:queue:owner` | HASH | `attempt_id` → `worker_id` | "who owns this call" observability | cleared on ack/fail |

**Claim** is one Lua script (`orchestrator/queue.lua`, loaded once,
`EVALSHA`'d thereafter): `ZRANGEBYSCORE ready -inf <now> LIMIT 0 <batch>`,
then for each candidate `ZREM` from `ready` and `ZADD` into `inflight` with
score `now + lease_seconds`, `HSET owner`. Doing this server-side in one
round trip is what makes it a real atomic claim rather than a
check-then-act race between workers reading overlapping candidate lists —
two workers can still both see the same candidate in their
`ZRANGEBYSCORE`, but only one's `ZREM` inside the script actually removes
it (Redis single-threads script execution), so the other's script simply
finds nothing left to move for that id.

**Ack** (success): `ZREM inflight` + `HDEL owner`. **Fail-retryable**:
`ZREM inflight` + `HDEL owner`, then a *new* attempt's id is `ZADD`ed into
`ready` with `score = now + backoff`. **Fail-terminal**: `ZREM inflight` +
`HDEL owner`, nothing re-added.

**No busy loop**: `QUEUE_POLL_INTERVAL_SECONDS` (config, default 0.5s) is
how often an idle worker slot re-checks `ready`. This is disciplined,
explicit, configurable polling, not `sleep(1)` buried in logic — Redis's
sorted-set primitives have no blocking "wait for a score ≤ now" operation,
so some poll interval is the honest minimum here (documented in
`orchestrator/queue.py`'s module docstring, not asserted without
justification).

**Worker-crash recovery**: a separate reaper coroutine (owned by
`WorkerRuntime`, started/cancelled with it) scans `inflight` for
`score < now` every `QUEUE_POLL_INTERVAL_SECONDS`. For each expired
member it removes the Redis bookkeeping, then branches on the attempt's
*current Postgres status* (the durable truth): still `PENDING` → the crash
happened before the RUNNING transition, just re-enqueue the same attempt;
`RUNNING` → mark `INTERRUPTED`, ask `RetryPolicy`, either create the next
attempt and enqueue it or mark the `Call` `FAILED`; already terminal →
stale lease, nothing to do (a completion and a lease expiry raced, and the
completion won).

## Concurrency / worker acquisition

Two bounds, not one, because the schema already has both dimensions:

1. **Global**: `asyncio.Semaphore(MAX_CONCURRENT_CALLS)` inside
   `WorkerRuntime` — Phase 1 runs as background asyncio tasks inside the
   same FastAPI process (per Phase 0's own explicit "one FastAPI process,
   no microservice split" decision), so this is the one process's actual
   resource bound. `N` slots = `N` concurrently-running claim loops, each
   independently polling and, once it claims, executing to completion
   before polling again — "work exists" (Redis `ready`) is fully decoupled
   from "capacity exists" (a free slot), so when capacity is exhausted a
   `Call` simply waits in `ready`, it never disappears.
2. **Per-organization**: `organizations.plan_max_concurrent_calls`
   (already in Phase 0's schema, previously unused) is enforced at the
   PENDING→RUNNING transition: before a slot commits to running a claimed
   attempt, it counts that org's currently-`RUNNING` attempts; if at cap,
   it releases the claim back to `ready` (short delay, not a failure) and
   moves on to the next candidate instead. A partial unique index,
   `CREATE UNIQUE INDEX ix_attempts_one_running_per_call ON call_attempts
   (call_id) WHERE status = 'running'`, is the real database-enforced
   invariant that the *same call* can never be running twice regardless of
   any application-level race — this is the actual "same call cannot be
   concurrently executed twice" guarantee, not just the Redis claim.

## Failure taxonomy and retry

`orchestrator/failures.py`: one `FailureCategory` enum (`VALIDATION`,
`AUTHORIZATION`, `TENANT_VIOLATION`, `CAPACITY`, `PROVIDER`, `TIMEOUT`,
`CANCELLATION`, `TRANSIENT_INFRA`, `PERMANENT_EXECUTION`,
`BUSINESS_TERMINAL`) and one `RetryPolicy` (`max_attempts`,
`initial_delay_seconds`, `backoff_multiplier`, `max_delay_seconds`,
`jitter_fraction`, and the set of categories it considers retryable —
`PROVIDER`, `TIMEOUT`, `TRANSIENT_INFRA` by default). `RetryPolicy.decide(
category, attempt_number) -> Retry(delay_seconds) | Terminal`. Every
failure path (provider result, reaper-detected crash, timeout) goes
through this one function — nothing does `if "timeout" in str(exc)`
anywhere.

## Fake telephony provider (`telephony/fake.py`)

Implements `telephony.contracts.TelephonyProvider` (a `Protocol`:
`async def place_call(attempt_context) -> AsyncIterator[ProviderEvent]`,
emitting a realistic sequence like `DIALING → RINGING → CONNECTED →
COMPLETED`, or `DIALING → BUSY`, etc.). Scenario selection is **entirely
test/caller-supplied configuration**, never runtime business logic: the
constructor takes a `ScenarioSource` (a simple callable
`(call_attempt) -> Scenario`, default: always `SUCCESS`). Tests inject a
dict-backed source keyed however they like (by `lead_id`, by call count,
round-robin, whatever the test needs) — the production code path
(`conversation/runtime.py`, `WorkerRuntime`) only ever calls
`provider.place_call(...)` and reacts generically to whatever
`ProviderEvent`/`FailureCategory` comes back. No `if lead_id == "demo"`
anywhere in non-test code.

## Composition root

`app/main.py` remains the sole composition root (per `app/layers.py`,
already the only module allowed to import anything and the only one
nothing else may import). Its `_lifespan` now additionally: builds one
`redis.asyncio.Redis` client, one `orchestrator.queue.Queue`, one
`FakeTelephonyProvider`, one `RetryPolicy` from settings, and one
`WorkerRuntime`; calls `await runtime.start()` (spawns the `N` worker slot
tasks + the reaper + the reconciliation sweep, all tracked in one
`list[asyncio.Task]` the runtime itself owns); on shutdown calls
`await runtime.stop(grace_period)` before closing Redis/Postgres. Nothing
below `app/main.py` constructs a `Redis` client, an `Engine`, or a
`TelephonyProvider` itself — they're all injected in.

## Shutdown

```
SIGTERM/SIGINT (uvicorn) → FastAPI lifespan shutdown
  → runtime.stop(grace_period):
      1. flip a `_stopping` flag: worker slots finish their CURRENT
         claimed attempt (if any) but stop polling for new ones; the
         reaper and reconciliation loops stop scheduling new work too
      2. await all slot tasks with `asyncio.wait(..., timeout=grace_period)`
      3. any task still running past the grace period is cancelled — its
         in-flight attempt is left RUNNING in Postgres deliberately (not
         force-marked FAILED): the *next* process's reaper will find its
         Redis lease expired and INTERRUPT+retry it correctly, so a slow
         shutdown degrades to "one attempt retried a little early," not to
         "state silently lost"
      4. cancel the reaper/reconciliation tasks
  → close the Redis client, dispose the SQLAlchemy engine
  → process exits
```

## Testing strategy

- **Unit** (`tests/unit/`): `RetryPolicy.decide` truth table, state
  transition validators (legal/illegal moves), the Lua claim script's pure
  logic isn't unit-testable in isolation (it's Redis-side) — covered under
  integration instead.
- **Integration** (`tests/integration/`, real Postgres + real Redis, fake
  provider only): queue enqueue/claim/ack/fail/reaper-recovery against a
  real Redis; idempotency under real concurrent Postgres transactions;
  full admission→persist→enqueue→claim→execute→complete path.
- **Contract** (`tests/contract/`): `POST /v1/calls` end-to-end through the
  real composed FastAPI app, asserting on the response and on the eventual
  DB state once the runtime processes it.
- **Concurrency**: N requests against M configured capacity, asserting
  active-RUNNING-count never exceeds M and no call ever double-executes.
- **Tenant isolation**: org A cannot see/claim/complete org B's call
  through the queue or the worker runtime (queue entries are bare attempt
  ids; every DB touch after a claim goes through `org_scoped_session`
  keyed off the attempt's *own* `organization_id`, read from Postgres, not
  from any caller-supplied value).
- **Architecture**: extend `tests/layering` with two rules — `conversation/`
  may not import `orchestrator/` (the runtime calls conversation, never the
  reverse — keeps the extension point in "Phase 2 preparation" clean), and
  `orchestrator/` may not import `fastapi` (keeps the runtime usable
  without the web framework, e.g. from a future standalone worker process).

## Phase 2 extension point

`conversation/runtime.py`'s `execute_call_attempt` returns a plain
`ExecutionResult` (outcome, failure category, disposition, ordered events)
computed from a hardcoded deterministic script in Phase 1. Phase 2 replaces
*only* the inside of that function — script-driven turns become
`SemanticInterpreter` → `Planner` → `Guardrails` → `ConversationAction`
driven turns — without `orchestrator/`, the queue, persistence, tenant
isolation, the API, idempotency, the telephony boundary, or shutdown
changing at all, because none of them know or care how a turn's content
was decided, only that `conversation.runtime` eventually returns an
`ExecutionResult`.
