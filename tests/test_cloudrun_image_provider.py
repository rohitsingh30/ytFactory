"""Tests for `pipeline.images_cloudrun` — laptop-side cloud image
provider with render-level circuit breaker.

Three layers:

1. **Dispatcher + URL routing** — fast, no network. Verifies
   `_service_url(model)` reads the right env var and raises a
   useful error when unset.

2. **Circuit breaker** — the key divergence from the TTS client.
   First failure trips the breaker; subsequent calls in the same
   process skip cloud entirely until `reset_circuit_breaker()`
   clears it. Critical for renders with ~30 image calls.

3. **Live cloud smoke** — actually hits the deployed service and
   confirms a real PNG comes back. Slow (warm ~5 s, cold ~7 min).
   Auto-skipped unless ``CLOUDRUN_IMAGE_LIVE=1`` AND the
   per-model URL env is set::

       export CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=https://...
       CLOUDRUN_IMAGE_LIVE=1 .venv/bin/python -m unittest \\
           tests.test_cloudrun_image_provider
"""
from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image

from pipeline import images_cloudrun
from pipeline.images_cloudrun import CloudRunUnavailable


def _tiny_png_b64() -> str:
    """Return a base64-encoded 32x32 red PNG (used to fake a
    /generate response without bringing up a real model)."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 0, 0)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _fake_generate_response(*, cold: bool = False) -> dict:
    return {
        "model_repo": "test/fake-model",
        "runtime": "diffusers",
        "diffusers_class": "FakePipeline",
        "width": 768, "height": 1344,
        "steps": 4, "guidance_scale": 1.0, "seed": 42,
        "wall_s": 2.5, "cold_loaded": cold,
        "sha256": "deadbeef" * 8,
        "png_bytes": 642,
        "output_inline": _tiny_png_b64(),
    }


# ---------------------------------------------------------- URL routing


class TestServiceUrl(unittest.TestCase):
    """`_service_url(model)` must read the per-model env var."""

    def setUp(self) -> None:
        # Clear any inherited cloudrun image env vars
        for k in list(os.environ):
            if k.startswith("CLOUDRUN_IMAGE_"):
                del os.environ[k]

    def test_routes_per_model(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = "https://flux/"
        os.environ["CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL"] = "https://zimg"
        self.assertEqual(images_cloudrun._service_url("flux2_klein"), "https://flux")
        self.assertEqual(images_cloudrun._service_url("z_image_turbo"), "https://zimg")

    def test_unknown_model_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            images_cloudrun._service_url("definitely-not-a-model")
        self.assertIn("flux2_klein", str(cm.exception))
        self.assertIn("z_image_turbo", str(cm.exception))

    def test_missing_env_raises_cloudrun_unavailable(self) -> None:
        with self.assertRaises(CloudRunUnavailable) as cm:
            images_cloudrun._service_url("flux2_klein")
        self.assertIn("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL", str(cm.exception))


# ---------------------------------------------------------- circuit breaker


class TestCircuitBreaker(unittest.TestCase):
    """Render-level circuit breaker — the key divergence from the
    TTS client's per-call fallback."""

    def setUp(self) -> None:
        images_cloudrun.reset_circuit_breaker()
        os.environ["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = "https://flux-test.example/"
        for k in ("CLOUDRUN_IMAGE_DISABLE_FALLBACK", "CLOUDRUN_IMAGE_FALLBACK_MODE"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        images_cloudrun.reset_circuit_breaker()

    def test_first_failure_trips_breaker_subsequent_skip_cloud(self) -> None:
        """Once-per-render mode: first cloud failure trips, then
        every subsequent call in the same process bypasses cloud
        and goes directly to local fallback. This is the bug class
        the breaker exists to prevent (30 × 600 s timeouts on a
        single Short during a cloud outage)."""
        out = Path(tempfile.gettempdir()) / "breaker-test.png"
        local_calls = 0

        def fake_local(*, prompt, seed, out_path, width, height, steps):
            nonlocal local_calls
            local_calls += 1
            out_path.write_bytes(b"local-fallback")
            return out_path

        # First call: cloud fails → breaker trips, fallback runs.
        with patch.object(images_cloudrun, "_post_generate", side_effect=CloudRunUnavailable("503 simulated")), \
             patch.object(images_cloudrun, "_local_fallback", side_effect=fake_local) as mock_local:
            images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="x", seed=1, out_path=out,
                width=768, height=1344, steps=4,
            )
        self.assertEqual(local_calls, 1)
        self.assertTrue(images_cloudrun._breaker_open(),
                        "First failure must trip the breaker")

        # Second call: must NOT touch _post_generate (breaker open).
        post_call_count = 0

        def boom_if_called(*a, **kw):
            nonlocal post_call_count
            post_call_count += 1
            raise AssertionError("breaker open should have skipped cloud")

        with patch.object(images_cloudrun, "_post_generate", side_effect=boom_if_called), \
             patch.object(images_cloudrun, "_local_fallback", side_effect=fake_local):
            images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="y", seed=2, out_path=out,
                width=768, height=1344, steps=4,
            )
        self.assertEqual(post_call_count, 0,
                         "Breaker open: cloud must not be re-attempted")
        self.assertEqual(local_calls, 2)

    def test_reset_clears_breaker(self) -> None:
        """`reset_circuit_breaker()` clears the per-render flag so
        the NEXT render gets a fresh chance at cloud."""
        out = Path(tempfile.gettempdir()) / "breaker-reset.png"

        def fake_local(*, prompt, seed, out_path, width, height, steps):
            out_path.write_bytes(b"local")
            return out_path

        with patch.object(images_cloudrun, "_post_generate", side_effect=CloudRunUnavailable("first failure")), \
             patch.object(images_cloudrun, "_local_fallback", side_effect=fake_local):
            images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="x", seed=1, out_path=out,
                width=768, height=1344, steps=4,
            )
        self.assertTrue(images_cloudrun._breaker_open())

        images_cloudrun.reset_circuit_breaker()
        self.assertFalse(images_cloudrun._breaker_open())

        # After reset the next call SHOULD reach _post_generate again.
        with patch.object(images_cloudrun, "_post_generate", return_value=_fake_generate_response()) as mock_post:
            images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="z", seed=3, out_path=out,
                width=768, height=1344, steps=4,
            )
            mock_post.assert_called_once()

    def test_per_image_mode_does_not_trip_breaker(self) -> None:
        """`CLOUDRUN_IMAGE_FALLBACK_MODE=per_image` keeps legacy
        per-call fallback (no breaker) for debugging."""
        os.environ["CLOUDRUN_IMAGE_FALLBACK_MODE"] = "per_image"
        out = Path(tempfile.gettempdir()) / "per-image-mode.png"

        def fake_local(*, prompt, seed, out_path, width, height, steps):
            out_path.write_bytes(b"local")
            return out_path

        with patch.object(images_cloudrun, "_post_generate", side_effect=CloudRunUnavailable("503")), \
             patch.object(images_cloudrun, "_local_fallback", side_effect=fake_local):
            images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="x", seed=1, out_path=out,
                width=768, height=1344, steps=4,
            )
        self.assertFalse(
            images_cloudrun._breaker_open(),
            "per_image mode must NOT trip the breaker",
        )

    def test_disable_fallback_env_var_re_raises(self) -> None:
        """`CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` must surface cloud
        failures (canary use)."""
        os.environ["CLOUDRUN_IMAGE_DISABLE_FALLBACK"] = "1"
        out = Path(tempfile.gettempdir()) / "no-fallback.png"
        with patch.object(images_cloudrun, "_post_generate", side_effect=CloudRunUnavailable("503")), \
             patch.object(images_cloudrun, "_local_fallback") as mock_local:
            with self.assertRaises(CloudRunUnavailable):
                images_cloudrun._generate_cloudrun_flux2_klein(
                    prompt="x", seed=1, out_path=out,
                    width=768, height=1344, steps=4,
                )
            mock_local.assert_not_called()

    def test_4xx_does_not_fall_back(self) -> None:
        """A 4xx (bad input — wrong aspect, empty prompt) is the
        caller's fault, not a service outage. It should re-raise the
        HTTPError, NOT trigger fallback."""
        out = Path(tempfile.gettempdir()) / "4xx-test.png"
        # `requests` raises HTTPError on resp.raise_for_status(); our
        # _post_generate re-raises as-is for 4xx (no wrap, no breaker).
        import requests as _requests
        http_err = _requests.exceptions.HTTPError("400 Client Error")
        with patch.object(images_cloudrun, "_post_generate", side_effect=http_err):
            with self.assertRaises(_requests.exceptions.HTTPError):
                images_cloudrun._generate_cloudrun_flux2_klein(
                    prompt="", seed=1, out_path=out,
                    width=768, height=1344, steps=4,
                )
        self.assertFalse(
            images_cloudrun._breaker_open(),
            "4xx must NOT trip the breaker (caller bug, not outage)",
        )


