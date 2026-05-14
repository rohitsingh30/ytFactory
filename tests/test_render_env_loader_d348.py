"""Audit D3.48 — shared `.env` loader for the render pipeline.

Pre-fix three modules had near-identical `_load_env` implementations,
two of which stripped outer quotes (`.env` line `KEY="value"` →
`os.environ["KEY"] = "value"`) and one of which did NOT (it wrote
the literal `"value"` string with quotes). Tests pin the shared
loader's contract so all three render entry points behave the same.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.render.shared.env_loader import (
    _strip_outer_quotes,
    load_dotenv_into_environ,
)


class StripOuterQuotesTest(unittest.TestCase):
    def test_double_quotes_stripped(self):
        self.assertEqual(_strip_outer_quotes('"hello"'), "hello")

    def test_single_quotes_stripped(self):
        self.assertEqual(_strip_outer_quotes("'hello'"), "hello")

    def test_unmatched_quotes_kept(self):
        # Pre-fix the long_form/sports_doc version called
        # `.strip('"').strip("'")` which would strip mismatched outer
        # quotes too — leaking quote chars in some configs. The
        # shared helper only strips MATCHING pairs.
        self.assertEqual(_strip_outer_quotes('"hello\''), '"hello\'')

    def test_no_quotes(self):
        self.assertEqual(_strip_outer_quotes("hello"), "hello")

    def test_empty_string(self):
        self.assertEqual(_strip_outer_quotes(""), "")

    def test_just_two_quotes_yields_empty(self):
        # `""` → strip outer pair → `` (empty string).
        self.assertEqual(_strip_outer_quotes('""'), "")
        self.assertEqual(_strip_outer_quotes("''"), "")

    def test_inner_quotes_preserved(self):
        # KEY="hello "world"" → strip outer ⇒ `hello "world"` (the
        # inner quotes survive).
        self.assertEqual(_strip_outer_quotes('"hello "world""'), 'hello "world"')


class LoadDotenvTest(unittest.TestCase):
    def setUp(self):
        self.env_keys: list[str] = []

    def tearDown(self):
        for k in self.env_keys:
            os.environ.pop(k, None)

    def _track(self, *keys: str) -> None:
        self.env_keys.extend(keys)

    def test_no_env_file_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            load_dotenv_into_environ(Path(tmp))  # must not raise

    def test_quoted_value_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                'HF_TOKEN="hf_abc123"\n'
                "PLAIN_KEY=plain_value\n"
                "SINGLE_QUOTED='quoted'\n"
            )
            self._track("HF_TOKEN", "PLAIN_KEY", "SINGLE_QUOTED")
            for k in ("HF_TOKEN", "PLAIN_KEY", "SINGLE_QUOTED"):
                os.environ.pop(k, None)
            load_dotenv_into_environ(Path(tmp))
        self.assertEqual(os.environ["HF_TOKEN"], "hf_abc123",
                         "outer quotes must be stripped")
        self.assertEqual(os.environ["PLAIN_KEY"], "plain_value")
        self.assertEqual(os.environ["SINGLE_QUOTED"], "quoted")

    def test_setdefault_does_not_override_shell(self):
        # Shell-set env wins over .env (`setdefault` semantics).
        self._track("__D3_48_TEST_VAR")
        os.environ["__D3_48_TEST_VAR"] = "shell-wins"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "__D3_48_TEST_VAR=dotenv-loses\n"
            )
            load_dotenv_into_environ(Path(tmp))
        self.assertEqual(os.environ["__D3_48_TEST_VAR"], "shell-wins")

    def test_comments_and_blank_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "# This is a comment\n"
                "\n"
                "REAL_KEY=real_value\n"
                "  # indented comment is skipped after strip\n"
                "  ANOTHER=value2\n"
            )
            self._track("REAL_KEY", "ANOTHER")
            for k in ("REAL_KEY", "ANOTHER"):
                os.environ.pop(k, None)
            load_dotenv_into_environ(Path(tmp))
        self.assertEqual(os.environ["REAL_KEY"], "real_value")
        self.assertEqual(os.environ["ANOTHER"], "value2")

    def test_value_with_equals_in_it(self):
        # KEY=a=b=c → split on FIRST `=` → value is `a=b=c`.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("URL=https://example.com/?k=v&x=y\n")
            self._track("URL")
            os.environ.pop("URL", None)
            load_dotenv_into_environ(Path(tmp))
        self.assertEqual(os.environ["URL"], "https://example.com/?k=v&x=y")


class RenderEntryPointsUseSharedLoaderTest(unittest.TestCase):
    """Audit D3.48 — pin that all three render entry points delegate
    to the shared loader.

    Pre-2026-05-14 this asserted the legacy renderer modules
    (`_legacy/long_form.py`, `_legacy/sports_doc.py`) delegated to
    the shared loader. Those legacy modules were deleted in the
    bigbang cleanup (their orchestrator entrypoints are replaced by
    the engine path; the helpers were moved into the plugin layer).
    The shared loader itself is still pinned by the `LoadDotenvTest`
    + `StripOuterQuotesTest` classes above.
    """

    def test_shared_loader_module_is_importable(self):
        # Smoke: the shared loader API still exists and the cloud
        # worker / engine path can reach it.
        from pipeline.render.shared.env_loader import (
            load_dotenv_into_environ,
            _strip_outer_quotes,
        )
        self.assertTrue(callable(load_dotenv_into_environ))
        self.assertTrue(callable(_strip_outer_quotes))


if __name__ == "__main__":
    unittest.main()
