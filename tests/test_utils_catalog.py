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


# ── GCS-backed catalog reads (cloud cutover 2026-05-09) ────────────────────


class CatalogGcsBranch(unittest.TestCase):
    """When YTFACTORY_STATE_BUCKET is set, list_catalog reads from
    gs://<bucket>/<channel>/uploads/*.json instead of disk."""

    def setUp(self) -> None:
        self._patches = [
            patch("pipeline.utils.catalog.CHANNEL_REGISTRY", _FAKE_REGISTRY),
        ]
        for p in self._patches:
            p.start()
        # Wipe the cache so each test's mocked client is re-consulted.
        catalog._GCS_ENTRIES_CACHE.clear()
        catalog._GCS_CLIENT = None

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        catalog._GCS_ENTRIES_CACHE.clear()
        catalog._GCS_CLIENT = None

    def _make_blob(self, name: str, body: dict) -> object:
        from unittest.mock import MagicMock
        b = MagicMock()
        b.name = name
        b.download_as_text = MagicMock(return_value=json.dumps(body))
        return b

    def test_list_catalog_reads_from_gcs(self) -> None:
        from unittest.mock import MagicMock
        rec_chan1 = {
            "video_id": "vid001", "slug": "story-001", "title": "Hello",
            "url": "https://youtube.com/watch?v=vid001", "uploaded_at": "2026-04-01",
        }
        rec_chan2 = {
            "video_id": "vid002", "slug": "story-002", "title": "World",
            "uploaded_at": "2026-05-01",  # newer → first in sort
        }
        cli = MagicMock()
        # Per-call: each list_blobs call is scoped to one channel prefix.
        def _list_blobs(bucket_name, prefix):
            if prefix == "chan1/":
                return [self._make_blob("chan1/uploads/story-001.json", rec_chan1)]
            if prefix == "chan2/":
                return [self._make_blob("chan2/uploads/story-002.json", rec_chan2)]
            return []
        cli.list_blobs = _list_blobs
        cli.bucket = MagicMock(return_value=MagicMock())

        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(catalog, "_gcs_client", return_value=cli):
            entries = catalog.list_catalog()

        self.assertEqual(len(entries), 2)
        # Newer first.
        self.assertEqual(entries[0].video_id, "vid002")
        self.assertEqual(entries[1].video_id, "vid001")
        # URL fallback works when rec lacks one (chan2 record above).
        self.assertEqual(entries[0].url, "https://youtube.com/watch?v=vid002")

    def test_list_catalog_skips_underscore_and_x_sidecars(self) -> None:
        from unittest.mock import MagicMock
        cli = MagicMock()
        rec_real = {"video_id": "real001", "slug": "real",
                    "uploaded_at": "2026-04-01"}
        rec_x = {"video_id": "shouldnotappear", "slug": "real"}
        rec_under = {"video_id": "alsoshouldnotappear", "slug": "_pending"}

        def _list_blobs(bucket_name, prefix):
            if prefix == "chan1/":
                return [
                    self._make_blob("chan1/uploads/real.json", rec_real),
                    self._make_blob("chan1/uploads/real.x.json", rec_x),
                    self._make_blob("chan1/uploads/_pending.json", rec_under),
                ]
            return []
        cli.list_blobs = _list_blobs
        cli.bucket = MagicMock(return_value=MagicMock())

        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(catalog, "_gcs_client", return_value=cli):
            entries = catalog.list_catalog()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].video_id, "real001")

    def test_list_catalog_picks_up_niched_uploads(self) -> None:
        """mystoriesanimated-style layout: <channel>/<niche>/uploads/<slug>.json."""
        from unittest.mock import MagicMock
        cli = MagicMock()
        rec = {"video_id": "n1", "slug": "n1", "uploaded_at": "2026-05-01"}

        def _list_blobs(bucket_name, prefix):
            if prefix == "chan1/":
                return [self._make_blob(
                    "chan1/reddit_amitheasshole/uploads/n1.json", rec)]
            return []
        cli.list_blobs = _list_blobs
        cli.bucket = MagicMock(return_value=MagicMock())

        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(catalog, "_gcs_client", return_value=cli):
            entries = catalog.list_catalog()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].video_id, "n1")

    def test_catalog_count_uses_gcs(self) -> None:
        from unittest.mock import MagicMock
        cli = MagicMock()

        def _list_blobs(bucket_name, prefix):
            if prefix == "chan1/":
                return [
                    self._make_blob("chan1/uploads/a.json", {"video_id": "a"}),
                    self._make_blob("chan1/uploads/b.json", {"video_id": "b"}),
                    self._make_blob("chan1/uploads/_pending.json", {}),  # skipped
                    self._make_blob("chan1/uploads/x.x.json", {}),  # skipped
                ]
            if prefix == "chan2/":
                return [self._make_blob("chan2/uploads/c.json", {"video_id": "c"})]
            return []
        cli.list_blobs = _list_blobs

        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(catalog, "_gcs_client", return_value=cli):
            n = catalog.catalog_count()

        self.assertEqual(n, 3)


if __name__ == "__main__":
    unittest.main()
