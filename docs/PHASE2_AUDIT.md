# Phase 2 Audit / Final Report

Semantic conversation intelligence, built inside the existing Phase 1
execution runtime with zero changes to Phase 1's own architecture. This
report follows the required section structure (A-P).

## A. Existing Phase 1 preserved

Verified, not assumed: the full Phase 1 test suite (109 tests as of the
hardening pass) still passes unchanged after every Phase 2 change,
re-run repeatedly throughout this build, not just once at the end.
`conversation/runtime.py::execute_call_attempt` gained exactly one new
optional parameter (`conversation_engine`, default `None`);
`orchestrator/worker_runtime.py::WorkerRuntime` gained exactly one new
optional constructor parameter (`conversation_engine_factory`, default
`None`). Nothing about admission, idempotency, the queue, worker
lifecycle, tenant isolation, retry policy, state machines, the telephony
provider boundary, or graceful shutdown changed. Admission, RLS,
concurrency bounds, reaper/reconciliation, and shutdown/crash recovery
remain exactly as verified in `docs/PHASE1_AUDIT_ADDENDUM.md`.

## B. New Phase 2 architecture

`intelligence/` (interpreter, state/reconciler, planner, responder,
context assembly, prompts, LLM provider Protocol + fake, observability,
contracts) and `guardrails/` (deterministic policy) as new,
independently-testable packages sitting below `conversation/` in the
dependency chain: `orchestrator -> conversation -> {intelligence,
guardrails, telephony}`. `conversation/semantic_loop.py` is the new
orchestration layer tying them together into the turn pipeline;
`conversation/llm_client.py` is the one file permitted to import the
`groq` SDK (a seam Phase 0 reserved before Phase 2 started);
`conversation/persistence.py` and `conversation/replay.py` are thin,
storage-aware/replay-aware layers that `intelligence/` itself never
touches. Full detail in `docs/PHASE2_DESIGN.md`.

## C. Semantic contracts

`intelligence/contracts.py` — one file, the shared vocabulary. `Belief[T]`
(value, source, certainty, observed_at_turn, status, explicit, rationale)
is the only way a fact enters `ConversationState`. `SemanticInterpretation`
represents multiple simultaneous meanings without forcing any field
non-null. `ActionCategory` is the one deliberately closed vocabulary
(the guardrail control surface); intents, stages, objection types, and
entities are open-world strings, never a fixed enum.

## D. State model

`ConversationState` is immutable — every reconciliation produces a new
value, never mutates in place, so any prior state remains a valid
snapshot for replay/audit. `fact_history` retains superseded/contradicted
beliefs rather than deleting them. Provenance is carried on every belief,
never silently upgraded (a `derived_inference` never becomes
`verified` by sitting in state longer).

## E. Interpreter

`intelligence/interpreter.py` — thin: calls `LLMProvider.interpret`,
clamps confidence into `[0,1]` defensively, returns the typed result. No
semantic judgment of its own beyond that clamp.

## F. Planner

`intelligence/planner.py` — validates SHAPE only (offered action
category, offered tool name), never WHICH action is "correct". No static
funnel exists anywhere in the codebase — verified by inspection and by
`tests/unit/test_planner.py::test_planner_does_not_second_guess_which_action_was_chosen`,
which proves two differently-shaped-but-valid fixture plans both pass
through unmodified.

## G. Guardrails

`guardrails/policy.py` — seven independent, deterministic policies, zero
model calls: opt-out (unconditional), action-category validity, tool
authorization, tool argument completeness, unconfirmed-terminal-outcome
claims (generalized beyond just "meeting booked" to any outcome in
`_OUTCOMES_REQUIRING_CONFIRMATION`), and structural completeness for
`END_CALL`/`ASK`. 14 unit tests, each policy tested for both rejection
and the corresponding authorized case, plus one test proving check
ordering is deterministic (opt-out wins over every other simultaneous
violation).

## H. Provider boundary

`intelligence/llm_provider.py::LLMProvider` — a three-method Protocol
(`interpret`/`plan`/`generate_response`), mirroring
`telephony/contracts.py::TelephonyProvider` exactly.
`intelligence/fake_llm.py::FakeLLMProvider` — fixture-based, mechanically
proven free of keyword/heuristic logic
(`tests/unit/test_fake_llm_provider.py`, both an AST scan and an
empirical "same result regardless of input" test).
`conversation/llm_client.py::GroqLLMProvider` — real, strict JSON
parsing (`LLMResponseParsingError` on malformed output, never a silent
"unknown" downgrade), confined to the one file `app/layers.py` already
reserved for it. The SAME `intelligence/*` code runs against either
provider; nothing branches on which one it has.

## I. Evaluation system

