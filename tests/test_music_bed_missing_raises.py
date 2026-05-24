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
        # Use a deliberately non-existent bed name so all three resolver
        # locations (spec.extra music_dir, <channel>/music/, AND the
        # data/music/ central fallback added 2026-05-24) miss. Picking
        # a real bed like "ambient_low" would now resolve via the
        # central fallback — which is the GOOD path, but defeats this
        # test's purpose (verifying the RAISE on a true miss).
        spec.music.default_bed = "definitely-not-a-real-bed-name-xyz"
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
        self.assertIn("definitely-not-a-real-bed-name-xyz", msg)
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
        spec.music.default_bed = "definitely-not-a-real-bed-xyz"
        if spec.extra is None:
            spec.extra = {}
        spec.extra["music_dir"] = str(self.tmp)
        return spec

    def test_missing_bed_raises_filenotfound(self) -> None:
        spec = self._spec_with_missing_bed()
        with self.assertRaises(FileNotFoundError) as cm:
            SingleBed().compose(spec, narration_duration_s=2.0)
        msg = str(cm.exception)
        self.assertIn("definitely-not-a-real-bed-xyz", msg)
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
        # Use a deliberately non-existent bed name so all three resolver
        # locations (spec.extra music_dir, <channel>/music/, AND the
        # data/music/ central fallback added 2026-05-24) miss. Picking
        # a real bed like "ambient_low" would now resolve via the
        # central fallback — which is the GOOD path, but defeats this
        # test's purpose (verifying the RAISE on a true miss).
        spec.music.default_bed = "definitely-not-a-real-bed-name-xyz"
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
        self.assertEqual(picks[0]["metadata"]["configured_bed"],
                         "definitely-not-a-real-bed-name-xyz")


class CentralMusicDirFallbackWorks(unittest.TestCase):
    """When ``data/music/<bed>.{wav,mp3}`` exists at repo root, the
    resolver must find it even though the channel has no
    ``<channel>/music/`` dir of its own. Pre-2026-05-24 the resolver
    only checked spec.extra['music_dir'] then ``<channel>/music/`` —
    every channel YAML's default bed (``ambient_low.mp3``, etc.)
    sits in ``data/music/`` and the resolver returned None, raising
    the unshippable FileNotFoundError. Pin the central-dir fallback
    so it can't silently regress."""

    def test_data_music_fallback_resolves_ambient_low(self) -> None:
        from pipeline.paths import PROJECT_ROOT
        bed = PROJECT_ROOT / "data" / "music" / "ambient_low.mp3"
        # Sanity: the asset is actually in the repo.
        self.assertTrue(
            bed.exists(),
            f"data/music/ambient_low.mp3 must exist in the repo "
            f"({bed} missing) — the central asset dir is the canonical "
            f"home of every default bed. If you're moving the dir, "
            f"update pipeline/render/music/{{ducked_loop,single_bed}}.py "
            f"AND cloud/render-worker-v2/Dockerfile's COPY line.",
        )

        spec = build_spec(
            {"channel": "test_channel", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        spec.music.default_bed = "ambient_low"
        # Use an empty music_dir so spec.extra path fails AND
        # <channel>/music/ doesn't exist either — only the
        # data/music/ fallback should resolve.
        with tempfile.TemporaryDirectory(prefix=".empty-") as empty:
            spec.extra = {"music_dir": empty}
            resolved = DuckedLoop()._resolve_bed_path(spec, "ambient_low")
            self.assertEqual(
                resolved, bed,
                f"resolver must find ambient_low at the central "
                f"data/music/ location; got {resolved}",
            )

    def test_data_music_fallback_resolves_for_single_bed_too(self) -> None:
        from pipeline.paths import PROJECT_ROOT
        bed = PROJECT_ROOT / "data" / "music" / "cinematic.mp3"
        self.assertTrue(bed.exists(), f"data/music/cinematic.mp3 missing ({bed})")

        spec = build_spec(
            {"channel": "test_channel", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        with tempfile.TemporaryDirectory(prefix=".empty-") as empty:
            spec.extra = {"music_dir": empty}
            resolved = SingleBed()._resolve_bed_path(spec, "cinematic")
            self.assertEqual(resolved, bed)


class DockerfileBakesCentralMusicDir(unittest.TestCase):
    """Pin the Dockerfile-COPY drift (F24) for the music asset dir.
    Without this COPY, the resolver fix above is useless in production:
    the file system on the deployed worker has no data/music/, so the
    fallback returns None and the render raises the unshippable error.
    """

    def test_dockerfile_copies_data_music(self) -> None:
        from pipeline.paths import PROJECT_ROOT
        dockerfile = PROJECT_ROOT / "cloud" / "render-worker-v2" / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        self.assertIn(
            "COPY data/music/",
            text,
            "render-worker-v2/Dockerfile must COPY data/music/ — the "
            "central music bed assets. The resolver in "
            "ducked_loop.py + single_bed.py falls back to "
            "/workspace/data/music/; if the COPY line is removed the "
            "fallback resolves to a path that doesn't exist on the "
            "deployed worker and every render using a default ambient "
            "bed dies at the music stage.",
        )


if __name__ == "__main__":
    unittest.main()
