"""Tests for the Cloud Run render-worker-v2 preflight (2026-05-11).

The preflight runs BEFORE Firestore is touched and validates that
every env var the worker depends on is wired. Pre-2026-05-11 missing
env (e.g. operator forgot to re-mount AZURE_OPENAI_ENDPOINT after a
redeploy) surfaced as a `ClaudeCLIError` mid-rewrite — the
user-visible Firestore job got marked `dispatching` then `failed`
with a giant Python traceback. Now the preflight catches it and
exits with a single-line "operator action required" error.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint():
    """Import cloud/render-worker-v2/entrypoint.py without polluting sys.path.

    The file's parent dir contains a hyphen so a regular import can't
    reach it; importlib.util gives us a one-shot module handle for the
    test only.
    """
    spec = importlib.util.spec_from_file_location(
        "render_worker_v2_entrypoint_for_tests",
        ENTRYPOINT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _PreflightEnvMixin:
    """Provide a clean env baseline so test order doesn't matter."""

    # Env vars the preflight inspects — we clear all of them in setUp
    # then opt-in per test.
    _PREFLIGHT_ENV_KEYS = (
        "GOOGLE_CLOUD_PROJECT",
        "YTFACTORY_BUCKET",
        "YTFACTORY_LLM_BACKEND",
        "YTFACTORY_RENDER_MODE",
        "YTFACTORY_ASR_PROVIDER",
        "YTFACTORY_PREFLIGHT_ONLY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_MODEL",
        "AZURE_OPENAI_MODEL_HAIKU",
        "AZURE_OPENAI_MODEL_SONNET",
        "AZURE_OPENAI_MODEL_OPUS",
        "ANTHROPIC_API_KEY",
        "CLOUDRUN_TTS_URL",
        "CLOUDRUN_TTS_F5_URL",
        "CLOUDRUN_TTS_CHATTERBOX_URL",
        "CLOUDRUN_TTS_INDICPARLER_URL",
        "CLOUDRUN_TTS_INDICF5_URL",
        "CLOUDRUN_TTS_HIGGS_URL",
        "CLOUDRUN_TTS_COSYVOICE_URL",
        "CLOUDRUN_IMAGE_FLUX2_KLEIN_URL",
        "CLOUDRUN_IMAGE_Z_TURBO_URL",
        "CLOUDRUN_IMAGE_QWEN_URL",
        "CLOUDRUN_IMAGE_HIDREAM_URL",
    )

    def _clean_env(self):
        return {k: v for k, v in os.environ.items() if k not in self._PREFLIGHT_ENV_KEYS}

    def _real_mode_env(self, **overrides):
        env = self._clean_env()
        env["GOOGLE_CLOUD_PROJECT"] = "ytfactory-prod-v3"
        env["YTFACTORY_BUCKET"] = "ytfactory-prod-v3-artifacts"
        env["YTFACTORY_RENDER_MODE"] = "real"
        env["YTFACTORY_LLM_BACKEND"] = "azure_openai"
        env["AZURE_OPENAI_ENDPOINT"] = "https://test.openai.azure.com"
        env["AZURE_OPENAI_API_KEY"] = "k"
        env["AZURE_OPENAI_MODEL"] = "gpt-5.3-chat"
        env["CLOUDRUN_TTS_CHATTERBOX_URL"] = "https://tts.example.com"
        env["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"] = "https://img.example.com"
        env["YTFACTORY_ASR_PROVIDER"] = "faster_whisper"
        env.update(overrides)
        return env


class TestPreflightHappyPath(_PreflightEnvMixin, unittest.TestCase):
    def test_real_mode_with_full_env_passes(self):
        ep = _load_entrypoint()
        with mock.patch.dict(os.environ, self._real_mode_env(), clear=True):
            problems = ep._preflight()
        self.assertEqual(problems, [], f"unexpected problems: {problems}")

    def test_stub_mode_skips_llm_and_cloud_checks(self):
        """Stub mode only needs GCP project + bucket; LLM + cloud URLs
        aren't called so missing env should NOT be flagged."""
        ep = _load_entrypoint()
        env = self._clean_env()
        env["GOOGLE_CLOUD_PROJECT"] = "p"
        env["YTFACTORY_BUCKET"] = "b"
        env["YTFACTORY_RENDER_MODE"] = "stub"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        self.assertEqual(problems, [])


