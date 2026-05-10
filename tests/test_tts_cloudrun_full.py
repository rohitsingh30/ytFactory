"""100% line-coverage tests for pipeline.tts.cloudrun (the uncovered lines).

Covers all uncovered sections: _service_url, _timeout_s, _fallback_disabled,
_post_synth (success, 429/503 retry, URLError retry, 5xx, 4xx, all-attempts-
fail), _materialise_wav (inline, GCS, neither), _split_for_chunked_synth,
_derive_chunk_prosody, _synth_cloudrun, _synth_cloudrun_chunked, and all
per-model wrappers.

No real HTTP, GPU, or filesystem I/O — everything mocked.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pipeline.tts.cloudrun as _mod


class TestServiceUrl(unittest.TestCase):
    def setUp(self):
        # Remove all cloudrun URL env vars so we start from a clean state
        self._remove = [
            "CLOUDRUN_TTS_URL",
            "CLOUDRUN_TTS_F5_URL", "CLOUDRUN_TTS_HIGGS_URL",
            "CLOUDRUN_TTS_COSYVOICE_URL", "CLOUDRUN_TTS_CHATTERBOX_URL",
            "CLOUDRUN_TTS_INDICPARLER_URL", "CLOUDRUN_TTS_INDICF5_URL",
        ]
        self._saved = {k: os.environ.pop(k, None) for k in self._remove}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_per_model_env_takes_precedence(self):
        os.environ["CLOUDRUN_TTS_F5_URL"] = "https://f5.example.com/ "
        os.environ["CLOUDRUN_TTS_URL"] = "https://fallback.example.com"
        url = _mod._service_url(model="f5")
        self.assertEqual(url, "https://f5.example.com")  # stripped

    def test_falls_back_to_global_url(self):
        os.environ["CLOUDRUN_TTS_URL"] = "https://global.example.com"
        url = _mod._service_url(model="higgs")  # no per-model env set
        self.assertEqual(url, "https://global.example.com")

    def test_no_url_raises_cloud_run_unavailable(self):
        with self.assertRaises(_mod.CloudRunUnavailable) as ctx:
            _mod._service_url(model="f5")
        self.assertIn("cloudrun_f5", str(ctx.exception))

    def test_model_none_uses_hint_in_error(self):
        with self.assertRaises(_mod.CloudRunUnavailable) as ctx:
            _mod._service_url(model=None)
        self.assertIn("cloudrun_f5", str(ctx.exception))

    def test_unknown_model_falls_back_to_global(self):
        os.environ["CLOUDRUN_TTS_URL"] = "https://fallback.example.com"
        url = _mod._service_url(model="unknown_model_xyz")
        self.assertEqual(url, "https://fallback.example.com")

    def test_all_known_per_model_envs(self):
        pairs = [
            ("f5", "CLOUDRUN_TTS_F5_URL"),
            ("higgs", "CLOUDRUN_TTS_HIGGS_URL"),
            ("cosyvoice", "CLOUDRUN_TTS_COSYVOICE_URL"),
            ("chatterbox", "CLOUDRUN_TTS_CHATTERBOX_URL"),
            ("indicparler", "CLOUDRUN_TTS_INDICPARLER_URL"),
            ("indicf5", "CLOUDRUN_TTS_INDICF5_URL"),
        ]
        for model, env_var in pairs:
            os.environ[env_var] = f"https://{model}.example.com"
            url = _mod._service_url(model=model)
            self.assertEqual(url, f"https://{model}.example.com")
            del os.environ[env_var]


class TestTimeoutS(unittest.TestCase):
    def test_default_180(self):
        env = dict(os.environ)
        env.pop("CLOUDRUN_TTS_TIMEOUT", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_mod._timeout_s(), 180)

    def test_env_override(self):
        with patch.dict(os.environ, {"CLOUDRUN_TTS_TIMEOUT": "90"}):
            self.assertEqual(_mod._timeout_s(), 90)

    def test_invalid_returns_180(self):
        with patch.dict(os.environ, {"CLOUDRUN_TTS_TIMEOUT": "bad"}):
            self.assertEqual(_mod._timeout_s(), 180)


class TestFallbackDisabled(unittest.TestCase):
    def test_one_true(self):
        with patch.dict(os.environ, {"CLOUDRUN_TTS_DISABLE_FALLBACK": "1"}):
            self.assertTrue(_mod._fallback_disabled())

    def test_true_string_true(self):
        with patch.dict(os.environ, {"CLOUDRUN_TTS_DISABLE_FALLBACK": "true"}):
            self.assertTrue(_mod._fallback_disabled())

    def test_zero_false(self):
        with patch.dict(os.environ, {"CLOUDRUN_TTS_DISABLE_FALLBACK": "0"}):
            self.assertFalse(_mod._fallback_disabled())

    def test_unset_false(self):
        env = dict(os.environ)
        env.pop("CLOUDRUN_TTS_DISABLE_FALLBACK", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(_mod._fallback_disabled())


def _make_http_resp(body: dict, status: int = 200):
    """Create a minimal mock for urllib response context manager."""
    raw = json.dumps(body).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, body: bytes = b"error"):
    """Create an urllib.error.HTTPError with given code."""
    err = urllib.error.HTTPError(
        url="https://example.com/synth",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    err.read = MagicMock(return_value=body)
    return err


class TestPostSynth(unittest.TestCase):
    def setUp(self):
        self._url_patcher = patch.object(
            _mod, "_service_url", return_value="https://tts.example.com"
        )
        self._token_patcher = patch.object(
            _mod, "_get_id_token", return_value="fake-token"
        )
        self._timeout_patcher = patch.object(_mod, "_timeout_s", return_value=5)
        self._url_patcher.start()
        self._token_patcher.start()
        self._timeout_patcher.start()

    def tearDown(self):
        self._url_patcher.stop()
        self._token_patcher.stop()
        self._timeout_patcher.stop()

    def test_success_returns_json(self):
        resp = _make_http_resp({"output_inline": "abc", "duration_s": 1.0})
        with patch("urllib.request.urlopen", return_value=resp):
            result = _mod._post_synth({"model": "f5", "text": "hello"})
        self.assertEqual(result["output_inline"], "abc")

    def test_429_retries_then_succeeds(self):
        """First attempt gets 429, second succeeds."""
        ok_resp = _make_http_resp({"output_inline": "ok"})
        side_effects = [_http_error(429), ok_resp]
        with patch("urllib.request.urlopen", side_effect=side_effects), \
             patch("time.sleep") as mock_sleep:
            result = _mod._post_synth({"model": "f5", "text": "hello"})
        self.assertEqual(result["output_inline"], "ok")
        mock_sleep.assert_called_once_with(1)  # first backoff

    def test_503_retries_then_succeeds(self):
        """First 4 attempts get 503, 5th succeeds."""
        ok_resp = _make_http_resp({"output_inline": "ok"})
        side_effects = [_http_error(503)] * 4 + [ok_resp]
        with patch("urllib.request.urlopen", side_effect=side_effects), \
             patch("time.sleep"):
            result = _mod._post_synth({"model": "higgs", "text": "text"})
        self.assertEqual(result["output_inline"], "ok")

    def test_5_consecutive_503s_raises_cloud_run_unavailable(self):
        """5 consecutive 503 responses → CloudRunUnavailable (5xx → wraps)."""
        side_effects = [_http_error(503)] * 5
        with patch("urllib.request.urlopen", side_effect=side_effects), \
             patch("time.sleep"):
            with self.assertRaises(_mod.CloudRunUnavailable):
                _mod._post_synth({"model": "f5", "text": "text"})

    def test_5_consecutive_429s_re_raises_http_error(self):
        """5 consecutive 429s: attempts 1-4 retry, attempt 5 re-raises HTTPError."""
        side_effects = [_http_error(429)] * 5
        with patch("urllib.request.urlopen", side_effect=side_effects), \
             patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                _mod._post_synth({"model": "f5", "text": "text"})

    def test_5xx_not_429_503_raises_immediately(self):
        """500 → CloudRunUnavailable without retry."""
        with patch("urllib.request.urlopen", side_effect=_http_error(500)), \
             patch("time.sleep") as mock_sleep:
            with self.assertRaises(_mod.CloudRunUnavailable):
                _mod._post_synth({"model": "f5", "text": "text"})
        mock_sleep.assert_not_called()

    def test_4xx_non_rate_limit_re_raises_http_error(self):
        """400 → re-raise HTTPError (not CloudRunUnavailable)."""
        with patch("urllib.request.urlopen", side_effect=_http_error(400)):
            with self.assertRaises(urllib.error.HTTPError):
                _mod._post_synth({"model": "f5", "text": "text"})

    def test_url_error_retries_then_raises(self):
        """URLError on all 5 attempts → CloudRunUnavailable."""
        err = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=[err] * 5), \
             patch("time.sleep"):
            with self.assertRaises(_mod.CloudRunUnavailable):
                _mod._post_synth({"model": "f5", "text": "text"})

    def test_os_error_retries_then_raises(self):
        """OSError retried 4 times then CloudRunUnavailable."""
        err = OSError("broken pipe")
        with patch("urllib.request.urlopen", side_effect=[err] * 5), \
             patch("time.sleep"):
            with self.assertRaises(_mod.CloudRunUnavailable):
                _mod._post_synth({"model": "f5", "text": "text"})

    def test_timeout_error_retries(self):
        """TimeoutError retried then succeeds."""
        ok_resp = _make_http_resp({"output_inline": "ok"})
        side_effects = [TimeoutError("timeout")] + [ok_resp]
        with patch("urllib.request.urlopen", side_effect=side_effects), \
             patch("time.sleep"):
            result = _mod._post_synth({"model": "f5", "text": "text"})
        self.assertEqual(result["output_inline"], "ok")


class TestMaterialiseWav(unittest.TestCase):
    def test_inline_base64_written(self):
        import base64
        payload_bytes = b"RIFF....fake wav"
        encoded = base64.b64encode(payload_bytes).decode("ascii")
        resp = {"output_inline": encoded}
        out = Path("/fake/output.wav")

        with patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes") as mock_wb:
            result = _mod._materialise_wav(resp, out)

        self.assertEqual(result, out)
        mock_wb.assert_called_once_with(payload_bytes)

    def test_gcs_uri_calls_gcloud(self):
        resp = {"output_gcs": "gs://bucket/path/output.wav"}
        out = Path("/fake/output.wav")

        with patch("pathlib.Path.mkdir"), \
             patch("subprocess.run") as mock_sp:
            result = _mod._materialise_wav(resp, out)

        self.assertEqual(result, out)
        cmd = mock_sp.call_args[0][0]
        self.assertIn("gcloud", cmd)
        self.assertIn("gs://bucket/path/output.wav", cmd)

    def test_neither_raises_runtime_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            _mod._materialise_wav({}, Path("/fake/out.wav"))
        self.assertIn("output_inline", str(ctx.exception))


class TestSplitForChunkedSynth(unittest.TestCase):
    def test_multi_paragraph_returns_paragraphs(self):
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = _mod._split_for_chunked_synth(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], "Para one.")

    def test_short_single_para_returns_single(self):
        text = "Short text."
        chunks = _mod._split_for_chunked_synth(text)
        self.assertEqual(chunks, ["Short text."])

    def test_long_single_para_sentence_groups(self):
        # Build a long single paragraph exceeding 350 chars
        long_text = "This is sentence one. " * 25  # ~550 chars
        chunks = _mod._split_for_chunked_synth(long_text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 350 + 50)  # generous slack for last word

    def test_empty_chunks_filtered(self):
        text = "\n\n  \n\n"
        chunks = _mod._split_for_chunked_synth(text)
        self.assertEqual(chunks, [])

    def test_custom_max_chars(self):
        text = "Sentence one. Sentence two. Sentence three."
        chunks = _mod._split_for_chunked_synth(text, max_chars=20)
        self.assertGreater(len(chunks), 1)


class TestDeriveChunkProsody(unittest.TestCase):
    def test_none_prosody_returns_none(self):
        result = _mod._derive_chunk_prosody(["p1", "p2"], None)
        self.assertIsNone(result)

    def test_empty_prosody_returns_none(self):
        result = _mod._derive_chunk_prosody(["p1"], [])
        self.assertIsNone(result)

    def test_matching_sentence_uses_its_speed(self):
        paragraphs = ["Hello world. How are you?"]
        prosody = [
            {"text": "Hello world.", "speed": 0.9, "post_pause_s": 0.0},
            {"text": "How are you?", "speed": 1.1, "post_pause_s": 0.2},
        ]
        result = _mod._derive_chunk_prosody(paragraphs, prosody)
        self.assertIsNotNone(result)
        # Mean of 0.9 and 1.1 = 1.0
        self.assertAlmostEqual(result[0][0], 1.0, places=5)
        # Last matching entry's pause = 0.2
        self.assertAlmostEqual(result[0][1], 0.2, places=5)

    def test_unmatched_paragraph_gets_defaults(self):
        paragraphs = ["Completely unrelated text here."]
        prosody = [{"text": "Some other sentence.", "speed": 0.85, "post_pause_s": 0.1}]
        result = _mod._derive_chunk_prosody(paragraphs, prosody)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], (1.0, 0.0))  # default for unmatched

    def test_pause_capped_at_040(self):
        paragraphs = ["The quick brown fox."]
        prosody = [{"text": "The quick brown fox.", "speed": 1.0, "post_pause_s": 1.5}]
        result = _mod._derive_chunk_prosody(paragraphs, prosody)
        self.assertAlmostEqual(result[0][1], 0.40, places=5)

    def test_entry_with_empty_text_skipped(self):
        """Prosody entries with empty/missing text field are skipped (line 417)."""
        paragraphs = ["Para A."]
        prosody = [
            {"text": "", "speed": 0.5, "post_pause_s": 0.1},  # empty → skip
            {"text": "Para A.", "speed": 0.9, "post_pause_s": 0.0},
        ]
        result = _mod._derive_chunk_prosody(paragraphs, prosody)
        # The empty entry was skipped; only "Para A." entry counted
        self.assertAlmostEqual(result[0][0], 0.9, places=5)

    def test_next_annotated_branch_applies_ritard(self):
        """Unannotated para with annotated para after it gets auto_s=0.96 (line 460)."""
        paragraphs = ["Para A.", "Para B.", "Para C."]
        prosody = [
            {"text": "Para A.", "speed": 1.0, "post_pause_s": 0.0},
            {"text": "Para B.", "speed": 1.0, "post_pause_s": 0.0},
            {"text": "Para C.", "speed": 0.8, "post_pause_s": 0.0},
        ]
        result = _mod._derive_chunk_prosody(paragraphs, prosody)
        # Para A (idx=0): unannotated, no prev, next (Para B) is also unannotated → parity (1.03)
        self.assertAlmostEqual(result[0][0], 1.03, places=5)
        # Para B (idx=1): unannotated, prev unannotated, NEXT (Para C) IS annotated → ritard (0.96)
        self.assertAlmostEqual(result[1][0], 0.96, places=5)
        # Para C (idx=2): annotated → unchanged
        self.assertAlmostEqual(result[2][0], 0.8, places=5)


class TestSynthCloudrun(unittest.TestCase):
    """Tests for the core _synth_cloudrun dispatcher."""

    def test_success_returns_out_path(self):
        out = Path("/fake/out.wav")
        resp = {"output_inline": "abc", "duration_s": 1.0, "wall_s": 0.2, "rtf": 0.1}
        with patch.object(_mod, "_read_ref_b64", return_value="refb64"), \
             patch.object(_mod, "_post_synth", return_value=resp), \
             patch.object(_mod, "_materialise_wav", return_value=out):
            result = _mod._synth_cloudrun(
                model="f5", text="hello", ref_audio_path="/ref.wav",
                ref_audio_text="hello", out_path=out, speed=1.0, seed=42,
            )
        self.assertEqual(result, out)

    def test_empty_ref_audio_path_sends_empty_b64(self):
        out = Path("/fake/out.wav")
        resp = {"output_inline": "abc", "duration_s": 0.5, "wall_s": 0.1, "rtf": 0.05}
        with patch.object(_mod, "_post_synth", return_value=resp) as mock_post, \
             patch.object(_mod, "_materialise_wav", return_value=out):
            _mod._synth_cloudrun(
                model="indicparler", text="text", ref_audio_path="",
                ref_audio_text=None, out_path=out, speed=1.0,
            )
        payload = mock_post.call_args[0][0]
        self.assertEqual(payload["ref_audio_b64"], "")


class TestSynthCloudrunChunked(unittest.TestCase):
    """Tests for _synth_cloudrun_chunked — single and multi-chunk paths."""

    def test_single_chunk_calls_synth_cloudrun_directly(self):
        """When text has only one chunk, bypasses chunking entirely."""
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_split_for_chunked_synth", return_value=["short text"]), \
             patch.object(_mod, "_synth_cloudrun", return_value=out) as mock_direct:
            result = _mod._synth_cloudrun_chunked(
                model="indicf5", text="short text", ref_audio_path="/ref.wav",
                ref_audio_text="ref", out_path=out, speed=1.0,
            )
        self.assertEqual(result, out)
        mock_direct.assert_called_once()

    def test_multi_chunk_synthesises_and_concat(self):
        """Two-chunk path: calls _synth_cloudrun twice + ffmpeg twice."""
        out = Path("/fake/dir/out.wav")
        chunks = ["Para one.", "Para two."]

        with patch.object(_mod, "_split_for_chunked_synth", return_value=chunks), \
             patch.object(_mod, "_synth_cloudrun", return_value=MagicMock()), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_cloudrun_chunked(
                model="indicf5", text="Para one.\n\nPara two.",
                ref_audio_path="/ref.wav", ref_audio_text="ref",
                out_path=out, speed=1.0, seed=99,
            )
        self.assertEqual(result, out)

    def test_seed_auto_derived_when_none(self):
        """When seed=None and chunking, a deterministic seed is derived from text."""
        out = Path("/fake/dir/out.wav")
        chunks = ["Para A.", "Para B."]
        derived_seeds = []

        def capture_synth(**kwargs):
            derived_seeds.append(kwargs["seed"])
            return MagicMock()

        with patch.object(_mod, "_split_for_chunked_synth", return_value=chunks), \
             patch.object(_mod, "_synth_cloudrun", side_effect=capture_synth), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_cloudrun_chunked(
                model="indicparler", text="Para A.\n\nPara B.",
                ref_audio_path="", ref_audio_text="desc",
                out_path=out, speed=1.0, seed=None,
            )
        # All chunks get the same derived seed
        self.assertTrue(all(s == derived_seeds[0] for s in derived_seeds))
        self.assertIsNotNone(derived_seeds[0])

    def test_voice_clone_anchoring_switches_ref_after_chunk0(self):
        """For voice-clone-capable models, chunk 1+ use chunk0's wav as ref."""
        out = Path("/fake/dir/out.wav")
        chunks = ["Para one.", "Para two.", "Para three."]
        call_kwargs = []

        chunk0_path = out.with_suffix("").parent / f".{out.stem}.chunks" / "chunk_000.wav"

        def capture_synth(**kwargs):
            call_kwargs.append(kwargs.copy())
            return MagicMock()

        with patch.object(_mod, "_split_for_chunked_synth", return_value=chunks), \
             patch.object(_mod, "_synth_cloudrun", side_effect=capture_synth), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_cloudrun_chunked(
                model="indicf5",  # voice_clone_capable
                text="Para one.\n\nPara two.\n\nPara three.",
                ref_audio_path="/channel/ref.wav",
                ref_audio_text="ref",
                out_path=out, speed=1.0, seed=1,
            )
        # chunk 0: uses original ref
        self.assertEqual(call_kwargs[0]["ref_audio_path"], "/channel/ref.wav")
        # chunks 1+: use chunk_000.wav
        self.assertTrue(str(call_kwargs[1]["ref_audio_path"]).endswith("chunk_000.wav"))
        self.assertTrue(str(call_kwargs[2]["ref_audio_path"]).endswith("chunk_000.wav"))

    def test_indicparler_not_voice_clone_anchored(self):
        """indicparler is not voice-clone-capable — ref stays original for all chunks."""
        out = Path("/fake/dir/out.wav")
        chunks = ["Para one.", "Para two."]
        call_kwargs = []

        def capture_synth(**kwargs):
            call_kwargs.append(kwargs.copy())
            return MagicMock()

        with patch.object(_mod, "_split_for_chunked_synth", return_value=chunks), \
             patch.object(_mod, "_synth_cloudrun", side_effect=capture_synth), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_cloudrun_chunked(
                model="indicparler",
                text="Para one.\n\nPara two.",
                ref_audio_path="",
                ref_audio_text="desc",
                out_path=out, speed=1.0, seed=1,
            )
        # Both chunks use the same (empty) ref
        self.assertEqual(call_kwargs[0]["ref_audio_path"], "")
        self.assertEqual(call_kwargs[1]["ref_audio_path"], "")

    def test_silence_inserted_between_chunks_when_pause_large(self):
        """When prosody has pause > 0.05, silence WAV is generated via ffmpeg."""
        out = Path("/fake/dir/out.wav")
        chunks = ["Para A.", "Para B."]
        prosody = [
            {"text": "Para A.", "speed": 0.9, "post_pause_s": 0.3},
            {"text": "Para B.", "speed": 1.0, "post_pause_s": 0.0},
        ]
        subprocess_calls = []

        def capture_sp(cmd, **kwargs):
            subprocess_calls.append(cmd)

        with patch.object(_mod, "_split_for_chunked_synth", return_value=chunks), \
             patch.object(_mod, "_synth_cloudrun", return_value=MagicMock()), \
             patch("subprocess.run", side_effect=capture_sp), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_cloudrun_chunked(
                model="indicf5",
                text="Para A.\n\nPara B.",
                ref_audio_path="/ref.wav",
                ref_audio_text="ref",
                out_path=out, speed=1.0, seed=1,
                narration_prosody=prosody,
            )

        # One of the subprocess calls should be an anullsrc (silence gen)
        silence_cmds = [c for c in subprocess_calls if "anullsrc" in " ".join(c)]
        self.assertGreater(len(silence_cmds), 0)

    def test_indicf5_prosody_rebaselined(self):
        """indicf5: prosody speeds are normalized to center around caller speed."""
        out = Path("/fake/dir/out.wav")
        chunks = ["Para A.", "Para B."]
        prosody = [
            {"text": "Para A.", "speed": 0.8, "post_pause_s": 0.0},
            {"text": "Para B.", "speed": 1.2, "post_pause_s": 0.0},
        ]
        call_kwargs = []

        def capture_synth(**kwargs):
            call_kwargs.append(kwargs.copy())
            return MagicMock()

        with patch.object(_mod, "_split_for_chunked_synth", return_value=chunks), \
             patch.object(_mod, "_synth_cloudrun", side_effect=capture_synth), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_cloudrun_chunked(
                model="indicf5",
                text="Para A.\n\nPara B.",
                ref_audio_path="/ref.wav",
                ref_audio_text="ref",
                out_path=out, speed=0.9, seed=1,
                narration_prosody=prosody,
            )

        # Speeds should be rebaselined: mean(0.8, 1.2) = 1.0
        # chunk0_speed = 0.8/1.0 * 0.9 = 0.72, chunk1_speed = 1.2/1.0 * 0.9 = 1.08
        self.assertAlmostEqual(call_kwargs[0]["speed"], 0.72, places=5)
        self.assertAlmostEqual(call_kwargs[1]["speed"], 1.08, places=5)


