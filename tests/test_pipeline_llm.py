"""Tests for pipeline.llm — focuses on the pure JSON-extraction logic.

The actual `claude -p` subprocess call is not exercised here (that's
integration); we test that the parser tolerates the messes that real
LLM outputs come back with: ```json fences, surrounding chatter,
JSON-looking but invalid content, etc.
"""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import ClaudeCLIError, _parse_inner_json


class ParseInnerJsonHappyPathTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(_parse_inner_json('{"a": 1}'), {"a": 1})

    def test_plain_array(self):
        self.assertEqual(_parse_inner_json("[1, 2, 3]"), [1, 2, 3])

    def test_nested_object(self):
        out = _parse_inner_json('{"narrator": {"description": "x", "age_band": "adult"}}')
        self.assertEqual(out["narrator"]["description"], "x")
        self.assertEqual(out["narrator"]["age_band"], "adult")

    def test_unicode_passthrough(self):
        out = _parse_inner_json('{"emoji": "👍"}')
        self.assertEqual(out["emoji"], "👍")


class ParseInnerJsonFenceStrippingTest(unittest.TestCase):
    def test_json_code_fence(self):
        out = _parse_inner_json('```json\n{"a": 1}\n```')
        self.assertEqual(out, {"a": 1})

    def test_bare_code_fence(self):
        out = _parse_inner_json("```\n{\"a\": 2}\n```")
        self.assertEqual(out, {"a": 2})

    def test_fenced_array(self):
        out = _parse_inner_json('```json\n[{"k": "v"}]\n```')
        self.assertEqual(out, [{"k": "v"}])

    def test_extra_whitespace_around_fences(self):
        out = _parse_inner_json('  ```json\n  {"a": 1}\n  ```  \n')
        self.assertEqual(out, {"a": 1})


class ParseInnerJsonFallbackExtractionTest(unittest.TestCase):
    """When the model wraps JSON in chatter, the parser falls back to
    finding the first balanced {...} or [...] block."""

    def test_object_buried_in_text(self):
        out = _parse_inner_json(
            "Here's the result you asked for:\n"
            '{"score": 7, "reason": "fine"}\n'
            "Hope that helps."
        )
        self.assertEqual(out, {"score": 7, "reason": "fine"})

    def test_array_buried_in_text(self):
        out = _parse_inner_json(
            "Sure thing! [\"a\", \"b\", \"c\"] there you go"
        )
        self.assertEqual(out, ["a", "b", "c"])

    def test_brace_balance_handles_nested(self):
        # Inner braces shouldn't terminate the outer scan early.
        out = _parse_inner_json(
            'preface {"outer": {"inner": {"deep": 42}}} suffix'
        )
        self.assertEqual(out, {"outer": {"inner": {"deep": 42}}})

    def test_prefers_object_over_later_array(self):
        # First balanced block wins when both are present.
        out = _parse_inner_json('{"a": 1} then [1, 2]')
        self.assertEqual(out, {"a": 1})


class ParseInnerJsonFailureTest(unittest.TestCase):
    def test_no_json_at_all_raises(self):
        with self.assertRaises(ClaudeCLIError):
            _parse_inner_json("just words, no JSON here")

    def test_truncated_object_raises(self):
        # Unbalanced braces — extraction can't find a complete block
        with self.assertRaises(ClaudeCLIError):
            _parse_inner_json('{"a": 1, "b": ')

    def test_garbage_inside_braces_raises(self):
        with self.assertRaises(ClaudeCLIError):
            _parse_inner_json("{not actually json}")


# ---- Additional coverage for backend selection and provider adapters ----

import json
import os
import subprocess
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.llm import cli as llm_cli


