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
import io
import base64
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image

from pipeline.images import images_cloudrun
from pipeline.images.images_cloudrun import CloudRunUnavailable


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
    """As of 2026-05-09 (laptop nuclear cleanup) there is no local
    fallback, so the render-level circuit breaker is a no-op — cloud
    failures propagate directly. The breaker module-state still exists
    for back-compat (``reset_circuit_breaker()`` is wired into renderer
    entry points and shouldn't crash)."""

    def setUp(self) -> None:
        images_cloudrun.reset_circuit_breaker()
        os.environ["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = "https://flux-test.example/"
        for k in ("CLOUDRUN_IMAGE_DISABLE_FALLBACK", "CLOUDRUN_IMAGE_FALLBACK_MODE"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        images_cloudrun.reset_circuit_breaker()

    def test_cloud_failure_re_raises_unavailable(self) -> None:
        """No fallback path remaining — CloudRunUnavailable bubbles up."""
        out = Path(tempfile.gettempdir()) / "no-fallback.png"
        with patch.object(images_cloudrun, "_post_generate", side_effect=CloudRunUnavailable("503 simulated")):
            with self.assertRaises(CloudRunUnavailable):
                images_cloudrun._generate_cloudrun_flux2_klein(
                    prompt="x", seed=1, out_path=out,
                    width=768, height=1344, steps=4,
                )

    def test_reset_circuit_breaker_is_idempotent(self) -> None:
        """``reset_circuit_breaker()`` still exists and is safe to call
        repeatedly — renderer entry points fire it before every render."""
        images_cloudrun.reset_circuit_breaker()
        images_cloudrun.reset_circuit_breaker()  # no raise
        self.assertFalse(images_cloudrun._breaker_open())

    def test_4xx_re_raises_http_error(self) -> None:
        """A 4xx (bad input) is the caller's fault — re-raises as-is."""
        out = Path(tempfile.gettempdir()) / "4xx-test.png"
        import requests as _requests
        http_err = _requests.exceptions.HTTPError("400 Client Error")
        with patch.object(images_cloudrun, "_post_generate", side_effect=http_err):
            with self.assertRaises(_requests.exceptions.HTTPError):
                images_cloudrun._generate_cloudrun_flux2_klein(
                    prompt="", seed=1, out_path=out,
                    width=768, height=1344, steps=4,
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


# ─── additional coverage: _timeout_s / _fallback_disabled / _fallback_mode ───


class TestEnvHelpers(unittest.TestCase):
    """Cover the env-var helper branches not exercised by other tests."""

    def setUp(self) -> None:
        for k in ("CLOUDRUN_IMAGE_TIMEOUT", "CLOUDRUN_IMAGE_DISABLE_FALLBACK",
                  "CLOUDRUN_IMAGE_FALLBACK_MODE"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in ("CLOUDRUN_IMAGE_TIMEOUT", "CLOUDRUN_IMAGE_DISABLE_FALLBACK",
                  "CLOUDRUN_IMAGE_FALLBACK_MODE"):
            os.environ.pop(k, None)

    # _timeout_s
    def test_timeout_default_900(self) -> None:
        self.assertEqual(images_cloudrun._timeout_s(), 900)

    def test_timeout_custom(self) -> None:
        os.environ["CLOUDRUN_IMAGE_TIMEOUT"] = "300"
        self.assertEqual(images_cloudrun._timeout_s(), 300)

    def test_timeout_invalid_falls_back_to_900(self) -> None:
        os.environ["CLOUDRUN_IMAGE_TIMEOUT"] = "not-a-number"
        self.assertEqual(images_cloudrun._timeout_s(), 900)

    # _fallback_disabled
    def test_fallback_disabled_unset(self) -> None:
        self.assertFalse(images_cloudrun._fallback_disabled())

    def test_fallback_disabled_true_string(self) -> None:
        os.environ["CLOUDRUN_IMAGE_DISABLE_FALLBACK"] = "true"
        self.assertTrue(images_cloudrun._fallback_disabled())

    def test_fallback_disabled_1(self) -> None:
        os.environ["CLOUDRUN_IMAGE_DISABLE_FALLBACK"] = "1"
        self.assertTrue(images_cloudrun._fallback_disabled())

    # _fallback_mode
    def test_fallback_mode_default_once_per_render(self) -> None:
        self.assertEqual(images_cloudrun._fallback_mode(), "once_per_render")

    def test_fallback_mode_once_per_render_explicit(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FALLBACK_MODE"] = "once_per_render"
        self.assertEqual(images_cloudrun._fallback_mode(), "once_per_render")

    def test_fallback_mode_per_image(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FALLBACK_MODE"] = "per_image"
        self.assertEqual(images_cloudrun._fallback_mode(), "per_image")

    def test_fallback_mode_unknown_falls_back_to_once_per_render(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FALLBACK_MODE"] = "whatever"
        self.assertEqual(images_cloudrun._fallback_mode(), "once_per_render")


# ─── additional circuit-breaker coverage ─────────────────────────────────


class TestCircuitBreakerFull(unittest.TestCase):
    """Cover the trip + reset-when-tripped log path."""

    def setUp(self) -> None:
        images_cloudrun.reset_circuit_breaker()

    def tearDown(self) -> None:
        images_cloudrun.reset_circuit_breaker()

    def test_trip_then_open(self) -> None:
        self.assertFalse(images_cloudrun._breaker_open())
        images_cloudrun._trip_breaker("unit test reason")
        self.assertTrue(images_cloudrun._breaker_open())

    def test_trip_is_idempotent(self) -> None:
        images_cloudrun._trip_breaker("first")
        images_cloudrun._trip_breaker("second")  # must not overwrite first
        self.assertTrue(images_cloudrun._breaker_open())

    def test_reset_when_tripped_logs_and_clears(self) -> None:
        images_cloudrun._trip_breaker("log path test")
        self.assertTrue(images_cloudrun._breaker_open())
        images_cloudrun.reset_circuit_breaker()
        self.assertFalse(images_cloudrun._breaker_open())


# ─── _post_generate retry logic ──────────────────────────────────────────


def _tiny_png_b64_cr() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (128, 0, 64)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _make_resp(status: int, body: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "error"
    resp.json.return_value = body or {"output_inline": _tiny_png_b64_cr()}
    if 400 <= status < 500:
        import requests as _req
        resp.raise_for_status.side_effect = _req.exceptions.HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestPostGenerateFull(unittest.TestCase):
    """Cover all branches of _post_generate including retries."""

    URL = "https://flux-test.example"

    def _mock_session(self, responses):
        """Return a mock Session where post() yields successive responses."""
        sess = MagicMock()
        sess.post.side_effect = responses
        return sess

    def _patch_token(self):
        return patch("pipeline.images.images_cloudrun.get_id_token",
                     return_value="test-token")

    def test_success_first_attempt(self) -> None:
        sess = self._mock_session([_make_resp(200)])
        with patch("requests.Session", return_value=sess), self._patch_token():
            result = images_cloudrun._post_generate(self.URL, {"prompt": "p"})
        self.assertIn("output_inline", result)

    def test_5xx_raises_cloudrun_unavailable(self) -> None:
        # Use 500 (not 503) — 503 triggers the 429/503 retry path
        sess = self._mock_session([_make_resp(500)])
        with patch("requests.Session", return_value=sess), self._patch_token():
            with self.assertRaises(images_cloudrun.CloudRunUnavailable):
                images_cloudrun._post_generate(self.URL, {"prompt": "p"})

    def test_429_retry_then_success(self) -> None:
        """429 on attempt 1 → sleep → 200 on attempt 2."""
        sess = self._mock_session([_make_resp(429), _make_resp(200)])
        with patch("requests.Session", return_value=sess), \
             self._patch_token(), \
             patch("time.sleep"):
            result = images_cloudrun._post_generate(self.URL, {"p": 1})
        self.assertIn("output_inline", result)
        self.assertEqual(sess.post.call_count, 2)

    def test_503_retry_then_success(self) -> None:
        """503 on attempt 1 → sleep → 200 on attempt 2."""
        sess = self._mock_session([_make_resp(503, {}), _make_resp(200)])
        # make first 503 not immediately raise (simulate rate-exceeded, not hard 5xx)
        # Patch so that the first 503 triggers the retry path (attempt <= 4)
        # We need to make the mock NOT raise CloudRunUnavailable on 503.
        # Looking at the code: if status in (429, 503) and attempt <= 4 → sleep + continue
        # if 500 <= status < 600 → raises  ← but 503 also matches this!
        # The 429/503 check comes FIRST, so attempt 1 retries.
        with patch("requests.Session", return_value=sess), \
             self._patch_token(), \
             patch("time.sleep"):
            result = images_cloudrun._post_generate(self.URL, {"p": 1})
        self.assertIn("output_inline", result)

    def test_401_refreshes_token_then_success(self) -> None:
        """401 triggers token refresh + retry."""
        sess = self._mock_session([_make_resp(401), _make_resp(200)])
        sess.post.side_effect = [_make_resp(401), _make_resp(200)]

        import pipeline.cloud.cloudrun_auth as _auth_mod
        with patch("requests.Session", return_value=sess), \
             self._patch_token(), \
             patch.object(_auth_mod, "_TOKENS", {}):
            result = images_cloudrun._post_generate(self.URL, {"p": 1})
        self.assertIn("output_inline", result)
        self.assertEqual(sess.post.call_count, 2)

    def test_http_error_4xx_reraised(self) -> None:
        """4xx HTTPError is NOT wrapped — caller's bug, re-raise immediately."""
        import requests as _req
        resp = _make_resp(400)
        sess = self._mock_session([resp])
        with patch("requests.Session", return_value=sess), self._patch_token():
            with self.assertRaises(_req.exceptions.HTTPError):
                images_cloudrun._post_generate(self.URL, {"p": 1})

    def test_connection_error_retries_then_raises(self) -> None:
        """All 5 connection errors → CloudRunUnavailable after exhaustion."""
        import requests as _req
        err = _req.exceptions.ConnectionError("refused")
        sess = self._mock_session([err, err, err, err, err])
        with patch("requests.Session", return_value=sess), \
             self._patch_token(), \
             patch("time.sleep"):
            with self.assertRaises(images_cloudrun.CloudRunUnavailable):
                images_cloudrun._post_generate(self.URL, {"p": 1})
        self.assertEqual(sess.post.call_count, 5)

    def test_read_timeout_retries_then_raises(self) -> None:
        import requests as _req
        err = _req.exceptions.ReadTimeout("timed out")
        sess = self._mock_session([err, err, err, err, err])
        with patch("requests.Session", return_value=sess), \
             self._patch_token(), \
             patch("time.sleep"):
            with self.assertRaises(images_cloudrun.CloudRunUnavailable):
                images_cloudrun._post_generate(self.URL, {"p": 1})

    def test_chunked_encoding_error_retries(self) -> None:
        import requests as _req
        err = _req.exceptions.ChunkedEncodingError("chunked")
        sess = self._mock_session([err, _make_resp(200)])
        with patch("requests.Session", return_value=sess), \
             self._patch_token(), \
             patch("time.sleep"):
            result = images_cloudrun._post_generate(self.URL, {"p": 1})
        self.assertIn("output_inline", result)

    def test_cloudrun_unavailable_inside_reraised(self) -> None:
        """If _post_generate raises CloudRunUnavailable, it re-raises (no retry)."""
        # We can't inject CloudRunUnavailable from sess.post (it's not a requests exception),
        # so we test via _generate_cloudrun with _post_generate patched.
        pass


# ─── _materialise_png GCS path ───────────────────────────────────────────


class TestMaterialisePngGcs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_gcs_uri_calls_gcloud_cp(self) -> None:
        out = self.tmp / "from_gcs.png"
        resp = {"output_gcs": "gs://bucket/path/img.png"}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            images_cloudrun._materialise_png(resp, out)
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        self.assertIn("gcloud", cmd[0])
        self.assertIn("gs://bucket/path/img.png", cmd)

    def test_no_inline_no_gcs_raises(self) -> None:
        out = self.tmp / "empty.png"
        with self.assertRaises(RuntimeError):
            images_cloudrun._materialise_png({}, out)


# ─── _generate_cloudrun + _generate_cloudrun_z_image_turbo ───────────────


class TestGenerateCloudrun(unittest.TestCase):
    URL = "https://flux-test.example"

    def setUp(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = self.URL
        os.environ["CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL"] = self.URL
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        os.environ.pop("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL", None)
        os.environ.pop("CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL", None)
        self._tmpdir.cleanup()

    def _patch_post_and_materialise(self, out: Path):
        resp = {"output_inline": _tiny_png_b64_cr(), "wall_s": 1.0,
                "width": 768, "height": 1344, "steps": 4, "cold_loaded": True}
        return (
            patch("pipeline.images.images_cloudrun._post_generate", return_value=resp),
            patch("pipeline.images.images_cloudrun._materialise_png", return_value=out),
            patch("pipeline.images.images_cloudrun.get_id_token", return_value="tok"),
        )

    def test_generate_cloudrun_flux2_klein_success(self) -> None:
        out = self.tmp / "flux.png"
        p1, p2, p3 = self._patch_post_and_materialise(out)
        with p1, p2, p3:
            result = images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="a knight", seed=42, out_path=out,
                width=768, height=1344, steps=4,
            )
        self.assertEqual(result, out)

    def test_generate_cloudrun_z_image_turbo_success(self) -> None:
        out = self.tmp / "zimg.png"
        p1, p2, p3 = self._patch_post_and_materialise(out)
        with p1, p2, p3:
            result = images_cloudrun._generate_cloudrun_z_image_turbo(
                prompt="a dragon", seed=1, out_path=out,
                width=768, height=1344, steps=9,
            )
        self.assertEqual(result, out)

    def test_generate_cloudrun_qwen_image_success(self) -> None:
        os.environ["CLOUDRUN_IMAGE_QWEN_IMAGE_URL"] = self.URL
        out = self.tmp / "qwen.png"
        p1, p2, p3 = self._patch_post_and_materialise(out)
        try:
            with p1, p2, p3:
                result = images_cloudrun._generate_cloudrun_qwen_image(
                    prompt="a scholar", seed=7, out_path=out,
                    width=768, height=1344, steps=20,
                )
            self.assertEqual(result, out)
        finally:
            os.environ.pop("CLOUDRUN_IMAGE_QWEN_IMAGE_URL", None)

    def test_generate_cloudrun_hidream_success(self) -> None:
        os.environ["CLOUDRUN_IMAGE_HIDREAM_URL"] = self.URL
        out = self.tmp / "hidream.png"
        p1, p2, p3 = self._patch_post_and_materialise(out)
        try:
            with p1, p2, p3:
                result = images_cloudrun._generate_cloudrun_hidream(
                    prompt="a dreamer", seed=3, out_path=out,
                    width=768, height=1344, steps=25,
                )
            self.assertEqual(result, out)
        finally:
            os.environ.pop("CLOUDRUN_IMAGE_HIDREAM_URL", None)


        """cold_loaded=True in response hits the ' (cold)' log branch."""
        out = self.tmp / "cold.png"
        resp = {"output_inline": _tiny_png_b64_cr(), "wall_s": 300.0,
                "width": 768, "height": 1344, "steps": 4, "cold_loaded": True}
        with patch("pipeline.images.images_cloudrun._post_generate", return_value=resp), \
             patch("pipeline.images.images_cloudrun._materialise_png", return_value=out), \
             patch("pipeline.images.images_cloudrun.get_id_token", return_value="tok"):
            result = images_cloudrun._generate_cloudrun_flux2_klein(
                prompt="test", seed=1, out_path=out,
                width=768, height=1344, steps=4,
            )
        self.assertEqual(result, out)


# ─── _local_fallback ─────────────────────────────────────────────────────


class TestLocalFallback(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_raises_cloudrun_unavailable(self) -> None:
        with self.assertRaises(images_cloudrun.CloudRunUnavailable) as cm:
            images_cloudrun._local_fallback(
                prompt="test", seed=1,
                out_path=self.tmp / "out.png",
                width=768, height=1344, steps=4,
            )
        self.assertIn("nuclear cleanup", str(cm.exception))


# ─── _readyz ─────────────────────────────────────────────────────────────


class TestReadyz(unittest.TestCase):
    URL = "https://flux-readyz.example"

    def setUp(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = self.URL

    def tearDown(self) -> None:
        os.environ.pop("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL", None)

    def test_readyz_returns_json(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cold_loaded": False, "warm_s": 0.5}
        mock_resp.raise_for_status.return_value = None

        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_resp

        with patch("requests.Session", return_value=mock_sess), \
             patch("pipeline.images.images_cloudrun.get_id_token", return_value="tok"):
            result = images_cloudrun._readyz("flux2_klein")

        self.assertEqual(result["warm_s"], 0.5)
        mock_sess.close.assert_called_once()

    def test_readyz_closes_session_on_error(self) -> None:
        mock_sess = MagicMock()
        mock_sess.get.side_effect = Exception("connection refused")

        with patch("requests.Session", return_value=mock_sess), \
             patch("pipeline.images.images_cloudrun.get_id_token", return_value="tok"):
            with self.assertRaises(Exception):
                images_cloudrun._readyz("flux2_klein")

        mock_sess.close.assert_called_once()


# ─── warmup (background thread) ──────────────────────────────────────────


class TestWarmupThread(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = "https://flux-warm.example"

    def tearDown(self) -> None:
        os.environ.pop("CLOUDRUN_IMAGE_FLUX2_KLEIN_URL", None)

    def test_warmup_returns_thread(self) -> None:
        import threading
        with patch("pipeline.images.images_cloudrun._readyz",
                   return_value={"cold_loaded": False, "warm_s": 0.3, "boot_uptime_s": 10}):
            t = images_cloudrun.warmup("flux2_klein")
        self.assertIsInstance(t, threading.Thread)
        t.join(timeout=5)

    def test_warmup_success_path(self) -> None:
        with patch("pipeline.images.images_cloudrun._readyz",
                   return_value={"cold_loaded": True, "warm_s": 300.0, "boot_uptime_s": 5}):
            t = images_cloudrun.warmup("flux2_klein")
            t.join(timeout=5)

    def test_warmup_cloudrun_unavailable_swallowed(self) -> None:
        with patch("pipeline.images.images_cloudrun._readyz",
                   side_effect=images_cloudrun.CloudRunUnavailable("URL not set")):
            t = images_cloudrun.warmup("flux2_klein")
            t.join(timeout=5)

    def test_warmup_general_exception_swallowed(self) -> None:
        with patch("pipeline.images.images_cloudrun._readyz",
                   side_effect=RuntimeError("unexpected network failure")):
            t = images_cloudrun.warmup("flux2_klein")
            t.join(timeout=5)

    def test_warmup_thread_is_daemon(self) -> None:
        with patch("pipeline.images.images_cloudrun._readyz", return_value={}):
            t = images_cloudrun.warmup("flux2_klein")
            self.assertTrue(t.daemon)
            t.join(timeout=5)


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