class TestSynthCloudrunCosyvoice(unittest.TestCase):
    def test_missing_ref_audio_text_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _mod._synth_cloudrun_cosyvoice(
                text="hello", ref_audio_path="/r.wav",
                ref_audio_text=None, out_path=Path("/out.wav"), speed=1.0,
            )
        self.assertIn("ref_audio_text", str(ctx.exception))

    def test_empty_ref_audio_text_raises(self):
        with self.assertRaises(ValueError):
            _mod._synth_cloudrun_cosyvoice(
                text="hello", ref_audio_path="/r.wav",
                ref_audio_text="", out_path=Path("/out.wav"), speed=1.0,
            )

    def test_valid_call_delegates_to_synth_cloudrun(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun", return_value=out) as mock_cr:
            result = _mod._synth_cloudrun_cosyvoice(
                text="test", ref_audio_path="/r.wav",
                ref_audio_text="ref", out_path=out, speed=1.0,
            )
        self.assertEqual(result, out)
        self.assertEqual(mock_cr.call_args[1]["model"], "cosyvoice")


class TestSynthCloudrunIndicf5(unittest.TestCase):
    def test_missing_ref_audio_text_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _mod._synth_cloudrun_indicf5(
                text="hello", ref_audio_path="/r.wav",
                ref_audio_text="", out_path=Path("/out.wav"), speed=1.0,
            )
        self.assertIn("ref_audio_text", str(ctx.exception))

    def test_valid_call_delegates_to_chunked(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun_chunked", return_value=out) as mock_ck:
            result = _mod._synth_cloudrun_indicf5(
                text="test", ref_audio_path="/r.wav",
                ref_audio_text="ref", out_path=out, speed=1.0,
            )
        self.assertEqual(result, out)
        self.assertEqual(mock_ck.call_args[1]["model"], "indicf5")


class TestSynthCloudrunIndicparler(unittest.TestCase):
    def test_uses_default_description_when_none(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun_chunked", return_value=out) as mock_ck:
            _mod._synth_cloudrun_indicparler(
                text="test", ref_audio_path=None, ref_audio_text=None,
                out_path=out, speed=1.0, description=None,
            )
        kwargs = mock_ck.call_args[1]
        self.assertIn("Indian female voice", kwargs["ref_audio_text"])

    def test_custom_description_used(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun_chunked", return_value=out) as mock_ck:
            _mod._synth_cloudrun_indicparler(
                text="test", ref_audio_path=None, ref_audio_text=None,
                out_path=out, speed=1.0, description="A calm male voice.",
            )
        kwargs = mock_ck.call_args[1]
        self.assertEqual(kwargs["ref_audio_text"], "A calm male voice.")

    def test_model_is_indicparler(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun_chunked", return_value=out) as mock_ck:
            _mod._synth_cloudrun_indicparler(
                text="test", ref_audio_path=None, ref_audio_text=None,
                out_path=out, speed=1.0,
            )
        self.assertEqual(mock_ck.call_args[1]["model"], "indicparler")


class TestSynthCloudrunChatterbox(unittest.TestCase):
    def test_delegates_to_chunked_with_chatterbox_model(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun_chunked", return_value=out) as mock_ck:
            result = _mod._synth_cloudrun_chatterbox(
                text="test", ref_audio_path="/r.wav", ref_audio_text=None,
                out_path=out, speed=1.0,
            )
        self.assertEqual(result, out)
        self.assertEqual(mock_ck.call_args[1]["model"], "chatterbox")


class TestSynthCloudrunF5(unittest.TestCase):
    def test_delegates_to_synth_cloudrun_with_f5_model(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun", return_value=out) as mock_cr:
            result = _mod._synth_cloudrun_f5(
                text="test", ref_audio_path="/r.wav",
                ref_audio_text="ref", out_path=out, speed=1.0,
            )
        self.assertEqual(result, out)
        self.assertEqual(mock_cr.call_args[1]["model"], "f5")


class TestSynthCloudrunHiggs(unittest.TestCase):
    def test_delegates_to_synth_cloudrun_with_higgs_model(self):
        out = Path("/fake/out.wav")
        with patch.object(_mod, "_synth_cloudrun", return_value=out) as mock_cr:
            result = _mod._synth_cloudrun_higgs(
                text="test", ref_audio_path="/r.wav",
                ref_audio_text=None, out_path=out, speed=1.0,
            )
        self.assertEqual(result, out)
        self.assertEqual(mock_cr.call_args[1]["model"], "higgs")


if __name__ == "__main__":
    unittest.main()
