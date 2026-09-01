"""Pure unit tests for orchestrator/failures.py's RetryPolicy — no DB, no
Redis, no clock mocking needed since delay computation is deterministic
given a seeded random source."""
from __future__ import annotations

import random

import pytest

from orchestrator.failures import DEFAULT_RETRYABLE_CATEGORIES, RetryPolicy
from telephony.contracts import FailureCategory


def _policy(**overrides):
    defaults = dict(
        max_attempts=3,
        initial_delay_seconds=5.0,
        backoff_multiplier=2.0,
        max_delay_seconds=120.0,
        jitter_fraction=0.0,
    )
    defaults.update(overrides)
    return RetryPolicy(**defaults)


class TestRetryability:
    @pytest.mark.parametrize("category", sorted(DEFAULT_RETRYABLE_CATEGORIES, key=str))
    def test_default_retryable_categories_retry_on_first_failure(self, category):
        decision = _policy().decide(category, attempt_number=1)
        assert decision.should_retry is True

    @pytest.mark.parametrize(
        "category",
        [c for c in FailureCategory if c not in DEFAULT_RETRYABLE_CATEGORIES],
    )
    def test_non_retryable_categories_never_retry_even_on_first_failure(self, category):
        decision = _policy().decide(category, attempt_number=1)
        assert decision.should_retry is False
        assert decision.delay_seconds == 0.0

    def test_retryable_category_stops_once_max_attempts_reached(self):
        policy = _policy(max_attempts=3)
        assert policy.decide(FailureCategory.TIMEOUT, attempt_number=2).should_retry is True
        assert policy.decide(FailureCategory.TIMEOUT, attempt_number=3).should_retry is False

    def test_custom_retryable_set_can_include_categories_not_retryable_by_default(self):
        policy = _policy(retryable_categories=frozenset({FailureCategory.CAPACITY}))
        assert policy.decide(FailureCategory.CAPACITY, attempt_number=1).should_retry is True
        assert policy.decide(FailureCategory.PROVIDER, attempt_number=1).should_retry is False


class TestBackoff:
    def test_first_retry_uses_initial_delay(self):
        policy = _policy(initial_delay_seconds=5.0, backoff_multiplier=2.0)
        decision = policy.decide(FailureCategory.TIMEOUT, attempt_number=1)
        assert decision.delay_seconds == pytest.approx(5.0)

    def test_delay_grows_by_multiplier_each_attempt(self):
        policy = _policy(
            initial_delay_seconds=5.0, backoff_multiplier=2.0, max_delay_seconds=1000.0, max_attempts=10
        )
        assert policy.decide(FailureCategory.TIMEOUT, attempt_number=1).delay_seconds == pytest.approx(5.0)
        assert policy.decide(FailureCategory.TIMEOUT, attempt_number=2).delay_seconds == pytest.approx(10.0)
        assert policy.decide(FailureCategory.TIMEOUT, attempt_number=3).delay_seconds == pytest.approx(20.0)

    def test_delay_is_capped_at_max_delay_seconds(self):
        policy = _policy(initial_delay_seconds=5.0, backoff_multiplier=10.0, max_delay_seconds=30.0, max_attempts=5)
        decision = policy.decide(FailureCategory.TIMEOUT, attempt_number=3)
        assert decision.delay_seconds == pytest.approx(30.0)

    def test_jitter_adds_a_bounded_positive_amount_never_reduces_delay(self):
        policy = _policy(
            initial_delay_seconds=10.0, jitter_fraction=0.5, max_delay_seconds=1000.0, _random=random.Random(42)
        )
        decision = policy.decide(FailureCategory.TIMEOUT, attempt_number=1)
        assert 10.0 <= decision.delay_seconds <= 15.0

    def test_zero_jitter_fraction_is_fully_deterministic(self):
        policy = _policy(initial_delay_seconds=7.0, jitter_fraction=0.0)
        d1 = policy.decide(FailureCategory.TIMEOUT, attempt_number=1).delay_seconds
        d2 = policy.decide(FailureCategory.TIMEOUT, attempt_number=1).delay_seconds
        assert d1 == d2 == pytest.approx(7.0)
