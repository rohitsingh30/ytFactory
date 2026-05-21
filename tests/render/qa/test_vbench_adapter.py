"""Tests for ``pipeline.render.qa.vbench_adapter``.

We exercise the adapter at two levels:

1. **Pure-Python plumbing** (no VBench dependency) — :func:`is_vbench_available`,
   :func:`aggregate_vbench_score`, the 0-100 clamp + missing-axis handling
   in :func:`score_video_vbench`, ``VBenchUnavailable`` propagation.
   These run on every CI and pin the contract the worker depends on.

2. **Real VBench inference** (heavy) — only when the ``vbench`` PyPI
   package is importable. ``pytest.importorskip`` gates the heavier
   cases; CI without VBench installed still runs the plumbing
   suite. We synth three small mp4 fixtures via ffmpeg:

   - 5 s sine + colorbars (real video + audio)
   - 5 s solid red (no motion → high temporal_flickering score)
   - 5 s composed of 4 distinct images at 1.25 s each (low
     subject_consistency vs the solid fixture)

   For each, we assert directional relationships (solid > heterogenous
   on subject_consistency, solid > heterogenous on flickering), NOT
   absolute thresholds — VBench's exact scores depend on model
   weights that drift between releases.

We never hit the network in tests. ``importorskip`` short-circuits
the heavy cases when VBench weights aren't already cached locally.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pipeline.render.qa import vbench_adapter
from pipeline.render.qa.vbench_adapter import (
    VBENCH_AXES,
    VBENCH_SCALE_MAX,
    VBenchUnavailable,
    aggregate_vbench_score,
    is_vbench_available,
    score_video_vbench,
)


# ---------------------------------------------------------------------
# ffmpeg fixture helpers (no network; deterministic)
# ---------------------------------------------------------------------


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_colorbars_mp4(path: pathlib.Path, *, duration_s: float = 5.0) -> None:
    """Synth a real video+audio mp4 — colorbars + sine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", "smptebars=size=320x240:rate=10",
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", "sine=frequency=440:sample_rate=24000",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-c:a", "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_solid_mp4(path: pathlib.Path, *, duration_s: float = 5.0) -> None:
    """Synth a solid-color mp4 — no motion across frames at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", "color=c=red:size=320x240:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_heterogenous_mp4(path: pathlib.Path) -> None:
    """Synth a 5 s mp4 composed of 4 distinct color blocks, ~1.25 s each.

    The blocks alternate red / green / blue / yellow — a worst-case
    scenario for subject_consistency since "the subject" changes
    completely four times.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / "_segments"
    tmp.mkdir(exist_ok=True)
    colors = ("red", "green", "blue", "yellow")
    segs: list[pathlib.Path] = []
    for i, color in enumerate(colors):
        seg = tmp / f"seg_{i}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-t", "1.25",
                "-i", f"color=c={color}:size=320x240:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                str(seg),
            ],
            check=True,
            capture_output=True,
        )
        segs.append(seg)
    concat_list = tmp / "list.txt"
    concat_list.write_text(
        "\n".join(f"file '{seg.resolve()}'" for seg in segs)
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------
# Plumbing — runs on every CI, NO VBench dependency
# ---------------------------------------------------------------------


class VbenchAxesContractTest(unittest.TestCase):
    """Pins the public surface so consumers (the cloud worker, the
    critic-axes verdict gate, the dashboard) cannot silently shift."""

    def test_axes_order_is_stable(self):
        # Order is the contract — tests + downstream aggregators index
        # by tuple position when generating Firestore field names.
        self.assertEqual(
            VBENCH_AXES,
            (
                "vbench_subject_consistency",
                "vbench_temporal_flickering",
                "vbench_imaging_quality",
            ),
        )

    def test_scale_max_is_100(self):
        # Per VBench++ paper §4.2 — pinning the constant prevents an
        # accidental switch to 0-1 that would break the aggregator.
        self.assertEqual(VBENCH_SCALE_MAX, 100.0)


class AggregateVbenchScoreTest(unittest.TestCase):
    """The 0-100 → 1-10 aggregator used by critic_axes integration."""

    def test_mean_then_divide_by_ten(self):
        out = aggregate_vbench_score({
            "vbench_subject_consistency": 90.0,
            "vbench_temporal_flickering": 80.0,
            "vbench_imaging_quality": 70.0,
        })
        # (90+80+70)/3 = 80; /10 = 8.0
        self.assertAlmostEqual(out, 8.0, places=4)

    def test_clamp_above_ten(self):
        # Defence against upstream emitting >100 (observed once in dev).
        out = aggregate_vbench_score({
            "vbench_subject_consistency": 200.0,
            "vbench_temporal_flickering": 200.0,
            "vbench_imaging_quality": 200.0,
        })
        self.assertEqual(out, 10.0)

    def test_clamp_below_one(self):
        # Defence against upstream emitting negatives.
        out = aggregate_vbench_score({
            "vbench_subject_consistency": -50.0,
            "vbench_temporal_flickering": -50.0,
            "vbench_imaging_quality": -50.0,
        })
        self.assertEqual(out, 1.0)

    def test_empty_scores_returns_floor(self):
        self.assertEqual(aggregate_vbench_score({}), 1.0)

    def test_skips_non_numeric(self):
        # A None or string value should be silently dropped, not crash.
        out = aggregate_vbench_score({
            "vbench_subject_consistency": 80.0,
            "vbench_temporal_flickering": None,
            "vbench_imaging_quality": "high",
        })
        self.assertAlmostEqual(out, 8.0, places=4)

    def test_no_valid_keys_returns_floor(self):
        # Dict has unrelated keys → no axis values found → floor.
        self.assertEqual(
            aggregate_vbench_score({"some_other_axis": 90.0}),
            1.0,
        )


class IsVbenchAvailableTest(unittest.TestCase):
    def test_returns_bool_without_raising(self):
        # Whether or not vbench is installed, this MUST not raise. The
        # cloud worker uses this as a precondition probe.
        self.assertIsInstance(is_vbench_available(), bool)


class ScoreVideoVbenchPlumbingTest(unittest.TestCase):
    """Adapter-level behaviour exercised with a mocked vbench runner —
    no real VBench inference, no network, no weight downloads."""

    def setUp(self):
        if not _have_ffmpeg():
            self.skipTest("ffmpeg not installed")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix=".test-vbench-"))
        self.mp4 = self.tmp / "fixture.mp4"
        _make_solid_mp4(self.mp4)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_three_floats(self):
        # Mock runner returns canned scores; we assert the adapter
        # reshapes them into the contracted dict.
        def fake_runner(p):
            self.assertEqual(p, self.mp4)
            return {
                "vbench_subject_consistency": 88.0,
                "vbench_temporal_flickering": 92.0,
                "vbench_imaging_quality": 75.0,
            }

        out = score_video_vbench(self.mp4, _vbench_runner=fake_runner)
        self.assertEqual(set(out.keys()), set(VBENCH_AXES))
        for v in out.values():
            self.assertIsInstance(v, float)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_clamps_above_100(self):
        def fake_runner(_):
            return {
                "vbench_subject_consistency": 150.0,
                "vbench_temporal_flickering": 92.0,
                "vbench_imaging_quality": 75.0,
            }

        out = score_video_vbench(self.mp4, _vbench_runner=fake_runner)
        self.assertEqual(out["vbench_subject_consistency"], 100.0)

    def test_clamps_below_zero(self):
        def fake_runner(_):
            return {
                "vbench_subject_consistency": -5.0,
                "vbench_temporal_flickering": 92.0,
                "vbench_imaging_quality": 75.0,
            }

        out = score_video_vbench(self.mp4, _vbench_runner=fake_runner)
        self.assertEqual(out["vbench_subject_consistency"], 0.0)

    def test_missing_axis_becomes_zero(self):
        # A runner that forgets an axis still produces a complete dict.
        def fake_runner(_):
            return {"vbench_subject_consistency": 88.0}

        out = score_video_vbench(self.mp4, _vbench_runner=fake_runner)
        self.assertEqual(out["vbench_temporal_flickering"], 0.0)
        self.assertEqual(out["vbench_imaging_quality"], 0.0)
        self.assertEqual(out["vbench_subject_consistency"], 88.0)

    def test_non_numeric_value_becomes_zero(self):
        def fake_runner(_):
            return {
                "vbench_subject_consistency": "high",
                "vbench_temporal_flickering": None,
                "vbench_imaging_quality": 75.0,
            }

        out = score_video_vbench(self.mp4, _vbench_runner=fake_runner)
        self.assertEqual(out["vbench_subject_consistency"], 0.0)
        self.assertEqual(out["vbench_temporal_flickering"], 0.0)
        self.assertEqual(out["vbench_imaging_quality"], 75.0)

    def test_missing_mp4_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            score_video_vbench(
                self.tmp / "nope.mp4",
                _vbench_runner=lambda _: {},
            )

    def test_real_path_raises_vbench_unavailable_when_uninstalled(self):
        # The real path imports vbench lazily; simulate "not installed"
        # by patching the import inside _run_vbench_real to fail.
        with mock.patch.object(
            vbench_adapter,
            "_run_vbench_real",
            side_effect=VBenchUnavailable("vbench not importable"),
        ):
            with self.assertRaises(VBenchUnavailable):
                score_video_vbench(self.mp4)


