"""The REAL `LLMProvider` implementation, backed by Groq. This is the
ONLY module in the whole repository permitted to import the `groq` SDK —
enforced by `app/layers.py`'s pre-existing vendor-confinement rule (this
seam was reserved before Phase 2 started) and
`tests/layering/test_import_boundaries.py`. `intelligence/interpreter.py`,
`planner.py`, and `responder.py` depend only on
`intelligence.llm_provider.LLMProvider` and never import this module or
`groq` directly — the composition root (`app/main.py`) is the only place
that constructs a `GroqLLMProvider` and injects it, exactly mirroring how
`telephony/fake.py` vs. a future real telephony adapter would work
(docs/PHASE2_DESIGN.md "Model boundary").

Parsing is STRICT: a response that doesn't match the expected JSON shape
raises `LLMResponseParsingError` rather than silently degrading to
"unknown" — "LLM proposes, runtime validates" means a validation failure
is a real, surfaced failure, not something this module smooths over. The
caller (`conversation/semantic_loop.py`) is responsible for deciding this
is a `FailureCategory.TRANSIENT_INFRA`-class failure and routing it
through the SAME retry policy Phase 1 already has — no new failure-
handling machinery for Phase 2.
"""
from __future__ import annotations

import json
import time
from typing import Any

from groq import AsyncGroq

from intelligence.contracts import (
    ActionCategory,
    Certainty,
    ConversationInput,
    ConversationPlan,
    Entity,
    InterestLevel,
    ModelInvocationMeta,
    Objection,
    PlanningContext,
    SemanticInterpretation,
    SpeechAct,
)
from intelligence.llm_provider import InterpretationResult, PlanningResult, ResponseResult
from intelligence.prompts import (
    INTERPRETER_PROMPT_VERSION,
    PLANNER_PROMPT_VERSION,
    RESPONDER_PROMPT_VERSION,
    build_interpreter_prompt,
    build_planner_prompt,
    build_responder_prompt,
)

DEFAULT_MODEL = "llama-3.3-70b-versatile"
POLICY_VERSION = "llm-client-v1"


class LLMResponseParsingError(ValueError):
    """The model's response could not be parsed into the expected typed
    contract. A real, surfaced failure — see module docstring."""


def _get_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _get_str_required(data: dict[str, Any], key: str, default: str) -> str:
    value = _get_str(data, key)
    return value if value is not None else default


def _get_str_tuple(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v.strip())


