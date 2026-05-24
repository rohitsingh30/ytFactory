"""Regression tests for the music-bed silent-fallback fix (Fix #6, 2026-05-24).

Background — see
``data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.bugs.md``
(CLASS-OF-BUG #F) and
``.claude/skills/diagnose-render/learnings/845bdb0df20e4ba3885ca33c7749e74d.md``
(Finding 6).

On the 845bdb0d render:
* ``pipeline/channels/mystoriesanimated.yaml:202`` declared
  ``music_bed_default: ambient_low.mp3``.
* The asset wasn't present in the worker's resolved channel music
  dir (Dockerfile-COPY drift class, F24).
* ``pipeline/render/music/ducked_loop.py`` emitted
  ``music.pick source=missing_bed track_id=silent`` and fell through
  to ``SilentMusic``.
* The render shipped 26 minutes of narration over dead silence.

The user-directive in
``feedback_silent_fallback_unshippable_output``: "When a stage failure
can produce unshippable output (vs degraded-but-watchable), retry-
then-RAISE — never `return {}` silently."

Both ``DuckedLoop`` and ``SingleBed`` now RAISE ``FileNotFoundError``
when the configured ``music_bed_default`` resolves to a missing file.
Opt-outs for callers that genuinely want silence: set the channel YAML
``music_bed_default: off`` (routes through the bed_name == "off"
branch which intentionally selects silent / synth ambient) or set
``music_policy: none`` on the proposal (routes through ``SilentMusic``
directly, bypassing this plugin).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.render.music.ducked_loop import DuckedLoop
from pipeline.render.music.single_bed import SingleBed
from pipeline.render.spec import build_spec


class DuckedLoopMissingBedRaises(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-ducked-missing-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec_with_missing_bed(self):
        spec = build_spec(
            {"channel": "test_channel", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        # Point music to a non-existent bed asset. The resolver looks
        # in spec.extra["music_dir"] first; we set it to an empty
        # temp dir so resolution definitively fails.
        spec.music.default_bed = "ambient_low"
        if spec.extra is None:
            spec.extra = {}
        spec.extra["music_dir"] = str(self.tmp)
        return spec

    def test_missing_bed_raises_filenotfound(self) -> None:
        spec = self._spec_with_missing_bed()
        with self.assertRaises(FileNotFoundError) as cm:
            DuckedLoop().compose(spec, narration_duration_s=2.0)
        msg = str(cm.exception)
        # The error message must name the configured bed AND point at
        # the silent_fallback memory note so the operator knows the
        # fix path without trawling source.
        self.assertIn("ambient_low", msg)
        self.assertIn("silent_fallback_unshippable_output", msg)

    def test_off_bed_still_returns_silent_path(self) -> None:
        """``music_bed_default: off`` is the explicit opt-in for silence —
        the operator declared intent. This path MUST still return a
        silent wav, not raise.
        """
        spec = self._spec_with_missing_bed()
        spec.music.default_bed = "off"
        result = DuckedLoop().compose(spec, narration_duration_s=0.5)
        try:
            self.assertTrue(result.exists())
        finally:
            result.unlink(missing_ok=True)

    def test_empty_bed_still_returns_silent_path(self) -> None:
        """Empty bed name is treated the same as ``off`` — operator
        explicitly opted out of music."""
        spec = self._spec_with_missing_bed()
        spec.music.default_bed = ""
        result = DuckedLoop().compose(spec, narration_duration_s=0.5)
        try:
            self.assertTrue(result.exists())
        finally:
            result.unlink(missing_ok=True)


class SingleBedMissingBedRaises(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-single-bed-missing-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec_with_missing_bed(self):
        spec = build_spec(
            {"channel": "test_channel", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        spec.music.default_bed = "aether-loop"
        if spec.extra is None:
            spec.extra = {}
        spec.extra["music_dir"] = str(self.tmp)
        return spec

    def test_missing_bed_raises_filenotfound(self) -> None:
        spec = self._spec_with_missing_bed()
        with self.assertRaises(FileNotFoundError) as cm:
            SingleBed().compose(spec, narration_duration_s=2.0)
        msg = str(cm.exception)
        self.assertIn("aether-loop", msg)
        self.assertIn("silent_fallback_unshippable_output", msg)

    def test_off_bed_returns_synth_ambient(self) -> None:
        """``off`` is the explicit opt-in for synth ambient on
        SingleBed — the operator declared intent. This path MUST
        still return a real wav (synth drone), not raise."""
        spec = self._spec_with_missing_bed()
        spec.music.default_bed = "off"
        result = SingleBed().compose(spec, narration_duration_s=0.5)
        try:
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 0)
        finally:
            result.unlink(missing_ok=True)


class MusicPickEventSignalsFailure(unittest.TestCase):
    """When the missing-bed path fires, the ``music.pick`` event must
    carry ``success=False`` so downstream consumers (/diagnose-render
    + the operator dashboard) can flag the render. Pre-fix the event
    was emitted with ``success=True`` — telemetrically indistinguishable
    from a successful music selection."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-music-pick-"))
        self.events: list[dict] = []
        from pipeline.observability import subscribe
        subscribe(self.events.append)

    def tearDown(self) -> None:
        from pipeline.observability import unsubscribe
        try:
            unsubscribe(self.events.append)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_music_pick_missing_bed_event_carries_success_false(self) -> None:
        spec = build_spec(
            {"channel": "test_channel", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        spec.music.default_bed = "ambient_low"
        if spec.extra is None:
            spec.extra = {}
        spec.extra["music_dir"] = str(self.tmp)
        with self.assertRaises(FileNotFoundError):
            DuckedLoop().compose(spec, narration_duration_s=2.0)
        # Find the music.pick event for the failed selection.
        picks = [e for e in self.events if e["event"] == "music.pick"]
        self.assertEqual(len(picks), 1,
                         f"expected one music.pick event, got {len(picks)}")
        self.assertFalse(
            picks[0]["success"],
            "music.pick must emit success=False on missing_bed so the "
            "telemetry distinguishes a real selection from the fallback. "
            "Pre-fix the event emitted success=True even on the silent "
            "fallback path.",
        )
        self.assertEqual(picks[0]["metadata"]["source"], "missing_bed")
        self.assertEqual(picks[0]["metadata"]["configured_bed"], "ambient_low")


if __name__ == "__main__":
    unittest.main()
