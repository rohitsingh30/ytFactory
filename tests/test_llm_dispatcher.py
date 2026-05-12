"""Tests for the multi-backend LLM dispatcher in ``pipeline.llm.cli``.

Covers:
- ``_select_backend`` precedence (env override → auto-detect → cli default)
- ``call_claude_cli`` dispatching to the right backend implementation
- Azure OpenAI backend: client construction, JSON-schema response_format,
  fallback to json_object on response_format errors, telemetry tagging
- Anthropic SDK backend: client construction, JSON parsing, telemetry
- Vision-aware kwargs (add_dirs / allowed_tools) rejected on non-cli backends
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import cli as llm_cli


def _clear_backend_env(monkey_keys: list[str] | None = None) -> dict[str, str]:
    """Snapshot + clear every env var the dispatcher cares about."""
    keys = monkey_keys or [
        "YTFACTORY_LLM_BACKEND",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_MODEL",
        "AZURE_OPENAI_MODEL_HAIKU",
        "AZURE_OPENAI_MODEL_OPUS",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL_OPUS",
    ]
    saved = {k: os.environ.pop(k, None) for k in keys if k in os.environ}
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


class SelectBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clear_backend_env()

    def tearDown(self) -> None:
        _clear_backend_env()
        _restore_env(self._saved)

    def test_default_falls_back_to_cli_when_no_claude_or_creds(self) -> None:
        with patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_CLI)

    def test_claude_binary_on_path_wins_when_no_explicit_env(self) -> None:
        with patch.object(llm_cli, "_shutil_which", return_value="/usr/bin/claude"):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_CLI)

    def test_explicit_env_wins_over_claude_binary(self) -> None:
        os.environ["YTFACTORY_LLM_BACKEND"] = "azure_openai"
        with patch.object(llm_cli, "_shutil_which", return_value="/usr/bin/claude"):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_AZURE)
        os.environ["YTFACTORY_LLM_BACKEND"] = "anthropic_sdk"
        with patch.object(llm_cli, "_shutil_which", return_value="/usr/bin/claude"):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_ANTHROPIC)

    def test_unknown_backend_falls_through_to_autodetect(self) -> None:
        os.environ["YTFACTORY_LLM_BACKEND"] = "bogus-name"
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://x.openai.azure.com"
        os.environ["AZURE_OPENAI_API_KEY"] = "sk-azure"
        with patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_AZURE)

    def test_autodetect_azure_when_no_claude_and_creds_present(self) -> None:
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://x.openai.azure.com"
        os.environ["AZURE_OPENAI_API_KEY"] = "sk-azure"
        with patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_AZURE)

    def test_autodetect_anthropic_when_only_anthropic_key(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant"
        with patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), llm_cli.BACKEND_ANTHROPIC)

    def test_should_use_sdk_helper(self) -> None:
        with patch.object(llm_cli, "_shutil_which", return_value="/usr/bin/claude"):
            self.assertFalse(llm_cli._should_use_sdk())
        os.environ["YTFACTORY_LLM_BACKEND"] = "azure_openai"
        self.assertTrue(llm_cli._should_use_sdk())

    def test_select_backend_alias(self) -> None:
        self.assertIs(llm_cli._select_backend, llm_cli._choose_backend)


class TierMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clear_backend_env()

    def tearDown(self) -> None:
        _clear_backend_env()
        _restore_env(self._saved)

    def test_azure_default_per_tier(self) -> None:
        self.assertEqual(llm_cli._azure_model_for("haiku"),  "gpt-4o-mini")
        self.assertEqual(llm_cli._azure_model_for("sonnet"), "gpt-4o-mini")
        self.assertEqual(llm_cli._azure_model_for("opus"),   "gpt-4o")

    def test_azure_per_tier_override(self) -> None:
        os.environ["AZURE_OPENAI_MODEL_HAIKU"] = "gpt-4o-mini-deploy"
        os.environ["AZURE_OPENAI_MODEL_OPUS"] = "gpt-5.3-chat"
        self.assertEqual(llm_cli._azure_model_for("haiku"), "gpt-4o-mini-deploy")
        self.assertEqual(llm_cli._azure_model_for("opus"),  "gpt-5.3-chat")
        self.assertEqual(llm_cli._azure_model_for("sonnet"), "gpt-4o-mini")

    def test_azure_generic_override_applies_to_all_tiers(self) -> None:
        os.environ["AZURE_OPENAI_MODEL"] = "gpt-5.3-chat"
        for tier in ("haiku", "sonnet", "opus"):
            self.assertEqual(llm_cli._azure_model_for(tier), "gpt-5.3-chat")

    def test_azure_per_tier_beats_generic(self) -> None:
        os.environ["AZURE_OPENAI_MODEL"] = "gpt-5.3-chat"
        os.environ["AZURE_OPENAI_MODEL_HAIKU"] = "gpt-4o-mini"
        self.assertEqual(llm_cli._azure_model_for("haiku"), "gpt-4o-mini")
        self.assertEqual(llm_cli._azure_model_for("opus"),  "gpt-5.3-chat")

    def test_azure_deployment_alias(self) -> None:
        self.assertIs(llm_cli._azure_deployment, llm_cli._azure_model_for)

    def test_anthropic_default_per_tier(self) -> None:
        self.assertEqual(llm_cli._anthropic_model_for("haiku"),  "claude-haiku-4-5")
        self.assertEqual(llm_cli._anthropic_model_for("sonnet"), "claude-sonnet-4-5")
        self.assertEqual(llm_cli._anthropic_model_for("opus"),   "claude-opus-4-5")

    def test_anthropic_unknown_tier_passes_through(self) -> None:
        # Lets call sites pass a literal model id.
        self.assertEqual(
            llm_cli._anthropic_model_for("claude-opus-4-7"),
            "claude-opus-4-7",
        )

    def test_anthropic_env_override(self) -> None:
        os.environ["ANTHROPIC_MODEL_OPUS"] = "claude-opus-4-7-internal"
        self.assertEqual(llm_cli._anthropic_model_for("opus"),
                         "claude-opus-4-7-internal")


class DispatcherRoutingTest(unittest.TestCase):
    """Verify ``call_claude_cli`` dispatches to the right private function."""

    def setUp(self) -> None:
        self._saved = _clear_backend_env()

    def tearDown(self) -> None:
        _clear_backend_env()
        _restore_env(self._saved)

    def test_routes_to_cli_subprocess_by_default(self) -> None:
        with patch.object(llm_cli, "_shutil_which", return_value="/usr/bin/claude"), \
             patch.object(llm_cli, "_call_claude_cli_subprocess",
                          return_value={"ok": 1}) as mock:
            out = llm_cli.call_claude_cli("hi", model="opus", stage="rewrite")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(mock.call_count, 1)

    def test_routes_to_azure_when_env_set(self) -> None:
        os.environ["YTFACTORY_LLM_BACKEND"] = "azure_openai"
        with patch.object(llm_cli, "_call_azure_openai",
                          return_value={"x": 2}) as mock:
            out = llm_cli.call_claude_cli("hi", model="opus", stage="rewrite")
        self.assertEqual(out, {"x": 2})
        self.assertEqual(mock.call_count, 1)
        kwargs = mock.call_args.kwargs
        self.assertEqual(kwargs["model"], "opus")
        self.assertEqual(kwargs["stage"], "rewrite")

    def test_routes_to_anthropic_when_env_set(self) -> None:
        os.environ["YTFACTORY_LLM_BACKEND"] = "anthropic_sdk"
        with patch.object(llm_cli, "_call_anthropic_sdk",
                          return_value="hello") as mock:
            out = llm_cli.call_claude_cli(
                "hi", output_json=False, model="haiku", stage="rewrite"
            )
        self.assertEqual(out, "hello")
        self.assertEqual(mock.call_count, 1)

    def test_vision_kwargs_rejected_on_azure_backend(self) -> None:
        os.environ["YTFACTORY_LLM_BACKEND"] = "azure_openai"
        with self.assertRaises(llm_cli.ClaudeCLIError) as ctx:
            llm_cli.call_claude_cli(
                "critique these frames",
                add_dirs=[Path("/tmp/frames")],
                allowed_tools=["Read"],
                model="opus",
                stage="critic",
            )
        self.assertIn("does not support add_dirs", str(ctx.exception))

    def test_vision_kwargs_allowed_on_cli_backend(self) -> None:
        os.environ["YTFACTORY_LLM_BACKEND"] = "cli"
        with patch.object(llm_cli, "_call_claude_cli_subprocess",
                          return_value={"ok": 1}) as mock:
            llm_cli.call_claude_cli(
                "critique these frames",
                add_dirs=[Path("/tmp/frames")],
                allowed_tools=["Read"],
                model="opus",
                stage="critic",
            )
        self.assertEqual(mock.call_count, 1)


class AzureBackendTest(unittest.TestCase):
    """Drive _call_azure_openai end-to-end with a fake openai SDK."""

    def setUp(self) -> None:
        self._saved = _clear_backend_env()
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://x.openai.azure.com"
        os.environ["AZURE_OPENAI_API_KEY"] = "sk-azure"
        # Inject a fake `openai` module so the lazy import inside
        # _build_azure_client picks it up.
        self._saved_module = sys.modules.get("openai")
        self._fake_client = MagicMock(name="AzureOpenAIClient")
        self._fake_client.chat.completions.create = MagicMock()
        fake_openai = types.ModuleType("openai")
        fake_openai.AzureOpenAI = MagicMock(return_value=self._fake_client)  # type: ignore[attr-defined]
        sys.modules["openai"] = fake_openai

    def tearDown(self) -> None:
        if self._saved_module is not None:
            sys.modules["openai"] = self._saved_module
        else:
            sys.modules.pop("openai", None)
        _clear_backend_env()
        _restore_env(self._saved)

    def _make_resp(self, content: str, *, prompt_tokens: int = 10,
                   completion_tokens: int = 20):
        return MagicMock(
            choices=[MagicMock(message=MagicMock(content=content))],
            usage=MagicMock(prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens),
        )

    def test_text_call_returns_string(self) -> None:
        self._fake_client.chat.completions.create.return_value = \
            self._make_resp("plain text reply")
        out = llm_cli._call_azure_openai(
            "say hi", output_json=False, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite",
        )
        self.assertEqual(out, "plain text reply")
        kwargs = self._fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["messages"][0]["role"], "user")
        # No JSON_HINT augmentation when output_json=False.
        self.assertNotIn("ONLY a JSON object", kwargs["messages"][0]["content"])
        self.assertNotIn("response_format", kwargs)

    def test_json_call_augments_prompt_and_uses_json_object(self) -> None:
        self._fake_client.chat.completions.create.return_value = \
            self._make_resp('{"hook":"AITA story","narration":"..."}')
        out = llm_cli._call_azure_openai(
            "rewrite this", output_json=True, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite",
        )
        self.assertEqual(out, {"hook": "AITA story", "narration": "..."})
        kwargs = self._fake_client.chat.completions.create.call_args.kwargs
        self.assertIn("ONLY a JSON object", kwargs["messages"][0]["content"])
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_json_schema_uses_structured_response_format(self) -> None:
        self._fake_client.chat.completions.create.return_value = \
            self._make_resp('{"score": 7}')
        schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
        out = llm_cli._call_azure_openai(
            "critique this", output_json=True, json_schema=schema,
            model="opus", timeout_s=30, stage="audio_critic",
        )
        self.assertEqual(out, {"score": 7})
        kwargs = self._fake_client.chat.completions.create.call_args.kwargs
        rf = kwargs["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["name"], "audio_critic")
        self.assertEqual(rf["json_schema"]["schema"], schema)
        # Schema text embedded in user prompt.
        self.assertIn("matching this schema", kwargs["messages"][0]["content"])

    def test_response_format_failure_retries_without_response_format(self) -> None:
        self._fake_client.chat.completions.create.side_effect = [
            Exception("Invalid 'response_format' parameter for this deployment"),
            self._make_resp('{"score": 5}'),
        ]
        schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
        out = llm_cli._call_azure_openai(
            "critique", output_json=True, json_schema=schema,
            model="opus", timeout_s=30, stage="audio_critic",
        )
        self.assertEqual(out, {"score": 5})
        self.assertEqual(self._fake_client.chat.completions.create.call_count, 2)
        retry_kwargs = self._fake_client.chat.completions.create.call_args.kwargs
        # Retry drops response_format entirely (the spec — older
        # deployments reject ALL response_format values).
        self.assertNotIn("response_format", retry_kwargs)

    def test_max_tokens_failure_retries_with_max_completion_tokens(self) -> None:
        # Exact prod failure 2026-05-12: gpt-5.3-chat (and o-series
        # reasoning deployments in general) reject ``max_tokens`` and
        # demand ``max_completion_tokens``. We retry once with the key
        # swapped — same value, same intent. The original 502 cascade
        # (Reddit-403 → LLM-500 → 502) was directly caused by NOT
        # retrying this.
        self._fake_client.chat.completions.create.side_effect = [
            Exception(
                "Error code: 400 - {'error': {'message': "
                "\"Unsupported parameter: 'max_tokens' is not supported "
                "with this model. Use 'max_completion_tokens' instead.\", "
                "'type': 'invalid_request_error', 'param': 'max_tokens', "
                "'code': 'unsupported_parameter'}}"
            ),
            self._make_resp('{"items": [{"topic": "Cool topic"}]}'),
        ]
        out = llm_cli._call_azure_openai(
            "brainstorm", output_json=True, json_schema=None,
            model="haiku", timeout_s=30, stage="discover_brainstorm",
        )
        self.assertEqual(out, {"items": [{"topic": "Cool topic"}]})
        self.assertEqual(self._fake_client.chat.completions.create.call_count, 2)

        first_kwargs = self._fake_client.chat.completions.create.call_args_list[0].kwargs
        retry_kwargs = self._fake_client.chat.completions.create.call_args_list[1].kwargs
        # Original call had max_tokens; retry has max_completion_tokens
        # with the SAME value. response_format is preserved.
        self.assertIn("max_tokens", first_kwargs)
        self.assertNotIn("max_completion_tokens", first_kwargs)
        self.assertNotIn("max_tokens", retry_kwargs)
        self.assertIn("max_completion_tokens", retry_kwargs)
        self.assertEqual(
            retry_kwargs["max_completion_tokens"], first_kwargs["max_tokens"],
        )
        self.assertEqual(retry_kwargs["response_format"], first_kwargs["response_format"])

    def test_max_tokens_retry_failure_surfaces_with_retry_label(self) -> None:
        # If the retry also fails (e.g. content filter rejects), the
        # error message must name the retry that was attempted so the
        # operator can tell whether the original or the retry blew up.
        self._fake_client.chat.completions.create.side_effect = [
            Exception(
                "Unsupported parameter: 'max_tokens' is not supported "
                "with this model. Use 'max_completion_tokens' instead."
            ),
            Exception("content filter triggered"),
        ]
        with self.assertRaises(llm_cli.ClaudeCLIError) as ctx:
            llm_cli._call_azure_openai(
                "x", output_json=True, json_schema=None,
                model="haiku", timeout_s=30, stage="discover_brainstorm",
            )
        self.assertIn("post-retry=max_completion_tokens", str(ctx.exception))
        self.assertIn("content filter", str(ctx.exception))

    def test_other_exceptions_propagate_as_claude_cli_error(self) -> None:
        self._fake_client.chat.completions.create.side_effect = \
            Exception("rate limit exceeded")
        with self.assertRaises(llm_cli.ClaudeCLIError) as ctx:
            llm_cli._call_azure_openai(
                "x", output_json=True, json_schema=None,
                model="opus", timeout_s=30, stage="rewrite",
            )
        self.assertIn("rate limit", str(ctx.exception))

    def test_missing_credentials_raises(self) -> None:
        os.environ.pop("AZURE_OPENAI_API_KEY", None)
        with self.assertRaises(llm_cli.ClaudeCLIError) as ctx:
            llm_cli._build_azure_client(timeout_s=30)
        self.assertIn("AZURE_OPENAI_ENDPOINT", str(ctx.exception))

    def test_per_tier_env_resolves_to_deployment(self) -> None:
        os.environ["AZURE_OPENAI_MODEL_HAIKU"] = "gpt-4o-mini-deploy"
        self._fake_client.chat.completions.create.return_value = \
            self._make_resp('{}')
        llm_cli._call_azure_openai(
            "x", output_json=True, json_schema=None,
            model="haiku", timeout_s=30, stage="rewrite",
        )
        kwargs = self._fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-mini-deploy")


class AnthropicBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clear_backend_env()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        self._saved_module = sys.modules.get("anthropic")
        self._fake_client = MagicMock(name="AnthropicClient")
        self._fake_client.messages.create = MagicMock()
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = MagicMock(return_value=self._fake_client)  # type: ignore[attr-defined]
        sys.modules["anthropic"] = fake_anthropic

    def tearDown(self) -> None:
        if self._saved_module is not None:
            sys.modules["anthropic"] = self._saved_module
        else:
            sys.modules.pop("anthropic", None)
        _clear_backend_env()
        _restore_env(self._saved)

    def _make_resp(self, text: str, *, input_tokens: int = 10,
                   output_tokens: int = 20):
        block = MagicMock(type="text", text=text)
        return MagicMock(
            content=[block],
            usage=MagicMock(input_tokens=input_tokens,
                            output_tokens=output_tokens),
        )

    def test_text_call_returns_string(self) -> None:
        self._fake_client.messages.create.return_value = \
            self._make_resp("plain reply")
        out = llm_cli._call_anthropic_sdk(
            "say hi", output_json=False, json_schema=None,
            model="haiku", timeout_s=30, stage="rewrite",
        )
        self.assertEqual(out, "plain reply")
        kwargs = self._fake_client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-4-5")

    def test_json_call_augments_prompt(self) -> None:
        self._fake_client.messages.create.return_value = \
            self._make_resp('{"hook":"x"}')
        out = llm_cli._call_anthropic_sdk(
            "rewrite", output_json=True, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite",
        )
        self.assertEqual(out, {"hook": "x"})
        kwargs = self._fake_client.messages.create.call_args.kwargs
        self.assertIn("ONLY a JSON object", kwargs["messages"][0]["content"])

    def test_json_schema_embedded_in_prompt(self) -> None:
        self._fake_client.messages.create.return_value = \
            self._make_resp('{"a":1}')
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        llm_cli._call_anthropic_sdk(
            "rewrite", output_json=True, json_schema=schema,
            model="opus", timeout_s=30, stage="rewrite",
        )
        kwargs = self._fake_client.messages.create.call_args.kwargs
        self.assertIn("matching this schema", kwargs["messages"][0]["content"])

    def test_concatenates_multiple_text_blocks(self) -> None:
        block1 = MagicMock(type="text", text='{"a":')
        block2 = MagicMock(type="text", text=' 1}')
        block3 = MagicMock(type="tool_use", text=None)  # ignored
        self._fake_client.messages.create.return_value = MagicMock(
            content=[block1, block2, block3], usage=None,
        )
        out = llm_cli._call_anthropic_sdk(
            "x", output_json=True, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite",
        )
        self.assertEqual(out, {"a": 1})

    def test_unknown_model_passes_through_to_api(self) -> None:
        self._fake_client.messages.create.return_value = \
            self._make_resp("hi")
        llm_cli._call_anthropic_sdk(
            "x", output_json=False, json_schema=None,
            model="claude-opus-4-7-experimental", timeout_s=30, stage="rewrite",
        )
        kwargs = self._fake_client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-4-7-experimental")

    def test_exception_wraps_as_claude_cli_error(self) -> None:
        self._fake_client.messages.create.side_effect = \
            Exception("api unavailable")
        with self.assertRaises(llm_cli.ClaudeCLIError) as ctx:
            llm_cli._call_anthropic_sdk(
                "x", output_json=False, json_schema=None,
                model="opus", timeout_s=30, stage="rewrite",
            )
        self.assertIn("api unavailable", str(ctx.exception))

    def test_missing_api_key_propagates_via_sdk_error(self) -> None:
        # When ANTHROPIC_API_KEY is missing the real SDK raises
        # AuthenticationError on the first call. We simulate that by
        # making the fake client raise; our adapter must wrap it.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self._fake_client.messages.create.side_effect = \
            Exception("authentication failed: no api key")
        with self.assertRaises(llm_cli.ClaudeCLIError) as ctx:
            llm_cli._call_anthropic_sdk(
                "x", output_json=False, json_schema=None,
                model="opus", timeout_s=30, stage="rewrite",
            )
        self.assertIn("authentication", str(ctx.exception).lower())


class CallLlmAliasTest(unittest.TestCase):
    """Verify the new ``call_llm`` alias is the same dispatcher."""

    def test_call_llm_is_same_object_as_call_claude_cli(self) -> None:
        from pipeline.llm import call_llm, call_claude_cli
        self.assertIs(call_llm, call_claude_cli)


class MaxTokensForTest(unittest.TestCase):
    """Pin the per-stage output-token budget table.

    Regression — the 2026-05-12 mystoriesanimated 30-min render came
    back at 17 min (2289-word script) because both SDK adapters
    defaulted to 4096 max output tokens. ``max_tokens_for("rewrite_long_form")``
    must now be high enough to fit a 30-min sectioned script (~6500
    prose tokens + JSON envelope overhead).
    """

    def setUp(self) -> None:
        self._saved = {
            k: os.environ.pop(k, None)
            for k in [
                "YTFACTORY_MAX_TOKENS_REWRITE_LONG_FORM",
                "YTFACTORY_MAX_TOKENS_REWRITE",
                "YTFACTORY_MAX_TOKENS_CAST",
                "YTFACTORY_MAX_TOKENS_CUSTOM_STAGE",
            ]
            if k in os.environ
        }

    def tearDown(self) -> None:
        for k in [
            "YTFACTORY_MAX_TOKENS_REWRITE_LONG_FORM",
            "YTFACTORY_MAX_TOKENS_REWRITE",
            "YTFACTORY_MAX_TOKENS_CAST",
            "YTFACTORY_MAX_TOKENS_CUSTOM_STAGE",
        ]:
            os.environ.pop(k, None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_long_form_rewrite_gets_at_least_8k_tokens(self) -> None:
        # 4500-word 30-min script ≈ 6500 prose tokens + JSON wrapper.
        # Cap below 8k risks truncation again.
        self.assertGreaterEqual(
            llm_cli.max_tokens_for("rewrite_long_form"),
            8000,
            "rewrite_long_form must allow ≥8k output tokens to avoid "
            "truncating 30-min scripts (the 2026-05-12 17-min-vs-30-min bug)",
        )

    def test_short_stages_stay_at_4096_default(self) -> None:
        # Conservative default for everything we haven't bumped on
        # purpose — keeps spend predictable.
        self.assertEqual(llm_cli.max_tokens_for("rewrite"), 4096)
        self.assertEqual(llm_cli.max_tokens_for("cast"), 4096)
        self.assertEqual(llm_cli.max_tokens_for("prompts"), 4096)

    def test_unknown_stage_falls_back_to_4096(self) -> None:
        self.assertEqual(llm_cli.max_tokens_for("totally_new_stage"), 4096)

    def test_none_stage_falls_back_to_4096(self) -> None:
        self.assertEqual(llm_cli.max_tokens_for(None), 4096)

    def test_env_override_per_stage(self) -> None:
        os.environ["YTFACTORY_MAX_TOKENS_REWRITE_LONG_FORM"] = "16000"
        self.assertEqual(llm_cli.max_tokens_for("rewrite_long_form"), 16000)

    def test_env_override_clamped_to_minimum(self) -> None:
        # Even if a hostile env sets 0, we floor at 256 so the call
        # doesn't error out with "max_tokens must be positive".
        os.environ["YTFACTORY_MAX_TOKENS_REWRITE"] = "0"
        self.assertEqual(llm_cli.max_tokens_for("rewrite"), 256)

    def test_env_override_invalid_int_falls_back_to_default(self) -> None:
        os.environ["YTFACTORY_MAX_TOKENS_REWRITE"] = "not_a_number"
        self.assertEqual(llm_cli.max_tokens_for("rewrite"), 4096)


class MaxTokensWiredIntoBackendsTest(unittest.TestCase):
    """The dispatcher's max_tokens_for(stage) value must reach BOTH SDK
    backends — Azure as ``max_tokens`` and Anthropic as ``max_tokens``.
    Regression: pre-fix, Azure had no token cap (deployment default
    4096) and Anthropic hardcoded 4096. Both truncated 30-min long-form
    scripts."""

    def setUp(self) -> None:
        self._saved = _clear_backend_env()
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://x.openai.azure.com"
        os.environ["AZURE_OPENAI_API_KEY"] = "sk-azure"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant"

        self._saved_openai = sys.modules.get("openai")
        self._fake_az = MagicMock(name="AzureClient")
        self._fake_az.chat.completions.create = MagicMock()
        self._fake_az.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="hi"))],
            usage=MagicMock(prompt_tokens=1, completion_tokens=1),
        )
        fake_openai = types.ModuleType("openai")
        fake_openai.AzureOpenAI = MagicMock(return_value=self._fake_az)  # type: ignore[attr-defined]
        sys.modules["openai"] = fake_openai

        self._saved_anthropic = sys.modules.get("anthropic")
        self._fake_ant = MagicMock(name="AnthropicClient")
        self._fake_ant.messages.create = MagicMock()
        block = MagicMock(type="text", text="hi")
        self._fake_ant.messages.create.return_value = MagicMock(
            content=[block], usage=MagicMock(input_tokens=1, output_tokens=1),
        )
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = MagicMock(return_value=self._fake_ant)  # type: ignore[attr-defined]
        sys.modules["anthropic"] = fake_anthropic

    def tearDown(self) -> None:
        if self._saved_openai is not None:
            sys.modules["openai"] = self._saved_openai
        else:
            sys.modules.pop("openai", None)
        if self._saved_anthropic is not None:
            sys.modules["anthropic"] = self._saved_anthropic
        else:
            sys.modules.pop("anthropic", None)
        _clear_backend_env()
        _restore_env(self._saved)

    def test_azure_passes_stage_max_tokens(self) -> None:
        llm_cli._call_azure_openai(
            "x", output_json=False, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite_long_form",
        )
        kwargs = self._fake_az.chat.completions.create.call_args.kwargs
        self.assertEqual(
            kwargs.get("max_tokens"),
            llm_cli.max_tokens_for("rewrite_long_form"),
            "Azure backend must pass max_tokens=max_tokens_for(stage) so "
            "long-form rewrite gets the bumped budget",
        )

    def test_anthropic_passes_stage_max_tokens(self) -> None:
        llm_cli._call_anthropic_sdk(
            "x", output_json=False, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite_long_form",
        )
        kwargs = self._fake_ant.messages.create.call_args.kwargs
        self.assertEqual(
            kwargs.get("max_tokens"),
            llm_cli.max_tokens_for("rewrite_long_form"),
            "Anthropic backend must pass max_tokens=max_tokens_for(stage) "
            "instead of the old hardcoded 4096",
        )

    def test_azure_default_stage_uses_fallback(self) -> None:
        llm_cli._call_azure_openai(
            "x", output_json=False, json_schema=None,
            model="opus", timeout_s=30, stage="rewrite",
        )
        kwargs = self._fake_az.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs.get("max_tokens"), 4096)


class CliSubprocessMaxTokensTest(unittest.TestCase):
    """Audit T1.4 — the CLI subprocess can't pass --max-tokens (the
    Anthropic CLI doesn't expose it), but we must (1) surface the
    requested cap in telemetry so the dashboard sees it symmetric
    with the SDK backends, and (2) scale --max-budget-usd to roughly
    match so a long-form rewrite isn't dollar-capped at the default
    when the SDK backends would have allowed 12-32k tokens."""

    def setUp(self) -> None:
        self._saved = _clear_backend_env()

    def tearDown(self) -> None:
        _restore_env(self._saved)

    def _make_envelope_proc(self, payload_str: str) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ('{"is_error": false, "result": "' + payload_str
                       + '", "usage": {"input_tokens": 10, "output_tokens": 5}}')
        proc.stderr = ""
        return proc

    def test_long_form_stage_scales_budget_above_default(self) -> None:
        # rewrite_long_form has max_tokens_for=12000 → derived budget
        # must exceed the default $2.00 cap so long-form prompts aren't
        # silently dollar-capped at the haiku-tier default.
        derived_budget_args: list[str] = []
        with patch("subprocess.run",
                   return_value=self._make_envelope_proc("hi")) as mock_run:
            llm_cli._call_claude_cli_subprocess(
                "long-form prompt",
                output_json=False,
                model="opus",
                stage="rewrite_long_form",
            )
            cmd = mock_run.call_args.args[0]
        # Find --max-budget-usd <value> in the cmd.
        idx = cmd.index("--max-budget-usd")
        budget_str = cmd[idx + 1]
        derived_budget_args.append(budget_str)
        budget_val = float(budget_str)
        self.assertGreater(
            budget_val, llm_cli.DEFAULT_BUDGET_USD,
            f"long-form stage should scale --max-budget-usd above "
            f"the {llm_cli.DEFAULT_BUDGET_USD} default; got {budget_val}",
        )

    def test_explicit_budget_kwarg_not_overridden(self) -> None:
        # When the caller passes a non-default budget_usd, the
        # subprocess must honour it as-is (operator override).
        with patch("subprocess.run",
                   return_value=self._make_envelope_proc("hi")) as mock_run:
            llm_cli._call_claude_cli_subprocess(
                "x",
                output_json=False,
                model="opus",
                stage="rewrite_long_form",
                budget_usd=99.99,
            )
            cmd = mock_run.call_args.args[0]
        idx = cmd.index("--max-budget-usd")
        self.assertEqual(cmd[idx + 1], "99.99")

    def test_telemetry_records_max_tokens_requested(self) -> None:
        # Audit T1.4 — even though the CLI can't enforce the cap, the
        # would-be max_tokens MUST land in telemetry metadata so the
        # dashboard sees per-stage budgets symmetric with SDK backends.
        captured: list[dict] = []
        from pipeline.llm import cli as cli_mod

        def fake_track(*_a, **kwargs):
            captured.append(kwargs.get("metadata") or {})

        with patch.object(cli_mod._tlm, "track", side_effect=fake_track), \
             patch("subprocess.run",
                   return_value=self._make_envelope_proc("hi")):
            cli_mod._call_claude_cli_subprocess(
                "x", output_json=False, model="opus",
                stage="rewrite_long_form",
            )
        # Pick up the success track call's metadata.
        success_metas = [m for m in captured if "max_tokens_requested" in m]
        self.assertTrue(success_metas, "expected max_tokens_requested in CLI metadata")
        self.assertEqual(
            success_metas[0]["max_tokens_requested"],
            llm_cli.max_tokens_for("rewrite_long_form"),
        )
        self.assertGreater(success_metas[0]["budget_usd_cap"],
                           llm_cli.DEFAULT_BUDGET_USD)


if __name__ == "__main__":
    unittest.main()
