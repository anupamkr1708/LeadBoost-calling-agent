"""Runs eval/scenarios.py through the REAL conversation pipeline
(conversation/semantic_loop.py, guardrails/policy.py, intelligence/*)
with a FakeLLMProvider seeded from each scenario's fixture outputs, and
scores each scenario's independent dimension checks
(docs/PHASE2_DESIGN.md "AI evaluation" / master prompt Section 30).

This is a REGRESSION harness, not a pass/fail gate on model quality --
since every fixture output is scripted, a scenario "failing" here means
the DETERMINISTIC pipeline (reconciliation, guardrails, dispatch) handled
a given interpretation/plan incorrectly, not that a model produced a bad
interpretation. That's the correct scope for Phase 2's CI-safe evaluation
suite (master prompt Section 41: "Do not let live API tests become the
ordinary CI test suite") -- a live-model evaluation comparing prompt/model
versions is a separate, explicitly non-CI concern (see
eval/live_smoke.py).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from conversation.semantic_loop import ConversationLoopResult, run_conversation
from eval.scenarios import SCENARIOS, EvalScenario
from guardrails.policy import GuardrailContext
from intelligence.contracts import initial_state
from intelligence.fake_llm import FakeLLMProvider


class _ScriptedProspect:
    def __init__(self, utterances: list[str]) -> None:
        self._utterances = utterances
        self._index = 0

    async def next_utterance(self, state: object) -> str | None:
        if self._index >= len(self._utterances):
            return None
        utterance = self._utterances[self._index]
        self._index += 1
        return utterance


@dataclass(frozen=True)
class DimensionResult:
    dimension: str
    passed: bool
    description: str


@dataclass(frozen=True)
class ScenarioResult:
    scenario_name: str
    category: str
    dimension_results: tuple[DimensionResult, ...]

    @property
    def all_passed(self) -> bool:
        return all(d.passed for d in self.dimension_results)


async def run_scenario(scenario: EvalScenario) -> tuple[ScenarioResult, ConversationLoopResult]:
    turn_index = {"n": 0}

    def interp_source(_turn_input: object):
        turn = scenario.turns[turn_index["n"]]
        return turn.fixture_interpretation

    def plan_source(_ctx: object):
        return scenario.turns[turn_index["n"]].fixture_plan

    def response_source(_obj: str, _facts: tuple[str, ...], _ctx: object) -> str:
        turn = scenario.turns[turn_index["n"]]
        turn_index["n"] += 1
        return turn.fixture_response

    provider = FakeLLMProvider(
        interpretation_source=interp_source, plan_source=plan_source, response_source=response_source
    )
    prospect = _ScriptedProspect([t.prospect_utterance for t in scenario.turns])
    state = initial_state(uuid.uuid4(), objective=scenario.objective, context_version=1)
    guardrail_context = GuardrailContext(
        opted_out=scenario.guardrail_opted_out,
        authorized_tools=(),
        required_tool_arguments={},
        confirmed_terminal_outcomes=scenario.guardrail_confirmed_terminal_outcomes,
    )

    result = await run_conversation(
        provider,
        state,
        prospect,
        call_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        session_id=state.session_id,
        organization_id=1,
        lead_id=1,
        guardrail_context=guardrail_context,
    )

    dimension_results = tuple(
        DimensionResult(dimension=c.dimension, passed=bool(c.check(result)), description=c.description)
        for c in scenario.dimension_checks
    )
    return ScenarioResult(scenario_name=scenario.name, category=scenario.category, dimension_results=dimension_results), result


async def run_all_scenarios() -> list[ScenarioResult]:
    results = []
    for scenario in SCENARIOS:
        result, _loop_result = await run_scenario(scenario)
        results.append(result)
    return results


def print_report(results: list[ScenarioResult]) -> bool:
    """Prints a per-scenario, per-dimension scorecard. Returns True iff
    every scenario passed every dimension."""
    all_passed = True
    for result in results:
        status = "PASS" if result.all_passed else "FAIL"
        print(f"[{status}] {result.scenario_name} ({result.category})")
        for dim in result.dimension_results:
            mark = "  ok" if dim.passed else "  XX"
            print(f"    {mark} {dim.dimension}: {dim.description}")
        if not result.all_passed:
            all_passed = False
    total_dims = sum(len(r.dimension_results) for r in results)
    passed_dims = sum(sum(1 for d in r.dimension_results if d.passed) for r in results)
    print(f"\n{passed_dims}/{total_dims} dimension checks passed across {len(results)} scenarios.")
    return all_passed


if __name__ == "__main__":
    results = asyncio.run(run_all_scenarios())
    ok = print_report(results)
    raise SystemExit(0 if ok else 1)