def _proc(stdout, *, stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class CallClaudeCLISubprocessTest(unittest.TestCase):
    def _run(self, stdout, **kwargs):
        env = {"YTFACTORY_LLM_BACKEND": "cli"}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(llm_cli.subprocess, "run", return_value=_proc(stdout, **kwargs)), \
             patch.object(llm_cli._tlm, "track"):
            return llm_cli.call_claude_cli("prompt", model="haiku")

    def test_cli_success_parses_inner_json_and_builds_command(self):
        stdout = json.dumps({"result": '{"ok": true}', "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01})
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "cli"}, clear=False), \
             patch.object(llm_cli.subprocess, "run", return_value=_proc(stdout)) as run, \
             patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli.call_claude_cli("prompt", allowed_tools=["Read"], add_dirs=[PROJECT_ROOT], budget_usd=0.2), {"ok": True})
        cmd = run.call_args.args[0]
        self.assertIn("--allowedTools", cmd)
        self.assertIn("Read", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIn(str(PROJECT_ROOT), cmd)

    def test_cli_output_json_false_returns_result_text(self):
        stdout = json.dumps({"result": "plain text"})
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "cli"}, clear=False), \
             patch.object(llm_cli.subprocess, "run", return_value=_proc(stdout)), \
             patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli.call_claude_cli("prompt", output_json=False), "plain text")

    def test_cli_json_schema_prefers_structured_output(self):
        stdout = json.dumps({"result": "", "structured_output": [{"a": 1}]})
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "cli"}, clear=False), \
             patch.object(llm_cli.subprocess, "run", return_value=_proc(stdout)), \
             patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli.call_claude_cli("prompt", json_schema={"type": "array"}), [{"a": 1}])

    def test_cli_timeout_nonzero_nonjson_error_envelope_and_missing_result_raise(self):
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "cli"}, clear=False), \
             patch.object(llm_cli.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 1)), \
             patch.object(llm_cli._tlm, "track"):
            with self.assertRaises(ClaudeCLIError):
                llm_cli.call_claude_cli("prompt", timeout_s=1)
        bad_cases = [
            _proc("", stderr="boom", returncode=2),
            _proc("not json"),
            _proc(json.dumps({"is_error": True, "result": "bad"})),
            _proc(json.dumps({"usage": {}})),
        ]
        for proc in bad_cases:
            with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "cli"}, clear=False), \
                 patch.object(llm_cli.subprocess, "run", return_value=proc), \
                 patch.object(llm_cli._tlm, "track"):
                with self.assertRaises(ClaudeCLIError):
                    llm_cli.call_claude_cli("prompt")

    def test_call_claude_routes_to_selected_cloud_backends(self):
        if not hasattr(llm_cli, "_call_azure_openai"):
            self.skipTest("cloud backend adapters not present in this source version")
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "azure_openai"}, clear=False), \
             patch.object(llm_cli, "_call_azure_openai", return_value={"azure": True}) as azure:
            self.assertEqual(llm_cli.call_claude_cli("p"), {"azure": True})
        azure.assert_called_once()
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "anthropic_sdk"}, clear=False), \
             patch.object(llm_cli, "_call_anthropic_sdk", return_value={"sdk": True}) as sdk:
            self.assertEqual(llm_cli.call_claude_cli("p"), {"sdk": True})
        sdk.assert_called_once()


class BackendSelectionTest(unittest.TestCase):
    def test_azure_model_for_env_specific_fallback_and_defaults(self):
        if not hasattr(llm_cli, "_azure_model_for"):
            self.skipTest("azure backend not present in this source version")
        with patch.dict(os.environ, {"AZURE_OPENAI_MODEL_OPUS": "opus-deploy"}, clear=True):
            self.assertEqual(llm_cli._azure_model_for("opus"), "opus-deploy")
        with patch.dict(os.environ, {"AZURE_OPENAI_MODEL": "fallback"}, clear=True):
            self.assertEqual(llm_cli._azure_model_for("haiku"), "fallback")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm_cli._azure_model_for("opus"), "gpt-4o")
            self.assertEqual(llm_cli._azure_model_for("sonnet"), "gpt-4o-mini")

    def test_shutil_which_and_backend_priority(self):
        if not hasattr(llm_cli, "_choose_backend"):
            self.skipTest("backend selector not present in this source version")
        with patch.object(llm_cli.shutil if hasattr(llm_cli, 'shutil') else llm_cli, "_unused", create=True):
            pass
        self.assertIsNotNone(llm_cli._shutil_which("sh"))
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "anthropic_sdk"}, clear=True):
            self.assertEqual(llm_cli._choose_backend(), "anthropic_sdk")
        with patch.dict(os.environ, {"YTFACTORY_LLM_BACKEND": "bogus"}, clear=True), patch.object(llm_cli, "_shutil_which", return_value="/bin/claude"):
            self.assertEqual(llm_cli._choose_backend(), "cli")
        with patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": "e", "AZURE_OPENAI_API_KEY": "k"}, clear=True), patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), "azure_openai")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=True), patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), "anthropic_sdk")
            self.assertTrue(llm_cli._should_use_sdk())
        with patch.dict(os.environ, {}, clear=True), patch.object(llm_cli, "_shutil_which", return_value=None):
            self.assertEqual(llm_cli._choose_backend(), "cli")
            self.assertFalse(llm_cli._should_use_sdk())


