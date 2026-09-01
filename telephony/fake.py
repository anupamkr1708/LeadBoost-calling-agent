"""A realistic, deterministic fake telephony adapter — the ONLY provider
Phase 1 implements (docs/PHASE1_DESIGN.md "Fake telephony provider"). It
exercises real production code paths (the runtime reacts generically to
whatever `ProviderEvent`s come back) while letting tests configure exactly
which scenario each call takes, entirely from the outside — no
`if lead_id == "demo"` anywhere in this file or in any caller of it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from enum import StrEnum

from telephony.contracts import CallAttemptContext, FailureCategory, ProviderEvent, ProviderEventType


class Scenario(StrEnum):
    SUCCESS = "success"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    PROVIDER_FAILURE = "provider_failure"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"


# `(context) -> Scenario` — callers (tests, or a future Phase configuring
# canary traffic) supply this. The default always succeeds, which is the
# right default for "the runtime works" tests that aren't exercising
# failure handling.
ScenarioSource = Callable[[CallAttemptContext], Scenario]


def _always_success(_context: CallAttemptContext) -> Scenario:
    return Scenario.SUCCESS


_SCENARIO_TERMINAL_EVENT: dict[Scenario, ProviderEvent] = {
    Scenario.SUCCESS: ProviderEvent(type=ProviderEventType.COMPLETED),
    Scenario.BUSY: ProviderEvent(type=ProviderEventType.BUSY, failure_category=FailureCategory.PROVIDER),
    Scenario.NO_ANSWER: ProviderEvent(
        type=ProviderEventType.NO_ANSWER, failure_category=FailureCategory.PROVIDER
    ),
    Scenario.PROVIDER_FAILURE: ProviderEvent(
        type=ProviderEventType.FAILED,
        failure_category=FailureCategory.PROVIDER,
        detail="fake provider: simulated provider_failure scenario",
    ),
    # A BUSINESS cancellation (e.g. the callee hangs up mid-call) is a
    # normal terminal event, NOT a raised asyncio.CancelledError — using
    # real task cancellation to simulate this would be indistinguishable
    # from genuine shutdown-triggered cancellation and could get silently
    # swallowed by whatever catches it, which is exactly the bug graceful
    # shutdown depends on NOT happening (docs/PHASE1_DESIGN.md "Shutdown").
    Scenario.CANCELLATION: ProviderEvent(
        type=ProviderEventType.CANCELLED,
        failure_category=FailureCategory.CANCELLATION,
        detail="fake provider: simulated cancellation scenario",
    ),
}


class FakeTelephonyProvider:
    """Implements `telephony.contracts.TelephonyProvider`. `step_delay_seconds`
    lets tests keep the simulated call fast (default) or slow enough to
    exercise timeout/cancellation/shutdown races deterministically."""

    def __init__(
        self,
        scenario_source: ScenarioSource = _always_success,
        step_delay_seconds: float = 0.0,
    ) -> None:
        self._scenario_source = scenario_source
        self._step_delay_seconds = step_delay_seconds

    async def place_call(self, context: CallAttemptContext) -> AsyncIterator[ProviderEvent]:
        scenario = self._scenario_source(context)

        if scenario is Scenario.TIMEOUT:
            # Simulates a provider that never responds — the caller's
            # asyncio.wait_for/asyncio.timeout is what actually turns this
            # into a TIMEOUT failure (see telephony/contracts.py's
            # TelephonyProvider docstring: the provider doesn't decide its
            # own timeout). This DOES rely on real asyncio cancellation
            # internally (wait_for cancels the sleeping task) — that's
            # correct and intentional here, unlike the business-CANCELLATION
            # scenario below.
            yield ProviderEvent(type=ProviderEventType.DIALING)
            await asyncio.sleep(3600)
            return

        yield ProviderEvent(type=ProviderEventType.DIALING)
        await self._step()
        yield ProviderEvent(type=ProviderEventType.RINGING)
        await self._step()

        if scenario is Scenario.SUCCESS:
            yield ProviderEvent(type=ProviderEventType.CONNECTED)
            await self._step()
        yield _SCENARIO_TERMINAL_EVENT[scenario]

    async def _step(self) -> None:
        if self._step_delay_seconds:
            await asyncio.sleep(self._step_delay_seconds)