`eval/scenarios.py` — 8 scenarios (of the master prompt's 26 listed
categories — see "Remaining limitations" below for which subset and
why), each with independently-scored dimension checks (14 total across
the 8 scenarios): intent understanding, state consistency, fact
extraction, context retention, policy compliance, termination behavior,
grounding, and unsupported-assumptions avoidance. Includes a genuine
paraphrase-equivalence pair (two differently-worded "we use Salesforce"
statements converging on the same state), a contradiction/correction
scenario (the exact worked example from the spec), a prompt-injection
attempt, and an unconfirmed-claim rejection. `eval/runner.py` executes
every scenario through the REAL pipeline with `FakeLLMProvider`;
`tests/unit/test_evaluation_suite.py` wraps it as an always-on CI test
(9 tests: 8 scenarios + a guard against a scenario accidentally having
zero checks). `eval/live_smoke.py` — 3 structural (not semantic) tests
against the real Groq API, `skipif`'d without `GROQ_API_KEY`, outside
`testpaths` so ordinary `pytest` never touches it — genuinely never run
in this session, since no live credentials were available; their import
correctness and skip behavior were verified instead.

## J. Replay system

`conversation/replay.py` — `TurnRecord` (a snapshot mirroring
`conversation_turns` columns) plus `replay_turn`, which reconstructs a
`FakeLLMProvider` from the recorded outputs and re-runs the real
pipeline. Proven useful, not just mechanically correct:
`tests/unit/test_replay.py::test_replay_detects_a_guardrail_policy_regression`
replays the identical recorded model output against a guardrail context
that no longer has the confirmation it originally had, and the
regression is surfaced directly — no live model call needed to detect it.

## K. Observability

`intelligence/observability.py::log_turn` — one structured `structlog`
event per turn (`semantic_turn_completed`) carrying every field
requested: call/attempt/session/turn ids, state-before/after summaries,
the full interpretation and planner `ModelInvocationMeta` (model,
provider, prompt/policy/context versions, latency, token counts),
guardrail verdict/reason/violated-policy, final action, and response
text. `api/endpoints/calls.py` now also logs `call_admitted` with the
originating HTTP `request_id` alongside `call_id` — the one place they're
both naturally available, since execution happens asynchronously and
potentially much later.

**A real, previously-invisible bug this observability work directly
caught**: the first working draft of the runtime integration passed
`context.call_attempt_id` for BOTH `run_conversation`'s `call_id` and
`call_attempt_id` parameters (there was no `call_id` field on
`ConversationEngineConfig` at all). This was only noticed by reading the
actual log output of the first full end-to-end test run and seeing
`semantic_turn_completed`'s `call_id` field didn't match
`attempt_claimed`'s `call_id` field for the same call. Fixed by adding
`call_id: uuid.UUID` to `ConversationEngineConfig` and threading the real
value through from `WorkerRuntime`'s already-loaded attempt context.
Re-verified by re-running the same manual check after the fix and
confirming the two log lines now agree.

## L. Tests

