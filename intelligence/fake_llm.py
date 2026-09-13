"""A deterministic FIXTURE provider — not a fake semantic engine.

This is the single most important file for keeping Phase 2's tests
honest (docs/PHASE2_DESIGN.md "Fake LLM" / master prompt §19, §42's "do
not let 'all tests pass' actually mean 'the API key happened to work'").
`FakeLLMProvider` NEVER inspects `turn_input.transcript` and NEVER
branches on its content — every method just calls a caller-supplied
source function and returns whatever it says, verbatim. If a test wants
"the fake provider returns X when the transcript contains Y", that
branching logic lives in the TEST's own source function, in the TEST
file, not here — keeping this file itself provably free of the keyword
heuristics the spec prohibits (checked mechanically, not just by
convention: `tests/unit/test_fake_llm_provider.py` asserts this
provider's own source code contains no transcript/text inspection).

Default sources return `Certainty.UNKNOWN` / empty collections — "unknown
is a valid result" (docs/PHASE2_DESIGN.md), never a fabricated guess —
and, notably, default sources don't even look at their input to decide
that: the default IS the same value regardless of what's asked, which is
the strongest form of "not classifying by content" this fixture can
demonstrate.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from intelligence.contracts import (
    Certainty,
    ConversationInput,
    ConversationPlan,
    InterestLevel,
    ModelInvocationMeta,
    PlanningContext,
    SemanticInterpretation,
    SpeechAct,
)
from intelligence.llm_provider import InterpretationResult, PlanningResult, ResponseResult

InterpretationSource = Callable[[ConversationInput], SemanticInterpretation]
PlanSource = Callable[[PlanningContext], ConversationPlan]
ResponseSource = Callable[[str, tuple[str, ...], PlanningContext], str]


def _default_interpretation(turn_input: ConversationInput) -> SemanticInterpretation:
    """Deliberately ignores `turn_input` entirely — the point is that the
    default doesn't need to look at anything to be a valid "unknown"
    result, which is exactly the property that makes this NOT a
    heuristic."""
    return SemanticInterpretation(
        primary_intent="unknown",
        secondary_intents=(),
        speech_act=SpeechAct.OTHER,
        user_goal=None,
        conversation_stage="unknown",
        interest=InterestLevel.UNKNOWN,
        interest_certainty=Certainty.UNKNOWN,
        sentiment=None,
        emotion=None,
        objections=(),
        concerns=(),
        motivations=(),
        questions=(),
        requests=(),
        commitments=(),
        timing_signal=None,
        timing_certainty=Certainty.UNKNOWN,
        urgency=None,
        budget_signal=None,
        authority_signal=None,
        current_solution=None,
        competitor_mentions=(),
        pain_points=(),
        desired_outcomes=(),
        constraints=(),
        entities=(),
        new_facts=(),
        disputed_facts=(),
        missing_information=(),
        unresolved_items=(),
        implied_meaning=None,
        confidence=0.0,
        uncertainty_notes=("no fixture configured for this test",),
    )


def _default_plan(context: PlanningContext) -> ConversationPlan:
    from intelligence.contracts import ActionCategory

    return ConversationPlan(
        action_category=ActionCategory.WAIT,
        objective="no fixture configured",
        rationale="FakeLLMProvider default: no PlanSource supplied",
    )


def _default_response(action_objective: str, grounded_facts: tuple[str, ...], context: PlanningContext) -> str:
    return ""


class FakeLLMProvider:
    """Implements `intelligence.llm_provider.LLMProvider`. Construct with
    one or more of `interpretation_source` / `plan_source` /
    `response_source` — plain callables, not a fixture DSL, so tests can
    use a `functools.partial`, a closure, a dict lookup keyed by
    `turn_input.turn_number`, or anything else that's convenient and
    still lives entirely in the test file.
    """

    def __init__(
        self,
        interpretation_source: InterpretationSource = _default_interpretation,
        plan_source: PlanSource = _default_plan,
        response_source: ResponseSource = _default_response,
        model_name: str = "fake-llm",
        prompt_version: str = "fake-v1",
        policy_version: str = "fake-policy-v1",
        latency_ms: float = 0.0,
    ) -> None:
        self._interpretation_source = interpretation_source
        self._plan_source = plan_source
        self._response_source = response_source
        self._model_name = model_name
        self._prompt_version = prompt_version
        self._policy_version = policy_version
        self._latency_ms = latency_ms

    def _meta(self, context_version: int, start: float) -> ModelInvocationMeta:
        elapsed_ms = self._latency_ms if self._latency_ms else (time.monotonic() - start) * 1000
        return ModelInvocationMeta(
            model=self._model_name,
            provider="fake",
            prompt_version=self._prompt_version,
            policy_version=self._policy_version,
            context_version=context_version,
            latency_ms=elapsed_ms,
        )

    async def interpret(self, turn_input: ConversationInput) -> InterpretationResult:
        start = time.monotonic()
        interpretation = self._interpretation_source(turn_input)
        return InterpretationResult(interpretation=interpretation, meta=self._meta(turn_input.context_version, start))

    async def plan(self, context: PlanningContext) -> PlanningResult:
        start = time.monotonic()
        plan = self._plan_source(context)
        return PlanningResult(plan=plan, meta=self._meta(context.state.context_version, start))

    async def generate_response(
        self, action_objective: str, grounded_facts: tuple[str, ...], context: PlanningContext
    ) -> ResponseResult:
        start = time.monotonic()
        text = self._response_source(action_objective, grounded_facts, context)
        return ResponseResult(response_text=text, meta=self._meta(context.state.context_version, start))
