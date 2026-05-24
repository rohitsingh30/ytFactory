"""Regression tests for the ``image.gen`` event dedupe (Fix #8, 2026-05-24).

Background — see
``data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.bugs.md``
(CLASS-OF-BUG #G) and
``.claude/skills/diagnose-render/learnings/845bdb0df20e4ba3885ca33c7749e74d.md``
(Finding 5).

On the 845bdb0d render, ``pipeline/images/images.py::generate`` emitted
77 events with name ``image.gen`` AND 77 events with name ``image_gen``
for the same 77 panels. Root cause: the function opened an
``_obs.timed("image_gen", ...)`` context manager around ``_generate_impl``
AND, in the ``finally`` block, called ``_record_image_gen_telemetry`` which
in turn called ``track_io("image.gen", ...)``. Both fired per panel,
doubling Cloud Logging volume and forcing downstream consumers to
dedupe by ``panel_index + duration``.

The fix removed the ``_obs.timed`` wrapper. ``track_io("image.gen", ...)``
inside the ``finally`` block measures duration via ``_time.perf_counter``
and is the single canonical emit site. The dot-separated name matches
the pipeline's convention (``stage.start``, ``tts.chunk``, ``llm.call``).

These tests pin:

1. For one successful ``generate`` call, exactly ONE event is emitted
   AND its name is ``image.gen`` (dot).
2. For a failed ``generate`` call (raised exception), exactly ONE
   ``image.gen`` event still fires (from the ``finally`` block) and
   no ``image_gen`` underscore alias.
3. The total count of ``image.gen`` + ``image_gen`` events equals the
   number of panels generated — never 2x.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import images as images_mod
from pipeline import observability as obs
from pipeline.images import images_cloudrun as ic


class ImageGenEventDedupe(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()
        ic.reset_circuit_breaker()
        self.events: list[dict] = []
        from pipeline.observability import subscribe
        subscribe(self.events.append)

    def tearDown(self) -> None:
        from pipeline.observability import unsubscribe
        try:
            unsubscribe(self.events.append)
        except Exception:  # noqa: BLE001
            pass
        ic.reset_circuit_breaker()
        obs.reset_for_tests()

    def _image_gen_events(self) -> list[dict]:
        """Return the union of ``image.gen`` AND ``image_gen`` events."""
        return [e for e in self.events
                if e.get("event") in {"image.gen", "image_gen"}]

    def test_single_generate_emits_exactly_one_event(self) -> None:
        with patch(
            "pipeline.images.images_cloudrun._generate_cloudrun_z_image_turbo",
            side_effect=lambda **kw: kw["out_path"],
        ):
            out = Path("/tmp/img.png")
            out.write_bytes(b"x" * 16)
            images_mod.generate(
                "test scene", "test style",
                seed=42, out_path=out,
                width=768, height=1344, steps=4,
                provider="cloudrun_z_image_turbo",
            )

        emitted = self._image_gen_events()
        names = [e["event"] for e in emitted]
        self.assertEqual(
            len(emitted), 1,
            f"expected exactly one image-gen event for one generate() "
            f"call; got {len(emitted)} (names={names}). This is the "
            f"845bdb0d 2x duplicate emit regression — see "
            f"data/critiques/...-845bdb0d.bugs.md CLASS-OF-BUG #G.",
        )
        self.assertEqual(
            emitted[0]["event"], "image.gen",
            "canonical event name is dot-separated 'image.gen'; the "
            "legacy underscore alias 'image_gen' was retired 2026-05-24.",
        )

    def test_failed_generate_emits_exactly_one_event(self) -> None:
        # Force a failure inside _generate_impl by passing an unknown
        # provider. The finally-block emit still fires with success=False.
        with self.assertRaises(ValueError):
            images_mod.generate(
                "x", "y", seed=1, out_path=Path("/tmp/q.png"),
                provider="not_real",
            )

        emitted = self._image_gen_events()
        self.assertEqual(
            len(emitted), 1,
            f"failed generate() must emit exactly ONE image.gen event "
            f"(from the finally block); got {len(emitted)}.",
        )
        self.assertEqual(emitted[0]["event"], "image.gen")
        self.assertFalse(emitted[0]["success"])

    def test_n_panels_emit_n_events_not_2n(self) -> None:
        with patch(
            "pipeline.images.images_cloudrun._generate_cloudrun_z_image_turbo",
            side_effect=lambda **kw: kw["out_path"],
        ):
            for i in range(5):
                out = Path(f"/tmp/img_{i}.png")
                out.write_bytes(b"x" * 16)
                images_mod.generate(
                    f"scene {i}", "style",
                    seed=i, out_path=out,
                    width=768, height=1344, steps=4,
                    provider="cloudrun_z_image_turbo",
                )

        emitted = self._image_gen_events()
        self.assertEqual(
            len(emitted), 5,
            f"5 panels must emit 5 image-gen events, not 10. Got "
            f"{len(emitted)}. This is the 845bdb0d 2x duplicate "
            f"regression.",
        )
        names = {e["event"] for e in emitted}
        self.assertEqual(
            names, {"image.gen"},
            f"only 'image.gen' (dot) should appear; saw {names}.",
        )


if __name__ == "__main__":
    unittest.main()
