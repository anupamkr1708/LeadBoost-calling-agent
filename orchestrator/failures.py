"""Failure taxonomy and retry policy.

One `RetryPolicy`, consuming `telephony.contracts.FailureCategory` (defined
there, not here — see that module's docstring for why: it's the lowest
layer in the dependency chain, orchestrator -> conversation -> telephony,
and a provider event's failure category is a telephony-boundary concept
before it's a retry-policy concept). Every failure path in the runtime — a
provider result, a reaper-detected worker crash, a timeout — resolves to a
`FailureCategory` and then asks `RetryPolicy.decide(...)`. Nothing in this
codebase does `if "timeout" in str(exc)`; see docs/PHASE1_DESIGN.md
"Failure taxonomy and retry".
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from telephony.contracts import FailureCategory


@dataclass(frozen=True)
class RetryDecision:
    """What `RetryPolicy.decide` returns. `should_retry=False` means the
    attempt's failure is final for this Call — the caller marks the Call
    FAILED, it does not schedule another attempt."""

    should_retry: bool
    delay_seconds: float = 0.0


# Categories a well-behaved retry policy schedules another attempt for.
# Deliberately a policy-level default, not hardcoded into the runtime —
# callers can construct a RetryPolicy with a different set.
DEFAULT_RETRYABLE_CATEGORIES = frozenset(
    {FailureCategory.PROVIDER, FailureCategory.TIMEOUT, FailureCategory.TRANSIENT_INFRA}
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_delay_seconds: float
    backoff_multiplier: float
    max_delay_seconds: float
    jitter_fraction: float = 0.0
    retryable_categories: frozenset[FailureCategory] = field(
        default_factory=lambda: DEFAULT_RETRYABLE_CATEGORIES
    )
    # Injectable for deterministic tests; production uses random.random.
    _random: random.Random = field(default_factory=random.Random, repr=False, compare=False)

    def decide(self, category: FailureCategory, attempt_number: int) -> RetryDecision:
        """`attempt_number` is the attempt that just failed (1-indexed).
        Returns whether a NEW attempt (attempt_number + 1) should be
        scheduled, and after how long."""
        if category not in self.retryable_categories:
            return RetryDecision(should_retry=False)
        if attempt_number >= self.max_attempts:
            return RetryDecision(should_retry=False)
        delay = min(
            self.initial_delay_seconds * (self.backoff_multiplier ** (attempt_number - 1)),
            self.max_delay_seconds,
        )
        if self.jitter_fraction:
            jitter = delay * self.jitter_fraction * self._random.random()
            delay += jitter
        return RetryDecision(should_retry=True, delay_seconds=delay)