class AnthropicSDKAdapterTest(unittest.TestCase):
    def _fake_module(self, *, text='{"ok": true}', exc=None):
        fake = types.ModuleType("anthropic")
        holder = {}
        class FakeMessages:
            def __init__(self):
                self.calls = []
            def create(self, **kwargs):
                self.calls.append(kwargs)
                if exc:
                    raise exc
                usage = SimpleNamespace(input_tokens=3, output_tokens=4)
                return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=usage)
        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout
                self.messages = FakeMessages()
                holder["client"] = self
        fake.Anthropic = FakeClient
        return fake, holder

    def test_import_error_is_wrapped(self):
        if not hasattr(llm_cli, "_call_anthropic_sdk"):
            self.skipTest("anthropic adapter not present in this source version")
        with patch.dict(sys.modules, {"anthropic": None}):
            with self.assertRaises(ClaudeCLIError):
                llm_cli._call_anthropic_sdk("p", output_json=True, json_schema=None, model="haiku", timeout_s=1)

    def test_success_json_text_and_schema_prompt(self):
        if not hasattr(llm_cli, "_call_anthropic_sdk"):
            self.skipTest("anthropic adapter not present in this source version")
        fake, holder = self._fake_module(text='{"ok": true}')
        with patch.dict(sys.modules, {"anthropic": fake}), patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli._call_anthropic_sdk("p", output_json=True, json_schema={"type": "object"}, model="haiku", timeout_s=5), {"ok": True})
        call_prompt = holder["client"].messages.calls[0]["messages"][0]["content"]
        self.assertIn("matching this schema", call_prompt)
        fake, _ = self._fake_module(text="plain")
        with patch.dict(sys.modules, {"anthropic": fake}), patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli._call_anthropic_sdk("p", output_json=False, json_schema=None, model="custom", timeout_s=5), "plain")
        fake, holder = self._fake_module(text='{"ok": true}')
        with patch.dict(sys.modules, {"anthropic": fake}), patch.object(llm_cli._tlm, "track"):
            llm_cli._call_anthropic_sdk("p", output_json=True, json_schema=None, model="sonnet", timeout_s=5)
        self.assertIn("ONLY a JSON object", holder["client"].messages.calls[0]["messages"][0]["content"])

    def test_sdk_failure_is_wrapped(self):
        if not hasattr(llm_cli, "_call_anthropic_sdk"):
            self.skipTest("anthropic adapter not present in this source version")
        fake, _ = self._fake_module(exc=RuntimeError("network"))
        with patch.dict(sys.modules, {"anthropic": fake}), patch.object(llm_cli._tlm, "track"):
            with self.assertRaises(ClaudeCLIError):
                llm_cli._call_anthropic_sdk("p", output_json=True, json_schema=None, model="haiku", timeout_s=5)


