# Phase 2 Design — Semantic Conversation Intelligence

Scope: add genuine semantic conversation intelligence inside the
existing, unmodified Phase 1 execution runtime. No Phase 1 architecture
changed to make this possible — admission, idempotency, PostgreSQL
durability, the Redis queue, worker lifecycle, tenant isolation, retry
policy, state machines, the telephony provider boundary, and graceful
shutdown are exactly what they were at the end of the Phase 1 hardening
pass (`docs/PHASE1_AUDIT_ADDENDUM.md`). Still text-only, still driving
`telephony/fake.py` — no real telephony, no STT/TTS, no streaming audio.

## Two worlds, one boundary

```
DETERMINISTIC WORLD                    INTELLIGENT WORLD
(unchanged from Phase 1, plus           (new: intelligence/, guardrails/)
guardrails/ — deterministic policy)
  auth, tenant isolation,                 semantic interpretation
  idempotency, queueing,                  context reasoning
  worker ownership, lifecycle,            planning
  persistence, state-transition           response generation
  authority, retry policy,
  timeout/cancellation,
  provider adapters, guardrails,
  audit, observability
```

The rule, unchanged from the master prompt's own framing: **the LLM
proposes, the runtime validates, the provider executes.** Concretely:
`intelligence/interpreter.py` and `intelligence/planner.py` call an
`LLMProvider` and get back a *proposal* (`SemanticInterpretation`,
`ConversationPlan`); `guardrails/policy.py` — pure, deterministic, zero
model calls — decides whether a plan becomes an executable
`ConversationAction`; `conversation/semantic_loop.py` (the runtime) is
the only thing that ever dispatches an authorized action. Nothing in
`intelligence/` or `guardrails/` can reach a database, a queue, or a
provider — enforced by AST-parsed architecture tests
(`tests/layering/test_import_boundaries.py`), not convention.

## No keyword system, anywhere

Grep the whole `intelligence/` and `guardrails/` tree for `if ... in
transcript` or any string-containment check against conversational
content: there isn't one. `tests/unit/test_fake_llm_provider.py` proves
this mechanically for the fake provider specifically (AST-walks the
module, fails on any `in`/`not in` comparison or attribute access on
`.transcript`/`.text`/`.speaker`), and empirically (the same transcript
content produces the identical default result regardless of what it
says). `FakeLLMProvider` is a fixture provider — tests and evaluation
scenarios supply exactly what interpretation/plan/response a given input
should produce; the fake never inspects the input to decide.

