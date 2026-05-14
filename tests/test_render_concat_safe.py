"""Audit Q2.25 — ffmpeg concat demuxer single-quote escape.

Pre-fix every concat-list builder wrote ``f"file '{p.resolve()}'"``
which silently corrupted any path containing a `'` character. The
ffmpeg parser then failed mid-render with a cryptic "Unable to
parse line N" error pointing at the FOLLOWING line.

Tests exercise: pipeline/render/shared/concat_safe.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

from pipeline.render.shared.concat_safe import concat_file_line, escape_concat_path


class TestEscapeConcatPath(unittest.TestCase):
    def test_no_quote_passthrough(self):
        self.assertEqual(escape_concat_path("/tmp/a/b.wav"), "/tmp/a/b.wav")

    def test_single_quote_escaped_with_close_escape_reopen(self):
        # ffmpeg's escape sequence: end the quoted string, emit an
        # escaped single quote, restart the quoted string.
        self.assertEqual(
            escape_concat_path("/tmp/can't/touch this.mp4"),
            "/tmp/can'\\''t/touch this.mp4",
        )

    def test_multiple_quotes_each_escaped(self):
        self.assertEqual(
            escape_concat_path("a'b'c"),
            "a'\\''b'\\''c",
        )

    def test_path_object_accepted(self):
        self.assertEqual(escape_concat_path(Path("/tmp/x.wav")), "/tmp/x.wav")


class TestConcatFileLine(unittest.TestCase):
    def test_no_quote_path(self):
        self.assertEqual(concat_file_line("/tmp/x.mp4"), "file '/tmp/x.mp4'")

    def test_quoted_path_wrapped_correctly(self):
        # The full produced line must round-trip through ffmpeg's parser.
        # i.e. when the parser sees `file '<value>'`, after handling the
        # `'\''` sequence the inner string must equal the original path.
        out = concat_file_line("/tmp/can't/x.mp4")
        self.assertEqual(out, "file '/tmp/can'\\''t/x.mp4'")
        # Round-trip simulation: drop the outer file ' / ', then
        # collapse `'\''` back to `'`.
        inner = out[len("file '"):-1]
        self.assertEqual(inner.replace("'\\''", "'"), "/tmp/can't/x.mp4")


if __name__ == "__main__":
    unittest.main()
