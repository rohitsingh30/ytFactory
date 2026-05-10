"""100% line-coverage tests for pipeline.tts.azure_aks.

All HTTP, filesystem, and auth calls are mocked — no real network / GPU
needed. Uses only stdlib unittest (no pytest).
"""
from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pipeline.tts.azure_aks as _mod


class TestServiceUrl(unittest.TestCase):
    def test_unknown_model_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _mod._service_url("nonexistent_model")
        self.assertIn("unknown Azure TTS model", str(ctx.exception))

    def test_env_set_returns_stripped_url(self):
        with patch.dict(os.environ, {"AZURE_TTS_F5_URL": "https://az.example.com/tts/ "}):
            url = _mod._service_url("f5")
        self.assertEqual(url, "https://az.example.com/tts")

    def test_env_unset_raises_azure_unavailable(self):
        env = {k: "" for k in [
            "AZURE_TTS_F5_URL", "AZURE_TTS_HIGGS_URL", "AZURE_TTS_COSYVOICE_URL",
            "AZURE_TTS_CHATTERBOX_URL", "AZURE_TTS_INDICPARLER_URL", "AZURE_TTS_INDICF5_URL",
        ]}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._service_url("higgs")

    def test_all_known_models_accepted(self):
        for model, env_var in [
            ("f5", "AZURE_TTS_F5_URL"),
            ("higgs", "AZURE_TTS_HIGGS_URL"),
            ("cosyvoice", "AZURE_TTS_COSYVOICE_URL"),
            ("chatterbox", "AZURE_TTS_CHATTERBOX_URL"),
            ("indicparler", "AZURE_TTS_INDICPARLER_URL"),
            ("indicf5", "AZURE_TTS_INDICF5_URL"),
        ]:
            with patch.dict(os.environ, {env_var: "https://svc.example.com"}):
                url = _mod._service_url(model)
            self.assertEqual(url, "https://svc.example.com")


