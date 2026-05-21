"""Provider-capabilities table coverage.

Audit Q2.66 originally enforced that ``_PROVIDER_CAPABILITIES`` declared
every provider that had a cloudrun wrapper, so the dispatcher couldn't
silently reject an opt-in. Post 2026-05-16 cost-optimization sweep
only ``cloudrun_z_image_turbo`` remains. This file now pins both
directions of the contract: the surviving provider IS registered, and
the dropped providers (qwen_image, hidream, flux2_klein, flux2_dev)
are NOT.

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

    def test_dropped_providers_not_registered(self):
        # flux2_klein, flux2_dev, qwen_image, hidream + all azure_* dropped
        # 2026-05-16 — see docs/cost_optimized_deploy.md.
        for dropped in (
            "cloudrun_flux2_klein", "cloudrun_flux2_dev",
            "cloudrun_qwen_image", "cloudrun_hidream",
            "azure_flux2_klein", "azure_z_image_turbo",
        ):
            with self.subTest(provider=dropped):
                self.assertNotIn(dropped, _PROVIDER_CAPABILITIES)


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
