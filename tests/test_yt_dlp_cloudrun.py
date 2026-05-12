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


if __name__ == "__main__":
    unittest.main()
