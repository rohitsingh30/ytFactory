"""P2c — image-gen telemetry verification.

* ``images.generate(...)`` emits exactly ONE ``image.gen`` event via
  ``track_io`` with the expected attributes (provider, prompt_chars,
  width, height, steps, seed).
* The legacy ``image_gen`` (underscore) span emit was removed
  2026-05-24 to close F28 (845bdb0d render duplicate-emit; 77+77
  events for 77 panels). See ``tests/test_image_gen_event_dedupe.py``
  for the dedicated regression guard.
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
    def test_generate_emits_one_image_gen_event(self) -> None:
        # Patch the cloud provider implementation to avoid network I/O.
        with patch(
            "pipeline.images.images_cloudrun._generate_cloudrun_z_image_turbo",
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
                provider="cloudrun_z_image_turbo",
            )

        # Canonical event name is dot-separated ``image.gen``; the
        # legacy ``image_gen`` underscore variant was deleted
        # 2026-05-24 to close F28 (845bdb0d render emitted 77+77
        # duplicates).
        evts = [e for e in self._events() if e["event"] == "image.gen"]
        legacy = [e for e in self._events() if e["event"] == "image_gen"]
        self.assertEqual(
            len(evts), 1,
            f"expected exactly one image.gen event, got {len(evts)}: {evts}",
        )
        self.assertEqual(
            len(legacy), 0,
            f"legacy image_gen (underscore) emit must not return — "
            f"got {len(legacy)} events",
        )
        e = evts[0]
        meta = e.get("metadata", {})
        self.assertEqual(meta.get("model"), "cloudrun_z_image_turbo")
        self.assertEqual(meta.get("width"), 768)
        self.assertEqual(meta.get("height"), 1344)
        self.assertEqual(meta.get("steps"), 4)
        self.assertEqual(meta.get("seed"), 42)
        self.assertIsNotNone(e.get("duration_ms"))

    def test_unknown_provider_emits_failed_image_gen(self) -> None:
        with self.assertRaises(ValueError):
            images_mod.generate(
                "x", "y", seed=1, out_path=Path("/tmp/q.png"),
                provider="not_real",
            )
        # Even on failure, exactly one image.gen event fires (in the
        # finally block) with success=False.
        evts = [e for e in self._events() if e["event"] == "image.gen"]
        legacy = [e for e in self._events() if e["event"] == "image_gen"]
        self.assertEqual(len(evts), 1)
        self.assertEqual(len(legacy), 0)
        self.assertFalse(evts[0]["success"])


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
