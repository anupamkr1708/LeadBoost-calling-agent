"""The state reconciler: folds one turn's `SemanticInterpretation` into
the existing `ConversationState`, producing a NEW state
(docs/PHASE2_DESIGN.md "State reconciliation"). This module makes exactly
ONE architectural promise: **it never calls an LLM**. Reconciliation is
deterministic post-processing over already-structured data the
interpreter produced — merging structured beliefs is a different problem
from understanding language, and conflating them back into "just ask the
model again" would silently reopen the keyword/heuristic risk the spec
is entirely about avoiding, just one layer removed.

What "deterministic" means here, precisely, because it's easy to
misread as "does semantic reasoning without a model":

- For NAMED belief fields (current_solution, interest, primary_intent,
  stage, timing) — the reconciler compares the NEW interpretation's
  ALREADY-STRUCTURED value against the EXISTING belief's ALREADY-
  STRUCTURED value for equality. This is safe, ordinary code (an `!=`
  check on two enum/string values the LLM already extracted) — it is
  NOT the "string matching against raw transcript text" the master
  prompt prohibits for contradiction detection. The semantic judgment
  ("does this utterance mean something different from what we already
  believed") was already made by the interpreter; the reconciler's job
  is only to apply that judgment's structural consequence (supersede the
  old belief, promote the new one) consistently.
- For the generic `facts`/`objections`/`concerns`/`commitments` buckets,
  the reconciler does NOT attempt semantic contradiction detection at
  all — new items are appended, existing ones of the same category are
  left alone. Genuine cross-statement contradiction detection in a
  free-form bucket is a real NLP problem; scoping it out here (rather
  than faking it with string similarity) is a deliberate, documented
  limitation (see docs/PHASE2_AUDIT.md), not an oversight.
"""
from __future__ import annotations

from intelligence.contracts import (
    Belief,
    BeliefSource,
    BeliefStatus,
    Certainty,
    ConversationState,
    Entity,
    SemanticInterpretation,
    Speaker,
)


def _speaker_source(speaker: Speaker) -> BeliefSource:
    return BeliefSource.PROSPECT_STATEMENT if speaker == Speaker.PROSPECT else BeliefSource.AGENT_STATEMENT


def _reconcile_named_belief(
    current: Belief[str],
    new_value: str | None,
    *,
    explicit: bool,
    source: BeliefSource,
    certainty: Certainty,
    turn_number: int,
) -> Belief[str]:
    """The one piece of real merge logic in this module — see module
    docstring for exactly what "deterministic" means here. A turn that
    doesn't mention this belief at all (`new_value is None`) is NOT
    evidence that it changed; only a genuinely new, different value
    supersedes the old one."""
    if new_value is None:
        return current
    if current.value == new_value:
        return current  # reconfirmation, not a change — avoid noisy history churn
    return Belief(
        value=new_value,
        source=source,
        certainty=certainty,
        observed_at_turn=turn_number,
        explicit=explicit,
    )


def _superseded(belief: Belief[str]) -> Belief[str]:
    return Belief(
        value=belief.value,
        source=belief.source,
        certainty=belief.certainty,
        observed_at_turn=belief.observed_at_turn,
        status=BeliefStatus.SUPERSEDED,
        source_turn_id=belief.source_turn_id,
        explicit=belief.explicit,
        rationale=belief.rationale,
    )


def _append_belief_bucket(
    existing: tuple[Belief[str], ...],
    new_values: tuple[str, ...],
    *,
    source: BeliefSource,
    certainty: Certainty,
    turn_number: int,
) -> tuple[Belief[str], ...]:
    """Append-only, deduplicated by exact value match among CURRENT
    beliefs in this bucket — not semantic dedup, just "don't add the
    literal same string twice in a row"."""
    existing_current_values = {b.value for b in existing if b.status == BeliefStatus.CURRENT}
    additions = tuple(
        Belief(value=v, source=source, certainty=certainty, observed_at_turn=turn_number)
        for v in new_values
        if v not in existing_current_values
    )
    return existing + additions


def _merge_entities(existing: tuple[Entity, ...], new: tuple[Entity, ...]) -> tuple[Entity, ...]:
    seen = {(e.entity_type, e.value) for e in existing}
    additions = tuple(e for e in new if (e.entity_type, e.value) not in seen)
    return existing + additions


