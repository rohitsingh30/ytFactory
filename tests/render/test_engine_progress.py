"""Pin the substage-boundary ``progress_cb`` events fired by the
short and long engines (post-2026-05-14 bigbang).

After the engine bigbang `_run_renderer_via_engines` runs in-process
instead of shelling out, so the cloud worker's stdout-classifier path
no longer sees the legacy `[1/4] TTS …` markers between stages where
the plugin is fully native. The fix plumbs ``progress_cb(stage, msg)``
through ``render_via_engines`` → ``render_short`` / ``render_long`` so
each engine fires explicit boundary events for tts / asr / images /
compose. Without these the dashboard's substage pills freeze at
"pending" while the in-process render runs.

These tests use the same fixture-loading plugin variants as
test_short_engine_golden / test_long_engine_golden so they're fast
(<5s), deterministic, and exercise the REAL engine wiring.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# Eager-import every plugin package so the registry is populated.
import pipeline.render.audio  # noqa: F401
import pipeline.render.compose  # noqa: F401
import pipeline.render.music  # noqa: F401
import pipeline.render.overlays  # noqa: F401
import pipeline.render.timeline  # noqa: F401
import pipeline.render.visualize  # noqa: F401
from pipeline.render.long_engine import render_long
from pipeline.render.short_engine import render_short
from pipeline.render.spec import build_spec


def _make_fixture_wav(path: Path, duration_s: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-c:a", "pcm_s16le", str(path),
    ], check=True)


def _make_fixture_mp4(path: Path, duration_s: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "color=size=720x1280:rate=24:color=black",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


def _make_fixture_beats(path: Path, duration_s: float = 2.0) -> None:
    """Match the timeline_from_fixture loader's expected JSON shape."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 2
    each = duration_s / n
    beats = [
        {
            "start_s": i * each,
            "end_s": (i + 1) * each,
            "text": f"beat {i}",
            "anchor_id": f"beat_{i:03d}",
            "kind": "beat",
        }
        for i in range(n)
    ]
    path.write_text(json.dumps(beats))


class _BaseEngineProgressTest(unittest.TestCase):
    DURATION_S = 2.0

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audio_fixture = self.tmp / "fixtures" / "narration.wav"
        self.visuals_fixture = self.tmp / "fixtures" / "visuals.mp4"
        self.timeline_fixture = self.tmp / "fixtures" / "beats.json"
        _make_fixture_wav(self.audio_fixture, duration_s=self.DURATION_S)
        _make_fixture_mp4(self.visuals_fixture, duration_s=self.DURATION_S)
        _make_fixture_beats(self.timeline_fixture, duration_s=self.DURATION_S)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _events_by_stage(self, events):
        out: dict[str, list[str]] = {}
        for stage, msg in events:
            out.setdefault(stage, []).append(msg)
        return out