The one thing that IS a fixed, closed vocabulary — deliberately — is
`ActionCategory` (`SPEAK`, `ASK`, `TOOL_CALL`, `WAIT`, `TRANSFER`,
`END_CALL`): this is the control surface guardrails validates against,
not a semantic classification. `primary_intent`, `conversation_stage`,
objection/entity types, and everything else conversational is
open-world — plain strings the model produces, never mapped onto a fixed
enum (`intelligence/contracts.py`'s module docstring: "a bounded core
ontology plus extensibility").

## Contracts (`intelligence/contracts.py`)

One file, deliberately — these types are a single cohesive vocabulary
(the turn pipeline's data model), not independent responsibilities.
`Belief[T]` is the load-bearing type: every durable fact in
`ConversationState` carries `source` (`BeliefSource`: leadboost_context /
prospect_statement / agent_statement / tool_result / derived_inference /
system_metadata), `certainty` (`Certainty`: verified / high / moderate /
low / unknown — never a bare float pretending to be a business
threshold), `observed_at_turn`, `status` (current / superseded /
contradicted), and `explicit` (stated vs. inferred). Nothing writes a
bare value into `ConversationState` without one of these — there is no
code path that does.

`SemanticInterpretation` is deliberately rich and deliberately NOT
forced non-null: `interest`, `objections`, `timing_signal`, `entities`,
etc. are all independently optional, because "unknown" is a valid and
common result, and multiple simultaneous meanings (continued interest +
a switching-cost objection + a timing constraint, all in one utterance)
are represented together, never collapsed to one label.

## State reconciliation (`intelligence/state.py`)

Each turn's `SemanticInterpretation` is evidence, not a replacement.
`reconcile(state, interpretation, speaker, turn_number) -> ConversationState`
is a pure, deterministic function — no model call — that folds new
evidence into the existing state:

- **New facts** (`interpretation.new_facts`) become new `Belief`s.
- **Disputed facts** (`interpretation.disputed_facts`) mark the matching
  CURRENT belief `SUPERSEDED`/`CONTRADICTED` and move it into
  `fact_history` — retained, not deleted, so replay and audit can see
  what the system used to believe and when that changed. The worked
  example from the master prompt (turn 2: "I use Salesforce"; turn 8:
  "we moved off Salesforce") resolves to exactly one CURRENT
  `current_solution` belief at any time, with the superseded one
  preserved in history — proven directly in
  `tests/unit/test_state_reconciler.py` and again end-to-end in
  `tests/unit/test_semantic_loop_pipeline.py`.
- **Repeated facts** (the same fact restated) do not duplicate — the
  reconciler recognizes an unchanged current belief and leaves it alone.
- **Uncertain statements** get a `Certainty` reflecting what the
  interpreter actually reported, never upgraded to `VERIFIED` by the
  reconciler.

`intelligence/state.py`'s own module docstring is explicit about what
this reconciler does NOT attempt: correction detection is currently
keyed on the interpreter explicitly flagging `disputed_facts` — the
reconciler does not itself run any semantic contradiction-detection
logic (that would be exactly the kind of heuristic the spec prohibits;
detecting that two statements conflict IS a semantic judgment, which
belongs to the interpreter, not to deterministic reconciliation code).
This is a real, deliberate, documented boundary — see
`docs/PHASE2_AUDIT.md` "Remaining limitations".

## Planner (`intelligence/planner.py`)

No static funnel. There is no `if stage == DISCOVERY: ASK_PAIN_POINT`
anywhere in this codebase. `PlanningContext` (state, latest
interpretation, recent turns, objective, permitted actions/tools) goes
to the model; `propose_next_action` validates only the SHAPE of what
comes back — is the action category one that was actually offered, does
a `TOOL_CALL` name a tool that was actually offered — never which action
was the "right" choice. That judgment is entirely the model's,
`tests/unit/test_planner.py::test_planner_does_not_second_guess_which_action_was_chosen`
proves this directly: two fixtures proposing different, equally
shape-valid actions both pass through unmodified.

## Guardrails (`guardrails/policy.py`)

Zero model calls, zero semantic classification — every check is a
structural/policy check over already-typed data. Seven independent,
individually-tested policies (`tests/unit/test_guardrails.py`, 14
tests): no action after explicit opt-out (unconditional — deliberately
does not defer to the planner's rationale), no unauthorized tool, no
tool call missing required arguments, no claiming a confirmation-
requiring terminal outcome (`meeting_booked`, `callback_scheduled`,
`transfer_completed`) without an actual tool result backing it up, and
structural completeness checks (`END_CALL` needs a reason, `ASK` needs a
question objective). A rejected plan degrades to a safe, silent `WAIT`
— `conversation/semantic_loop.py::_blocked_wait_plan` — never a crash,
never the rejected action executing anyway.

## Response generation (`intelligence/responder.py`)

Planning ("what to accomplish") and wording ("how to say it") are
separate steps, on purpose — the responder receives only an
already-authorized `ConversationAction`'s objective and an explicit,
caller-selected `grounded_facts` tuple, never the full
`ConversationState` or raw transcript. It cannot invent a fact it wasn't
handed, because it was never given anything to invent FROM beyond what
was explicitly passed in — a structural grounding guarantee, not an
instruction the model could ignore.
`WAIT`/`TOOL_CALL`/`TRANSFER` never reach the provider at all (nothing to
say); `tests/unit/test_responder.py` proves this and proves the
grounding boundary directly.

## Model boundary (`intelligence/llm_provider.py`, `intelligence/fake_llm.py`, `conversation/llm_client.py`)

`LLMProvider` is a `Protocol` with three typed methods —
`interpret`/`plan`/`generate_response` — mirroring
`telephony/contracts.py::TelephonyProvider`'s exact pattern. Two
implementations exist: `intelligence/fake_llm.py::FakeLLMProvider`
(fixture-based, the only provider any deterministic test or the
always-on evaluation suite ever uses) and
`conversation/llm_client.py::GroqLLMProvider` (real, Groq-backed). The
same `intelligence/*` code runs against either — nothing branches on
which provider it has. `groq` the vendor SDK is confined to exactly this
one file, a seam Phase 0 reserved before Phase 2 started
(`app/layers.py`'s pre-existing `VendorConfinementRule`), enforced by
`tests/layering::test_vendor_sdk_confinement`.

`GroqLLMProvider` parses model output STRICTLY —
`LLMResponseParsingError` on malformed JSON, never a silent downgrade to
"unknown". A parsing failure is a real failure; the caller
(`conversation/runtime.py`) treats it as `FailureCategory.TRANSIENT_INFRA`
and routes it through Phase 1's existing retry policy — no new
failure-handling machinery for Phase 2.

## Prompt versioning (`intelligence/prompts.py`)

Every prompt is built in this one module — no scattered string
construction at call sites. Three plain version constants
(`INTERPRETER_PROMPT_VERSION`, `PLANNER_PROMPT_VERSION`,
`RESPONDER_PROMPT_VERSION`) are recorded on every `ModelInvocationMeta`,
alongside `policy_version` (`guardrails/policy.py::POLICY_VERSION`) and
`context_version` — the three axes replay/regression comparison needs to
answer "did this turn's outcome change because the prompt changed, the
policy changed, or the input changed?" No secrets are ever interpolated
into a prompt.

## Context management (`intelligence/context.py`, `intelligence/prompts.py`)

Not the full transcript, ever. `_state_summary_for_prompt` sends CURRENT
beliefs only (superseded history is excluded) — a compact, roughly
constant-sized payload regardless of how long the conversation runs —
plus `recent_turns` (bounded by what the caller passes, not unbounded
accumulation). No vector database; Phase 2 doesn't need one yet
(`intelligence/context.py`'s own docstring: retrieval can be expanded
later, this is deliberately the minimum architecture that's actually
needed now).

## Memory boundary

- **Short-term**: `recent_turns` — the caller-bounded transcript window
  passed into `PlanningContext`/prompts.
- **Working memory**: `ConversationState`'s CURRENT beliefs — active
  objections, unresolved questions, current commitments, current
  solution, current stage.
- **Longer-term**: `fact_history` — superseded/contradicted beliefs,
  retained for provenance and replay, not sent back into prompts by
  default (that would defeat the point of superseding them).

No generic memory framework — this is exactly the three concepts the
Calling Agent needs, nothing more.

## Turn pipeline / production integration (`conversation/semantic_loop.py`, `conversation/runtime.py`)

```
ConversationInput -> interpret_turn -> SemanticInterpretation
  -> reconcile -> ConversationState
  -> assemble_planning_context -> PlanningContext
  -> propose_next_action -> ConversationPlan
  -> authorize -> GuardrailResult
  -> authorized_action_from -> ConversationAction
  -> generate_response -> AgentTurn
```

`conversation/semantic_loop.py::run_turn` is pure orchestration — every
actual decision is delegated downstream, as above. `run_conversation` is
the multi-turn loop, bounded by `max_turns` (a deterministic safety
bound, not a semantic decision — an unbounded loop is an infrastructure
risk regardless of model quality, the same reasoning Phase 1 applied to
`queue_lease_seconds`/`max_concurrent_calls`). It terminates on a
guardrail-authorized `END_CALL`, the prospect source returning `None`
(they hung up — text-conversation analogue of a real STT stream ending),
or `max_turns`.

**Integration point**: `conversation/runtime.py::execute_call_attempt`
gained exactly ONE new optional parameter, `conversation_engine:
ConversationEngineConfig | None = None`. When `None` (the default, and
what every Phase 1 test still passes), behavior is byte-identical to
Phase 1 — verified by re-running the full Phase 1 suite unchanged after
this integration, not just asserted. Imports of `intelligence`/
`guardrails` are deferred inside `_run_semantic_conversation` specifically
so a plain Phase 1 call never even imports the Phase 2 packages. Once
`CONNECTED`, if an engine is configured, execution hands off to
`run_conversation` instead of continuing to iterate the provider's own
event stream, then maps the loop's `outcome_category` onto Phase 1's
`ExecutionResult` vocabulary: `outcome="completed"` (the call
mechanically succeeded — even a "not_interested" business result is a
successful call *execution*) with `disposition=outcome_category`, unless
the semantic loop itself raises (treated as `TRANSIENT_INFRA`, retryable
through Phase 1's existing policy).

`orchestrator/worker_runtime.py::WorkerRuntime` gained one optional
constructor parameter, `conversation_engine_factory` — a callable
`(attempt_context, attempt_id, session_id) -> ConversationEngineConfig |
None`, called once per claimed attempt at the single existing
`execute_call_attempt` call site. `None` (the default) reproduces Phase
1 exactly. This is the entire Phase 2 footprint inside `WorkerRuntime` —
no lifecycle, retry, capacity, or state-transition logic changed.
`tests/integration/test_semantic_e2e.py` proves the whole path — real
Postgres, real Redis, a real `WorkerRuntime`, admission through queue
through claim through semantic conversation through persistence through
completion — with only the two genuine external boundaries
(telephony, LLM) faked.

**Known, documented limitation**: once the semantic conversation ends,
execution does not currently signal a "hang up" back to the telephony
provider (it simply returns without consuming the provider's own
remaining events). Harmless for `telephony/fake.py` (no real resource to
release); a real telephony adapter would need this addressed — explicitly
deferred, not silently glossed over. See `docs/PHASE2_AUDIT.md`.

## Persistence (`conversation/persistence.py`, `conversation_turns` table)

One new table (migration `c4d5e6f7a8b9`), RLS-enabled/forced exactly like
every Phase 1 tenant table, granted to `calling_agent_app` only — no
cross-org worker-role grant needed here (unlike `call_attempts`), because
semantic turns are always persisted from within an already-resolved
organization's `org_scoped_session` (the worker has already succeeded
past `_try_start_running` by the time a turn runs). `persist_turn` is
injected into `run_conversation` as an optional callback
(`TurnPersistCallback`), not hardcoded — this is what keeps
`conversation/semantic_loop.py` testable with pure asyncio and no
database at all (`tests/unit/test_semantic_loop_pipeline.py`), matching
this codebase's established DI philosophy.

## Observability (`intelligence/observability.py`)

One structured `structlog` event per turn, `semantic_turn_completed`,
carrying every field the master prompt asks for: `call_id`,
`attempt_id`, `session_id`, `turn_number`, state-before/after summaries,
the interpretation's key fields plus its full `ModelInvocationMeta`
(model/provider/prompt_version/policy_version/context_version/latency/
tokens), the planner's rationale plus its meta, the guardrail verdict
and reason, the final action, and the response text plus its meta.
Deliberately structlog, not stdlib `logging` — Phase 1's own hardening
pass (`docs/PHASE1_AUDIT_ADDENDUM.md` item J) found that stdlib logging
in this codebase has no configured handler and is effectively silent;
Phase 2 does not repeat that mistake anywhere.

## Replay (`conversation/replay.py`)

`TurnRecord` mirrors `conversation_turns` columns directly — a snapshot
per turn, not event sourcing. `to_fixture_provider` builds a
`FakeLLMProvider` that deterministically returns exactly a record's
recorded outputs; `replay_turn` re-runs `run_turn` against it.
`tests/unit/test_replay.py` proves this is genuinely useful, not just
mechanically correct: replaying the same recorded model output against a
CHANGED `GuardrailContext` (simulating a policy config change since the
turn was first recorded) surfaces a guardrail regression directly,
without re-invoking any model.

## Evaluation (`eval/scenarios.py`, `eval/runner.py`, `eval/live_smoke.py`)

`eval/scenarios.py` is a representative subset (8 scenarios, not all 26
categories the master prompt lists — see `docs/PHASE2_AUDIT.md` for which
ones and why) of the semantic evaluation dataset, each with independently
scored `DimensionCheck`s (intent understanding, state consistency, fact
extraction, policy compliance, termination behavior, grounding,
context retention, unsupported assumptions) — never one pass/fail bit
per scenario. `eval/runner.py` runs every scenario through the REAL
pipeline (`conversation/semantic_loop.py`, `guardrails/policy.py`,
`intelligence/*`) with `FakeLLMProvider` seeded from each scenario's
fixtures, and is wrapped as an ordinary pytest suite
(`tests/unit/test_evaluation_suite.py`) so it runs on every CI push —
"evaluation is not optional." `eval/live_smoke.py` is the deliberately
separate, opt-in exception: three tests against the real Groq API,
`skipif`'d without a real `GROQ_API_KEY`, structural (does it respond,
parse, report latency) rather than semantic, and outside
`pyproject.toml`'s `testpaths` so a plain `pytest` invocation never
touches it.

## Architecture tests

`app/layers.py`'s `LAYER_GRAPH` gained: `intelligence/**` and
`guardrails/**` may never import `storage`, `orchestrator`, `telephony`,
or `fastapi` (checked by AST across the whole repo, not just these new
packages — `tests/layering/test_import_boundaries.py::test_internal_dependency_bans`).
Combined with Phase 1's existing rules (`conversation` ↛ `orchestrator`,
`orchestrator` ↛ `fastapi`, `system_session` confined to `orchestrator`),
the enforced dependency direction is now:
`orchestrator -> conversation -> {intelligence, guardrails, telephony}`,
one way, checked on every push, not by convention.

## Test pyramid

- **Unit** (`tests/unit/`): contracts/reconciler (12 tests, pre-existing
  from earlier in this build), guardrails (14), planner (5), responder
  (4), fake-provider self-check (2), full pipeline via `FakeLLMProvider`
  (6), replay (3), evaluation suite (9).
- **Integration** (`tests/integration/`): real Postgres, real Redis,
  `FakeLLMProvider` — persistence + RLS (2), the full E2E path through a
  real `WorkerRuntime` (1).
- **Live LLM smoke** (`eval/live_smoke.py`): 3 tests, opt-in only.
- **Evaluation**: `eval/scenarios.py`'s 8 scenarios, 14 dimension checks,
  always-on via the unit suite above.

See `docs/PHASE2_AUDIT.md` for exact current pass counts and what remains
deliberately out of scope for this pass.
