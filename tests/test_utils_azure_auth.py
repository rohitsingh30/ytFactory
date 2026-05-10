"""100% line coverage for pipeline/utils/azure_auth.py."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.utils import azure_auth


class TestGetApiKey(unittest.TestCase):
    def test_returns_key_when_set(self) -> None:
        with patch.dict(os.environ, {"AZURE_API_KEY": "my-secret"}):
            key = azure_auth.get_api_key()
        self.assertEqual(key, "my-secret")

    def test_strips_whitespace_from_key(self) -> None:
        with patch.dict(os.environ, {"AZURE_API_KEY": "  stripped  "}):
            key = azure_auth.get_api_key()
        self.assertEqual(key, "stripped")

    def test_raises_when_key_missing(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if k != "AZURE_API_KEY"}
        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaises(azure_auth.AzureKeyMissing):
                azure_auth.get_api_key()

    def test_raises_when_key_empty_string(self) -> None:
        with patch.dict(os.environ, {"AZURE_API_KEY": ""}):
            with self.assertRaises(azure_auth.AzureKeyMissing):
                azure_auth.get_api_key()

    def test_raises_when_key_whitespace_only(self) -> None:
        with patch.dict(os.environ, {"AZURE_API_KEY": "   "}):
            with self.assertRaises(azure_auth.AzureKeyMissing):
                azure_auth.get_api_key()


class TestTlsVerify(unittest.TestCase):
    def _verify_with(self, value: str | None) -> bool:
        if value is None:
            env = {k: v for k, v in os.environ.items() if k != "AZURE_TLS_VERIFY"}
            with patch.dict(os.environ, env, clear=True):
                return azure_auth.tls_verify()
        with patch.dict(os.environ, {"AZURE_TLS_VERIFY": value}):
            return azure_auth.tls_verify()

    def test_default_is_true(self) -> None:
        self.assertTrue(self._verify_with(None))

    def test_one_is_true(self) -> None:
        self.assertTrue(self._verify_with("1"))

    def test_zero_is_false(self) -> None:
        self.assertFalse(self._verify_with("0"))

    def test_false_string_is_false(self) -> None:
        self.assertFalse(self._verify_with("false"))

    def test_no_string_is_false(self) -> None:
        self.assertFalse(self._verify_with("no"))

    def test_off_string_is_false(self) -> None:
        self.assertFalse(self._verify_with("off"))

    def test_other_truthy_value(self) -> None:
        self.assertTrue(self._verify_with("yes"))


if __name__ == "__main__":
    unittest.main()
