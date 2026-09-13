"""Unit tests for intelligence/state.py's reconciler — pure, no DB/Redis/LLM.
Exercises the specific scenarios docs/PHASE2_DESIGN.md calls out: evidence
preservation across turns, explicit correction/supersession, and the
"a turn that doesn't mention X isn't evidence X changed" rule.
"""
from __future__ import annotations

import uuid

from intelligence.contracts import (
    BeliefSource,
    BeliefStatus,
    Certainty,
    Entity,
    InterestLevel,
    Objection,
    SemanticInterpretation,
    Speaker,
    SpeechAct,
    initial_state,
)
from intelligence.state import reconcile

SESSION_ID = uuid.uuid4()


def _blank_interpretation(**overrides) -> SemanticInterpretation:
    defaults = dict(
        primary_intent="unknown",
        secondary_intents=(),
        speech_act=SpeechAct.STATEMENT,
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
        confidence=0.8,
        uncertainty_notes=(),
    )
    defaults.update(overrides)
    return SemanticInterpretation(**defaults)  # type: ignore[arg-type]  # known mypy limitation: dict-unpack into a dataclass with a broad-union value type cannot be statically verified, even though every value is runtime-correct


def _initial():
    return initial_state(SESSION_ID, objective="book_meeting", context_version=1)


class TestBasicAccumulation:
    def test_new_fact_is_added_as_current_belief(self):
        state = _initial()
        interp = _blank_interpretation(new_facts=("company_size=200",))
        result = reconcile(state, interp, speaker=Speaker.PROSPECT, turn_number=1)
        assert len(result.facts) == 1
        assert result.facts[0].value == "company_size=200"
        assert result.facts[0].status == BeliefStatus.CURRENT
        assert result.facts[0].source == BeliefSource.PROSPECT_STATEMENT

    def test_turn_count_and_last_updated_turn_advance(self):
        state = _initial()
        result = reconcile(state, _blank_interpretation(), speaker=Speaker.PROSPECT, turn_number=3)
        assert result.turn_count == 1
        assert result.last_updated_turn == 3

    def test_trajectory_accumulates_interest_labels(self):
        state = _initial()
        s1 = reconcile(
            state,
            _blank_interpretation(interest=InterestLevel.LOW, interest_certainty=Certainty.HIGH),
            speaker=Speaker.PROSPECT,
            turn_number=1,
        )
        s2 = reconcile(
            s1,
            _blank_interpretation(interest=InterestLevel.CONDITIONAL, interest_certainty=Certainty.MODERATE),
            speaker=Speaker.PROSPECT,
            turn_number=2,
        )
        assert s2.trajectory == ("low", "conditional")


class TestNamedBeliefSupersession:
    def test_explicit_correction_supersedes_old_belief_and_retains_history(self):
        state = _initial()
        s1 = reconcile(
            state, _blank_interpretation(current_solution="Salesforce"), speaker=Speaker.PROSPECT, turn_number=2
        )
        assert s1.current_solution.value == "Salesforce"
        assert s1.current_solution.status == BeliefStatus.CURRENT
        assert len(s1.fact_history) == 0

        s2 = reconcile(
            s1,
            _blank_interpretation(current_solution="none (switched off Salesforce last quarter)"),
            speaker=Speaker.PROSPECT,
            turn_number=8,
        )
        assert s2.current_solution.value == "none (switched off Salesforce last quarter)"
        assert s2.current_solution.status == BeliefStatus.CURRENT
        # the old belief must be retained, not deleted, and marked superseded
        superseded_solutions = [b for b in s2.fact_history if b.value == "Salesforce"]
        assert len(superseded_solutions) == 1
        assert superseded_solutions[0].status == BeliefStatus.SUPERSEDED

    def test_turn_not_mentioning_a_belief_does_not_erase_it(self):
        """The specific rule from docs/PHASE2_DESIGN.md: a turn that says
        nothing about current_solution must not be treated as evidence
        that it changed (or unset it)."""
        state = _initial()
        s1 = reconcile(
            state, _blank_interpretation(current_solution="Salesforce"), speaker=Speaker.PROSPECT, turn_number=1
        )
        s2 = reconcile(
            s1, _blank_interpretation(current_solution=None), speaker=Speaker.PROSPECT, turn_number=2
        )
        assert s2.current_solution.value == "Salesforce"
        assert s2.current_solution.observed_at_turn == 1  # unchanged, not bumped by the silent turn

    def test_reconfirming_the_same_value_does_not_create_history_noise(self):
        state = _initial()
        s1 = reconcile(
            state, _blank_interpretation(current_solution="Salesforce"), speaker=Speaker.PROSPECT, turn_number=1
        )
        s2 = reconcile(
            s1, _blank_interpretation(current_solution="Salesforce"), speaker=Speaker.PROSPECT, turn_number=5
        )
        assert s2.current_solution.observed_at_turn == 1  # same belief object, not "updated"
        assert len(s2.fact_history) == 0