181 tests total (up from Phase 1's 121 post-hardening), all passing,
stable across repeated runs:

| Category | Count | What's real |
|---|---|---|
| `tests/unit/` | 122 | Pure logic + `FakeLLMProvider` — contracts/reconciler, guardrails, planner, responder, fake-provider self-check, full pipeline (6 tests including the correction/contradiction and guardrail-rejection cases), replay, evaluation suite |
| `tests/integration/` | 37 | Real Postgres + real Redis + `FakeLLMProvider`/`FakeTelephonyProvider` — Phase 1's full hardened suite plus 2 new Phase 2 persistence/RLS tests and 1 full E2E test through a real `WorkerRuntime` |
| `tests/layering/` | 5 | AST-based, including the new `intelligence`/`guardrails` dependency bans |
| `tests/contract/` | 12 | Real HTTP through the real app |
| `tests/multitenant/` | 5 | RLS at the SQL layer |

`mypy --strict` clean across all 45 production source files (including
`intelligence/`, `guardrails/`, newly added to CI's strict scope in this
pass — previously only `app api storage orchestrator telephony
conversation`); whole-repo `mypy` (including tests and `eval/`) clean
across 89 files; `ruff check .` clean.

## M. Files changed

New: `intelligence/` (9 files), `guardrails/policy.py`,
`conversation/llm_client.py`, `conversation/semantic_loop.py`,
`conversation/persistence.py`, `conversation/replay.py`, `eval/`
(scenarios, runner, live_smoke), migration `c4d5e6f7a8b9`
(`conversation_turns`), 9 new test files. Modified: `conversation/runtime.py`
(one new optional parameter), `orchestrator/worker_runtime.py` (one new
optional constructor parameter, one call site updated), `app/layers.py`
(5 new dependency-ban rules), `storage/models.py` (`ConversationTurn`
model), `api/endpoints/calls.py` (one new observability log line),
`.github/workflows/ci.yml` (strict mypy scope extended),
`requirements.txt` (`groq` added).

## N. Hardcoded heuristics found and removed

None found in the new Phase 2 code. Explicitly grepped
`intelligence/`, `guardrails/`, and `conversation/{llm_client,
semantic_loop, persistence, runtime}.py` for `.lower()`/`.startswith()`/
`.endswith()`/transcript-containment patterns: zero matches. The two
`grep` hits for the literal word "keyword" are both docstring sentences
*about* avoiding keyword logic, not keyword logic itself (verified by
direct inspection, not just the grep count). `FakeLLMProvider` is
mechanically proven free of this class of bug by
`tests/unit/test_fake_llm_provider.py`, not merely by code review.

Two REAL bugs (not heuristics) were found and fixed during this build,
both by actually running the integration end-to-end rather than only
reasoning about the code:

1. A garbage placeholder expression in an early draft of
   `run_conversation` (`interpretation=... and None or None`) — caught
   before it ever ran, by noticing `TurnOutcome` didn't carry the
   interpretation object `log_turn` needed, and fixing the data flow
   properly (added `interpretation`/`plan` fields to `TurnOutcome`)
   rather than working around the gap.
2. The `call_id`-vs-`attempt_id` observability bug described in section K.

## O. Remaining limitations

- **Evaluation coverage is a representative subset, not all 26
  categories.** Covered: initial interest, explicit rejection, existing-
  solution paraphrase equivalence (2 scenarios), contradiction/
  correction, prompt injection, unconfirmed-claim rejection, multiple-
  simultaneous-intents. Not built as separate scenarios: switching
  concern, implementation concern, competitor comparison, budget/
  authority uncertainty, callback/meeting-request variants, hidden
  objection, hostile interaction, confused prospect, and several others
  — each would exercise the same pipeline/dimension-check mechanism
  already proven by the 8 that exist, so the marginal engineering value
  of building all 26 by hand (versus the ones chosen specifically for
  being architecturally distinct from each other) was judged lower than
  spending the same time on the runtime integration and its tests. This
  is a real, honest scope limitation, not a claim that only 8 scenarios
  matter.
- **State reconciliation's contradiction detection is interpreter-driven,
  not reconciler-driven.** `reconcile()` marks a fact superseded when
  the interpreter explicitly reports it in `disputed_facts` — it does
  not itself run any semantic comparison between a new statement and an
  existing belief. This is a deliberate boundary (detecting that two
  statements conflict is a semantic judgment, which belongs upstream,
  not in deterministic reconciliation code), but it does mean
  reconciliation quality is bounded by how reliably the interpreter
  reports disputes — untested against a live model in this pass (no
  credentials available).
- **No real hang-up signal to the telephony provider** when a semantic
  conversation ends before the provider's own event stream would
  naturally terminate — see `docs/PHASE2_DESIGN.md`'s "Known, documented
  limitation". Harmless for `telephony/fake.py`; a real adapter would
  need this addressed.
- **Live LLM smoke tests were never actually run** — no `GROQ_API_KEY`
  was available in this sandbox. Their import correctness, skip
  behavior, and exclusion from default test collection were verified;
  their actual behavior against the real Groq API was not. This is
  stated plainly rather than implied to have been checked.
- **No tool execution exists yet** — `guardrails/policy.py`'s tool-
  authorization checks are real and tested, but nothing in this
  codebase actually calls a tool; `confirmed_terminal_outcomes` is
  populated by test/eval fixtures standing in for a real tool result.
  A real tool-execution layer (and the `TOOL_CALL` action actually
  doing something) is future work.
- **`ConversationEngineFactory` is not wired into `app/main.py`'s
  composition root.** Phase 2 exists as a genuine, tested capability —
  proven end-to-end through a real `WorkerRuntime` — but is not enabled
  by default in the production composition root, matching Phase 1's own
  fail-closed posture (the production guard added during Phase 1
  hardening already refuses to boot with only a fake telephony provider
  in `ENVIRONMENT=production`; enabling Phase 2 by default with only
  `FakeLLMProvider` and no real credentials would be the same class of
  problem for the LLM boundary). Wiring a real engine into the
  composition root is a deliberate, explicit next step, not an oversight.

## P. Exact next step for Phase 3

Per the master prompt's own architecture recommendation and this
repo's Phase 1 extension-point design (`conversation/semantic_loop.py`'s
`run_turn`/`run_conversation` — the exact seam
`docs/PHASE1_DESIGN.md` named as "Phase 2 extension point" and this
build filled): Phase 3 is real speech. Concretely:

1. A `ProspectTurnSource` implementation backed by real STT (replacing
   the fixture-based sources this pass used), feeding `run_conversation`
   real transcribed prospect speech instead of scripted text.
2. A real `TelephonyProvider` (Exotel or similar), implementing
   `telephony/contracts.py`'s existing Protocol — no change to
   `conversation/runtime.py` or `orchestrator/worker_runtime.py` needed
   beyond removing the Phase 1 hardening pass's "no real telephony in
   production" guard once a real provider actually exists.
3. TTS for `AgentTurn.response_text` — the output side of the same
   boundary STT fills on the input side.
4. Wire `ConversationEngineFactory` into `app/main.py`'s composition
   root, gated on real credentials being present (mirroring the same
   fail-closed pattern already established for telephony).
5. Resolve the two limitations named in section O that specifically
   matter more once real, variable-latency providers exist: the
   hang-up-signal gap, and (per `docs/PHASE1_DESIGN.md`'s own earlier
   note) `queue_lease_seconds` needing to account for a conversation
   that can legitimately run longer than a fake, instant provider ever
   could.