class AzureOpenAIAdapterTest(unittest.TestCase):
    def _fake_openai(self, outcomes):
        fake = types.ModuleType("openai")
        holder = {"calls": [], "ctor": None}
        class FakeCompletions:
            def create(self, **kwargs):
                holder["calls"].append(kwargs)
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        class FakeClient:
            def __init__(self, **kwargs):
                holder["ctor"] = kwargs
                self.chat = SimpleNamespace(completions=FakeCompletions())
        fake.AzureOpenAI = FakeClient
        return fake, holder

    def _resp(self, text='{"ok": true}'):
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=6)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=usage)

    def test_import_and_env_errors(self):
        if not hasattr(llm_cli, "_call_azure_openai"):
            self.skipTest("azure adapter not present in this source version")
        with patch.dict(sys.modules, {"openai": None}):
            with self.assertRaises(ClaudeCLIError):
                llm_cli._call_azure_openai("p", output_json=True, json_schema=None, model="haiku", timeout_s=1)
        fake, _ = self._fake_openai([self._resp()])
        with patch.dict(sys.modules, {"openai": fake}), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ClaudeCLIError):
                llm_cli._call_azure_openai("p", output_json=True, json_schema=None, model="haiku", timeout_s=1)

    def test_success_json_text_schema_and_retry_without_response_format(self):
        if not hasattr(llm_cli, "_call_azure_openai"):
            self.skipTest("azure adapter not present in this source version")
        fake, holder = self._fake_openai([self._resp()])
        env = {"AZURE_OPENAI_ENDPOINT": "https://azure", "AZURE_OPENAI_API_KEY": "key", "AZURE_OPENAI_API_VERSION": "v", "AZURE_OPENAI_MODEL_HAIKU": "deploy"}
        with patch.dict(sys.modules, {"openai": fake}), patch.dict(os.environ, env, clear=True), patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli._call_azure_openai("p", output_json=True, json_schema={"type": "object"}, model="haiku", timeout_s=7), {"ok": True})
        self.assertEqual(holder["ctor"]["timeout"], 7.0)
        self.assertEqual(holder["calls"][0]["model"], "deploy")
        self.assertIn("response_format", holder["calls"][0])
        self.assertIn("schema", holder["calls"][0]["messages"][0]["content"])
        fake, _ = self._fake_openai([self._resp("plain")])
        with patch.dict(sys.modules, {"openai": fake}), patch.dict(os.environ, env, clear=True), patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli._call_azure_openai("p", output_json=False, json_schema=None, model="sonnet", timeout_s=7), "plain")
        fake, holder = self._fake_openai([ValueError("response_format unrecognized"), self._resp('{"retry": true}')])
        with patch.dict(sys.modules, {"openai": fake}), patch.dict(os.environ, env, clear=True), patch.object(llm_cli._tlm, "track"):
            self.assertEqual(llm_cli._call_azure_openai("p", output_json=True, json_schema=None, model="haiku", timeout_s=7), {"retry": True})
        self.assertIn("response_format", holder["calls"][0])
        self.assertNotIn("response_format", holder["calls"][1])

    def test_azure_failures_are_wrapped(self):
        if not hasattr(llm_cli, "_call_azure_openai"):
            self.skipTest("azure adapter not present in this source version")
        env = {"AZURE_OPENAI_ENDPOINT": "https://azure", "AZURE_OPENAI_API_KEY": "key"}
        fake, _ = self._fake_openai([RuntimeError("boom")])
        with patch.dict(sys.modules, {"openai": fake}), patch.dict(os.environ, env, clear=True), patch.object(llm_cli._tlm, "track"):
            with self.assertRaises(ClaudeCLIError):
                llm_cli._call_azure_openai("p", output_json=True, json_schema=None, model="haiku", timeout_s=7)
        fake, _ = self._fake_openai([ValueError("response_format unrecognized"), RuntimeError("retry boom")])
        with patch.dict(sys.modules, {"openai": fake}), patch.dict(os.environ, env, clear=True), patch.object(llm_cli._tlm, "track"):
            with self.assertRaises(ClaudeCLIError):
                llm_cli._call_azure_openai("p", output_json=True, json_schema=None, model="haiku", timeout_s=7)


if __name__ == "__main__":
    unittest.main()
