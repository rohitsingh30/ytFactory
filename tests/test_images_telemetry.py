"""P2c — image-gen telemetry verification.

* ``images.generate(...)`` emits an ``image_gen`` span with the
  expected attributes (provider, prompt_chars, width, height, steps,
  seed).
* The cloud-image circuit breaker emits a
  ``image_circuit_breaker_tripped`` event when first tripped, and is
  idempotent on the second trip within the same render.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import images as images_mod
from pipeline import observability as obs
from pipeline.images import images_cloudrun as ic


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()
        ic.reset_circuit_breaker()

    def tearDown(self) -> None:
        ic.reset_circuit_breaker()
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())

    def _events(self):
        from pipeline import telemetry as tlm
        return tlm.read_events()


class TestGenerateSpan(_Base):
    def test_generate_emits_image_gen_span(self) -> None:
        # Patch the cloud provider implementation to avoid network I/O.
        with patch(
            "pipeline.images.images_cloudrun._generate_cloudrun_flux2_klein",
            side_effect=lambda **kw: kw["out_path"],
        ):
            out = Path("/tmp/img.png")
            out.write_bytes(b"x" * 16)  # so .stat().st_size works
            images_mod.generate(
                "a sketched character holding a torch",
                "concept art, painterly",
                seed=42,
                out_path=out,
                width=768, height=1344, steps=4,
                provider="cloudrun_flux2_klein",
            )

        spans = self._spans()
        names = [s.name for s in spans]
        self.assertIn("image_gen", names)
        s = next(s for s in spans if s.name == "image_gen")
        self.assertEqual(s.attributes["ytfactory.meta.provider"],
                         "cloudrun_flux2_klein")
        self.assertEqual(s.attributes["ytfactory.meta.width"], 768)
        self.assertEqual(s.attributes["ytfactory.meta.height"], 1344)
        self.assertEqual(s.attributes["ytfactory.meta.steps"], 4)
        self.assertEqual(s.attributes["ytfactory.meta.seed"], 42)
        # prompt_chars = len("a sketched character holding a torch")
        self.assertEqual(s.attributes["ytfactory.meta.prompt_chars"], 36)

    def test_unknown_provider_fails_span(self) -> None:
        with self.assertRaises(ValueError):
            images_mod.generate(
                "x", "y", seed=1, out_path=Path("/tmp/q.png"),
                provider="not_real",
            )
        s = next(s for s in self._spans() if s.name == "image_gen")
        self.assertFalse(s.status.is_ok)


class TestBreakerTripEvent(_Base):
    def test_first_trip_emits_event(self) -> None:
        ic._trip_breaker("test reason: cloud 503")
        evts = [e for e in self._events()
                if e["event"] == "image_circuit_breaker_tripped"]
        self.assertEqual(len(evts), 1)
        self.assertFalse(evts[0]["success"])
        self.assertEqual(evts[0]["category"], "image")
        self.assertIn("test reason", evts[0]["metadata"]["reason"])

    def test_second_trip_within_render_is_silent(self) -> None:
        # First trip records, second is a no-op (the breaker is already open).
        ic._trip_breaker("first")
        ic._trip_breaker("second")
        evts = [e for e in self._events()
                if e["event"] == "image_circuit_breaker_tripped"]
        self.assertEqual(len(evts), 1)

    def test_reset_then_re_trip_emits_again(self) -> None:
        ic._trip_breaker("first")
        ic.reset_circuit_breaker()
        ic._trip_breaker("second after reset")
        evts = [e for e in self._events()
                if e["event"] == "image_circuit_breaker_tripped"]
        self.assertEqual(len(evts), 2)


if __name__ == "__main__":
    unittest.main()
