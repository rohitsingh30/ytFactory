"""Audit Q2.10 — ``traced`` decorator must respect ``async def``.

Pre-fix the decorator wrapped the call site with
``with timed(): return fn(*args, **kwargs)`` regardless of whether
the function was sync or async. For an ``async def`` that returned
a coroutine, the span closed the moment the coroutine object was
returned (microseconds), then the caller awaited the actual work
later — so dashboard latency for that stage was always near-zero.

This test file pins:
  - sync ``def`` is wrapped with the original sync wrapper;
  - ``async def`` is wrapped with an async wrapper that holds the
    span open across the ``await``;
  - ``capture=`` works on both sync and async paths;
  - ``__wrapped__`` survives so ``unittest.mock.patch.object`` /
    ``inspect.getsource`` keep working.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import unittest
from unittest.mock import patch

from pipeline.observability.decorators import _capture_args, traced


class TestTracedSyncDef(unittest.TestCase):
    def test_sync_def_returns_value_and_records_span(self):
        recorded: list = []

        def _fake_timed(name, *, category, metadata):
            class _CM:
                def __enter__(self_):
                    recorded.append({"name": name, "category": category,
                                     "metadata": dict(metadata)})
                    return self_
                def __exit__(self_, *exc):
                    return False
            return _CM()

        with patch("pipeline.observability.decorators.timed", side_effect=_fake_timed):
            @traced("rewrite_script", category="llm", capture=["channel", "slug"])
            def rewrite(text, *, channel, slug):
                return f"{channel}/{slug}/{text}"
            out = rewrite("hello", channel="ch", slug="s1")
        self.assertEqual(out, "ch/s1/hello")
        self.assertEqual(recorded[0]["name"], "rewrite_script")
        self.assertEqual(recorded[0]["category"], "llm")
        self.assertEqual(recorded[0]["metadata"], {"channel": "ch", "slug": "s1"})

    def test_wrapped_attribute_present(self):
        @traced()
        def fn(): return 1
        self.assertTrue(hasattr(fn, "__wrapped__"))
        self.assertFalse(inspect.iscoroutinefunction(fn))


class TestTracedAsyncDef(unittest.IsolatedAsyncioTestCase):
    async def test_async_def_holds_span_across_await(self):
        recorded: list = []
        durations: list[float] = []

        def _fake_timed(name, *, category, metadata):
            class _CM:
                def __enter__(self_):
                    self_._start = time.perf_counter()
                    recorded.append({"name": name, "metadata": dict(metadata)})
                    return self_
                def __exit__(self_, *exc):
                    durations.append(time.perf_counter() - self_._start)
                    return False
            return _CM()

        with patch("pipeline.observability.decorators.timed", side_effect=_fake_timed):
            @traced("async_stage", category="image", capture=["channel"])
            async def slow(*, channel):
                await asyncio.sleep(0.05)
                return f"done-{channel}"
            out = await slow(channel="ch1")
        self.assertEqual(out, "done-ch1")
        self.assertEqual(recorded[0]["name"], "async_stage")
        self.assertEqual(recorded[0]["metadata"], {"channel": "ch1"})
        # Audit Q2.10 — span must record the actual ~50ms async wait,
        # not the microseconds it took to construct the coroutine.
        self.assertGreater(durations[0], 0.04)

    async def test_async_def_iscoroutinefunction_preserved(self):
        @traced("a", category="llm")
        async def afn(): return 7
        self.assertTrue(inspect.iscoroutinefunction(afn))
        self.assertEqual(await afn(), 7)


class TestCaptureArgsHelper(unittest.TestCase):
    """The arg-extraction helper is now a separate function so both
    sync + async wrappers share one impl. Pin its behaviour."""

    def test_extracts_listed_primitive_args(self):
        def fn(a, *, b, c=10): pass
        sig = inspect.signature(fn)
        out = _capture_args(sig, ["a", "b", "c"], (1,), {"b": "x"})
        self.assertEqual(out, {"a": 1, "b": "x"})

    def test_skips_unlisted_args(self):
        def fn(a, b): pass
        sig = inspect.signature(fn)
        out = _capture_args(sig, ["a"], (1, 2), {})
        self.assertEqual(out, {"a": 1})

    def test_drops_none_values(self):
        def fn(a, b): pass
        sig = inspect.signature(fn)
        out = _capture_args(sig, ["a", "b"], (None, 5), {})
        self.assertEqual(out, {"b": 5})

    def test_coerces_non_primitive_to_short_string(self):
        def fn(a): pass
        sig = inspect.signature(fn)
        big = list(range(10000))
        out = _capture_args(sig, ["a"], (big,), {})
        self.assertIn("a", out)
        self.assertLessEqual(len(out["a"]), 200)

    def test_silent_on_bind_failure(self):
        def fn(a, b): pass
        sig = inspect.signature(fn)
        # Mismatched positional arity.
        out = _capture_args(sig, ["a"], (1, 2, 3), {})
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
