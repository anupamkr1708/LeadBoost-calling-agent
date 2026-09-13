"""The LLM provider boundary — a `Protocol`, exactly mirroring
`telephony/contracts.py`'s pattern for the same reason: an adapter has no
framework to inherit from, just a shape to match, and swapping the real
implementation in never touches the code that calls it.

`intelligence/*` (interpreter, planner, responder) depends only on
`LLMProvider` here, never on a concrete provider or an SDK — enforced by
`app/layers.py`'s existing vendor-confinement rule (Groq is confined to
`conversation/llm_client.py`, which implements this Protocol) and by
`tests/layering/test_import_boundaries.py`'s new intelligence-layer rules.

Three distinct call shapes, not one generic "chat" method — matching the
three distinct responsibilities in the turn pipeline
(docs/PHASE2_DESIGN.md "Distinguish interpretation from decision"): a
single `complete(prompt) -> text` method would make it easy to blur
interpretation/planning/response-generation back into one giant prompt,
exactly what the spec warns against. Each method takes and returns a
typed contract from `intelligence/contracts.py`, never a raw dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from intelligence.contracts import (
    ConversationInput,
    ConversationPlan,
    ModelInvocationMeta,
    PlanningContext,
    SemanticInterpretation,
)


@dataclass(frozen=True)
class InterpretationResult:
    interpretation: SemanticInterpretation
    meta: ModelInvocationMeta


@dataclass(frozen=True)
class PlanningResult:
    plan: ConversationPlan
    meta: ModelInvocationMeta


@dataclass(frozen=True)
class ResponseResult:
    response_text: str
    meta: ModelInvocationMeta


class LLMProvider(Protocol):
    """Implemented by `intelligence/fake_llm.py`'s `FakeLLMProvider`
    (fixture-based, zero heuristics — see that module's docstring for why
    that distinction is load-bearing) and by
    `conversation/llm_client.py`'s `GroqLLMProvider` (the real adapter).
    The SAME `intelligence/interpreter.py` / `planner.py` / `responder.py`
    code runs against either, unmodified — docs/PHASE2_DESIGN.md "Model
    boundary".
    """

    async def interpret(self, turn_input: ConversationInput) -> InterpretationResult:
        """Answers "what does the caller mean?" — see
        `intelligence/interpreter.py`."""
        ...

    async def plan(self, context: PlanningContext) -> PlanningResult:
        """Answers "what should happen next?" — see
        `intelligence/planner.py`."""
        ...

    async def generate_response(
        self, action_objective: str, grounded_facts: tuple[str, ...], context: PlanningContext
    ) -> ResponseResult:
        """Answers "how should that be expressed?" — see
        `intelligence/responder.py`. Takes ONLY the already-authorized
        action's objective and explicitly grounded facts, never the raw
        plan or state — the responder cannot invent facts it wasn't
        handed (docs/PHASE2_DESIGN.md "Response generation")."""
        ...
