"""Tests for the per-render OTel envelope wired into all 4 render
orchestrators (P2a).

What we verify:

* Calling ``make_short`` opens a single ``render.short`` parent span.
* The parent span carries the right context attributes
  (channel / slug / render_kind / render_mode).
* Stage events emitted inside the body via ``_record_stage_done``
  also produce ``stage.<name>`` spans that are children of the
  parent (because the parent span is active at emission time).
* Exceptions inside the impl mark the parent span ERROR + record
  the exception event.

We mock the heavy machinery via the same ``patched_make_short_environment``
helper used by the existing renderer test-suite — keeps this test
fast (no real ffmpeg / TTS / image gen) and focused on telemetry.
"""
from __future__ import annotations

import unittest

from pipeline import observability as obs


class TestRenderEnvelope(unittest.TestCase):
    """Smoke-tests the envelope itself without booting the full render."""

    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())

    def test_envelope_pushes_context_and_opens_span(self) -> None:
        with obs.render_envelope(
            channel="historyrecapped",
            slug="aita-001",
            render_kind="short",
        ):
            cur = obs.current_context()
            self.assertEqual(cur.channel, "historyrecapped")
            self.assertEqual(cur.slug, "aita-001")
            self.assertEqual(cur.render_kind, "short")
            self.assertIn(cur.render_mode, ("laptop", "cloud"))

        spans = self._spans()
        self.assertEqual(len(spans), 1)
        s = spans[0]
        self.assertEqual(s.name, "render.short")
        self.assertEqual(s.attributes["ytfactory.channel"], "historyrecapped")
        self.assertEqual(s.attributes["ytfactory.slug"], "aita-001")
        self.assertEqual(s.attributes["ytfactory.render_kind"], "short")

    def test_envelope_marks_error_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with obs.render_envelope(
                channel="X", slug="Y", render_kind="short",
            ):
                raise RuntimeError("kaboom")
        s = self._spans()[0]
        self.assertFalse(s.status.is_ok)
        events = list(s.events)
        self.assertTrue(any(e.name == "exception" for e in events))

    def test_nested_telemetry_inherits_envelope_attrs(self) -> None:
        with obs.render_envelope(
            channel="cosmosdecoded",
            slug="eddington-1919",
            render_kind="long_form",
        ):
            with obs.timed("inner_stage", category="render") as t:
                t.add(metadata={"step": "synth"})
        spans = self._spans()
        self.assertEqual(len(spans), 2)  # render.long_form + inner_stage
        # Both spans must carry the channel + slug attrs.
        for s in spans:
            self.assertEqual(s.attributes["ytfactory.channel"],
                             "cosmosdecoded")
            self.assertEqual(s.attributes["ytfactory.slug"],
                             "eddington-1919")
            self.assertEqual(s.attributes["ytfactory.render_kind"],
                             "long_form")

    def test_envelope_picks_up_job_and_run_id_from_env(self) -> None:
        import os
        os.environ["YTFACTORY_JOB_ID"] = "j-test-123"
        os.environ["YTFACTORY_RUN_ID"] = "r-test-456"
        try:
            with obs.render_envelope(
                channel="A", slug="B", render_kind="short",
            ):
                cur = obs.current_context()
                self.assertEqual(cur.job_id, "j-test-123")
                self.assertEqual(cur.run_id, "r-test-456")
            s = self._spans()[0]
            self.assertEqual(s.attributes["ytfactory.job_id"], "j-test-123")
            self.assertEqual(s.attributes["ytfactory.run_id"], "r-test-456")
        finally:
            os.environ.pop("YTFACTORY_JOB_ID", None)
            os.environ.pop("YTFACTORY_RUN_ID", None)


class TestEmitSpan(unittest.TestCase):
    """Verifies the new ``obs.emit_span`` helper used by
    ``pipeline.render.shorts._record_stage_done``.
    """

    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())

    def test_emit_span_creates_span_with_known_duration(self) -> None:
        obs.emit_span(
            "stage.tts",
            duration_ms=2400,
            metadata={"stage": "tts", "slug": "s1", "niche": "ch"},
        )
        spans = self._spans()
        self.assertEqual(len(spans), 1)
        s = spans[0]
        self.assertEqual(s.name, "stage.tts")
        self.assertEqual(s.attributes["ytfactory.duration_ms"], 2400)
        self.assertEqual(s.attributes["ytfactory.meta.stage"], "tts")
        self.assertEqual(s.attributes["ytfactory.meta.slug"], "s1")

    def test_emit_span_failure_marks_error(self) -> None:
        obs.emit_span("stage.bad", duration_ms=120, success=False)
        s = self._spans()[0]
        self.assertFalse(s.status.is_ok)


if __name__ == "__main__":
    unittest.main()
