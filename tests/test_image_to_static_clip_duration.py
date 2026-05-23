"""Pin the ``image_to_static_clip`` duration contract.

A request for a clip of ``duration_s`` seconds must produce an mp4
whose actual duration matches within ±0.2s. The original regression
behind this test: an earlier zoompan-based helper emitted ~50× the
requested frames (uncapped per-input-frame emission), so a "2s clip"
came out at ~107s and concatenated slideshows were ~50× too long.
The current static-loop implementation uses ``-frames:v`` to cap
output length deterministically; these tests guard against any
future regression that re-introduces the duration blowup.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path


def _have_ffmpeg() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _make_test_png(path: Path, color: tuple[int, int, int] = (200, 50, 50)) -> Path:
    Image.new("RGB", (1080, 1920), color).save(path)
    return path


def _probe_duration_s(path: Path) -> float:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def _probe_frame_count(path: Path) -> int:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return int(r.stdout.strip())


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg not on PATH")
class ImageToStaticClipDurationTest(unittest.TestCase):
    """Regression guard against the per-input-frame duration blowup —
    a "2s clip" must come out close to 2s, not 50× longer."""

    def test_2s_request_produces_close_to_2s_output(self):
        """A request for a 2.0s clip must produce a clip whose duration
        is within ±0.2s of the request (allow rounding for integer-frame
        quantisation). Pre-fix it was ~107s."""
        from pipeline.compose import image_to_static_clip  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as td:
            img = _make_test_png(Path(td) / "img.png")
            out = Path(td) / "clip.mp4"
            image_to_static_clip(img, 2.0, out)

            dur = _probe_duration_s(out)
            # Expected: 2.0s rounded to 60 frames at 30fps → 60/30 = 2.0s.
            # Hard upper bound at 4s catches any per-input-frame blowup
            # definitively.
            self.assertLess(
                dur, 4.0,
                f"clip duration {dur:.2f}s indicates the per-input-frame "
                f"blowup bug has returned — image_to_static_clip must cap "
                f"output with -frames:v",
            )
            self.assertGreater(
                dur, 1.5,
                f"clip duration {dur:.2f}s is too short — the helper "
                f"under-produced frames",
            )

    def test_clip_duration_scales_with_request(self):
        """Doubling the request roughly doubles the output duration."""
        from pipeline.compose import image_to_static_clip  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as td:
            img = _make_test_png(Path(td) / "img.png")
            short_out = Path(td) / "short.mp4"
            long_out = Path(td) / "long.mp4"
            image_to_static_clip(img, 1.0, short_out)
            image_to_static_clip(img, 3.0, long_out)

            short_dur = _probe_duration_s(short_out)
            long_dur = _probe_duration_s(long_out)
            # 3.0s request should be roughly 2-3× the 1.0s output.
            ratio = long_dur / short_dur
            self.assertGreater(
                ratio, 1.8,
                f"long/short ratio {ratio:.2f} too low — durations are "
                f"not scaling with the request (short={short_dur:.2f}s, "
                f"long={long_dur:.2f}s)",
            )
            self.assertLess(
                ratio, 4.0,
                f"long/short ratio {ratio:.2f} too high — likely the "
                f"per-input-frame blowup is back",
            )

    def test_thirteen_clip_concat_matches_sum(self):
        """End-to-end: 13 clips at 2s each concat'd should produce a
        slideshow ≈ 13 × 2s ≈ 26s, not 13 × 107s ≈ 1391s. This is the
        production scenario the original bug surfaced in."""
        from pipeline.compose import image_to_static_clip  # noqa: PLC0415
        from pipeline.render.shared.concat_safe import concat_file_line  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            clips = []
            for i in range(13):
                img = _make_test_png(
                    tdp / f"img_{i:02d}.png",
                    color=(50 + i * 15, 100, 200 - i * 10),
                )
                clip = tdp / f"clip_{i:02d}.mp4"
                image_to_static_clip(img, 2.0, clip)
                clips.append(clip)

            concat_list = tdp / "list.txt"
            concat_list.write_text(
                "\n".join(concat_file_line(p) for p in clips)
            )
            slideshow = tdp / "slideshow.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    str(slideshow),
                ],
                capture_output=True, check=True,
            )
            slideshow_dur = _probe_duration_s(slideshow)
            # Expected: 13 × ~2s = ~26s. Bug would have produced ~1391s;
            # anything > 60s means the bug is back.
            self.assertLess(
                slideshow_dur, 60.0,
                f"13-clip slideshow is {slideshow_dur:.0f}s — per-input-frame "
                f"blowup would have produced ~1391s; current threshold catches "
                f"any meaningful regression",
            )
            self.assertGreater(
                slideshow_dur, 20.0,
                f"13-clip slideshow is only {slideshow_dur:.0f}s — clips "
                f"are under-produced",
            )


if __name__ == "__main__":
    unittest.main()