class TestPreflightAzureBackend(_PreflightEnvMixin, unittest.TestCase):
    def test_missing_endpoint_caught(self):
        """The original prod outage: AZURE_OPENAI_ENDPOINT not set."""
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_ENDPOINT"]
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("AZURE_OPENAI_ENDPOINT", keys)

    def test_missing_api_key_caught(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_API_KEY"]
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("AZURE_OPENAI_API_KEY", keys)

    def test_missing_model_caught(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_MODEL"]
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("AZURE_OPENAI_MODEL", keys)

    def test_per_tier_model_satisfies_model_check(self):
        """AZURE_OPENAI_MODEL_HAIKU substitutes for the generic var."""
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_MODEL"]
        env["AZURE_OPENAI_MODEL_HAIKU"] = "gpt-4o-mini"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertNotIn("AZURE_OPENAI_MODEL", keys)


class TestPreflightAnthropicBackend(_PreflightEnvMixin, unittest.TestCase):
    def test_missing_anthropic_key_caught(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        env["YTFACTORY_LLM_BACKEND"] = "anthropic_sdk"
        # Azure env doesn't matter for anthropic backend; clear it to
        # prove the dispatcher only checks the active backend.
        for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_MODEL"):
            env.pop(k, None)
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("ANTHROPIC_API_KEY", keys)

    def test_anthropic_with_key_passes(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        env["YTFACTORY_LLM_BACKEND"] = "anthropic_sdk"
        env["ANTHROPIC_API_KEY"] = "sk-ant-test"
        for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_MODEL"):
            env.pop(k, None)
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        self.assertEqual(problems, [])


class TestPreflightCloudServiceUrls(_PreflightEnvMixin, unittest.TestCase):
    def test_no_tts_url_caught(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["CLOUDRUN_TTS_CHATTERBOX_URL"]
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("CLOUDRUN_TTS_*_URL", keys)

    def test_no_image_url_caught(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["CLOUDRUN_IMAGE_FLUX2_KLEIN_URL"]
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("CLOUDRUN_IMAGE_*_URL", keys)

    def test_alternate_tts_url_satisfies(self):
        """Hindi-only deploys may set INDICPARLER instead of CHATTERBOX."""
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["CLOUDRUN_TTS_CHATTERBOX_URL"]
        env["CLOUDRUN_TTS_INDICPARLER_URL"] = "https://hindi-tts.example.com"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertNotIn("CLOUDRUN_TTS_*_URL", keys)


class TestPreflightAsrProvider(_PreflightEnvMixin, unittest.TestCase):
    def test_apple_only_asr_provider_caught(self):
        """whisper_mlx is Apple-only; on Linux containers it crashes
        with ModuleNotFoundError. Catch at preflight."""
        ep = _load_entrypoint()
        env = self._real_mode_env()
        env["YTFACTORY_ASR_PROVIDER"] = "whisper_mlx"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("YTFACTORY_ASR_PROVIDER", keys)


class TestPreflightCliBackend(_PreflightEnvMixin, unittest.TestCase):
    def test_cli_backend_rejected_in_cloud(self):
        """Cloud image doesn't carry the `claude` binary — fail early
        instead of mid-rewrite."""
        ep = _load_entrypoint()
        env = self._real_mode_env()
        env["YTFACTORY_LLM_BACKEND"] = "cli"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("YTFACTORY_LLM_BACKEND", keys)


class TestPreflightAlwaysRequired(_PreflightEnvMixin, unittest.TestCase):
    def test_missing_project_caught_in_stub_mode_too(self):
        ep = _load_entrypoint()
        env = self._clean_env()
        env["YTFACTORY_RENDER_MODE"] = "stub"
        env["YTFACTORY_BUCKET"] = "b"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("GOOGLE_CLOUD_PROJECT", keys)

    def test_missing_bucket_caught_in_stub_mode_too(self):
        ep = _load_entrypoint()
        env = self._clean_env()
        env["YTFACTORY_RENDER_MODE"] = "stub"
        env["GOOGLE_CLOUD_PROJECT"] = "p"
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
        keys = [k for k, _ in problems]
        self.assertIn("YTFACTORY_BUCKET", keys)


class TestPreflightFormatting(_PreflightEnvMixin, unittest.TestCase):
    def test_format_preflight_error_includes_fix_instructions(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_ENDPOINT"]
        with mock.patch.dict(os.environ, env, clear=True):
            problems = ep._preflight()
            msg = ep._format_preflight_error(problems)
        # The error message must guide the operator to the fix command.
        self.assertIn("AZURE_OPENAI_ENDPOINT", msg)
        self.assertIn("gcloud run jobs update", msg)
        self.assertIn("YTFACTORY_PREFLIGHT_ONLY=1", msg)


class TestRunPreflightOrDie(_PreflightEnvMixin, unittest.TestCase):
    def test_passes_silently_on_valid_env(self):
        ep = _load_entrypoint()
        with mock.patch.dict(os.environ, self._real_mode_env(), clear=True):
            # Should NOT raise / SystemExit when env is valid.
            ep._run_preflight_or_die(job_id=None)

    def test_exits_zero_in_preflight_only_mode_on_success(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        env["YTFACTORY_PREFLIGHT_ONLY"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                ep._run_preflight_or_die(job_id=None)
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_two_on_missing_env(self):
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_ENDPOINT"]
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                ep._run_preflight_or_die(job_id=None)
        self.assertEqual(ctx.exception.code, 2)

    def test_marks_firestore_job_failed_when_job_id_known(self):
        """When YTFACTORY_JOB_ID is set, preflight failure must update
        the Firestore job to status=failed so the dashboard shows a
        useful message instead of "dispatching" forever."""
        ep = _load_entrypoint()
        env = self._real_mode_env()
        del env["AZURE_OPENAI_ENDPOINT"]
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(ep, "_update_job") as upd:
                with self.assertRaises(SystemExit):
                    ep._run_preflight_or_die(job_id="abc123")
        self.assertEqual(upd.call_count, 1)
        kwargs = upd.call_args.kwargs
        self.assertEqual(kwargs.get("status"), "failed")
        self.assertEqual(kwargs.get("stage"), "bootstrap")
        self.assertIn("AZURE_OPENAI_ENDPOINT", kwargs.get("error", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
