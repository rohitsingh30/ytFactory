"""Audit Q2.36 — `_channel_scan` cache key includes _holds.json mtime.

Pre-fix the cache used ONLY ``chan_root.stat().st_mtime`` as the
invalidation key. Editing a file INSIDE the channel (e.g. saving
``_holds.json`` via the dashboard) does NOT bump the parent's mtime
on APFS / ext4, so stale holds data persisted for up to 30 s after
an operator pinned a slug.

Tests exercise: web/server.py::_channel_scan
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

from web import server as _server


class TestChannelScanHoldsMtimeKey(unittest.TestCase):
    """Pin the audit Q2.36 fix: editing _holds.json inside a channel
    busts the cache immediately, NOT after the 30-s TTL."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.chan = Path(self.td.name) / "test-chan"
        self.chan.mkdir()
        # Required for _channel_summary callers; not strictly needed
        # by _channel_scan but matches the in-the-wild shape.
        (self.chan / "config.yaml").write_text("name: test\n")
        # Reset the module-level cache so each test starts fresh.
        with _server._CHAN_SCAN_LOCK:
            _server._CHAN_SCAN_CACHE.clear()

    def tearDown(self):
        self.td.cleanup()

    def test_holds_edit_busts_cache_within_ttl(self):
        # First scan — empty holds.
        out1 = _server._channel_scan(self.chan)
        self.assertEqual(out1["holds"], {})

        # Write _holds.json with a single pin.
        holds_file = self.chan / "_holds.json"
        holds_file.write_text(json.dumps({"slug-1": {"reason": "pinned by test"}}))

        # Audit Q2.36 — second scan must IMMEDIATELY pick up the new
        # holds, not return the cached empty dict (which would have
        # happened with the pre-fix mtime-of-parent-dir-only key).
        out2 = _server._channel_scan(self.chan)
        self.assertIn("slug-1", out2["holds"])
        self.assertEqual(out2["holds"]["slug-1"]["reason"], "pinned by test")

    def test_chan_dir_unchanged_returns_cached(self):
        # First scan caches.
        out1 = _server._channel_scan(self.chan)
        # Add a stray file that DOESN'T affect parent mtime — but no
        # _holds.json change. The cache key (chan_mtime, holds_mtime)
        # is unchanged → cache hit returns the same object.
        out2 = _server._channel_scan(self.chan)
        # Same object instance → was the cached payload.
        self.assertIs(out1, out2)

    def test_missing_holds_file_uses_zero_mtime(self):
        # When _holds.json doesn't exist, holds_mtime defaults to 0.0
        # — once it's CREATED, the cache key changes and the next
        # scan reads the new file.
        out1 = _server._channel_scan(self.chan)
        self.assertEqual(out1["holds"], {})

        (self.chan / "_holds.json").write_text(json.dumps({"x": {"reason": "y"}}))
        out2 = _server._channel_scan(self.chan)
        self.assertIn("x", out2["holds"])

    def test_missing_chan_dir_returns_empty(self):
        gone = Path(self.td.name) / "no-such-chan"
        out = _server._channel_scan(gone)
        self.assertEqual(out, {"uploads": [], "rendered": [], "holds": {}})


if __name__ == "__main__":
    unittest.main()
