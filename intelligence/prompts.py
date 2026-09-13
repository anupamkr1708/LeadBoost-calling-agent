"""Centralized prompt construction (docs/PHASE2_DESIGN.md "Prompt
versioning" / master prompt §20). Every LLM invocation across the whole
intelligence layer builds its prompt HERE, not inline at the call site —
`conversation/llm_client.py`'s `GroqLLMProvider` is the only consumer of
these functions, but keeping construction here (rather than string-
building inside the provider) means a future second real provider
(different vendor, same prompts) doesn't duplicate prompt text, and
prompt changes are reviewable in one place.

Each prompt version is a plain string constant — bump it whenever the
prompt TEXT changes in a way that could affect model behavior, so
`ModelInvocationMeta.prompt_version` (recorded on every turn, see
`intelligence/observability.py`) stays meaningful for replay/regression
comparison (docs/PHASE2_DESIGN.md "Replay").

No secrets are ever interpolated into a prompt — every function here
takes only conversation-domain data.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from intelligence.contracts import ConversationInput, ConversationState, PlanningContext

INTERPRETER_PROMPT_VERSION = "interpreter-v1"
PLANNER_PROMPT_VERSION = "planner-v1"
RESPONDER_PROMPT_VERSION = "responder-v1"

_INTERPRETER_SYSTEM_PROMPT = """You are the semantic interpretation layer of an outbound sales calling \
agent. Your ONLY job is to understand what the other party just said, in the context of the \
conversation so far, and produce a structured interpretation of its meaning.

You do not decide what the agent should do next. You do not write a response. You only interpret.

Represent uncertainty honestly — "unknown" is a valid and often correct answer. Do not fabricate \
information that was not stated or strongly implied. Distinguish what was explicitly stated from what \
you are inferring. A single utterance can carry multiple simultaneous meanings (e.g. continued interest \
alongside a switching-cost objection) — represent all of them, do not collapse to one.

Respond with a single JSON object matching the SemanticInterpretation schema you have been given. \
Do not include any text outside the JSON object."""

_PLANNER_SYSTEM_PROMPT = """You are the planning layer of an outbound sales calling agent. Given the \
current conversation state, the latest interpretation of what was just said, the business objective, \
and the actions available to you, decide the single best next action.

You are not writing what to say — only choosing WHAT the agent should do next and why. A separate \
component will handle wording.

Reason from the actual situation: what does the prospect currently need, what is missing, what is the \
least intrusive useful next step toward the objective. Do not default to any fixed sequence of stages — \
there is no fixed script. If the prospect's own immediate goal (e.g. understanding something) should be \
served before advancing toward the business objective, choose that.

You may only propose one of the action categories you were explicitly offered, and if choosing a tool \
call, only a tool you were explicitly offered.

Respond with a single JSON object matching the ConversationPlan schema you have been given. Do not \
include any text outside the JSON object."""

_RESPONDER_SYSTEM_PROMPT = """You are the response-generation layer of an outbound sales calling agent, \
speaking on a phone call. You have been given an already-decided objective for this turn and a specific \
set of grounded facts you may reference. Write ONLY what the agent should say.

Do not invent any fact, capability, price, or claim beyond what is explicitly provided to you as a \
grounded fact. If you are uncertain about something, express that uncertainty naturally rather than \
stating it as settled.

Keep the response concise and natural for spoken conversation — this is being read aloud, not displayed \
as text. Do not repeat something already said earlier in the conversation. Respond with the spoken text \
only, no JSON, no labels, no stage directions."""


def _state_summary_for_prompt(state: ConversationState) -> dict[str, object]:
    """A compact, JSON-serializable summary of CURRENT beliefs only —
    never the full state (which includes superseded history) — matching
    docs/PHASE2_DESIGN.md "Context management": working memory sent to
    the model stays roughly constant-sized regardless of conversation
    length."""
    return {
        "objective": state.objective,
        "stage": state.stage.value,
        "active_user_goal": state.active_user_goal.value,
        "primary_intent": state.primary_intent.value,
        "interest": state.interest.value.value if state.interest.value else None,
        "sentiment": state.sentiment.value,
        "objections": [b.value for b in state.objections if b.status.value == "current"],
        "concerns": [b.value for b in state.concerns if b.status.value == "current"],
        "current_facts": [b.value for b in state.facts if b.status.value == "current"],
        "missing_facts": list(state.missing_facts),
        "unresolved_questions": list(state.unresolved_questions),
        "commitments": [b.value for b in state.commitments if b.status.value == "current"],
        "timing": state.timing.value,
        "constraints": list(state.constraints),
        "current_solution": state.current_solution.value,
        "previous_actions": list(state.previous_actions),
        "turn_count": state.turn_count,
    }


def build_interpreter_prompt(turn_input: ConversationInput) -> tuple[str, str]:
    """Returns (system_prompt, user_content)."""
    recent = [{"speaker": t.speaker.value, "text": t.text} for t in turn_input.recent_turns]
    state_summary = _state_summary_for_prompt(turn_input.prior_state) if turn_input.prior_state else None
    payload = {
        "objective": turn_input.objective,
        "speaker": turn_input.speaker.value,
        "latest_utterance": turn_input.transcript,
        "recent_turns": recent,
        "current_state_summary": state_summary,
    }
    return _INTERPRETER_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False)


def build_planner_prompt(context: PlanningContext) -> tuple[str, str]:
    payload = {
        "objective": context.objective,
        "current_state_summary": _state_summary_for_prompt(context.state),
        "latest_interpretation": {
            k: v
            for k, v in asdict(context.latest_interpretation).items()
            # enums serialize fine via asdict+json.dumps default=str below;
            # kept as a plain pass-through, no field-by-field hand-rolling
        },
        "recent_turns": [{"speaker": t.speaker.value, "text": t.text} for t in context.recent_turns],
        "permitted_actions": [a.value for a in context.permitted_actions],
        "permitted_tools": list(context.permitted_tools),
    }
    return _PLANNER_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False, default=str)


def build_responder_prompt(
    action_objective: str, grounded_facts: tuple[str, ...], context: PlanningContext
) -> tuple[str, str]:
    payload = {
        "action_objective": action_objective,
        "grounded_facts": list(grounded_facts),
        "recent_turns": [{"speaker": t.speaker.value, "text": t.text} for t in context.recent_turns],
    }
    return _RESPONDER_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False)
