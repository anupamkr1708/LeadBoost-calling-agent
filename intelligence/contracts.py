"""Typed contracts for the semantic conversation intelligence layer.

Every dataclass here is the shared vocabulary `intelligence/*` and
`guardrails/*` pass structured data through — nothing in this layer
passes an untyped dict across a module boundary (docs/PHASE2_DESIGN.md
"Contracts"). This is deliberately ONE file: these types are a single
cohesive vocabulary (the turn pipeline's data model), not independent
responsibilities — splitting them into one-file-per-dataclass would be
exactly the "dozens of tiny abstractions with no real responsibility"
the spec explicitly warns against. The *behavior* that operates on these
types (interpreter, reconciler, planner, responder) lives in separate
modules; only the shapes live here.

Design principles encoded in these types, not just described in prose:
- Every field that represents a BELIEF (not raw input) is a `Belief[T]`
  carrying provenance (source, confidence, observed_at turn, status) —
  see "Fact provenance" below. Nothing here silently upgrades an
  inference to a verified fact.
- Unknown is representable everywhere it's legitimate: `Belief.unknown()`,
  optional fields, empty collections. Nothing is forced non-null.
- Confidence is a float in [0, 1] representing MODEL uncertainty, not a
  business threshold — nothing in this module compares it to a cutoff.
  (Guardrails, not contracts, is where any such policy would live, and
  guardrails/policy.py does not do this either — see its docstring.)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

# ============================================================
# Provenance — every durable belief carries this, never bare values.
# ============================================================


class BeliefSource(StrEnum):
    """Where a piece of information came from — never silently converted
    into a higher-trust source than it actually is (docs/PHASE2_DESIGN.md
    "Fact provenance")."""

    LEADBOOST_CONTEXT = "leadboost_context"
    PROSPECT_STATEMENT = "prospect_statement"
    AGENT_STATEMENT = "agent_statement"
    TOOL_RESULT = "tool_result"
    DERIVED_INFERENCE = "derived_inference"
    SYSTEM_METADATA = "system_metadata"


class Certainty(StrEnum):
    """Coarse, model-reported uncertainty — NOT a numeric confidence
    score pretending to be a business threshold. `UNKNOWN` is a first-class
    value here, not an absence of the field."""

    VERIFIED = "verified"  # from LeadBoost context or a tool result, not model-inferred
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    UNKNOWN = "unknown"


class BeliefStatus(StrEnum):
    """Whether a belief is still the system's current understanding.
    Superseded beliefs are RETAINED (not deleted) for provenance/replay —
    see docs/PHASE2_DESIGN.md "State reconciliation"."""

    CURRENT = "current"
    SUPERSEDED = "superseded"
    CONTRADICTED = "contradicted"  # superseded specifically by conflicting evidence, not just an update


@dataclass(frozen=True)
class Belief(Generic[T]):  # noqa: UP046 - PEP 695 generic syntax deliberately not used here; this is the
    # only generic class in the codebase and Generic[T] is the more broadly familiar/portable form for readers
    """A single piece of durable conversational knowledge with provenance.
    This is the ONLY way a fact enters `ConversationState` — there is no
    code path that writes a bare value into state without one of these.
    """

    value: T | None
    source: BeliefSource
    certainty: Certainty
    observed_at_turn: int
    status: BeliefStatus = BeliefStatus.CURRENT
    source_turn_id: uuid.UUID | None = None
    explicit: bool = True  # False = inferred, not directly stated — see "Explicit vs inferred"
    rationale: str | None = None  # brief evidence reference, e.g. quoted fragment or reasoning note

    @staticmethod
    def unknown(source: BeliefSource, observed_at_turn: int) -> Belief[T]:
        return Belief(value=None, source=source, certainty=Certainty.UNKNOWN, observed_at_turn=observed_at_turn)


# ============================================================
# ConversationInput — the strongly typed input for one turn.
# ============================================================


class Speaker(StrEnum):
    PROSPECT = "prospect"
    AGENT = "agent"


@dataclass(frozen=True)
class TranscriptTurn:
    """One line of dialogue — either side. Used both as input (recent
    turns for context) and as the record of what the agent just said."""

    speaker: Speaker
    text: str
    turn_number: int


@dataclass(frozen=True)
class ConversationInput:
    """Everything the interpreter needs for ONE turn. Deliberately
    narrow and typed — no arbitrary dict ever crosses this boundary
    (docs/PHASE2_DESIGN.md "Contract: ConversationInput")."""

    session_id: uuid.UUID
    call_attempt_id: uuid.UUID
    organization_id: int
    lead_id: int
    turn_number: int
    speaker: Speaker
    transcript: str
    prior_state: ConversationState | None
    recent_turns: tuple[TranscriptTurn, ...]
    objective: str
    context_version: int


# ============================================================
# SemanticInterpretation — the interpreter's structured output.
# ============================================================


class SpeechAct(StrEnum):
    STATEMENT = "statement"
    QUESTION = "question"
    OBJECTION = "objection"
    REQUEST = "request"
    COMMITMENT = "commitment"
    ACKNOWLEDGEMENT = "acknowledgement"
    GREETING = "greeting"
    CLOSING = "closing"
    OTHER = "other"


class InterestLevel(StrEnum):
    HIGH = "high"
    CONDITIONAL = "conditional"
    NEUTRAL = "neutral"
    LOW = "low"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Entity:
    """An open-world entity mention — NOT a closed enum. `entity_type` is
    a free-form string (e.g. "software", "person", "date") deliberately,
    per docs/PHASE2_DESIGN.md "Open-world semantics": the ontology for
    control-plane concepts (intent, speech act, interest) is bounded, but
    entities/topics are not forced into a fixed vocabulary."""

    entity_type: str
    value: str
    explicit: bool = True


@dataclass(frozen=True)
class Objection:
    objection_type: str  # open-world, e.g. "switching_risk", "pricing", "timing" — not a closed enum
    explicit: bool
    certainty: Certainty
    rationale: str | None = None


@dataclass(frozen=True)
class SemanticInterpretation:
    """The interpreter's answer to "what does the caller mean?" — rich
    enough to hold multiple simultaneous meanings (docs/PHASE2_DESIGN.md
    "Contract: SemanticInterpretation"). This is NOT the system's current
    belief state — it's evidence from ONE turn; `StateReconciler` is what
    turns a sequence of these into `ConversationState`."""

    primary_intent: str  # open-world string, not a fixed enum — see module docstring
    secondary_intents: tuple[str, ...]
    speech_act: SpeechAct
    user_goal: str | None
    conversation_stage: str  # open-world label the interpreter/planner agree on, not a hardcoded funnel
    interest: InterestLevel
    interest_certainty: Certainty
    sentiment: str | None  # supplementary only — never conflated with intent/interest, see guardrails/planner
    emotion: str | None
    objections: tuple[Objection, ...]
    concerns: tuple[str, ...]
    motivations: tuple[str, ...]
    questions: tuple[str, ...]
    requests: tuple[str, ...]
    commitments: tuple[str, ...]
    timing_signal: str | None
    timing_certainty: Certainty
    urgency: str | None
    budget_signal: str | None
    authority_signal: str | None
    current_solution: str | None
    competitor_mentions: tuple[str, ...]
    pain_points: tuple[str, ...]
    desired_outcomes: tuple[str, ...]
    constraints: tuple[str, ...]
    entities: tuple[Entity, ...]
    new_facts: tuple[str, ...]
    disputed_facts: tuple[str, ...]  # statements that contradict a CURRENT belief — see reconciler
    missing_information: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    implied_meaning: str | None
    confidence: float  # overall interpretation confidence, [0,1] — see module docstring
    uncertainty_notes: tuple[str, ...]
    rationale: str | None = None


# ============================================================
# ConversationState — the system's current structured belief.
# ============================================================


@dataclass(frozen=True)
class ConversationState:
    """The system's CURRENT structured belief about the conversation —
    built by folding each turn's `SemanticInterpretation` through
    `StateReconciler`, never replaced wholesale by a single LLM call
    (docs/PHASE2_DESIGN.md "State reconciliation"). Immutable — every
    reconciliation produces a NEW `ConversationState`, never mutates one
    in place, so a prior state remains a valid, inspectable snapshot for
    replay/audit.
    """

    session_id: uuid.UUID
    turn_count: int
    last_updated_turn: int
    context_version: int
    objective: str
    stage: Belief[str]
    active_user_goal: Belief[str]
    active_agent_goal: Belief[str]
    primary_intent: Belief[str]
    secondary_intents: tuple[str, ...]
    interest: Belief[InterestLevel]
    sentiment: Belief[str]
    objections: tuple[Belief[str], ...]
    concerns: tuple[Belief[str], ...]
    facts: tuple[Belief[str], ...]  # CURRENT facts only; superseded ones live in `fact_history`
    fact_history: tuple[Belief[str], ...]  # superseded/contradicted beliefs, retained for provenance/replay
    missing_facts: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    commitments: tuple[Belief[str], ...]
    requested_follow_up: Belief[str]
    timing: Belief[str]
    constraints: tuple[str, ...]
    entities: tuple[Entity, ...]
    current_solution: Belief[str]
    previous_actions: tuple[str, ...]  # action *categories* taken so far, for the planner's "don't repeat" check
    trajectory: tuple[str, ...]  # short interest/stage labels per turn, e.g. ["low","neutral","conditional"]


def initial_state(session_id: uuid.UUID, objective: str, context_version: int) -> ConversationState:
    """The state a session starts in — every Belief begins UNKNOWN, never
    guessed. `StateReconciler` is the only thing that ever produces a
    successor state from here."""
    return ConversationState(
        session_id=session_id,
        turn_count=0,
        last_updated_turn=0,
        context_version=context_version,
        objective=objective,
        stage=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        active_user_goal=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        active_agent_goal=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        primary_intent=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        secondary_intents=(),
        interest=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        sentiment=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        objections=(),
        concerns=(),
        facts=(),
        fact_history=(),
        missing_facts=(),
        unresolved_questions=(),
        commitments=(),
        requested_follow_up=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        timing=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        constraints=(),
        entities=(),
        current_solution=Belief.unknown(BeliefSource.SYSTEM_METADATA, 0),
        previous_actions=(),
        trajectory=(),
    )


# ============================================================
# Planning
# ============================================================


class ActionCategory(StrEnum):
    """The bounded, deterministic vocabulary of what the agent can DO —
    the planner picks one of these, never a free-form action
    (docs/PHASE2_DESIGN.md "Conversation action"). This is the control
    surface guardrails validate against; it is intentionally closed even
    though intents/topics above are open-world."""

    SPEAK = "speak"
    ASK = "ask"
    TOOL_CALL = "tool_call"
    WAIT = "wait"
    TRANSFER = "transfer"
    END_CALL = "end_call"


@dataclass(frozen=True)
class PlanningContext:
    """Everything the planner reasons from — assembled by
    `intelligence/context.py`, never the raw transcript
    (docs/PHASE2_DESIGN.md "Context management")."""

    state: ConversationState
    latest_interpretation: SemanticInterpretation
    recent_turns: tuple[TranscriptTurn, ...]
    objective: str
    permitted_actions: tuple[ActionCategory, ...]
    permitted_tools: tuple[str, ...]


@dataclass(frozen=True)
class ConversationPlan:
    """The planner's PROPOSAL — not yet authorized. Guardrails decide
    whether this is allowed to become a `ConversationAction`
    (docs/PHASE2_DESIGN.md "Planner")."""

    action_category: ActionCategory
    objective: str  # what this action is meant to accomplish
    rationale: str
    question_objective: str | None = None  # for ASK
    information_needed: str | None = None  # for ASK
    tool_name: str | None = None  # for TOOL_CALL
    tool_arguments: dict[str, str] | None = None  # for TOOL_CALL — string-only, validated by guardrails
    end_reason: str | None = None  # for END_CALL
    terminal_outcome: str | None = None  # for END_CALL, e.g. "meeting_booked", "not_interested", "opted_out"


# ============================================================
# Guardrail decision
# ============================================================


class GuardrailVerdict(StrEnum):
    AUTHORIZED = "authorized"
    REJECTED = "rejected"


@dataclass(frozen=True)
class GuardrailResult:
    verdict: GuardrailVerdict
    reason: str
    violated_policy: str | None = None  # a stable policy identifier, for observability/eval, not prose-only


# ============================================================
# ConversationAction — the authorized, executable action.
# ============================================================


@dataclass(frozen=True)
class ConversationAction:
    """The GUARDRAIL-AUTHORIZED action — this, not `ConversationPlan`, is
    what the responder/runtime actually execute. Structurally identical to
    `ConversationPlan` by design (guardrails authorize or reject, they
    don't rewrite), but a distinct type so "authorized" is something the
    type system can express, not just a runtime flag."""

    action_category: ActionCategory
    objective: str
    rationale: str
    question_objective: str | None = None
    information_needed: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, str] | None = None
    end_reason: str | None = None
    terminal_outcome: str | None = None

    @staticmethod
    def from_plan(plan: ConversationPlan) -> ConversationAction:
        return ConversationAction(
            action_category=plan.action_category,
            objective=plan.objective,
            rationale=plan.rationale,
            question_objective=plan.question_objective,
            information_needed=plan.information_needed,
            tool_name=plan.tool_name,
            tool_arguments=plan.tool_arguments,
            end_reason=plan.end_reason,
            terminal_outcome=plan.terminal_outcome,
        )


# ============================================================
# Response generation output
# ============================================================


@dataclass(frozen=True)
class AgentTurn:
    """The final output of one full pipeline pass: an authorized action,
    realized as natural language (or a non-verbal action with no text,
    e.g. WAIT)."""

    turn_number: int
    action: ConversationAction
    response_text: str | None
    grounded_facts_used: tuple[str, ...]


# ============================================================
# Model invocation metadata — attached to every LLM call for
# observability/replay (docs/PHASE2_DESIGN.md "Prompt versioning").
# ============================================================


@dataclass(frozen=True)
class ModelInvocationMeta:
    model: str
    provider: str
    prompt_version: str
    policy_version: str
    context_version: int
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


TurnKind = Literal["semantic_interpretation", "planning", "response_generation"]
