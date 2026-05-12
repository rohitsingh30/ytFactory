"""Audit Q2.31 — `cloud/yt-dlp` local-fallback glob restricts to
output_path's parent dir.

Pre-fix the local-fallback after %(ext)s substitution called
``Path("/").glob(stem_glob.lstrip("/"))`` which globbed the ENTIRE
filesystem starting at root. If the output path was passed as a
non-absolute string, lstrip("/") left a relative-looking glob that
walked from root — slow, dangerous, and matched unrelated files.

Tests exercise: pipeline/footage/yt_dlp_cloudrun.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub out heavyweight deps so import doesn't fail.
for name in ("google.cloud", "google.cloud.storage", "google.auth",
             "google.auth.transport", "google.auth.transport.requests"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

from pipeline.footage import yt_dlp_cloudrun as _mod


class TestLocalFallbackGlobScope(unittest.TestCase):
    """Q2.31 — local-fallback's %(ext)s glob must search within the
    configured output_path's parent dir, NOT the entire filesystem."""

    def _common_kwargs(self, output_path: Path) -> dict:
        return dict(
            format_string=None, audio_only=False, audio_ext="mp3",
            sections=None, extra_args=None, timeout_s=60,
        )

    def test_absolute_output_path_globs_in_its_parent(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "video.mp4").write_text("fake")
            (tdp / "decoy.txt").write_text("decoy")
            output_path = tdp / "video.%(ext)s"

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                p = _mod._local_fallback_download(
                    "https://x", output_path, **self._common_kwargs(output_path),
                )
            self.assertEqual(p, tdp / "video.mp4")

    def test_no_match_raises_with_search_dir_in_message(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            output_path = tdp / "missing.%(ext)s"
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                with self.assertRaises(_mod.CloudRunYtDlpFailed) as ctx:
                    _mod._local_fallback_download(
                        "https://x", output_path,
                        **self._common_kwargs(output_path),
                    )
            # Audit Q2.31 — message must surface the search dir so the
            # operator can see we're scoped, not full-FS-walking.
            self.assertIn(str(tdp), str(ctx.exception))


    def test_relative_output_path_uses_output_path_parent(self):
        # Pre-fix this would `Path("/").glob(stem.lstrip("/"))` and
        # walk the entire filesystem from root. Post-fix the relative
        # path uses output_path.parent — exercising the
        # ``not stem.is_absolute()`` branch.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "vid.mp4").write_text("fake")
            # output_path is a path; we craft a non-absolute -o string
            # by patching out_str via the underlying ``str()`` of a
            # PurePosixPath that doesn't start with /.
            absolute_output = tdp / "vid.%(ext)s"
            with patch.object(_mod.Path, "is_absolute",
                              autospec=True) as m_abs:
                # Make stem (the substituted glob path) appear non-absolute
                # so we hit the else-branch of the search-root selection.
                m_abs.side_effect = lambda self: False
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0,
                                                      stdout="", stderr="")
                    p = _mod._local_fallback_download(
                        "https://x", absolute_output,
                        **self._common_kwargs(absolute_output),
                    )
            self.assertEqual(p, tdp / "vid.mp4")


class TestPerChunkReadTimeout(unittest.TestCase):
    """Audit Q2.32 — pre-fix ``resp.iter_content(...)`` had NO read
    deadline. A slow-trickle server (or a stalled connection
    mid-body) could hang for the full request envelope. Now bound
    per-chunk wait via YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S.
    """

    def setUp(self):
        # Stash + clear env so each test starts fresh.
        self._saved = os.environ.pop("YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S"] = self._saved

    def _make_resp(self, chunks_with_delays):
        """Stub Response whose iter_content yields chunks separated by
        the configured per-chunk delays (seconds).
        """
        import time as _t

        class _Resp:
            status_code = 200
            text = ""
            headers = {"X-Ytdlp-Filename": "out.mp4"}

            def iter_content(self_inner, chunk_size=64 * 1024):
                for chunk, delay in chunks_with_delays:
                    if delay:
                        _t.sleep(delay)
                    yield chunk

        return _Resp()

    def test_stall_between_chunks_raises_failed(self):
        os.environ["YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S"] = "0.05"
        chunks = [(b"first-bytes", 0.0), (b"second-bytes", 0.2)]
        resp = self._make_resp(chunks)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.mp4"
            with patch("requests.post", return_value=resp), \
                 patch.object(_mod, "_id_token", return_value=None), \
                 patch.object(_mod, "_service_url",
                              return_value="https://example.com"):
                with self.assertRaises(_mod.CloudRunYtDlpFailed) as ctx:
                    _mod.download(
                        url="https://x", output_path=out,
                        format_string=None, audio_only=False,
                        audio_ext="mp3", sections=None,
                        extra_args=None, fallback_to_local=False,
                    )
            self.assertIn("body stalled", str(ctx.exception))

    def test_no_stall_succeeds(self):
        os.environ["YTFACTORY_YTDLP_CHUNK_READ_TIMEOUT_S"] = "10"
        chunks = [(b"hello", 0.0), (b"world", 0.001)]
        resp = self._make_resp(chunks)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.mp4"
            with patch("requests.post", return_value=resp), \
                 patch.object(_mod, "_id_token", return_value=None), \
                 patch.object(_mod, "_service_url",
                              return_value="https://example.com"):
                p = _mod.download(
                    url="https://x", output_path=out,
                    format_string=None, audio_only=False,
                    audio_ext="mp3", sections=None,
                    extra_args=None, fallback_to_local=False,
                )
            self.assertEqual(p.read_bytes(), b"helloworld")


if __name__ == "__main__":
    unittest.main()
