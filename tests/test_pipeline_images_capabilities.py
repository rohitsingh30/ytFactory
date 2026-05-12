"""Audit Q2.66 — `_PROVIDER_CAPABILITIES` must include every provider
that has a wrapper in `pipeline/images/images_cloudrun.py`.

Pre-fix the table was missing `cloudrun_qwen_image` + `cloudrun_hidream`
even though the cloudrun client ships per-model wrappers for both.
``validate_provider_config`` returned "unknown image_provider" for
those values → the wrappers were unreachable from the dispatcher.

Tests exercise: pipeline/images/images.py
"""
from __future__ import annotations

import unittest

from pipeline.images.images import _PROVIDER_CAPABILITIES, validate_provider_config


class TestProviderCapabilitiesCoverage(unittest.TestCase):
    def test_qwen_image_provider_registered(self):
        self.assertIn("cloudrun_qwen_image", _PROVIDER_CAPABILITIES)

    def test_hidream_provider_registered(self):
        self.assertIn("cloudrun_hidream", _PROVIDER_CAPABILITIES)

    def test_validate_qwen_provider_no_unknown_error(self):
        errs = validate_provider_config(
            "cloudrun_qwen_image", width=1024, height=1024, steps=20,
        )
        for e in errs:
            self.assertNotIn("unknown image_provider", e.lower())

    def test_validate_hidream_provider_no_unknown_error(self):
        errs = validate_provider_config(
            "cloudrun_hidream", width=1024, height=1024, steps=20,
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