class TestTimeoutS(unittest.TestCase):
    def test_default_is_240(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AZURE_TTS_TIMEOUT", None)
            self.assertEqual(_mod._timeout_s(), 240)

    def test_env_override(self):
        with patch.dict(os.environ, {"AZURE_TTS_TIMEOUT": "120"}):
            self.assertEqual(_mod._timeout_s(), 120)

    def test_invalid_value_returns_240(self):
        with patch.dict(os.environ, {"AZURE_TTS_TIMEOUT": "not_a_number"}):
            self.assertEqual(_mod._timeout_s(), 240)


class TestFallbackDisabled(unittest.TestCase):
    def test_one_returns_true(self):
        with patch.dict(os.environ, {"AZURE_TTS_DISABLE_FALLBACK": "1"}):
            self.assertTrue(_mod._fallback_disabled())

    def test_true_returns_true(self):
        with patch.dict(os.environ, {"AZURE_TTS_DISABLE_FALLBACK": "true"}):
            self.assertTrue(_mod._fallback_disabled())

    def test_zero_returns_false(self):
        with patch.dict(os.environ, {"AZURE_TTS_DISABLE_FALLBACK": "0"}):
            self.assertFalse(_mod._fallback_disabled())

    def test_unset_returns_false(self):
        env = dict(os.environ)
        env.pop("AZURE_TTS_DISABLE_FALLBACK", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(_mod._fallback_disabled())


class TestPostSynth(unittest.TestCase):
    def _payload(self, model="f5"):
        return {"model": model, "text": "hello", "speed": 1.0}

    def _setup_url_and_key(self, model="f5", url="https://az.example.com",
                            key="test-key", tls=True):
        """Patch _service_url, get_api_key, tls_verify for a clean test."""
        patches = [
            patch.object(_mod, "_service_url", return_value=url),
            patch("pipeline.tts.azure_aks.get_api_key", return_value=key),
            patch("pipeline.tts.azure_aks.tls_verify", return_value=tls),
        ]
        return patches

    def test_success_returns_json(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output_inline": "abc"}
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.post.return_value = mock_resp

        with patch.object(_mod, "_service_url", return_value="https://az.example.com"), \
             patch("pipeline.tts.azure_aks.get_api_key", return_value="k"), \
             patch("pipeline.tts.azure_aks.tls_verify", return_value=True), \
             patch("requests.Session", return_value=mock_session):
            result = _mod._post_synth({"model": "f5", "text": "hi"})

        self.assertEqual(result, {"output_inline": "abc"})

    def test_5xx_raises_azure_unavailable(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.post.return_value = mock_resp

        with patch.object(_mod, "_service_url", return_value="https://az.example.com"), \
             patch("pipeline.tts.azure_aks.get_api_key", return_value="k"), \
             patch("pipeline.tts.azure_aks.tls_verify", return_value=True), \
             patch("requests.Session", return_value=mock_session):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._post_synth({"model": "f5", "text": "hi"})

    def test_connection_error_raises_azure_unavailable(self):
        import requests as req
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.post.side_effect = req.ConnectionError("refused")

        with patch.object(_mod, "_service_url", return_value="https://az.example.com"), \
             patch("pipeline.tts.azure_aks.get_api_key", return_value="k"), \
             patch("pipeline.tts.azure_aks.tls_verify", return_value=True), \
             patch("requests.Session", return_value=mock_session):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._post_synth({"model": "f5", "text": "hi"})

    def test_timeout_raises_azure_unavailable(self):
        import requests as req
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.post.side_effect = req.Timeout("timed out")

        with patch.object(_mod, "_service_url", return_value="https://az.example.com"), \
             patch("pipeline.tts.azure_aks.get_api_key", return_value="k"), \
             patch("pipeline.tts.azure_aks.tls_verify", return_value=True), \
             patch("requests.Session", return_value=mock_session):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._post_synth({"model": "f5", "text": "hi"})

    def test_http_error_non5xx_re_raises(self):
        """4xx HTTPError → re-raised as AzureUnavailable (wrapped by except clause)."""
        import requests as req
        mock_resp = MagicMock()
        mock_resp.status_code = 200  # not 5xx, so first check passes
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = req.HTTPError("400 Bad Request")
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.post.return_value = mock_resp

        with patch.object(_mod, "_service_url", return_value="https://az.example.com"), \
             patch("pipeline.tts.azure_aks.get_api_key", return_value="k"), \
             patch("pipeline.tts.azure_aks.tls_verify", return_value=True), \
             patch("requests.Session", return_value=mock_session):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._post_synth({"model": "f5", "text": "hi"})

    def test_azure_key_missing_raises_azure_unavailable(self):
        from pipeline.utils.azure_auth import AzureKeyMissing
        with patch.object(_mod, "_service_url", return_value="https://az.example.com"), \
             patch("pipeline.tts.azure_aks.get_api_key",
                   side_effect=AzureKeyMissing("no key")):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._post_synth({"model": "f5", "text": "hi"})


class TestMaterialiseWav(unittest.TestCase):
    def test_inline_base64_written(self):
        payload_bytes = b"RIFF....fake wav"
        encoded = base64.b64encode(payload_bytes).decode("ascii")
        resp = {"output_inline": encoded}
        out = Path("/fake/output.wav")

        with patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes") as mock_wb:
            result = _mod._materialise_wav(resp, out)

        self.assertEqual(result, out)
        mock_wb.assert_called_once_with(payload_bytes)

    def test_missing_output_inline_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            _mod._materialise_wav({}, Path("/fake/out.wav"))
        self.assertIn("output_inline", str(ctx.exception))

    def test_empty_output_inline_raises(self):
        with self.assertRaises(RuntimeError):
            _mod._materialise_wav({"output_inline": ""}, Path("/fake/out.wav"))


class TestReadRefB64(unittest.TestCase):
    def test_reads_and_encodes_file(self):
        file_bytes = b"\x00\x01\x02\x03"
        with patch("pathlib.Path.read_bytes", return_value=file_bytes):
            result = _mod._read_ref_b64("/fake/ref.wav")
        expected = base64.b64encode(file_bytes).decode("ascii")
        self.assertEqual(result, expected)


class TestSynthAzure(unittest.TestCase):
    def test_success_path(self):
        out = Path("/fake/out.wav")
        resp = {"output_inline": base64.b64encode(b"RIFF").decode(), "duration_s": 1.5, "wall_s": 0.3}

        with patch.object(_mod, "_read_ref_b64", return_value="ref64"), \
             patch.object(_mod, "_post_synth", return_value=resp) as mock_post, \
             patch.object(_mod, "_materialise_wav", return_value=out) as mock_mat:
            result = _mod._synth_azure(
                model="f5",
                text="Hello world",
                ref_audio_path="/ref.wav",
                ref_audio_text="hello world",
                out_path=out,
                speed=1.0,
                seed=42,
            )

        self.assertEqual(result, out)
        payload = mock_post.call_args[0][0]
        self.assertEqual(payload["model"], "f5")
        self.assertEqual(payload["text"], "Hello world")
        self.assertEqual(payload["ref_audio_b64"], "ref64")
        mock_mat.assert_called_once_with(resp, out)

    def test_no_ref_audio_path_sends_empty_b64(self):
        out = Path("/fake/out.wav")
        resp = {"output_inline": base64.b64encode(b"X").decode(), "duration_s": 0.5, "wall_s": 0.1}

        with patch.object(_mod, "_post_synth", return_value=resp) as mock_post, \
             patch.object(_mod, "_materialise_wav", return_value=out):
            _mod._synth_azure(
                model="higgs",
                text="Test",
                ref_audio_path="",
                ref_audio_text=None,
                out_path=out,
                speed=0.9,
            )

        payload = mock_post.call_args[0][0]
        self.assertEqual(payload["ref_audio_b64"], "")


class TestWithFallbackToCloudrun(unittest.TestCase):
    def test_azure_success_no_fallback(self):
        out = Path("/fake/out.wav")
        mock_cloudrun_fn = MagicMock()

        with patch.object(_mod, "_synth_azure", return_value=out):
            result = _mod._with_fallback_to_cloudrun(
                azure_model="f5",
                cloudrun_fn=mock_cloudrun_fn,
                text="hi",
                ref_audio_path="/ref.wav",
                ref_audio_text="hi",
                out_path=out,
                speed=1.0,
                seed=None,
            )

        self.assertEqual(result, out)
        mock_cloudrun_fn.assert_not_called()

    def test_azure_fails_fallback_enabled_calls_cloudrun(self):
        out = Path("/fake/out.wav")
        mock_cloudrun_fn = MagicMock(return_value=out)

        with patch.object(_mod, "_synth_azure", side_effect=_mod.AzureUnavailable("down")), \
             patch.object(_mod, "_fallback_disabled", return_value=False):
            result = _mod._with_fallback_to_cloudrun(
                azure_model="f5",
                cloudrun_fn=mock_cloudrun_fn,
                text="hi",
                ref_audio_path="/ref.wav",
                ref_audio_text="hi",
                out_path=out,
                speed=1.0,
                seed=None,
            )

        self.assertEqual(result, out)
        mock_cloudrun_fn.assert_called_once()

    def test_azure_fails_fallback_disabled_re_raises(self):
        out = Path("/fake/out.wav")
        mock_cloudrun_fn = MagicMock()

        with patch.object(_mod, "_synth_azure", side_effect=_mod.AzureUnavailable("down")), \
             patch.object(_mod, "_fallback_disabled", return_value=True):
            with self.assertRaises(_mod.AzureUnavailable):
                _mod._with_fallback_to_cloudrun(
                    azure_model="f5",
                    cloudrun_fn=mock_cloudrun_fn,
                    text="hi",
                    ref_audio_path="/ref.wav",
                    ref_audio_text="hi",
                    out_path=out,
                    speed=1.0,
                    seed=None,
                )

        mock_cloudrun_fn.assert_not_called()


class TestPerModelWrappers(unittest.TestCase):
    """Each per-model wrapper imports a cloudrun function and routes to
    _with_fallback_to_cloudrun with the right azure_model string."""

    def _test_wrapper(self, wrapper_fn_name, expected_azure_model):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_with_fallback_to_cloudrun", return_value=out) as mock_fw:
            wrapper = getattr(_mod, wrapper_fn_name)
            wrapper(
                text="hello",
                ref_audio_path="/ref.wav",
                ref_audio_text="hello",
                out_path=out,
                speed=1.0,
                seed=7,
            )
        call_kwargs = mock_fw.call_args[1]
        self.assertEqual(call_kwargs["azure_model"], expected_azure_model)

    def test_f5_wrapper(self):
        self._test_wrapper("_synth_azure_f5", "f5")

    def test_higgs_wrapper(self):
        self._test_wrapper("_synth_azure_higgs", "higgs")

    def test_chatterbox_wrapper(self):
        self._test_wrapper("_synth_azure_chatterbox", "chatterbox")

    def test_cosyvoice_wrapper(self):
        self._test_wrapper("_synth_azure_cosyvoice", "cosyvoice")

    def test_indicparler_wrapper(self):
        self._test_wrapper("_synth_azure_indicparler", "indicparler")

    def test_indicf5_wrapper(self):
        self._test_wrapper("_synth_azure_indicf5", "indicf5")

    def test_f5_wrapper_uses_cloudrun_f5(self):
        """_synth_azure_f5 must import and route to _synth_cloudrun_f5."""
        out = Path("/fake/out.wav")
        mock_cloudrun_f5 = MagicMock(return_value=out)
        with patch("pipeline.tts.cloudrun._synth_cloudrun_f5", mock_cloudrun_f5), \
             patch.object(_mod, "_synth_azure", side_effect=_mod.AzureUnavailable("down")), \
             patch.object(_mod, "_fallback_disabled", return_value=False):
            _mod._synth_azure_f5(
                text="hi", ref_audio_path="/r.wav",
                ref_audio_text="hi", out_path=out, speed=1.0,
            )
        mock_cloudrun_f5.assert_called_once()


if __name__ == "__main__":
    unittest.main()
