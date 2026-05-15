"""Tests for pipeline/render/compose/beat_slideshow_mux.py.

Pin the FinalMux Protocol contract + actual ffmpeg invocation that
stacks visuals + audio + music + overlays into the final mp4.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline.render.compose.beat_slideshow_mux import BeatSlideshowMux
from pipeline.render.contracts import (
    AudioResult,
    FinalMux,
    OverlayElement,
    VisualTrack,
)
from pipeline.render.spec import build_spec


def _make_mp4(path: Path, duration_s: float = 2.0,
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


def _make_wav(path: Path, duration_s: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


def _ffprobe(path: Path, *args) -> str:
    return subprocess.check_output(
        ["ffprobe", "-v", "error", *args, str(path)]
    ).decode().strip()


class BeatSlideshowMuxProtocolTest(unittest.TestCase):
    def test_satisfies_final_mux_protocol(self):
        self.assertIsInstance(BeatSlideshowMux(), FinalMux)


class BeatSlideshowMuxRealFfmpegTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-beat-mux-"))
        self.visuals_path = self.tmp / "visuals.mp4"
        self.audio_path = self.tmp / "narr.wav"
        self.music_path = self.tmp / "music.wav"
        _make_mp4(self.visuals_path, duration_s=2.0)
        _make_wav(self.audio_path, duration_s=2.0)
        _make_wav(self.music_path, duration_s=2.0)

        self.spec = build_spec(
            {"channel": "x", "channel_overrides": {"music_policy": "ducked_loop"}},
            channel_yaml_path=None, variant_yaml_path=None,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mux_with_no_overlays_produces_valid_mp4(self):
        out_path = self.tmp / "out_no_overlays.mp4"
        result = BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=2.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=2.0),
            overlays=[],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        self.assertEqual(result, out_path)
        self.assertTrue(out_path.exists())
        self.assertGreater(out_path.stat().st_size, 0)

    def test_mux_output_codec_is_h264_aac(self):
        out_path = self.tmp / "out_codec.mp4"
        BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=2.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=2.0),
            overlays=[],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        v_codec = _ffprobe(out_path, "-select_streams", "v:0",
                           "-show_entries", "stream=codec_name",
                           "-of", "default=nokey=1:noprint_wrappers=1")
        a_codec = _ffprobe(out_path, "-select_streams", "a:0",
                           "-show_entries", "stream=codec_name",
                           "-of", "default=nokey=1:noprint_wrappers=1")
        self.assertEqual(v_codec, "h264")
        self.assertEqual(a_codec, "aac")

    def test_mux_resolution_matches_spec(self):
        out_path = self.tmp / "out_res.mp4"
        BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=2.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=2.0),
            overlays=[],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        wxh = _ffprobe(out_path, "-select_streams", "v:0",
                       "-show_entries", "stream=width,height",
                       "-of", "csv=p=0").replace(",", "x")
        self.assertEqual(wxh, "1080x1920")

    def test_mux_caps_duration_to_audio(self):
        # Audio is 2.0s; visuals is 2.0s; music is 2.0s. Output should
        # be ~2.0s, capped by the -t flag = audio.duration_s.
        out_path = self.tmp / "out_dur.mp4"
        BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=2.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=2.0),
            overlays=[],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        dur = float(_ffprobe(out_path, "-show_entries", "format=duration",
                             "-of", "default=nokey=1:noprint_wrappers=1"))
        self.assertAlmostEqual(dur, 2.0, delta=0.05)

    def test_mux_overlay_xy_default_caption_layer(self):
        # Layer 40 (captions) → bottom-centered placement.
        # Just verify the mux completes — no overlay PNG provided so we
        # use a minimal 1x1 PNG fixture.
        png_path = self.tmp / "overlay.png"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=white:s=200x100:d=0.1",
            "-frames:v", "1",
            str(png_path),
        ], check=True, capture_output=True)

        out_path = self.tmp / "out_with_overlay.mp4"
        BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=2.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=2.0),
            overlays=[OverlayElement(
                start_s=0.5, end_s=1.5, layer=40, asset_path=png_path,
            )],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        self.assertTrue(out_path.exists())
        self.assertGreater(out_path.stat().st_size, 0)


class BeatSlideshowMuxOverlayXyTest(unittest.TestCase):
    """Pin the per-layer overlay positioning convention."""

    def setUp(self):
        self.mux = BeatSlideshowMux()
        self.spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )

    def test_explicit_region_wins(self):
        ov = OverlayElement(start_s=0, end_s=1, layer=40,
                            asset_path=Path("/tmp/x.png"),
                            region=(100, 200, 300, 400))
        x, y = self.mux._overlay_xy(ov, self.spec)
        self.assertEqual(x, "100")
        self.assertEqual(y, "200")

    def test_caption_layer_default_bottom_centered(self):
        ov = OverlayElement(start_s=0, end_s=1, layer=40,
                            asset_path=Path("/tmp/x.png"))
        x, y = self.mux._overlay_xy(ov, self.spec)
        self.assertEqual(x, "(W-w)/2")
        self.assertEqual(y, "H-h-200")

    def test_lower_third_layer_default_bottom_left(self):
        ov = OverlayElement(start_s=0, end_s=1, layer=20,
                            asset_path=Path("/tmp/x.png"))
        x, y = self.mux._overlay_xy(ov, self.spec)
        self.assertEqual(x, "48")
        self.assertEqual(y, "H-h-48")

    def test_chapter_card_layer_default_centered(self):
        ov = OverlayElement(start_s=0, end_s=1, layer=30,
                            asset_path=Path("/tmp/x.png"))
        x, y = self.mux._overlay_xy(ov, self.spec)
        self.assertEqual(x, "(W-w)/2")
        self.assertEqual(y, "(H-h)/2")


class BeatSlideshowMuxAudioVideoDurationSyncTest(unittest.TestCase):
    """Pin the 2026-05-15 audio-overrun bug.

    Backstory: AITA Short job d3d5b40b rendered with visual_track =
    26s but audio = 40.5s. Pre-fix the compose plugin used::

        -t {audio.duration_s}

    which CAPS the output duration but doesn't EXTEND visuals past
    their source duration. ffmpeg silently truncated audio at 26s
    of paired-stream output AND continued to write 40.5s of audio
    after the visual stream ended → a 14.5s tail of audio over
    nothing in the demuxed mp4.

    Fix: ``tpad=stop_mode=clone:stop_duration=N`` after the scale
    filter clones the last visual frame for the audio overrun.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-beat-mux-pad-"))
        self.visuals_path = self.tmp / "visuals.mp4"
        self.audio_path = self.tmp / "narr.wav"
        self.music_path = self.tmp / "music.wav"
        # Visual SHORTER than audio — the realistic AITA bug shape.
        _make_mp4(self.visuals_path, duration_s=2.0)
        _make_wav(self.audio_path, duration_s=4.0)
        _make_wav(self.music_path, duration_s=4.0)
        self.spec = build_spec(
            {"channel": "x", "channel_overrides": {"music_policy": "ducked_loop"}},
            channel_yaml_path=None, variant_yaml_path=None,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_video_duration_matches_audio_when_visuals_shorter(self):
        out_path = self.tmp / "out_padded.mp4"
        BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=2.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=4.0),
            overlays=[],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        v_dur = float(_ffprobe(out_path, "-select_streams", "v:0",
                               "-show_entries", "stream=duration",
                               "-of", "default=nokey=1:noprint_wrappers=1"))
        a_dur = float(_ffprobe(out_path, "-select_streams", "a:0",
                               "-show_entries", "stream=duration",
                               "-of", "default=nokey=1:noprint_wrappers=1"))
        # Both streams MUST land on the same duration (within 200ms ffmpeg
        # rounding). Pre-fix the AITA Short had a >14s mismatch.
        self.assertAlmostEqual(
            v_dur, a_dur, delta=0.2,
            msg=f"video={v_dur:.2f}s audio={a_dur:.2f}s diff={abs(v_dur-a_dur):.2f}s "
                f"— streams must end together (tpad pad missing?)",
        )
        # Both should be approximately the audio duration (4s).
        self.assertAlmostEqual(v_dur, 4.0, delta=0.2)

    def test_no_pad_when_visuals_already_longer_than_audio(self):
        # Sentinel: when visuals already cover (or exceed) the audio,
        # tpad with stop_duration=0 is a no-op and -t still trims to
        # audio duration.
        _make_mp4(self.visuals_path, duration_s=4.0)
        _make_wav(self.audio_path, duration_s=2.0)
        _make_wav(self.music_path, duration_s=2.0)

        out_path = self.tmp / "out_no_pad.mp4"
        BeatSlideshowMux().mux(
            visuals=VisualTrack(video_path=self.visuals_path, duration_s=4.0),
            audio=AudioResult(narration_path=self.audio_path, duration_s=2.0),
            overlays=[],
            music=self.music_path,
            spec=self.spec,
            out_path=out_path,
        )
        v_dur = float(_ffprobe(out_path, "-select_streams", "v:0",
                               "-show_entries", "stream=duration",
                               "-of", "default=nokey=1:noprint_wrappers=1"))
        a_dur = float(_ffprobe(out_path, "-select_streams", "a:0",
                               "-show_entries", "stream=duration",
                               "-of", "default=nokey=1:noprint_wrappers=1"))
        # Both bounded by audio duration (~2s).
        self.assertAlmostEqual(v_dur, 2.0, delta=0.2)
        self.assertAlmostEqual(a_dur, 2.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
