"""Supplemental coverage for pipeline/utils/state_client.py.

Covers the lines missed by the existing tests/test_state_client.py:
  - Line 62:  empty response body → _request returns None
  - Lines 68-70: urllib.error.URLError → WebsiteUnreachableError
  - Line 119: _cli_put --file argument
  - Lines 133-139: _cli_delete (success + not-found)
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.utils import state_client
from pipeline.cloud.skill_dispatch import WebsiteUnreachableError

_BASE = Path(__file__).resolve().parent


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestStateClientExtra(unittest.TestCase):
    def setUp(self) -> None:
        self._auth_patch = patch(
            "pipeline.utils.state_client._auth_headers",
            return_value={},
        )
        self._auth_patch.start()

    def tearDown(self) -> None:
        self._auth_patch.stop()

    # ------------------------------------------------------------------
    # Line 62: empty body → returns None
    # ------------------------------------------------------------------
    def test_empty_response_body_returns_none(self) -> None:
        def fake_urlopen(req, timeout=None):
            return _FakeResponse(b"")

        with patch("pipeline.utils.state_client.urllib.request.urlopen", fake_urlopen):
            result = state_client._request("GET", "/api/state/ch/narrations/s")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Lines 68-70: urllib.error.URLError → WebsiteUnreachableError
    # ------------------------------------------------------------------
    def test_url_error_raises_website_unreachable(self) -> None:
        def raise_url_error(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        with patch("pipeline.utils.state_client.urllib.request.urlopen", raise_url_error):
            with self.assertRaises(WebsiteUnreachableError):
                state_client.get("ch", "narrations", "slug")

    # ------------------------------------------------------------------
    # Line 119: _cli_put --file reads from a real file
    # ------------------------------------------------------------------
    def test_cli_put_reads_from_file(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            json_file = scratch / "payload.json"
            json_file.write_text(json.dumps({"hello": "world"}))

            def fake_urlopen(req, timeout=None):
                return _FakeResponse(json.dumps({"ok": True, "uri": "gs://b/x"}).encode())

            with patch("pipeline.utils.state_client.urllib.request.urlopen", fake_urlopen):
                rc = state_client.main([
                    "put", "ch", "narrations", "slug",
                    "--file", str(json_file),
                ])
            self.assertEqual(rc, 0)
        finally:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)

    # ------------------------------------------------------------------
    # Lines 133-139: _cli_delete success
    # ------------------------------------------------------------------
    def test_cli_delete_success_prints_ok(self) -> None:
        def fake_urlopen(req, timeout=None):
            return _FakeResponse(json.dumps({"ok": True}).encode())

        buf = io.StringIO()
        with patch("pipeline.utils.state_client.urllib.request.urlopen", fake_urlopen):
            with patch("sys.stdout", buf):
                rc = state_client.main(["delete", "ch", "narrations", "slug"])
        self.assertEqual(rc, 0)
        self.assertIn("ok", buf.getvalue())

    # ------------------------------------------------------------------
    # Lines 135-137: _cli_delete not found → exits 4
    # ------------------------------------------------------------------
    def test_cli_delete_not_found_exits_4(self) -> None:
        def raise_404(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 404, "not found", {}, io.BytesIO(b"not found")
            )

        with patch("pipeline.utils.state_client.urllib.request.urlopen", raise_404):
            rc = state_client.main(["delete", "ch", "narrations", "missing"])
        self.assertEqual(rc, 4)

    # ------------------------------------------------------------------
    # non-404 HTTP error propagates as RuntimeError
    # ------------------------------------------------------------------
    def test_http_500_raises_runtime_error(self) -> None:
        def raise_500(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 500, "server error", {}, io.BytesIO(b"oops")
            )

        with patch("pipeline.utils.state_client.urllib.request.urlopen", raise_500):
            with self.assertRaises(RuntimeError):
                state_client.get("ch", "narrations", "slug")


if __name__ == "__main__":
    unittest.main()
