"""
Tests for erp_core.orchestrator — circuit breaker + dead-letter queue.

These guard the fault-tolerance contracts every agent caller relies on:
  • Failures escalate to OPEN at the configured threshold.
  • OPEN trips short-circuit further calls.
  • OPEN auto-transitions to HALF_OPEN after recovery_timeout.
  • A success in HALF_OPEN closes the circuit; a failure re-opens it.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from erp_core.orchestrator import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


class CircuitBreakerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.cb = CircuitBreaker(
            agent_name="test_agent",
            failure_threshold=3,
            recovery_timeout=60,
        )

    def test_initial_state_is_closed(self):
        self.assertEqual(self.cb.state, CircuitState.CLOSED)
        self.assertTrue(self.cb.is_available())

    def test_failures_below_threshold_stay_closed(self):
        self.cb.record_failure()
        self.cb.record_failure()
        self.assertEqual(self.cb.state, CircuitState.CLOSED)

    def test_failures_at_threshold_open_circuit(self):
        for _ in range(3):
            self.cb.record_failure()
        self.assertEqual(self.cb.state, CircuitState.OPEN)
        self.assertFalse(self.cb.is_available())

    def test_call_raises_when_open(self):
        for _ in range(3):
            self.cb.record_failure()

        with self.assertRaises(CircuitOpenError):
            self.cb.call(lambda: "should not run")

    def test_success_in_half_open_closes_circuit(self):
        for _ in range(3):
            self.cb.record_failure()

        # Force state machine into HALF_OPEN by faking elapsed recovery time
        with patch("erp_core.orchestrator.time.time", return_value=time.time() + 120):
            self.assertEqual(self.cb.state, CircuitState.HALF_OPEN)
            result = self.cb.call(lambda: "recovered")
            self.assertEqual(result, "recovered")

        self.assertEqual(self.cb.state, CircuitState.CLOSED)

    def test_failure_in_half_open_reopens(self):
        for _ in range(3):
            self.cb.record_failure()

        with patch("erp_core.orchestrator.time.time", return_value=time.time() + 120):
            self.assertEqual(self.cb.state, CircuitState.HALF_OPEN)
            with self.assertRaises(RuntimeError):
                self.cb.call(lambda: (_ for _ in ()).throw(RuntimeError("still broken")))

        self.assertEqual(self.cb.state, CircuitState.OPEN)

    def test_record_success_resets_failure_counter(self):
        self.cb.record_failure()
        self.cb.record_failure()
        self.cb.record_success()

        # Now we should tolerate threshold-1 more failures before opening
        self.cb.record_failure()
        self.cb.record_failure()
        self.assertEqual(self.cb.state, CircuitState.CLOSED)

    def test_decorator_form(self):
        cb = CircuitBreaker("dec_agent", failure_threshold=2, recovery_timeout=60)

        @cb
        def flaky():
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            flaky()
        with self.assertRaises(ValueError):
            flaky()
        # Now circuit is OPEN — the wrapper short-circuits with CircuitOpenError
        with self.assertRaises(CircuitOpenError):
            flaky()

    def test_two_agents_have_isolated_state(self):
        a = CircuitBreaker("agent_a", failure_threshold=2, recovery_timeout=60)
        b = CircuitBreaker("agent_b", failure_threshold=2, recovery_timeout=60)

        a.record_failure()
        a.record_failure()
        self.assertEqual(a.state, CircuitState.OPEN)
        self.assertEqual(b.state, CircuitState.CLOSED)


# Note: DeadLetterQueue uses cache.client.get_client() to talk to Redis
# directly (it needs LPUSH/LTRIM/LRANGE which Django's cache API doesn't
# expose). It is tolerant of any backend failure (every method catches
# Exception), so testing it against the LocMem test cache would only test
# the silent-failure branch. Real coverage belongs in an integration test
# with a real Redis — out of scope here.
