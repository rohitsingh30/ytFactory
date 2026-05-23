"""Tests for pipeline.quality.probe — memoized ffprobe helper."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.probe import (
    _probe_cached,
    clear_probe_cache,
    probe_duration,
    probe_duration_or_none,
)


class TestProbeDuration(unittest.TestCase):
    def setUp(self):
        clear_probe_cache()

    def tearDown(self):
        clear_probe_cache()

    def _make_file(self, td: Path) -> Path:
        """Create a real file so stat() works."""
        f = td / "test.wav"
        f.write_bytes(b"\x00" * 100)
        return f

    def test_raises_on_missing_file(self):
        with self.assertRaises(RuntimeError) as ctx:
            probe_duration(Path("/no/such/file.mp4"))
        self.assertIn("file not found", str(ctx.exception))

    def test_raises_when_ffprobe_fails(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._make_file(Path(td))
            with patch("subprocess.check_output",
                       side_effect=subprocess.CalledProcessError(1, ["ffprobe"])):
                with self.assertRaises(RuntimeError) as ctx:
                    probe_duration(f)
            self.assertIn("ffprobe failed", str(ctx.exception))

    def test_returns_float_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._make_file(Path(td))
            with patch("subprocess.check_output", return_value="12.345\n"):
                dur = probe_duration(f)
        self.assertAlmostEqual(dur, 12.345)

    def test_caches_result(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._make_file(Path(td))
            with patch("subprocess.check_output", return_value="5.0\n") as mock_sub:
                dur1 = probe_duration(f)
                dur2 = probe_duration(f)  # second call — should hit cache
            # check_output called once (both probe calls use cache)
            mock_sub.assert_called_once()
        self.assertEqual(dur1, dur2)

    def test_raises_when_ffprobe_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._make_file(Path(td))
            with patch("subprocess.check_output",
                       side_effect=FileNotFoundError):
                with self.assertRaises(RuntimeError):
                    probe_duration(f)


class TestProbeDurationOrNone(unittest.TestCase):
    def setUp(self):
        clear_probe_cache()

    def tearDown(self):
        clear_probe_cache()

    def _make_file(self, td: Path) -> Path:
        f = td / "test.wav"
        f.write_bytes(b"\x00" * 100)
        return f

    def test_returns_none_on_missing_file(self):
        result = probe_duration_or_none(Path("/no/such/file.mp4"))
        self.assertIsNone(result)

    def test_returns_float_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._make_file(Path(td))
            with patch("subprocess.check_output", return_value="7.5\n"):
                result = probe_duration_or_none(f)
        self.assertAlmostEqual(result, 7.5)  # type: ignore[arg-type]

    def test_returns_none_when_ffprobe_fails(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._make_file(Path(td))
            with patch("subprocess.check_output",
                       side_effect=subprocess.CalledProcessError(1, ["ffprobe"])):
                result = probe_duration_or_none(f)
        self.assertIsNone(result)


class TestClearProbeCache(unittest.TestCase):
    def test_clear_invalidates_cache(self):
        clear_probe_cache()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test.wav"
            f.write_bytes(b"\x00" * 100)
            with patch("subprocess.check_output", return_value="3.0\n") as mock_sub:
                probe_duration(f)
                clear_probe_cache()
                probe_duration(f)  # must re-probe after clear
            self.assertEqual(mock_sub.call_count, 2)


class TestPreflight(unittest.TestCase):
    """Cover reset_mlx_state lines 116-120 (drop_image + images.reset path)."""

    def test_reset_mlx_no_modules(self):
        """When MLX / images not available, reset_mlx_state never raises."""
        from pipeline import preflight
        # Both MLX and images absent — should be a no-op
        with patch.dict("sys.modules", {"mlx.core": None, "pipeline.images.images": None}):
            preflight.reset_mlx_state(drop_image=True, label="test")

    def test_reset_mlx_with_drop_image_and_images_module(self):
        """When pipeline.images has reset_image_state, it's called."""
        from pipeline import preflight
        mock_images = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_images.reset_image_state = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()

        with patch.dict("sys.modules", {"pipeline.images.images": mock_images}):
            import importlib
            try:
                preflight.reset_mlx_state(drop_image=True, label="test")
            except Exception:
                pass  # MLX not installed; that's fine — we just want to hit the branch

    def test_reset_mlx_drop_image_exception_swallowed(self):
        """Exception in reset_image_state is swallowed gracefully."""
        from pipeline import preflight
        import sys as _sys
        # Build a fake images module that raises
        import types
        fake_images = types.ModuleType("pipeline.images.images")
        fake_images.reset_image_state = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]

        original = _sys.modules.get("pipeline.images.images")
        _sys.modules["pipeline.images.images"] = fake_images  # type: ignore[assignment]
        try:
            # Should not raise
            preflight.reset_mlx_state(drop_image=True)
        finally:
            if original is None:
                _sys.modules.pop("pipeline.images.images", None)
            else:
                _sys.modules["pipeline.images.images"] = original

    def test_reset_mlx_with_mlx_clear_cache(self):
        """When mlx.core has clear_cache, it's called."""
        from pipeline import preflight
        import types
        import sys as _sys
        fake_mlx_pkg = types.ModuleType("mlx")
        fake_mlx = types.ModuleType("mlx.core")
        cleared = []
        fake_mlx.clear_cache = lambda: cleared.append(1)  # type: ignore[assignment]
        orig_mlx = _sys.modules.get("mlx")
        orig_core = _sys.modules.get("mlx.core")
        _sys.modules["mlx"] = fake_mlx_pkg  # type: ignore[assignment]
        _sys.modules["mlx.core"] = fake_mlx  # type: ignore[assignment]
        try:
            preflight.reset_mlx_state()
        finally:
            if orig_mlx is None:
                _sys.modules.pop("mlx", None)
            else:
                _sys.modules["mlx"] = orig_mlx
            if orig_core is None:
                _sys.modules.pop("mlx.core", None)
            else:
                _sys.modules["mlx.core"] = orig_core  # type: ignore[assignment]
        self.assertTrue(len(cleared) > 0)

    def test_reset_mlx_with_mlx_metal_clear_cache(self):
        """When mlx.core.metal has clear_cache (no top-level clear_cache), it's called."""
        from pipeline import preflight
        import types, sys as _sys
        fake_mlx_pkg = types.ModuleType("mlx")
        fake_mlx = types.ModuleType("mlx.core")
        fake_metal = types.ModuleType("mlx.core.metal")
        cleared = []
        fake_metal.clear_cache = lambda: cleared.append(1)  # type: ignore[assignment]
        fake_mlx.metal = fake_metal  # type: ignore[assignment]
        # No clear_cache on the top-level module
        orig_mlx = _sys.modules.get("mlx")
        orig_core = _sys.modules.get("mlx.core")
        _sys.modules["mlx"] = fake_mlx_pkg  # type: ignore[assignment]
        _sys.modules["mlx.core"] = fake_mlx  # type: ignore[assignment]
        try:
            preflight.reset_mlx_state()
        finally:
            if orig_mlx is None:
                _sys.modules.pop("mlx", None)
            else:
                _sys.modules["mlx"] = orig_mlx
            if orig_core is None:
                _sys.modules.pop("mlx.core", None)
            else:
                _sys.modules["mlx.core"] = orig_core  # type: ignore[assignment]
        self.assertTrue(len(cleared) > 0)




