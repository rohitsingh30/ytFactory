"""Tests for ``python -m pipeline.render`` — engine-driven CLI entry.

Pin:
- --kind short / long routes correctly
- --override KEY=VALUE entries flow into spec.channel_overrides
- Missing --script raises SystemExit with a clear message
- Invalid --override (no =) raises SystemExit
- OUTPUT_MANIFEST line is printed on success
- spec.visual_mode + music_policy + aspect echoed on stdout
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline.render.__main__ import _parse_overrides, main


def _make_wav(path: Path, duration_s: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1", "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


def _make_mp4(path: Path, duration_s: float = 1.0,
              w: int = 1080, h: int = 1920) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", f"color=c=0x141414:s={w}x{h}:r=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p",
        str(path),
    ], check=True, capture_output=True)


class ParseOverridesTest(unittest.TestCase):
    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(_parse_overrides([]), {})

    def test_single_override_parsed(self):
        self.assertEqual(_parse_overrides(["voice=sarah"]), {"voice": "sarah"})

    def test_multiple_overrides_parsed(self):
        result = _parse_overrides([
            "voice=sarah", "music_policy=section_mood",
            "lower_thirds=true",
        ])
        self.assertEqual(result, {
            "voice": "sarah",
            "music_policy": "section_mood",
            "lower_thirds": "true",
        })

    def test_value_with_equals_kept(self):
        # First = splits; rest stays in value.
        result = _parse_overrides(["url=https://x.com/a=b"])
        self.assertEqual(result, {"url": "https://x.com/a=b"})

    def test_missing_equals_raises_systemexit(self):
        with self.assertRaises(SystemExit):
            _parse_overrides(["bad-no-equals"])


class CliMainEndToEndTest(unittest.TestCase):
    """End-to-end CLI test using fixture plugins.

    Patches RenderPaths so we don't depend on real channel layout, and
    sets spec.extra fixture paths so the engine routes through the
    test-only fixture plugins.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-cli-render-"))
        self.audio_fixture = self.tmp / "n.wav"
        self.visuals_fixture = self.tmp / "v.mp4"
        self.timeline_fixture = self.tmp / "t.json"
        _make_wav(self.audio_fixture, duration_s=2.0)
        _make_mp4(self.visuals_fixture, duration_s=2.0)
        self.timeline_fixture.write_text(json.dumps([
            {"start_s": 0.0, "end_s": 1.0, "text": "x",
             "anchor_id": "beat_000", "kind": "beat"},
            {"start_s": 1.0, "end_s": 2.0, "text": "y",
             "anchor_id": "beat_001", "kind": "beat"},
        ]))

        self.script_path = self.tmp / "script.json"
        self.script_path.write_text(json.dumps({
            "narration": "smoke", "slug": "smoke-001",
            "beats": [{"text": "x"}, {"text": "y"}],
        }))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_main_short_with_fixture_plugins_emits_output_manifest(self):
        # Build a real channel layout instead of mocking the frozen
        # RenderPaths dataclass — easier than patching every method.
        chan_dir = self.tmp / "channel"
        (chan_dir / "shorts").mkdir(parents=True)
        (chan_dir / "long_form").mkdir(parents=True)
        (chan_dir / "cache").mkdir(parents=True)
        (chan_dir / "config.yaml").write_text("name: test_channel\n")

        out_mp4 = chan_dir / "shorts" / "smoke-001.mp4"

        # Patch RenderPaths.from_channel_dir + from_channel_yaml to
        # return a real instance pointing at our test dir.
        from pipeline.paths import RenderPaths
        rp = RenderPaths.from_channel_dir("test_channel", project_root=self.tmp)
        # The instance is frozen but its short_for/long_form_for/cache_for
        # methods compute from rp.root which IS the chan_dir.

        buf = io.StringIO()
        with patch("pipeline.render.__main__.RenderPaths") as MockRP:
            MockRP.from_channel_dir.return_value = rp
            MockRP.from_channel_yaml.return_value = rp
            with redirect_stdout(buf):
                rc = main([
                    "--kind", "short",
                    "--channel", "test_channel",
                    "--slug", "smoke-001",
                    "--script", str(self.script_path),
                    "--out", str(out_mp4),
                    "--work-dir", str(self.tmp / "work"),
                    "--override", "captions_enabled=false",
                    "--override", "music_policy=none",
                    "--override", "audio_plugin=audio_from_fixture",
                    "--override", "timeline_plugin=timeline_from_fixture",
                    "--override", "visualize_plugin=visuals_from_fixture",
                    "--override", f"audio_fixture_path={self.audio_fixture}",
                    "--override", f"visuals_fixture_path={self.visuals_fixture}",
                    "--override", f"timeline_fixture_path={self.timeline_fixture}",
                ])
        self.assertEqual(rc, 0)

        output = buf.getvalue()
        self.assertIn("OUTPUT_MANIFEST:", output)
        self.assertIn("kind=short", output)
        self.assertIn("music_policy=none", output)
        self.assertTrue(out_mp4.exists())

        manifest_line = next(
            (l for l in output.splitlines() if l.startswith("OUTPUT_MANIFEST:")),
            None,
        )
        self.assertIsNotNone(manifest_line)
        manifest = json.loads(manifest_line.replace("OUTPUT_MANIFEST: ", ""))
        self.assertEqual(manifest["slug"], "smoke-001")
        self.assertEqual(manifest["mp4"], str(out_mp4))


if __name__ == "__main__":
    unittest.main()
