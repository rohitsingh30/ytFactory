"""Pin the 2026-05-18 (round 7) setpts-stretch fix in
``BeatSlideshowMux.mux``.

Pre-fix the mux padded a too-short visual track to ``audio.duration_s``
with ``tpad=stop_mode=clone:stop_duration=N``, which clones the LAST
frame for the overrun. On render-41f77152 this would have meant 14s
of static beat_012.png after the 27s slideshow ended — viewers register
that as a frozen-frame bug ("the video stopped but the audio kept
going"). Post-fix the mux uses ``setpts=PTS*factor`` to smoothly
time-stretch the slideshow to the audio duration, so the visual
plays continuously through the entire audio (slightly slower, no
static tail).

Why this matters even after the ``image_to_static_clip`` frame-cap
fix in compose.py: that fix made each beat clip the right duration,
but the SLIDESHOW total is still ``13 × per_beat`` which doesn't
necessarily match the post-ASR audio duration. The two fixes work
together — the frame-cap restores per-beat correctness, the setpts
stretch fits the total.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.render.compose.beat_slideshow_mux import BeatSlideshowMux
from pipeline.render.contracts import AudioResult, VisualTrack


@dataclass
class _FakeMusic:
    mix_default: float = 0.1


@dataclass
class _FakeSpec:
    output_resolution: tuple[int, int] = (1080, 1920)
    output_fps: int = 30
    music: _FakeMusic = field(default_factory=_FakeMusic)


class SetptsStretchInsteadOfTpadCloneTest(unittest.TestCase):
    """The mux must use setpts to fit the visual track to audio
    duration, not tpad-clone (which freezes the last frame)."""

    def _run_mux_and_capture_cmd(
        self,
        visual_dur_s: float,
        audio_dur_s: float,
    ) -> list[str]:
        """Invoke the mux with mocked run_ffmpeg, return the cmd list."""
        captured: list[str] = []

        def _fake_run_ffmpeg(cmd: list[str]) -> None:
            captured.extend(cmd)

        spec = _FakeSpec()
        visuals = VisualTrack(
            video_path=Path("/tmp/_fake_slideshow.mp4"),
            duration_s=visual_dur_s,
        )
        audio = AudioResult(
            narration_path=Path("/tmp/_fake_narration.wav"),
            duration_s=audio_dur_s,
        )
        with patch(
            "pipeline.render.compose.beat_slideshow_mux.run_ffmpeg",
            side_effect=_fake_run_ffmpeg,
        ):
            BeatSlideshowMux().mux(
                visuals=visuals,
                audio=audio,
                overlays=[],
                music=Path("/tmp/_fake_music.wav"),
                spec=spec,
                out_path=Path("/tmp/_fake_out.mp4"),
            )
        return captured

    def test_short_visual_long_audio_uses_setpts_stretch(self):
        """26s visual + 41s audio → setpts stretches by ≈1.577."""
        cmd = self._run_mux_and_capture_cmd(
            visual_dur_s=26.0, audio_dur_s=41.0,
        )
        filter_str = self._extract_filter_complex(cmd)
        # setpts present with stretch factor close to 41/26 = 1.577.
        self.assertIn("setpts=PTS*", filter_str)
        # Pre-fix it was tpad-clone — that filter must NOT be present.
        self.assertNotIn(
            "tpad=stop_mode=clone", filter_str,
            "tpad-clone froze the last frame for 15s on AITA Shorts — "
            "regression: must use setpts stretch instead",
        )

    def test_stretch_factor_close_to_audio_over_visual_ratio(self):
        """The exact factor in the setpts expression must equal
        audio_dur/visual_dur (within float-formatting tolerance)."""
        import re

        cmd = self._run_mux_and_capture_cmd(
            visual_dur_s=26.0, audio_dur_s=41.0,
        )
        filter_str = self._extract_filter_complex(cmd)
        m = re.search(r"setpts=PTS\*([\d.]+)", filter_str)
        self.assertIsNotNone(m, f"no setpts factor in:\n{filter_str}")
        factor = float(m.group(1))
        expected = 41.0 / 26.0
        self.assertAlmostEqual(factor, expected, places=2)

    def test_visual_already_longer_than_audio_is_no_op_stretch(self):
        """When visual >= audio, stretch_factor=1.0 (no stretch) and
        the downstream ``-t audio.duration_s`` cap trims the overrun."""
        import re

        cmd = self._run_mux_and_capture_cmd(
            visual_dur_s=45.0, audio_dur_s=41.0,
        )
        filter_str = self._extract_filter_complex(cmd)
        m = re.search(r"setpts=PTS\*([\d.]+)", filter_str)
        self.assertIsNotNone(m, f"no setpts in:\n{filter_str}")
        factor = float(m.group(1))
        # max(1.0, 41/45) = max(1.0, 0.911) = 1.0
        self.assertAlmostEqual(factor, 1.0, places=2)

    def test_audio_duration_cap_still_present(self):
        """The ``-t audio.duration_s`` output cap MUST still trim the
        final mp4 — setpts stretch on its own can overshoot by a frame
        or two due to float quantisation."""
        cmd = self._run_mux_and_capture_cmd(
            visual_dur_s=26.0, audio_dur_s=41.0,
        )
        # The cmd should have a ``-t 41.000`` (or similar precision).
        self.assertIn("-t", cmd)
        t_idx = cmd.index("-t")
        # Find the LAST -t in the cmd (it's an output flag, not input).
        # Find from the END to be safe.
        for i in range(len(cmd) - 1, -1, -1):
            if cmd[i] == "-t":
                t_idx = i
                break
        t_val = float(cmd[t_idx + 1])
        self.assertAlmostEqual(t_val, 41.0, places=2)

    def _extract_filter_complex(self, cmd: list[str]) -> str:
        if "-filter_complex" not in cmd:
            return ""
        return cmd[cmd.index("-filter_complex") + 1]


if __name__ == "__main__":
    unittest.main()
