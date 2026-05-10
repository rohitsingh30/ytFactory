"""100% line coverage for pipeline/utils/catalog.py."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.utils import catalog

_BASE = Path(__file__).resolve().parent

_FAKE_REGISTRY = [
    {"key": "chan1", "label": "Channel One"},
    {"key": "chan2", "label": "Channel Two"},
]


class CatalogBase(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        # chan1 has an uploads dir; chan2 does not.
        (self._scratch / "chan1" / "uploads").mkdir(parents=True)
        self._patches = [
            patch("pipeline.utils.catalog.PROJECT_ROOT", self._scratch),
            patch("pipeline.utils.catalog.CHANNEL_REGISTRY", _FAKE_REGISTRY),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._scratch, ignore_errors=True)

    def _write(self, channel: str, fname: str, data: dict) -> Path:
        p = self._scratch / channel / "uploads" / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
        return p


class TestChannelUploadsDir(CatalogBase):
    def test_returns_channel_uploads_path(self) -> None:
        p = catalog._channel_uploads_dir("chan1")
        self.assertEqual(p, self._scratch / "chan1" / "uploads")


class TestEntriesForChannel(CatalogBase):
    def test_no_uploads_dir_returns_empty(self) -> None:
        result = catalog._entries_for_channel({"key": "chan2", "label": "Channel Two"})
        self.assertEqual(result, [])

    def test_full_record_produces_entry(self) -> None:
        self._write("chan1", "v1.json", {
            "video_id": "abc123", "slug": "v1",
            "title": "Test Video", "url": "https://y.com/watch?v=abc123",
            "uploaded_at": "2024-01-01T00:00:00",
        })
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(len(result), 1)
        e = result[0]
        self.assertEqual(e.video_id, "abc123")
        self.assertEqual(e.title, "Test Video")
        self.assertEqual(e.url, "https://y.com/watch?v=abc123")
        self.assertEqual(e.uploaded_at, "2024-01-01T00:00:00")

    def test_underscore_files_are_skipped(self) -> None:
        self._write("chan1", "_pending.json", {"video_id": "skip_me"})
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(result, [])

    def test_missing_video_id_skipped(self) -> None:
        self._write("chan1", "novid.json", {"slug": "novid", "title": "No ID"})
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(result, [])

    def test_none_record_skipped(self) -> None:
        # Force _load_upload_record to return None so 'if not rec' branch is hit.
        self._write("chan1", "x.json", {"video_id": "v1"})
        with patch("pipeline.utils.catalog._load_upload_record", return_value=None):
            result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(result, [])

    def test_url_fallback_from_video_id(self) -> None:
        self._write("chan1", "x.json", {"video_id": "vid123"})
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertIn("vid123", result[0].url)

    def test_title_fallback_to_slug(self) -> None:
        self._write("chan1", "x.json", {"video_id": "vid", "slug": "my-slug"})
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(result[0].title, "my-slug")

    def test_title_fallback_to_file_stem(self) -> None:
        self._write("chan1", "mystem.json", {"video_id": "vid"})
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(result[0].title, "mystem")

    def test_missing_uploaded_at_defaults_empty(self) -> None:
        self._write("chan1", "x.json", {"video_id": "v1", "title": "T"})
        result = catalog._entries_for_channel({"key": "chan1", "label": "Channel One"})
        self.assertEqual(result[0].uploaded_at, "")


class TestListCatalog(CatalogBase):
    def test_no_filter_returns_all_channels(self) -> None:
        self._write("chan1", "a.json", {"video_id": "v1", "uploaded_at": "2024-01-01"})
        result = catalog.list_catalog()
        self.assertEqual(len(result), 1)

    def test_channel_filter(self) -> None:
        self._write("chan1", "a.json", {"video_id": "v1"})
        result = catalog.list_catalog(channels=["chan2"])
        self.assertEqual(result, [])

    def test_invalid_channel_silently_skipped(self) -> None:
        result = catalog.list_catalog(channels=["nonexistent"])
        self.assertEqual(result, [])

    def test_sorted_newest_first(self) -> None:
        self._write("chan1", "a.json", {"video_id": "v1", "uploaded_at": "2024-01-01"})
        self._write("chan1", "b.json", {"video_id": "v2", "uploaded_at": "2024-12-01"})
        result = catalog.list_catalog()
        self.assertEqual(result[0].video_id, "v2")

    def test_missing_uploaded_at_sinks_to_bottom(self) -> None:
        self._write("chan1", "a.json", {"video_id": "v1", "uploaded_at": "2024-01-01"})
        self._write("chan1", "b.json", {"video_id": "v2"})
        result = catalog.list_catalog()
        self.assertEqual(result[0].video_id, "v1")


class TestListCatalogDicts(CatalogBase):
    def test_returns_list_of_dicts(self) -> None:
        self._write("chan1", "x.json", {
            "video_id": "abc", "slug": "x", "title": "T",
            "url": "https://y.com", "uploaded_at": "2024-01-01",
        })
        result = catalog.list_catalog_dicts()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], dict)
        self.assertEqual(result[0]["video_id"], "abc")


class TestCatalogCount(CatalogBase):
    def test_counts_only_non_underscore_files(self) -> None:
        self._write("chan1", "a.json", {"video_id": "v1"})
        self._write("chan1", "b.json", {"video_id": "v2"})
        self._write("chan1", "_skip.json", {"video_id": "v3"})
        self.assertEqual(catalog.catalog_count(), 2)

    def test_no_uploads_dir_counted_zero(self) -> None:
        self.assertEqual(catalog.catalog_count(), 0)


class TestCli(CatalogBase):
    def test_empty_catalog_prints_empty(self) -> None:
        buf = io.StringIO()
        with patch("sys.argv", ["catalog"]):
            with patch("sys.stdout", buf):
                rc = catalog._cli()
        self.assertEqual(rc, 0)
        self.assertIn("empty", buf.getvalue())

    def test_json_output(self) -> None:
        self._write("chan1", "x.json", {
            "video_id": "abc", "slug": "x", "title": "Title",
            "url": "https://y.com", "uploaded_at": "2024-01-01",
        })
        buf = io.StringIO()
        with patch("sys.argv", ["catalog", "--json"]):
            with patch("sys.stdout", buf):
                rc = catalog._cli()
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data[0]["video_id"], "abc")

    def test_table_output(self) -> None:
        self._write("chan1", "x.json", {
            "video_id": "vid001", "slug": "x", "title": "My Title",
            "url": "https://y.com", "uploaded_at": "2024-01-01",
        })
        buf = io.StringIO()
        with patch("sys.argv", ["catalog"]):
            with patch("sys.stdout", buf):
                rc = catalog._cli()
        self.assertEqual(rc, 0)
        self.assertIn("vid001", buf.getvalue())
        self.assertIn("videos", buf.getvalue())

    def test_channel_filter_cli(self) -> None:
        self._write("chan1", "x.json", {"video_id": "v1", "uploaded_at": "2024-01-01"})
        buf = io.StringIO()
        with patch("sys.argv", ["catalog", "--channel", "chan2"]):
            with patch("sys.stdout", buf):
                rc = catalog._cli()
        self.assertEqual(rc, 0)
        self.assertIn("empty", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