class TestInterestPreservesNuance:
    def test_conditional_interest_is_not_collapsed_by_a_neutral_turn(self):
        """docs/PHASE2_DESIGN.md's core example: 'we're evaluating but
        switching would be painful' must not just become interest=low."""
        state = _initial()
        s1 = reconcile(
            state,
            _blank_interpretation(interest=InterestLevel.CONDITIONAL, interest_certainty=Certainty.HIGH),
            speaker=Speaker.PROSPECT,
            turn_number=1,
        )
        assert s1.interest.value == InterestLevel.CONDITIONAL
        # a later turn with UNKNOWN interest (e.g. a pure factual answer)
        # must not silently erase the established interest level.
        s2 = reconcile(s1, _blank_interpretation(), speaker=Speaker.PROSPECT, turn_number=2)
        assert s2.interest.value == InterestLevel.CONDITIONAL


class TestObjectionsConcernsAccumulate:
    def test_objections_accumulate_across_turns(self):
        state = _initial()
        s1 = reconcile(
            state,
            _blank_interpretation(
                objections=(Objection(objection_type="switching_risk", explicit=False, certainty=Certainty.MODERATE),)
            ),
            speaker=Speaker.PROSPECT,
            turn_number=1,
        )
        s2 = reconcile(
            s1,
            _blank_interpretation(
                objections=(Objection(objection_type="pricing", explicit=True, certainty=Certainty.HIGH),)
            ),
            speaker=Speaker.PROSPECT,
            turn_number=3,
        )
        current_objection_types = {b.value for b in s2.objections if b.status == BeliefStatus.CURRENT}
        assert current_objection_types == {"switching_risk", "pricing"}

    def test_repeating_the_same_objection_type_does_not_duplicate(self):
        state = _initial()
        interp = _blank_interpretation(
            objections=(Objection(objection_type="pricing", explicit=True, certainty=Certainty.HIGH),)
        )
        s1 = reconcile(state, interp, speaker=Speaker.PROSPECT, turn_number=1)
        s2 = reconcile(s1, interp, speaker=Speaker.PROSPECT, turn_number=2)
        assert len(s2.objections) == 1


class TestOpenWorldFields:
    def test_entities_are_deduplicated_by_type_and_value(self):
        state = _initial()
        interp = _blank_interpretation(entities=(Entity(entity_type="software", value="Salesforce"),))
        s1 = reconcile(state, interp, speaker=Speaker.PROSPECT, turn_number=1)
        s2 = reconcile(s1, interp, speaker=Speaker.PROSPECT, turn_number=2)
        assert s2.entities == (Entity(entity_type="software", value="Salesforce"),)

    def test_missing_information_and_unresolved_items_merge_without_duplication(self):
        state = _initial()
        s1 = reconcile(
            state,
            _blank_interpretation(missing_information=("budget",), unresolved_items=("who decides",)),
            speaker=Speaker.PROSPECT,
            turn_number=1,
        )
        s2 = reconcile(
            s1,
            _blank_interpretation(missing_information=("budget", "timeline"), unresolved_items=("who decides",)),
            speaker=Speaker.PROSPECT,
            turn_number=2,
        )
        assert s2.missing_facts == ("budget", "timeline")
        assert s2.unresolved_questions == ("who decides",)


class TestImmutability:
    def test_reconcile_never_mutates_the_input_state(self):
        state = _initial()
        original_facts = state.facts
        reconcile(state, _blank_interpretation(new_facts=("x",)), speaker=Speaker.PROSPECT, turn_number=1)
        assert state.facts is original_facts
        assert state.facts == ()