def _get_float(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _parse_enum(enum_cls: type, data: dict[str, Any], key: str, default: Any) -> Any:
    value = data.get(key)
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    return default


def _parse_objections(data: dict[str, Any]) -> tuple[Objection, ...]:
    raw = data.get("objections")
    if not isinstance(raw, list):
        return ()
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        objection_type = _get_str(item, "objection_type") or _get_str(item, "type")
        if objection_type is None:
            continue
        result.append(
            Objection(
                objection_type=objection_type,
                explicit=bool(item.get("explicit", True)),
                certainty=_parse_enum(Certainty, item, "certainty", Certainty.UNKNOWN),
                rationale=_get_str(item, "rationale"),
            )
        )
    return tuple(result)


def _parse_entities(data: dict[str, Any]) -> tuple[Entity, ...]:
    raw = data.get("entities")
    if not isinstance(raw, list):
        return ()
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entity_type = _get_str(item, "entity_type") or _get_str(item, "type")
        value = _get_str(item, "value")
        if entity_type is None or value is None:
            continue
        result.append(Entity(entity_type=entity_type, value=value, explicit=bool(item.get("explicit", True))))
    return tuple(result)


def parse_semantic_interpretation(raw_json: str) -> SemanticInterpretation:
    """Strict parse: raises `LLMResponseParsingError` if `raw_json` isn't
    even valid JSON or isn't a JSON object — beyond that, every field is
    defensively defaulted to its "unknown" representation rather than
    raising per-field, since a model omitting an optional field (as
    opposed to returning structurally invalid output) is expected,
    routine behavior, not a parsing failure."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise LLMResponseParsingError(f"interpreter response was not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMResponseParsingError(f"interpreter response was not a JSON object: {raw_json[:200]!r}")

    return SemanticInterpretation(
        primary_intent=_get_str_required(data, "primary_intent", "unknown"),
        secondary_intents=_get_str_tuple(data, "secondary_intents"),
        speech_act=_parse_enum(SpeechAct, data, "speech_act", SpeechAct.OTHER),
        user_goal=_get_str(data, "user_goal"),
        conversation_stage=_get_str_required(data, "conversation_stage", "unknown"),
        interest=_parse_enum(InterestLevel, data, "interest", InterestLevel.UNKNOWN),
        interest_certainty=_parse_enum(Certainty, data, "interest_certainty", Certainty.UNKNOWN),
        sentiment=_get_str(data, "sentiment"),
        emotion=_get_str(data, "emotion"),
        objections=_parse_objections(data),
        concerns=_get_str_tuple(data, "concerns"),
        motivations=_get_str_tuple(data, "motivations"),
        questions=_get_str_tuple(data, "questions"),
        requests=_get_str_tuple(data, "requests"),
        commitments=_get_str_tuple(data, "commitments"),
        timing_signal=_get_str(data, "timing_signal"),
        timing_certainty=_parse_enum(Certainty, data, "timing_certainty", Certainty.UNKNOWN),
        urgency=_get_str(data, "urgency"),
        budget_signal=_get_str(data, "budget_signal"),
        authority_signal=_get_str(data, "authority_signal"),
        current_solution=_get_str(data, "current_solution"),
        competitor_mentions=_get_str_tuple(data, "competitor_mentions"),
        pain_points=_get_str_tuple(data, "pain_points"),
        desired_outcomes=_get_str_tuple(data, "desired_outcomes"),
        constraints=_get_str_tuple(data, "constraints"),
        entities=_parse_entities(data),
        new_facts=_get_str_tuple(data, "new_facts"),
        disputed_facts=_get_str_tuple(data, "disputed_facts"),
        missing_information=_get_str_tuple(data, "missing_information"),
        unresolved_items=_get_str_tuple(data, "unresolved_items"),
        implied_meaning=_get_str(data, "implied_meaning"),
        confidence=_get_float(data, "confidence", 0.0),
        uncertainty_notes=_get_str_tuple(data, "uncertainty_notes"),
        rationale=_get_str(data, "rationale"),
    )


def parse_conversation_plan(raw_json: str) -> ConversationPlan:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise LLMResponseParsingError(f"planner response was not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMResponseParsingError(f"planner response was not a JSON object: {raw_json[:200]!r}")

    action_category = _parse_enum(ActionCategory, data, "action_category", None)
    if action_category is None:
        raise LLMResponseParsingError(f"planner response has an invalid or missing action_category: {data!r}")

    tool_arguments = data.get("tool_arguments")
    if not isinstance(tool_arguments, dict):
        tool_arguments = None
    else:
        tool_arguments = {str(k): str(v) for k, v in tool_arguments.items()}

    return ConversationPlan(
        action_category=action_category,
        objective=_get_str_required(data, "objective", ""),
        rationale=_get_str_required(data, "rationale", ""),
        question_objective=_get_str(data, "question_objective"),
        information_needed=_get_str(data, "information_needed"),
        tool_name=_get_str(data, "tool_name"),
        tool_arguments=tool_arguments,
        end_reason=_get_str(data, "end_reason"),
        terminal_outcome=_get_str(data, "terminal_outcome"),
    )


class GroqLLMProvider:
    """Implements `intelligence.llm_provider.LLMProvider`. Constructed
    once by the composition root with a real API key; never constructed
    by `intelligence/*` itself."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._client = AsyncGroq(api_key=api_key)
        self._model = model

    async def _call(
        self, system_prompt: str, user_content: str, *, json_mode: bool
    ) -> tuple[str, float, int | None, int | None]:
        start = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"} if json_mode else None,
            temperature=0.2,
        )
        latency_ms = (time.monotonic() - start) * 1000
        content = response.choices[0].message.content or ""
        prompt_tokens = getattr(response.usage, "prompt_tokens", None) if response.usage else None
        completion_tokens = getattr(response.usage, "completion_tokens", None) if response.usage else None
        return content, latency_ms, prompt_tokens, completion_tokens

    async def interpret(self, turn_input: ConversationInput) -> InterpretationResult:
        system_prompt, user_content = build_interpreter_prompt(turn_input)
        content, latency_ms, prompt_tokens, completion_tokens = await self._call(
            system_prompt, user_content, json_mode=True
        )
        interpretation = parse_semantic_interpretation(content)
        meta = ModelInvocationMeta(
            model=self._model,
            provider="groq",
            prompt_version=INTERPRETER_PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            context_version=turn_input.context_version,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return InterpretationResult(interpretation=interpretation, meta=meta)

    async def plan(self, context: PlanningContext) -> PlanningResult:
        system_prompt, user_content = build_planner_prompt(context)
        content, latency_ms, prompt_tokens, completion_tokens = await self._call(
            system_prompt, user_content, json_mode=True
        )
        plan = parse_conversation_plan(content)
        meta = ModelInvocationMeta(
            model=self._model,
            provider="groq",
            prompt_version=PLANNER_PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            context_version=context.state.context_version,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return PlanningResult(plan=plan, meta=meta)

    async def generate_response(
        self, action_objective: str, grounded_facts: tuple[str, ...], context: PlanningContext
    ) -> ResponseResult:
        system_prompt, user_content = build_responder_prompt(action_objective, grounded_facts, context)
        content, latency_ms, prompt_tokens, completion_tokens = await self._call(
            system_prompt, user_content, json_mode=False
        )
        meta = ModelInvocationMeta(
            model=self._model,
            provider="groq",
            prompt_version=RESPONDER_PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            context_version=context.state.context_version,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return ResponseResult(response_text=content.strip(), meta=meta)