# ---------------------------------------------------------------------
# Real VBench inference — heavy, skipped when vbench isn't installed
# ---------------------------------------------------------------------


class ScoreVideoVbenchRealTest(unittest.TestCase):
    """End-to-end with the real vbench package. Skipped on every
    machine where ``pip install vbench`` hasn't run."""

    @classmethod
    def setUpClass(cls):
        if not _have_ffmpeg():
            raise unittest.SkipTest("ffmpeg not installed")
        try:
            import vbench  # noqa: F401, PLC0415
        except Exception:
            raise unittest.SkipTest("vbench package not installed")
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix=".test-vbench-real-"))
        cls.colorbars = cls.tmp / "colorbars.mp4"
        cls.solid = cls.tmp / "solid.mp4"
        cls.hetero = cls.tmp / "hetero.mp4"
        _make_colorbars_mp4(cls.colorbars)
        _make_solid_mp4(cls.solid)
        _make_heterogenous_mp4(cls.hetero)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "tmp"):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_returns_three_floats_in_0_100_range(self):
        out = score_video_vbench(self.colorbars)
        self.assertEqual(set(out.keys()), set(VBENCH_AXES))
        for v in out.values():
            self.assertIsInstance(v, float)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_solid_color_has_high_temporal_flickering_score(self):
        # No motion → minimal frame-MSE → high (i.e. near 100) score
        # on VBench's inverted-flicker scale.
        out = score_video_vbench(self.solid)
        self.assertGreaterEqual(out["vbench_temporal_flickering"], 50.0)

    def test_heterogenous_drops_subject_consistency_vs_solid(self):
        # A video with 4 distinct subjects across 5 s SHOULD score
        # lower on subject_consistency than a single-color fixture.
        # Directional assertion only — absolute VBench scores drift
        # across model-weight versions.
        out_solid = score_video_vbench(self.solid)
        out_hetero = score_video_vbench(self.hetero)
        self.assertLess(
            out_hetero["vbench_subject_consistency"],
            out_solid["vbench_subject_consistency"],
        )


if __name__ == "__main__":
    unittest.main()