class ShortEngineBoundaryEventsTest(_BaseEngineProgressTest):
    """``render_short`` MUST fire at least one event for every
    substage pill (``tts`` / ``asr`` / ``images`` / ``compose``) so
    the dashboard's timeline shows live transitions instead of
    freezing at "pending"."""

    def _build_spec(self):
        return build_spec({
            "channel": "test_channel",
            "channel_overrides": {
                "kind": "short",
                "captions_enabled": False,
                "music_policy": "none",
                "audio_plugin": "audio_from_fixture",
                "timeline_plugin": "timeline_from_fixture",
                "visualize_plugin": "visuals_from_fixture",
                "audio_fixture_path": str(self.audio_fixture),
                "visuals_fixture_path": str(self.visuals_fixture),
                "timeline_fixture_path": str(self.timeline_fixture),
            },
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_render_short_fires_event_for_every_substage(self):
        spec = self._build_spec()
        events: list[tuple[str, str]] = []
        out = self.tmp / "out" / "short.mp4"
        render_short(
            spec,
            script={"narration": "smoke test", "beats": [
                {"text": "first"}, {"text": "second"},
            ]},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=lambda s, m: events.append((s, m)),
        )

        by_stage = self._events_by_stage(events)
        # Every substage pill must see at least one event so the
        # dashboard knows it actually ran.
        for required in ("tts", "asr", "images", "compose"):
            self.assertIn(
                required, by_stage,
                f"engine never fired a {required!r} event; "
                f"dashboard pill would freeze at 'pending'. "
                f"events={events!r}",
            )

    def test_render_short_compose_emits_done_event_after_mux(self):
        """The final ``("compose", "Wrote X.mp4")`` event closes out
        the timeline so the user sees the render genuinely
        finished — not just a stuck "Stitching" message."""
        spec = self._build_spec()
        events: list[tuple[str, str]] = []
        out = self.tmp / "out" / "short.mp4"
        render_short(
            spec,
            script={"narration": "smoke", "beats": [
                {"text": "first"}, {"text": "second"},
            ]},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=lambda s, m: events.append((s, m)),
        )

        compose_msgs = [m for s, m in events if s == "compose"]
        self.assertTrue(
            any("Wrote" in m for m in compose_msgs),
            f"missing final 'Wrote …' compose event; got {compose_msgs!r}",
        )

    def test_render_short_progress_cb_optional(self):
        """``progress_cb=None`` must work — laptop CLI / tests don't
        always wire one up. Pre-fix the engine signature didn't
        accept progress_cb at all (engine bigbang docstring said
        'Drops progress_cb'), so omitting it was fine. We pin
        explicit ``None`` semantics so future refactors don't
        require it."""
        spec = self._build_spec()
        out = self.tmp / "out" / "short.mp4"
        # Should not raise.
        render_short(
            spec,
            script={"narration": "smoke", "beats": [
                {"text": "first"}, {"text": "second"},
            ]},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=None,
        )

    def test_render_short_swallows_progress_cb_exception(self):
        """A user-supplied callback that raises must NOT kill the
        render — same policy as the legacy stdout-tail path
        (_tail_renderer_log)."""
        spec = self._build_spec()
        out = self.tmp / "out" / "short.mp4"

        def boom(stage, msg):
            raise RuntimeError("simulated cb failure")

        # Render must complete despite the cb raising on every event.
        result = render_short(
            spec,
            script={"narration": "smoke", "beats": [
                {"text": "first"}, {"text": "second"},
            ]},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=boom,
        )
        self.assertEqual(result, out)
        self.assertTrue(out.exists())


class LongEngineBoundaryEventsTest(_BaseEngineProgressTest):
    """Same coverage as ShortEngineBoundaryEventsTest but for
    ``render_long``. The long engine has no overlap path — fully
    sequential."""

    def _build_spec(self):
        return build_spec({
            "channel": "test_channel",
            "channel_overrides": {
                "kind": "long_form",
                "captions_enabled": False,
                "music_policy": "none",
                "audio_plugin": "audio_from_fixture",
                "timeline_plugin": "timeline_from_fixture",
                "visualize_plugin": "visuals_from_fixture",
                "audio_fixture_path": str(self.audio_fixture),
                "visuals_fixture_path": str(self.visuals_fixture),
                "timeline_fixture_path": str(self.timeline_fixture),
                "compose_plugin": "beat_slideshow",  # section_video needs
                                                     # extras the fixture
                                                     # path doesn't carry
            },
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_render_long_fires_event_for_every_substage(self):
        spec = self._build_spec()
        events: list[tuple[str, str]] = []
        out = self.tmp / "out" / "long.mp4"
        render_long(
            spec,
            script={"narration": "longform smoke test"},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=lambda s, m: events.append((s, m)),
        )

        by_stage = self._events_by_stage(events)
        for required in ("tts", "asr", "images", "compose"):
            self.assertIn(
                required, by_stage,
                f"long engine never fired a {required!r} event; "
                f"dashboard pill would freeze at 'pending'. "
                f"events={events!r}",
            )

    def test_render_long_compose_emits_done_event_after_mux(self):
        spec = self._build_spec()
        events: list[tuple[str, str]] = []
        out = self.tmp / "out" / "long.mp4"
        render_long(
            spec,
            script={"narration": "longform smoke"},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=lambda s, m: events.append((s, m)),
        )

        compose_msgs = [m for s, m in events if s == "compose"]
        self.assertTrue(
            any("Wrote" in m for m in compose_msgs),
            f"missing final 'Wrote …' compose event; got {compose_msgs!r}",
        )

    def test_render_long_progress_cb_optional(self):
        spec = self._build_spec()
        out = self.tmp / "out" / "long.mp4"
        render_long(
            spec,
            script={"narration": "longform smoke"},
            work_dir=self.tmp / "work",
            out_path=out,
            progress_cb=None,
        )


if __name__ == "__main__":
    unittest.main()
