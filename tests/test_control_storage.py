"""Smoke tests for the GCS storage adapter — no live GCP calls.

Verifies URI parsing + the gc_heavy_artifacts orchestration calling
delete_prefix for each heavy relpath. The actual GCS client is mocked.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from control import storage


class UriHelpersTest(unittest.TestCase):
    def test_job_uri_no_relpath(self):
        with patch.dict("os.environ", {"YTFACTORY_BUCKET": "test-bucket"}, clear=False):
            self.assertEqual(storage.job_uri("job-1"), "gs://test-bucket/jobs/job-1")

    def test_job_uri_with_relpath(self):
        with patch.dict("os.environ", {"YTFACTORY_BUCKET": "test-bucket"}, clear=False):
            self.assertEqual(
                storage.job_uri("job-1", "beats/00.png"),
                "gs://test-bucket/jobs/job-1/beats/00.png",
            )

    def test_parse_uri(self):
        self.assertEqual(storage.parse_uri("gs://b/k/v"), ("b", "k/v"))
        self.assertEqual(storage.parse_uri("gs://b/"), ("b", ""))
        self.assertEqual(storage.parse_uri("gs://b"), ("b", ""))

    def test_parse_uri_rejects_non_gs(self):
        with self.assertRaises(ValueError):
            storage.parse_uri("https://example.com/x")


class GcHeavyArtifactsTest(unittest.TestCase):
    def test_calls_delete_prefix_for_each_heavy_relpath(self):
        with patch.dict("os.environ", {"YTFACTORY_BUCKET": "test-bucket"}, clear=False):
            with patch.object(storage, "delete_prefix") as mock_delete:
                mock_delete.side_effect = lambda p, dry_run=False: [f"{p}/x"]
                deleted = storage.gc_heavy_artifacts("job-1")

        # One call per heavy relpath.
        self.assertEqual(mock_delete.call_count, len(storage.HEAVY_ARTIFACT_RELPATHS))
        # Each prefix is rooted at the job_uri.
        called_prefixes = [c.args[0] for c in mock_delete.call_args_list]
        for rel in storage.HEAVY_ARTIFACT_RELPATHS:
            self.assertIn(f"gs://test-bucket/jobs/job-1/{rel}", called_prefixes)
        # short.mp4, thumb.png, proposal.json must NOT have been touched.
        for light in storage.LIGHT_ARTIFACT_RELPATHS:
            self.assertNotIn(f"gs://test-bucket/jobs/job-1/{light}", called_prefixes)

        # Returned list aggregates all deletions.
        self.assertEqual(len(deleted), len(storage.HEAVY_ARTIFACT_RELPATHS))

    def test_dry_run_propagates(self):
        with patch.object(storage, "delete_prefix") as mock_delete:
            mock_delete.return_value = []
            storage.gc_heavy_artifacts("job-1", dry_run=True)
        for call in mock_delete.call_args_list:
            self.assertTrue(call.kwargs.get("dry_run") is True)


class DeletePrefixTest(unittest.TestCase):
    def test_iterates_blobs_and_deletes(self):
        fake_blob_a = MagicMock(name="a")
        fake_blob_a.name = "jobs/j1/beats/00.png"
        fake_blob_b = MagicMock(name="b")
        fake_blob_b.name = "jobs/j1/beats/01.png"

        fake_client = MagicMock()
        fake_client.list_blobs.return_value = [fake_blob_a, fake_blob_b]

        fake_bucket = MagicMock()
        fake_client.bucket.return_value = fake_bucket

        with patch.object(storage, "_client", return_value=fake_client):
            deleted = storage.delete_prefix("gs://b/jobs/j1/beats/")

        self.assertEqual(deleted, [
            "gs://b/jobs/j1/beats/00.png",
            "gs://b/jobs/j1/beats/01.png",
        ])
        self.assertEqual(fake_bucket.blob.call_count, 2)

    def test_dry_run_skips_actual_delete(self):
        fake_blob = MagicMock()
        fake_blob.name = "jobs/j1/beats/00.png"
        fake_client = MagicMock()
        fake_client.list_blobs.return_value = [fake_blob]
        fake_bucket = MagicMock()
        fake_client.bucket.return_value = fake_bucket

        with patch.object(storage, "_client", return_value=fake_client):
            deleted = storage.delete_prefix("gs://b/jobs/j1/beats/", dry_run=True)

        self.assertEqual(len(deleted), 1)
        fake_bucket.blob.assert_not_called()


class UploadRecordHelpersTest(unittest.TestCase):
    """The dashboard mirrors <channel>/uploads/[<niche>/]<slug>.json to
    gs://<bucket>/upload-records/<channel>/[<niche>/]<slug>.json — verify
    the rel_key / uri builders survive both flat and niched layouts.
    """

    def test_rel_key_flat_layout(self):
        root = Path("/tmp/repo")
        local = root / "historyrecapped" / "uploads" / "battle.json"
        self.assertEqual(
            storage.upload_record_rel_key(local, root),
            "historyrecapped/battle.json",
        )

    def test_rel_key_niched_layout(self):
        root = Path("/tmp/repo")
        local = root / "mystoriesanimated" / "uploads" / "reddit_amitheasshole" / "foo.json"
        self.assertEqual(
            storage.upload_record_rel_key(local, root),
            "mystoriesanimated/reddit_amitheasshole/foo.json",
        )

    def test_upload_record_uri_uses_bucket(self):
        with patch.dict("os.environ", {"YTFACTORY_BUCKET": "test-bucket"}, clear=False):
            self.assertEqual(
                storage.upload_record_uri("historyrecapped/foo.json"),
                "gs://test-bucket/upload-records/historyrecapped/foo.json",
            )

    def test_list_upload_records_parses_keys(self):
        """Yields (channel, slug, record) — channel = first path segment."""
        flat_blob = MagicMock()
        flat_blob.name = "upload-records/historyrecapped/foo.json"
        niched_blob = MagicMock()
        niched_blob.name = "upload-records/mystoriesanimated/reddit_amitheasshole/bar.json"
        non_json_blob = MagicMock()  # should be skipped
        non_json_blob.name = "upload-records/historyrecapped/notes.txt"

        fake_client = MagicMock()
        fake_client.list_blobs.return_value = [flat_blob, niched_blob, non_json_blob]

        rec_flat = {"video_id": "abc", "title": "battle"}
        rec_niched = {"video_id": "xyz", "title": "aita"}

        def _download(uri):
            if uri.endswith("foo.json"):
                return json.dumps(rec_flat).encode()
            if uri.endswith("bar.json"):
                return json.dumps(rec_niched).encode()
            raise AssertionError(f"unexpected uri: {uri}")

        with patch.dict("os.environ", {"YTFACTORY_BUCKET": "test-bucket"}, clear=False):
            with patch.object(storage, "_client", return_value=fake_client):
                with patch.object(storage, "download_bytes", side_effect=_download):
                    out = list(storage.list_upload_records())

        self.assertEqual(len(out), 2)
        channels = {row[0] for row in out}
        self.assertEqual(channels, {"historyrecapped", "mystoriesanimated"})
        slugs = {row[1] for row in out}
        self.assertEqual(slugs, {"foo", "bar"})


if __name__ == "__main__":
    unittest.main()
