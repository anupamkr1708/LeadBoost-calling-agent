"""The telephony provider boundary — a `Protocol`, not a base class, so an
adapter has no framework to inherit from, just a shape to match (roadmap's
"provider isolation" goal). `telephony/fake.py` implements this for Phase
1; a real Exotel/Twilio adapter implementing the same Protocol is a Phase
3+ concern that requires zero changes to `conversation/runtime.py` or
`orchestrator/worker_runtime.py` — both depend only on this module, never
on a concrete provider (see docs/PHASE1_DESIGN.md "Fake telephony
provider").

`FailureCategory` lives HERE, not in `orchestrator/failures.py`, even
though `RetryPolicy` (which consumes it) lives there — this is the
lowest layer in the dependency chain (orchestrator -> conversation ->
telephony), and a provider event carrying a failure category is a
telephony-boundary concept before it's anything else. `orchestrator/`
imports it from here, never the reverse (enforced by
tests/layering/test_import_boundaries.py).
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class FailureCategory(StrEnum):
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    TENANT_VIOLATION = "tenant_violation"
    CAPACITY = "capacity"
    PROVIDER = "provider"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    TRANSIENT_INFRA = "transient_infra"
    PERMANENT_EXECUTION = "permanent_execution"
    BUSINESS_TERMINAL = "business_terminal"


class ProviderEventType(StrEnum):
    DIALING = "dialing"
    RINGING = "ringing"
    CONNECTED = "connected"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ProviderEvent:
    """One lifecycle event from a provider's `place_call` stream.
    `failure_category` is only set on a terminal, non-`COMPLETED` event —
    it's how the provider boundary tells the runtime WHY, in the runtime's
    own vocabulary (never a raw provider-specific string the runtime would
    have to pattern-match)."""

    type: ProviderEventType
    failure_category: FailureCategory | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CallAttemptContext:
    """Everything a provider needs to place a call — deliberately narrow
    (no campaign/lead/org business data leaks into the provider boundary;
    that's the Call Service's job, not the provider's)."""

    call_attempt_id: uuid.UUID
    attempt_number: int


class TelephonyProvider(Protocol):
    def place_call(self, context: CallAttemptContext) -> AsyncIterator[ProviderEvent]:
        """Yields an ordered sequence of `ProviderEvent`s ending in exactly
        one terminal event (`COMPLETED`, `BUSY`, `NO_ANSWER`, or `FAILED`).
        Implementations should raise `TimeoutError` if the operation
        exceeds `Settings.provider_operation_timeout_seconds` — the caller
        (`conversation/runtime.py`) is responsible for enforcing that
        timeout via `asyncio.wait_for`, not the provider itself, so the
        same timeout policy applies uniformly to every provider."""
        ...
