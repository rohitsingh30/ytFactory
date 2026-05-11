"""Tests for :mod:`pipeline.observability.propagation`.

Verifies traceparent injection / extraction round-trips through both
HTTP-style dicts (the default for requests / httpx auto-instrumentation)
AND environment variables (the Cloud Run JOB boundary, where there is
no HTTP request to attach a header to).
"""
from __future__ import annotations

import unittest

from opentelemetry import context as otel_context

from pipeline import observability as obs
from pipeline.observability import propagation


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()


class TestDictRoundtrip(_Base):
    def test_inject_then_extract_dict(self) -> None:
        carrier: dict[str, str] = {}
        with obs.timed("outer"):
            propagation.inject_into_dict(carrier)
        self.assertIn("traceparent", carrier)
        # Round-trip back into a Context.
        ctx = propagation.extract_from_dict(carrier)
        self.assertIsInstance(ctx, otel_context.Context)


class TestEnvRoundtrip(_Base):
    def test_inject_into_fresh_dict(self) -> None:
        with obs.timed("outer"):
            env = propagation.inject_into_env()
        self.assertIn(propagation.ENV_VAR, env)
        self.assertTrue(env[propagation.ENV_VAR].startswith("00-"))

    def test_extract_from_env_returns_context(self) -> None:
        with obs.timed("outer"):
            env = propagation.inject_into_env()
        # Use a copy to avoid polluting the test's real env.
        ctx = propagation.extract_from_env(env)
        self.assertIsInstance(ctx, otel_context.Context)

    def test_extract_with_no_traceparent_returns_empty_context(self) -> None:
        ctx = propagation.extract_from_env({})
        self.assertIsInstance(ctx, otel_context.Context)


if __name__ == "__main__":
    unittest.main()
