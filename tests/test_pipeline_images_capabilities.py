"""Provider-capabilities table coverage.

Pins that the sole production image provider, ``cloudrun_z_image_turbo``,
is registered in the capabilities table that the validator reads.

Tests exercise: pipeline/images/images.py
"""
from __future__ import annotations

import unittest

from pipeline.images.images import _PROVIDER_CAPABILITIES, validate_provider_config


class TestProviderCapabilitiesCoverage(unittest.TestCase):
    def test_z_image_turbo_provider_registered(self):
        self.assertIn("cloudrun_z_image_turbo", _PROVIDER_CAPABILITIES)

    def test_validate_z_image_turbo_provider_no_unknown_error(self):
        errs = validate_provider_config(
            "cloudrun_z_image_turbo", width=1024, height=1024, steps=8,
        )
        for e in errs:
            self.assertNotIn("unknown image_provider", e.lower())


class TestProviderCapabilitiesShape(unittest.TestCase):
    """Every entry must have the four keys the validator reads."""

    def test_every_entry_has_required_keys(self):
        required = {"native_dim", "max_dim", "step_range", "vertical_9_16_safe"}
        for name, cap in _PROVIDER_CAPABILITIES.items():
            with self.subTest(provider=name):
                self.assertTrue(
                    required.issubset(cap.keys()),
                    f"{name} missing: {required - cap.keys()}",
                )


if __name__ == "__main__":
    unittest.main()
