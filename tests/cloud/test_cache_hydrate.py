"""Tests for ``pipeline.cloud.cache.hydrate_cache``.

Worker startup pulls per-call artifacts from
``gs://<bucket>/jobs/<job_id>/cache/`` into ``work_dir/cache/<kind>/``
so the renderer's existing cache-skip checks short-circuit panels and
TTS chunks that were already paid for in a prior (SIGKILLed) attempt.

The B2 docket spec is in
``~/.copilot/session-state/<sid>/plan.md`` (search for
``B2 — Persistent per-call cache``).
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _import_cache():
    from pipeline.cloud import cache  # noqa: PLC0415
    return cache


class _FakeBlob:
    def __init__(self, name: str, payload: bytes):
        self.name = name
        self._payload = payload
        self.download_calls: list[str] = []

    def download_to_filename(self, target: str) -> None:
        self.download_calls.append(target)
        Path(target).write_bytes(self._payload)


class _FakeBucket:
    def __init__(self, blobs: list[_FakeBlob], list_should_raise: bool = False):
        self._blobs = blobs
        self._list_should_raise = list_should_raise
        self.list_calls: list[str] = []

    def list_blobs(self, prefix: str):
        self.list_calls.append(prefix)
        if self._list_should_raise:
            raise RuntimeError("simulated GCS list failure")
        return [b for b in self._blobs if b.name.startswith(prefix)]


class _FakeStorageClient:
    def __init__(self, bucket: _FakeBucket):
        self._bucket = bucket
        self.buckets_built: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.buckets_built.append(name)
        return self._bucket


def _install_fake_storage(bucket: _FakeBucket
                          ) -> tuple[mock._patch, mock._patch]:
    fake_module = types.ModuleType("google.cloud.storage")
    fake_module.Client = lambda: _FakeStorageClient(bucket)  # type: ignore[attr-defined]
    sys_modules_patch = mock.patch.dict(
        sys.modules, {"google.cloud.storage": fake_module}
    )
    import importlib  # noqa: PLC0415
    google_cloud = importlib.import_module("google.cloud")
    attr_patch = mock.patch.object(google_cloud, "storage", fake_module, create=True)
    return sys_modules_patch, attr_patch


class HydrateCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = _import_cache()
        self.cache.reset_state_for_tests()
        self.addCleanup(self.cache.reset_state_for_tests)

    def test_returns_empty_when_job_id_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = self.cache.hydrate_cache(Path(td))
        self.assertEqual(result, {})

    def test_returns_empty_when_bucket_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = self.cache.hydrate_cache(Path(td), job_id="j1")
        self.assertEqual(result, {})

    def test_downloads_into_work_dir_cache_subdir(self) -> None:
        """Files land at work_dir/cache/<kind>/<name>, matching the
        layout consumed by long_form_lib (cache_dir = work_dir/cache)."""
        blobs = [
            _FakeBlob("jobs/j-1/cache/panels/panel_000.png", b"P000"),
            _FakeBlob("jobs/j-1/cache/panels/panel_001.png", b"P001"),
            _FakeBlob("jobs/j-1/cache/tts_chunks/chunk_0000.wav", b"C000"),
        ]
        bucket = _FakeBucket(blobs)
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                counts = self.cache.hydrate_cache(work, job_id="j-1", bucket="bkt")
                # Counts: 2 panels + 1 chunk.
                self.assertEqual(counts, {"panels": 2, "tts_chunks": 1})
                # Files are in the right place — IMPORTANT: this exact
                # layout is what _generate_panel_stills's cache-skip
                # check looks for. If the test passes but the path
                # diverges from long_form_lib's expectation, the cache
                # is useless even though hydrate "worked".
                self.assertTrue((work / "cache" / "panels" / "panel_000.png").is_file())
                self.assertTrue((work / "cache" / "panels" / "panel_001.png").is_file())
                self.assertTrue((work / "cache" / "tts_chunks" / "chunk_0000.wav").is_file())
                # Payload preserved.
                self.assertEqual(
                    (work / "cache" / "panels" / "panel_000.png").read_bytes(),
                    b"P000",
                )
        finally:
            attr_p.stop()
            sys_p.stop()

        # Verify prefix passed to list_blobs is the canonical one.
        self.assertEqual(bucket.list_calls, ["jobs/j-1/cache/"])

    def test_lists_with_env_fallback(self) -> None:
        import os  # noqa: PLC0415
        blobs = [_FakeBlob("jobs/env-job/cache/panels/p.png", b"x")]
        bucket = _FakeBucket(blobs)
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            with mock.patch.dict(
                os.environ,
                {"YTFACTORY_JOB_ID": "env-job", "YTFACTORY_BUCKET": "env-bkt"},
            ):
                with tempfile.TemporaryDirectory() as td:
                    counts = self.cache.hydrate_cache(Path(td))
            self.assertEqual(counts, {"panels": 1})
        finally:
            attr_p.stop()
            sys_p.stop()

    def test_gcs_list_failure_returns_empty_no_raise(self) -> None:
        bucket = _FakeBucket([], list_should_raise=True)
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                # MUST NOT raise — worker startup should proceed with a
                # cold cache rather than crash.
                counts = self.cache.hydrate_cache(Path(td), job_id="j", bucket="b")
            self.assertEqual(counts, {})
        finally:
            attr_p.stop()
            sys_p.stop()

    def test_empty_prefix_returns_empty(self) -> None:
        """First attempt for a fresh job_id — empty bucket prefix."""
        bucket = _FakeBucket([])
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                counts = self.cache.hydrate_cache(Path(td), job_id="fresh", bucket="b")
            self.assertEqual(counts, {})
        finally:
            attr_p.stop()
            sys_p.stop()

    def test_ignores_dir_marker_blobs(self) -> None:
        """GCS sometimes lists synthetic ``foo/`` directory markers —
        skip them rather than blow up trying to mkdir-then-download."""
        blobs = [
            _FakeBlob("jobs/j/cache/panels/", b""),  # dir marker
            _FakeBlob("jobs/j/cache/panels/panel_000.png", b"P0"),
        ]
        bucket = _FakeBucket(blobs)
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                counts = self.cache.hydrate_cache(work, job_id="j", bucket="b")
                self.assertEqual(counts, {"panels": 1})
                self.assertTrue((work / "cache" / "panels" / "panel_000.png").is_file())
        finally:
            attr_p.stop()
            sys_p.stop()

    def test_storage_sdk_import_failure_returns_empty(self) -> None:
        """If the google.cloud.storage import itself fails (e.g. the
        SDK is missing on a stripped image), hydrate must return an
        empty dict, not crash the worker."""
        # Pollute sys.modules with a non-importable proxy.
        sentinel = types.ModuleType("google.cloud.storage")
        # Don't set ``Client`` — accessing it triggers AttributeError
        # inside the cache module's try block.
        sys_p = mock.patch.dict(sys.modules, {"google.cloud.storage": sentinel})
        import importlib  # noqa: PLC0415
        google_cloud = importlib.import_module("google.cloud")
        attr_p = mock.patch.object(google_cloud, "storage", sentinel, create=True)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                counts = self.cache.hydrate_cache(Path(td), job_id="j", bucket="b")
            self.assertEqual(counts, {})
        finally:
            attr_p.stop()
            sys_p.stop()


if __name__ == "__main__":
    unittest.main()
