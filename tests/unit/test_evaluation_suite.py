"""Wraps eval/runner.py as a pytest test so the semantic evaluation
dataset runs as part of ordinary CI (docs/PHASE2_DESIGN.md "AI
evaluation" / master prompt Section 30: "Evaluation is not optional") --
distinct from a live-model quality benchmark (eval/live_smoke.py, which
is explicitly NOT part of ordinary CI per master prompt Section 41), this
asserts the DETERMINISTIC pipeline handles every scripted scenario's
fixture outputs correctly, dimension by dimension.
"""
from __future__ import annotations

import pytest

from eval.runner import run_scenario
from eval.scenarios import SCENARIOS


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_passes_every_dimension_check(scenario):
    result, _loop_result = await run_scenario(scenario)
    failed = [d for d in result.dimension_results if not d.passed]
    assert not failed, f"{scenario.name} failed dimensions: {[(d.dimension, d.description) for d in failed]}"


@pytest.mark.asyncio
async def test_every_scenario_has_at_least_one_dimension_check():
    """Guards against a scenario silently having zero checks (which would
    trivially "pass" without testing anything) -- the same "vacuous test"
    concern the layering suite already guards against for itself."""
    for scenario in SCENARIOS:
        assert len(scenario.dimension_checks) >= 1, f"{scenario.name} has no dimension checks"
