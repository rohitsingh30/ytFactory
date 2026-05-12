"""Audit Q2.38 — `_serve_script_job_mp4` must contain mp4_path within
the project root or YTFACTORY_RENDER_OUT_DIR.

Pre-fix this handler walked upward via ``p.parent.parent.parent``
(in _resolve_mp4_for_script_job) with no containment check, then
handed whatever path landed in mp4_path straight to FileResponse.
A state.json with an mp4_path of /etc/passwd would have been served.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Set required env BEFORE importing web.server.
os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

from fastapi import HTTPException

from web import server as _server


class TestServeScriptJobMp4Containment(unittest.TestCase):
    """Pin the audit Q2.38 fix: outside-of-allowed-root mp4_path is
    refused with 404 (not served, not 500)."""

    def test_path_outside_project_root_returns_404(self):
        # Use a real existing file outside the repo to prove the gate
        # rejects on path-containment, not on file-existence.
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(b"fake mp4 content for test")
            tmp_path = tf.name
        try:
            # /tmp is outside PROJECT_ROOT (/Users/rohit/ytFactory)
            # AND no YTFACTORY_RENDER_OUT_DIR is set.
            with self.assertRaises(HTTPException) as ctx:
                _server._serve_script_job_mp4({"mp4_path": tmp_path})
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            os.unlink(tmp_path)

    def test_non_mp4_extension_refused_with_400(self):
        # /etc/passwd shaped attack — different extension, even if
        # the file existed at a path inside PROJECT_ROOT.
        with self.assertRaises(HTTPException) as ctx:
            _server._serve_script_job_mp4({"mp4_path": "/etc/passwd"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_traversal_through_dotdot_refused(self):
        # ../../../etc/whatever.mp4 → resolves outside PROJECT_ROOT.
        with self.assertRaises(HTTPException) as ctx:
            _server._serve_script_job_mp4({
                "mp4_path": str(Path("/tmp") / ".." / ".." / "etc" / "whatever.mp4"),
            })
        self.assertEqual(ctx.exception.status_code, 404)

    def test_path_inside_render_out_dir_env_is_allowed(self):
        # When YTFACTORY_RENDER_OUT_DIR is set, paths under that dir
        # are allowed (cloud worker writes mp4s into a dedicated dir
        # outside the repo).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mp4 = tdp / "x.mp4"
            mp4.write_bytes(b"fake")
            from unittest.mock import patch
            with patch.dict(os.environ, {"YTFACTORY_RENDER_OUT_DIR": str(tdp)}):
                # Should not raise — file exists, under allowed root,
                # has .mp4 extension. Returns a FileResponse.
                resp = _server._serve_script_job_mp4({"mp4_path": str(mp4)})
                # FastAPI's FileResponse.path attribute holds the
                # served path.
                self.assertEqual(Path(resp.path).resolve(), mp4.resolve())

    def test_missing_mp4_path_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            _server._serve_script_job_mp4({})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_nonexistent_path_under_root_returns_404(self):
        # Path is under PROJECT_ROOT and has .mp4 extension but
        # the file doesn't actually exist.
        fake = _server.PROJECT_ROOT / "nonexistent-test.mp4"
        with self.assertRaises(HTTPException) as ctx:
            _server._serve_script_job_mp4({"mp4_path": str(fake)})
        self.assertEqual(ctx.exception.status_code, 404)


class TestIsRelativeToHelper(unittest.TestCase):
    def test_child_under_parent(self):
        self.assertTrue(_server._is_relative_to(
            Path("/a/b/c"), Path("/a/b"),
        ))

    def test_child_not_under_parent(self):
        self.assertFalse(_server._is_relative_to(
            Path("/a/b/c"), Path("/x/y"),
        ))


if __name__ == "__main__":
    unittest.main()