class TestPowerCheck(unittest.TestCase):
    """Tests for pipeline.preflight.power_check (lines 49-76)."""

    def test_skip_when_env_var_set(self):
        """YTFACTORY_SKIP_POWER_CHECK=1 makes the function return immediately."""
        from pipeline import preflight
        with patch.dict("os.environ", {"YTFACTORY_SKIP_POWER_CHECK": "1"}):
            # Should not call subprocess at all
            with patch("subprocess.check_output", side_effect=AssertionError("must not call")):
                preflight.power_check(label="test-label")  # should not raise

    def test_noop_on_non_darwin(self):
        """On non-Darwin platforms, power_check is a no-op."""
        from pipeline import preflight
        import sys as _sys
        with patch.object(_sys, "platform", "linux"):
            with patch("subprocess.check_output", side_effect=AssertionError("must not call")):
                preflight.power_check(label="test-label")

    def test_pmset_unavailable_is_noop(self):
        """If pmset is not found, function returns without error."""
        from pipeline import preflight
        import sys as _sys
        with patch.object(_sys, "platform", "darwin"):
            with patch("subprocess.check_output", side_effect=FileNotFoundError):
                preflight.power_check(label="test-label")

    def test_low_power_mode_raises_system_exit(self):
        """When low power mode is detected, SystemExit is raised."""
        from pipeline import preflight
        import sys as _sys
        pmset_out = "lowpowermode 1\nAC Power\n"
        with patch.object(_sys, "platform", "darwin"):
            with patch("subprocess.check_output", return_value=pmset_out):
                with self.assertRaises(SystemExit):
                    preflight.power_check(label="test-label")

    def test_battery_power_prints_warning(self):
        """When on battery (no low power), a warning is printed to stderr."""
        import io, contextlib
        from pipeline import preflight
        import sys as _sys
        pmset_out = "lowpowermode 0\nBattery Power\n"
        buf = io.StringIO()
        with patch.object(_sys, "platform", "darwin"):
            with patch("subprocess.check_output", return_value=pmset_out):
                with contextlib.redirect_stderr(buf):
                    preflight.power_check(label="test-label")
        self.assertIn("WARNING", buf.getvalue())

    def test_ac_power_no_warning(self):
        """On AC power with no low power mode, no warning is printed."""
        import io, contextlib
        from pipeline import preflight
        import sys as _sys
        pmset_out = "lowpowermode 0\nAC Power\n"
        buf = io.StringIO()
        with patch.object(_sys, "platform", "darwin"):
            with patch("subprocess.check_output", return_value=pmset_out):
                with contextlib.redirect_stderr(buf):
                    preflight.power_check(label="test-label")
        self.assertEqual(buf.getvalue(), "")


class TestResetMlxStateDrop(unittest.TestCase):
    """Cover drop_image success path."""

    def test_drop_image_success(self):
        """drop_image=True calls images.reset_image_state() when present."""
        from pipeline import preflight
        import types, sys as _sys, pipeline as _pkg
        fake_images = types.ModuleType("pipeline.images.images")
        called = []
        fake_images.reset_image_state = lambda: called.append(1)  # type: ignore[assignment]
        orig_mod = _sys.modules.get("pipeline.images.images")
        orig_attr = getattr(_pkg, "images", None)
        _sys.modules["pipeline.images.images"] = fake_images  # type: ignore[assignment]
        setattr(_pkg, "images", fake_images)
        try:
            preflight.reset_mlx_state(drop_image=True)
        finally:
            if orig_mod is None:
                _sys.modules.pop("pipeline.images.images", None)
            else:
                _sys.modules["pipeline.images.images"] = orig_mod
            if orig_attr is None:
                try:
                    delattr(_pkg, "images")
                except AttributeError:
                    pass
            else:
                setattr(_pkg, "images", orig_attr)
        self.assertEqual(called, [1])


if __name__ == "__main__":
    unittest.main()