def _merge_strings(existing: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
    seen = set(existing)
    additions = tuple(s for s in new if s not in seen)
    return existing + additions


def _interpretation_certainty_as_certainty(confidence: float) -> Certainty:
    """Buckets the interpreter's numeric confidence into the coarse
    `Certainty` scale used for beliefs DERIVED from this interpretation
    (not a business threshold — see intelligence/contracts.py's module
    docstring on why confidence is never compared to a cutoff for
    control-flow; this bucketing is purely for the Belief's own
    provenance label)."""
    if confidence >= 0.85:
        return Certainty.HIGH
    if confidence >= 0.6:
        return Certainty.MODERATE
    if confidence > 0.0:
        return Certainty.LOW
    return Certainty.UNKNOWN


def reconcile(
    state: ConversationState,
    interpretation: SemanticInterpretation,
    *,
    speaker: Speaker,
    turn_number: int,
) -> ConversationState:
    """The ONE entry point — produces a new `ConversationState`, never
    mutates the input. See module docstring for what's deterministic and
    what's deliberately out of scope.
    """
    source = _speaker_source(speaker)
    certainty = _interpretation_certainty_as_certainty(interpretation.confidence)

    new_stage = _reconcile_named_belief(
        state.stage,
        interpretation.conversation_stage if interpretation.conversation_stage != "unknown" else None,
        explicit=False,
        source=BeliefSource.DERIVED_INFERENCE,
        certainty=certainty,
        turn_number=turn_number,
    )
    new_user_goal = _reconcile_named_belief(
        state.active_user_goal,
        interpretation.user_goal,
        explicit=False,
        source=BeliefSource.DERIVED_INFERENCE,
        certainty=certainty,
        turn_number=turn_number,
    )
    new_primary_intent = _reconcile_named_belief(
        state.primary_intent,
        interpretation.primary_intent if interpretation.primary_intent != "unknown" else None,
        explicit=True,
        source=source,
        certainty=certainty,
        turn_number=turn_number,
    )
    new_current_solution = _reconcile_named_belief(
        state.current_solution,
        interpretation.current_solution,
        explicit=True,
        source=source,
        certainty=certainty,
        turn_number=turn_number,
    )
    new_timing = _reconcile_named_belief(
        state.timing,
        interpretation.timing_signal,
        explicit=True,
        source=source,
        certainty=interpretation.timing_certainty,
        turn_number=turn_number,
    )

    # interest: a Belief[InterestLevel], reconciled the same way but with
    # the enum type rather than str — kept as inline logic since it's the
    # one field with a different value type from _reconcile_named_belief's
    # tuple[str] signature.
    if interpretation.interest.value != "unknown" and state.interest.value != interpretation.interest.value:
        new_interest = Belief(
            value=interpretation.interest,
            source=source,
            certainty=interpretation.interest_certainty,
            observed_at_turn=turn_number,
            explicit=True,
        )
    else:
        new_interest = state.interest

    new_sentiment = _reconcile_named_belief(
        state.sentiment,
        interpretation.sentiment,
        explicit=True,
        source=source,
        certainty=certainty,
        turn_number=turn_number,
    )

    fact_history_additions = tuple(
        _superseded(old)
        for old, new in (
            (state.stage, new_stage),
            (state.active_user_goal, new_user_goal),
            (state.primary_intent, new_primary_intent),
            (state.current_solution, new_current_solution),
            (state.timing, new_timing),
            (state.sentiment, new_sentiment),
        )
        if old is not new and old.status == BeliefStatus.CURRENT and old.value is not None
    )

    new_objections = _append_belief_bucket(
        state.objections,
        tuple(o.objection_type for o in interpretation.objections),
        source=source,
        certainty=certainty,
        turn_number=turn_number,
    )
    new_concerns = _append_belief_bucket(
        state.concerns, interpretation.concerns, source=source, certainty=certainty, turn_number=turn_number
    )
    new_commitments = _append_belief_bucket(
        state.commitments, interpretation.commitments, source=source, certainty=certainty, turn_number=turn_number
    )
    new_facts = _append_belief_bucket(
        state.facts, interpretation.new_facts, source=source, certainty=certainty, turn_number=turn_number
    )

    new_missing_facts = _merge_strings(state.missing_facts, interpretation.missing_information)
    new_unresolved = _merge_strings(state.unresolved_questions, interpretation.unresolved_items)
    new_constraints = _merge_strings(state.constraints, interpretation.constraints)
    new_entities = _merge_entities(state.entities, interpretation.entities)

    interest_label = interpretation.interest.value if interpretation.interest.value != "unknown" else "?"

    return ConversationState(
        session_id=state.session_id,
        turn_count=state.turn_count + 1,
        last_updated_turn=turn_number,
        context_version=state.context_version,
        objective=state.objective,
        stage=new_stage,
        active_user_goal=new_user_goal,
        active_agent_goal=state.active_agent_goal,
        primary_intent=new_primary_intent,
        secondary_intents=interpretation.secondary_intents,  # transient per-turn signal, not accumulated
        interest=new_interest,
        sentiment=new_sentiment,
        objections=new_objections,
        concerns=new_concerns,
        facts=new_facts,
        fact_history=state.fact_history + fact_history_additions,
        missing_facts=new_missing_facts,
        unresolved_questions=new_unresolved,
        commitments=new_commitments,
        requested_follow_up=state.requested_follow_up,
        timing=new_timing,
        constraints=new_constraints,
        entities=new_entities,
        current_solution=new_current_solution,
        previous_actions=state.previous_actions,
        trajectory=state.trajectory + (interest_label,),
    )


def record_action_taken(state: ConversationState, action_category: str) -> ConversationState:
    """Called by the runtime after an action is authorized and executed —
    NOT part of `reconcile` itself, since recording "what we did" is a
    runtime-driven fact, not something derived from interpreting the
    caller's speech."""
    return ConversationState(
        session_id=state.session_id,
        turn_count=state.turn_count,
        last_updated_turn=state.last_updated_turn,
        context_version=state.context_version,
        objective=state.objective,
        stage=state.stage,
        active_user_goal=state.active_user_goal,
        active_agent_goal=state.active_agent_goal,
        primary_intent=state.primary_intent,
        secondary_intents=state.secondary_intents,
        interest=state.interest,
        sentiment=state.sentiment,
        objections=state.objections,
        concerns=state.concerns,
        facts=state.facts,
        fact_history=state.fact_history,
        missing_facts=state.missing_facts,
        unresolved_questions=state.unresolved_questions,
        commitments=state.commitments,
        requested_follow_up=state.requested_follow_up,
        timing=state.timing,
        constraints=state.constraints,
        entities=state.entities,
        current_solution=state.current_solution,
        previous_actions=state.previous_actions + (action_category,),
        trajectory=state.trajectory,
    )
