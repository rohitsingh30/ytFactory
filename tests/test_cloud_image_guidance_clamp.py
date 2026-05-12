"""Audit Q2.67 — `guidance_scale` field bounds clamp instead of reject.

Pre-fix `cloud/image-flux2-klein/server.py:183` used
``Field(1.0, ge=1.0, le=1.0)`` and `image-z-image-turbo/server.py:134`
used ``Field(0.0, ge=0.0, le=0.0)``. pydantic-Field bounds REJECT
(422), they don't clamp. A client passing the documented "stable
API" guidance_scale=1.0 (or 0.0) could fail through float rounding
(e.g. 1.0000001 != 1.0).

Tests exercise:
  cloud/image-flux2-klein/server.py
  cloud/image-z-image-turbo/server.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


def _import_server(rel_dir: str, mod_alias: str):
    """Load cloud/<rel_dir>/server.py with heavyweight deps stubbed."""
    fake = sys.modules.copy()
    for mod_name in ("torch", "torchvision", "transformers",
                     "diffusers", "accelerate", "PIL", "numpy",
                     "huggingface_hub", "safetensors",
                     "transformers.utils"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    server_path = Path(__file__).resolve().parent.parent / "cloud" / rel_dir / "server.py"
    spec = importlib.util.spec_from_file_location(mod_alias, server_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        sys.modules.clear()
        sys.modules.update(fake)
        raise
    return module


class TestFlux2KleinGuidanceClamp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _import_server("image-flux2-klein", "ytf_test_flux2klein")

    def _make_in(self, **kw):
        return self.module.GenerateIn(prompt="hi", **kw)

    def test_default_guidance_is_1_0(self):
        m = self._make_in()
        self.assertEqual(m.guidance_scale, 1.0)

    def test_guidance_scale_2_0_is_clamped_to_1_0(self):
        # Audit Q2.67 — pre-fix this raised pydantic ValidationError.
        m = self._make_in(guidance_scale=2.0)
        self.assertEqual(m.guidance_scale, 1.0)

    def test_guidance_scale_off_by_rounding_is_accepted(self):
        # The exact regression the audit flagged.
        m = self._make_in(guidance_scale=1.0000001)
        self.assertEqual(m.guidance_scale, 1.0)

    def test_guidance_scale_zero_is_clamped_to_1_0(self):
        m = self._make_in(guidance_scale=0.0)
        self.assertEqual(m.guidance_scale, 1.0)


class TestZImageTurboGuidanceClamp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _import_server("image-z-image-turbo", "ytf_test_zimageturbo")

    def _make_in(self, **kw):
        return self.module.GenerateIn(prompt="hi", **kw)

    def test_default_guidance_is_0_0(self):
        m = self._make_in()
        self.assertEqual(m.guidance_scale, 0.0)

    def test_guidance_scale_5_0_is_clamped_to_0_0(self):
        m = self._make_in(guidance_scale=5.0)
        self.assertEqual(m.guidance_scale, 0.0)

    def test_guidance_scale_off_by_rounding_is_accepted(self):
        m = self._make_in(guidance_scale=0.0000001)
        self.assertEqual(m.guidance_scale, 0.0)


if __name__ == "__main__":
    unittest.main()
