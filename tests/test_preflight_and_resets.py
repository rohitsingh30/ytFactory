"""Tests for the shared preflight / MLX-state-reset module.

Tier-1 wiring fix (2026-05-05): the WindowServer-watchdog preflight
that previously lived inline in long_form.py was extracted to
pipeline/preflight.py so the SAME guard fires for footage_only,
shorts, and sports_doc renderers — not just long_form.

These tests cover:
- power_check refuses Low Power Mode
- power_check warns (does not block) on battery
- power_check is bypassable via YTFACTORY_SKIP_POWER_CHECK=1
- power_check no-ops on non-Darwin platforms
- power_check no-ops when pmset is missing
- reset_mlx_state never raises even if MLX / audio modules aren't loaded
- reset_mlx_state actually calls audio.reset_f5_state when drop_f5=True
- Each renderer's main() / render() calls power_check
- Each renderer's main() / render() calls reset_mlx_state after TTS
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from pipeline.quality import preflight


class PowerCheckTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("YTFACTORY_SKIP_POWER_CHECK")
        os.environ.pop("YTFACTORY_SKIP_POWER_CHECK", None)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("YTFACTORY_SKIP_POWER_CHECK", None)
        else:
            os.environ["YTFACTORY_SKIP_POWER_CHECK"] = self._prev

    def test_low_power_mode_raises_systemexit(self):
        fake = (
            "Currently drawing from 'AC Power'\n"
            " lowpowermode            1\n"
        )
        with patch("subprocess.check_output", return_value=fake), \
             patch.object(sys, "platform", "darwin"):
            with self.assertRaises(SystemExit) as cm:
                preflight.power_check(label="long-form")
        self.assertIn("Low Power Mode", str(cm.exception))
        self.assertIn("long-form", str(cm.exception))

    def test_label_appears_in_rejection_message(self):
        fake = " lowpowermode            1\n"
        with patch("subprocess.check_output", return_value=fake), \
             patch.object(sys, "platform", "darwin"):
            with self.assertRaises(SystemExit) as cm:
                preflight.power_check(label="footage-only Shorts/long-form")
        self.assertIn("footage-only Shorts/long-form", str(cm.exception))

    def test_normal_state_passes(self):
        fake = (
            "Currently drawing from 'AC Power'\n"
            " lowpowermode            0\n"
        )
        with patch("subprocess.check_output", return_value=fake), \
             patch.object(sys, "platform", "darwin"):
            preflight.power_check()  # no raise

    def test_skip_env_bypasses_check(self):
        os.environ["YTFACTORY_SKIP_POWER_CHECK"] = "1"
        fake = " lowpowermode            1\n"
        with patch("subprocess.check_output", return_value=fake), \
             patch.object(sys, "platform", "darwin"):
            preflight.power_check()  # no raise

    def test_battery_warns_does_not_block(self):
        fake = (
            "Currently drawing from 'Battery Power'\n"
            " lowpowermode            0\n"
        )
        with patch("subprocess.check_output", return_value=fake), \
             patch.object(sys, "platform", "darwin"):
            preflight.power_check()  # no raise

    def test_pmset_missing_does_not_block(self):
        with patch("subprocess.check_output", side_effect=FileNotFoundError("pmset")), \
             patch.object(sys, "platform", "darwin"):
            preflight.power_check()  # no raise

    def test_non_darwin_skips_check(self):
        with patch.object(sys, "platform", "linux"):
            preflight.power_check()  # no raise


class ResetMlxStateTests(unittest.TestCase):
    """``reset_mlx_state`` is a hygiene call. It must never raise even if
    pipeline.audio.audio / pipeline.images / mlx aren't importable. And when
    they ARE importable, it must actually drop the F5 singleton.

    All tests stub out mlx.core so the real Metal device is never
    touched (avoids nanobind re-registration warnings under repeated
    test runs and works on CI without MLX installed).
    """

    def setUp(self):
        self._fake_mx = MagicMock()
        self._fake_mx.clear_cache = MagicMock()
        self._fake_mx.metal = MagicMock()
        self._fake_mx.metal.clear_cache = MagicMock()
        self._mx_patcher = patch.dict(sys.modules, {"mlx.core": self._fake_mx})
        self._mx_patcher.start()

    def tearDown(self):
        self._mx_patcher.stop()

    def test_no_raise_when_image_reset_fails(self):
        # Simulate images.reset_image_state raising — function should swallow.
        from pipeline.images import images as _img
        with patch.object(_img, "reset_image_state", side_effect=RuntimeError("boom"), create=True):
            try:
                preflight.reset_mlx_state(drop_image=True)
            except Exception as e:  # noqa: BLE001
                self.fail(f"reset_mlx_state should not raise: {e}")

    def test_drop_f5_is_now_a_noop(self):
        # 2026-05-09 (laptop nuclear cleanup): the F5-MLX singleton was
        # removed when local TTS providers were ripped out. drop_f5 stays
        # in the signature for back-compat but does nothing.
        preflight.reset_mlx_state(drop_f5=True, label="noop-test")
        # No assertion — function should just complete without raising
        # and without calling any non-existent reset_f5_state.

    def test_calls_image_reset_when_available(self):
        from pipeline.images import images as _img
        # If reset_image_state isn't defined yet (Tier-3 hasn't shipped),
        # add a stub via patch.object — but use create=True to allow it.
        with patch.object(_img, "reset_image_state", MagicMock(), create=True) as mock_reset:
            preflight.reset_mlx_state(drop_f5=False, drop_image=True)
        mock_reset.assert_called_once()

    def test_skips_image_reset_when_function_missing(self):
        # Temporarily remove the attribute if present, ensure no crash.
        from pipeline.images import images as _img
        had_attr = hasattr(_img, "reset_image_state")
        original = getattr(_img, "reset_image_state", None)
        if had_attr:
            delattr(_img, "reset_image_state")
        try:
            preflight.reset_mlx_state(drop_f5=False, drop_image=True)
        finally:
            if had_attr and original is not None:
                _img.reset_image_state = original  # type: ignore[attr-defined]


class RendererPreflightWiringTests(unittest.TestCase):
    """The engine dispatch entry point MUST call preflight.power_check at
    the top of its driver. If someone refactors and drops the call, the
    Low-Power-Mode crash class regresses silently."""

    def _read(self, mod) -> str:
        with open(mod.__file__) as f:
            return f.read()

    def test_engine_main_calls_power_check(self):
        from pipeline.render import __main__ as engine_main
        src = self._read(engine_main)
        self.assertIn("power_check(", src)


class RendererF5ResetWiringTests(unittest.TestCase):
    """Each engine that may use F5-TTS-MLX must drop the singleton at the
    audio-stage boundary so 1.35 GB doesn't leak into video/mux."""

    def _read(self, mod) -> str:
        with open(mod.__file__) as f:
            return f.read()

    def test_short_engine_drops_f5_at_stage_boundary(self):
        from pipeline.render import short_engine
        src = self._read(short_engine)
        self.assertIn("reset_mlx_state(drop_f5=True", src)
        self.assertIn("short stage-1 TTS", src)

    def test_long_engine_drops_f5_at_stage_boundary(self):
        from pipeline.render import long_engine
        src = self._read(long_engine)
        self.assertIn("reset_mlx_state(drop_f5=True", src)
        self.assertIn("long stage-1 TTS", src)


if __name__ == "__main__":
    unittest.main()
