"""ffmpeg concat helpers shared across render entry points.

ffmpeg's `concat` demuxer parses each line as ``file 'PATH'`` where
PATH is single-quoted. The format DOES NOT support backslash escapes;
the only way to embed a literal single quote is to break out of the
quoted string with ``'\\''`` (close quote, escaped quote, reopen).

Audit Q2.25 — pre-fix every concat-list builder in the repo wrote
``f"file '{p.resolve()}'"`` directly, which silently corrupted any
path containing a ``'`` character. ffmpeg's parser then failed
mid-render with a cryptic "Unable to parse line N" error pointing
at the FOLLOWING line (because the unclosed quote ate it).

Use ``escape_concat_path(p)`` for every line you write into a
concat-demuxer file, then call it via ``concat_file_line(p)`` to
get the full ``file '...'`` line.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union


def escape_concat_path(path: Union[Path, str]) -> str:
    """Quote-safe encoding of ``path`` for the ffmpeg concat demuxer.

    Examples
    --------
    >>> escape_concat_path("/tmp/a/b.wav")
    "/tmp/a/b.wav"
    >>> escape_concat_path("/tmp/can't/touch this.mp4")
    "/tmp/can'\\\\''t/touch this.mp4"
    """
    s = str(path)
    # The concat demuxer's escape sequence: end the quoted string,
    # emit an escaped single quote, restart the quoted string.
    return s.replace("'", "'\\''")


def concat_file_line(path: Union[Path, str]) -> str:
    """Return the full ``file 'PATH'`` line ffmpeg's concat demuxer expects."""
    return f"file '{escape_concat_path(path)}'"


__all__ = ["escape_concat_path", "concat_file_line"]
