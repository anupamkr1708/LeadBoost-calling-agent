"""Unit tests for conversation/runtime.py driven by telephony/fake.py.
Pure asyncio, no DB, no Redis — this is exactly what
docs/PHASE1_DESIGN.md's "Testing strategy" means by using the fake
provider to exercise real production code paths without needing
infrastructure for the parts of the flow that don't need it."""
from __future__ import annotations

import uuid

import pytest

from conversation.runtime import execute_call_attempt
from telephony.contracts import CallAttemptContext, FailureCategory
from telephony.fake import FakeTelephonyProvider, Scenario

CONTEXT = CallAttemptContext(call_attempt_id=uuid.uuid4(), attempt_number=1)


@pytest.mark.asyncio
async def test_success_scenario_yields_completed_outcome():
    provider = FakeTelephonyProvider(scenario_source=lambda _: Scenario.SUCCESS)
    result = await execute_call_attempt(CONTEXT, provider, timeout_seconds=5.0)
    assert result.outcome == "completed"
    assert result.failure_category is None
    event_types = [e.event_type for e in result.events]
    assert event_types[0] == "session_started"
    assert event_types[-1] == "session_ended"
    assert "connected" in event_types
    assert "completed" in event_types


@pytest.mark.parametrize(
    "scenario,expected_category",
    [
        (Scenario.BUSY, FailureCategory.PROVIDER),
        (Scenario.NO_ANSWER, FailureCategory.PROVIDER),
        (Scenario.PROVIDER_FAILURE, FailureCategory.PROVIDER),
        (Scenario.CANCELLATION, FailureCategory.CANCELLATION),
    ],
)
@pytest.mark.asyncio
async def test_failure_scenarios_yield_failed_outcome_with_correct_category(scenario, expected_category):
    provider = FakeTelephonyProvider(scenario_source=lambda _: scenario)
    result = await execute_call_attempt(CONTEXT, provider, timeout_seconds=5.0)
    assert result.outcome == "failed"
    assert result.failure_category == expected_category


@pytest.mark.asyncio
async def test_timeout_scenario_yields_timeout_failure_not_a_hang():
    provider = FakeTelephonyProvider(scenario_source=lambda _: Scenario.TIMEOUT)
    result = await execute_call_attempt(CONTEXT, provider, timeout_seconds=0.05)
    assert result.outcome == "failed"
    assert result.failure_category == FailureCategory.TIMEOUT
    assert result.disposition == "timeout"


@pytest.mark.asyncio
async def test_events_are_strictly_ordered_and_start_end_bracketed():
    provider = FakeTelephonyProvider(scenario_source=lambda _: Scenario.SUCCESS)
    result = await execute_call_attempt(CONTEXT, provider, timeout_seconds=5.0)
    assert result.events[0].event_type == "session_started"
    assert result.events[-1].event_type == "session_ended"
    # dialing must precede ringing must precede connected must precede completed
    order = [e.event_type for e in result.events]
    assert order.index("dialing") < order.index("ringing") < order.index("connected") < order.index("completed")


@pytest.mark.asyncio
async def test_genuine_external_cancellation_propagates_and_is_not_swallowed():
    """This is the specific bug class fixed mid-implementation (see
    telephony/fake.py's comments): a genuine asyncio.CancelledError from
    an outer task.cancel() (simulating worker shutdown) must NOT be caught
    and converted into a normal ExecutionResult — that would corrupt
    graceful shutdown's guarantee that a cancelled attempt is left RUNNING
    for the reaper to recover, not silently marked failed."""
    provider = FakeTelephonyProvider(scenario_source=lambda _: Scenario.SUCCESS, step_delay_seconds=10.0)

    async def _run():
        return await execute_call_attempt(CONTEXT, provider, timeout_seconds=30.0)

    import asyncio

    task = asyncio.ensure_future(_run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_scenario_source_receives_the_actual_context_not_a_hardcoded_value():
    """Regression guard for the "no if lead_id == demo" requirement: the
    scenario source must be a real injection point, not decoration — this
    test would fail if execute_call_attempt ever hardcoded a scenario
    instead of asking the provider."""
    seen_contexts = []

    def source(context):
        seen_contexts.append(context)
        return Scenario.SUCCESS

    provider = FakeTelephonyProvider(scenario_source=source)
    specific_context = CallAttemptContext(call_attempt_id=uuid.uuid4(), attempt_number=7)
    await execute_call_attempt(specific_context, provider, timeout_seconds=5.0)
    assert seen_contexts == [specific_context]