# ----------------------------------------------------- materialise_png


class TestMaterialisePng(unittest.TestCase):
    def test_inline_base64_writes_png(self) -> None:
        out = Path(tempfile.gettempdir()) / "materialise-inline.png"
        if out.exists():
            out.unlink()
        resp = {"output_inline": _tiny_png_b64()}
        images_cloudrun._materialise_png(resp, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)
        # Sanity-check that it's a real PNG (PIL can re-open).
        with Image.open(out) as im:
            self.assertEqual(im.size, (32, 32))

    def test_no_inline_no_gcs_raises(self) -> None:
        out = Path(tempfile.gettempdir()) / "materialise-fail.png"
        with self.assertRaises(RuntimeError):
            images_cloudrun._materialise_png({"sha256": "abc"}, out)


# --------------------------------------------------------------- step clamping


class TestStepClamping(unittest.TestCase):
    def test_in_range_passes_through(self) -> None:
        self.assertEqual(images_cloudrun._clamp_steps(4, lo=2, hi=8, default=4), 4)

    def test_below_lo_uses_default(self) -> None:
        self.assertEqual(images_cloudrun._clamp_steps(1, lo=2, hi=8, default=4), 4)

    def test_above_hi_uses_default(self) -> None:
        self.assertEqual(images_cloudrun._clamp_steps(20, lo=2, hi=8, default=4), 4)


# --------------------------------------------------------------- live smoke


@unittest.skipUnless(
    os.environ.get("CLOUDRUN_IMAGE_LIVE") == "1"
    and os.environ.get("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"),
    "set CLOUDRUN_IMAGE_LIVE=1 (and CLOUDRUN_IMAGE_FLUX2_KLEIN_URL) to enable",
)
class TestCloudRunLiveSmokeFlux(unittest.TestCase):
    """Hits the real Cloud Run service. Slow — instance may need to
    cold-load (~7 min)."""

    def test_generate_returns_real_png(self) -> None:
        out = Path(tempfile.gettempdir()) / "cloudrun-live-flux.png"
        if out.exists():
            out.unlink()
        images_cloudrun._generate_cloudrun_flux2_klein(
            prompt="a serene mountain temple at dawn, photorealistic",
            seed=42, out_path=out,
            width=768, height=1344, steps=4,
        )
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 100_000,
                           "PNG should be >100 KB at 768x1344")
        with Image.open(out) as im:
            self.assertEqual(im.size, (768, 1344))


if __name__ == "__main__":
    unittest.main()
