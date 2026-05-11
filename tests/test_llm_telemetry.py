"""P2e — verifies the @obs.traced decorator works correctly and that
applied LLM modules emit spans on call.
"""
from __future__ import annotations

import unittest

from pipeline import observability as obs


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())


class TestTracedDecorator(_Base):
    def test_decorator_creates_span_per_call(self) -> None:
        @obs.traced("test.fn", category="llm", capture=["x"])
        def foo(x: int, y: str = "hi") -> int:
            return x + len(y)

        result = foo(5, y="world")
        self.assertEqual(result, 10)

        s = next(s for s in self._spans() if s.name == "test.fn")
        self.assertEqual(s.attributes["ytfactory.category"], "llm")
        self.assertEqual(s.attributes["ytfactory.meta.x"], 5)

    def test_decorator_records_exception(self) -> None:
        @obs.traced("test.bad", category="llm")
        def bad():
            raise ValueError("oops")

        with self.assertRaises(ValueError):
            bad()
        s = self._spans()[0]
        self.assertFalse(s.status.is_ok)

    def test_decorator_default_name(self) -> None:
        @obs.traced()
        def some_fn():
            return 42

        some_fn()
        names = [s.name for s in self._spans()]
        self.assertTrue(any("some_fn" in n for n in names))

    def test_decorator_drops_non_primitive_capture(self) -> None:
        @obs.traced("test.complex", capture=["d"])
        def f(d: dict) -> None:
            pass

        f({"x": [1, 2, 3]})
        s = self._spans()[0]
        # non-primitive captured as truncated str
        self.assertIn("ytfactory.meta.d", s.attributes)
        self.assertTrue(isinstance(s.attributes["ytfactory.meta.d"], str))

    def test_decorator_extra_metadata_appended(self) -> None:
        @obs.traced("test.extra", extra_metadata={"channel": "airecap"})
        def f():
            return 1

        f()
        s = self._spans()[0]
        self.assertEqual(s.attributes["ytfactory.meta.channel"], "airecap")


class TestLLMModulesEmitSpans(_Base):
    """Smoke-test that decorated LLM modules emit a span when called.

    We don't make real LLM calls — we exercise the deterministic
    helpers that don't touch claude (visualizability + cast_router +
    cast_lint + prompt_lint + script_lint).
    """

    def test_visualizability_score_emits_span(self) -> None:
        from pipeline.llm import visualizability
        score, reasons = visualizability.score_visualizability(
            "A young woman holds a phone and reads a message",
        )
        self.assertGreater(score, 0)
        s = next(s for s in self._spans()
                 if s.name == "llm.visualizability.score_visualizability")
        self.assertTrue(s.status.is_ok)

    def test_cast_lint_check_cast_emits_span(self) -> None:
        from pipeline.llm import cast_lint
        # An empty cast trips many issues but doesn't raise.
        issues = cast_lint.check_cast({})
        self.assertIsInstance(issues, list)
        s = next(s for s in self._spans()
                 if s.name == "llm.cast_lint.check_cast")
        self.assertTrue(s.status.is_ok)

    def test_script_lint_emits_span(self) -> None:
        from pipeline.llm import script_lint
        result = script_lint.lint_and_fix(
            "First sentence. Second sentence. Third sentence."
        )
        self.assertIsNotNone(result)
        s = next(s for s in self._spans()
                 if s.name == "llm.script_lint.lint_and_fix")
        self.assertTrue(s.status.is_ok)


if __name__ == "__main__":
    unittest.main()
