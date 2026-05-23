"""Tests for ``pipeline.cloud.cache.persist_artifact``.

Covers the env-driven no-op contract (laptop renders), the
fire-and-forget upload path (cloud renders), and the
upload-failure-must-not-raise contract.

The B2 docket spec is in
``~/.copilot/session-state/<sid>/plan.md`` (search for
``B2 — Persistent per-call cache``).
"""
from __future__ import annotations

import logging
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


def _import_cache():
    """Fresh import of ``pipeline.cloud.cache``.

    Tests need to flip ``YTFACTORY_*`` env vars between cases, and the
    module caches the first "persist disabled" reason at module scope.
    ``reset_state_for_tests`` clears that — call it in setUp.
    """
    from pipeline.cloud import cache  # noqa: PLC0415
    return cache


class _FakeBlob:
    def __init__(self, sink_calls: list[tuple[str, str]], blob_name: str,
                 fail: bool = False):
        self._sink = sink_calls
        self._blob_name = blob_name
        self._fail = fail
        self.upload_from_filename_calls: list[str] = []

    def upload_from_filename(self, local: str) -> None:
        self.upload_from_filename_calls.append(local)
        if self._fail:
            raise RuntimeError("simulated upload failure")
        self._sink.append((self._blob_name, local))


class _FakeBucket:
    def __init__(self, sink: list[tuple[str, str]], fail: bool = False):
        self._sink = sink
        self._fail = fail
        self.blobs_built: list[str] = []

    def blob(self, name: str) -> _FakeBlob:
        self.blobs_built.append(name)
        return _FakeBlob(self._sink, name, fail=self._fail)


class _FakeStorageClient:
    def __init__(self, sink: list[tuple[str, str]], fail: bool = False):
        self._sink = sink
        self._fail = fail
        self.buckets_built: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.buckets_built.append(name)
        return _FakeBucket(self._sink, fail=self._fail)


def _install_fake_storage(sink: list[tuple[str, str]], *, fail: bool = False
                          ) -> tuple[mock._patch, mock._patch]:
    """Install a fake ``google.cloud.storage`` module.

    Returns two patchers — both must be started AND stopped. We must
    patch *both* ``sys.modules['google.cloud.storage']`` AND
    ``getattr(google.cloud, 'storage')`` because the cache module does
    ``from google.cloud import storage`` which resolves via the parent
    package's attribute, not just sys.modules (see B3b's
    ``ReconcilerStubResistanceTest`` for the lesson).
    """
    fake_module = types.ModuleType("google.cloud.storage")
    fake_module.Client = lambda: _FakeStorageClient(sink, fail=fail)  # type: ignore[attr-defined]
    sys_modules_patch = mock.patch.dict(
        sys.modules, {"google.cloud.storage": fake_module}
    )
    # Make sure parent ``google.cloud`` exists so getattr resolves.
    # ``import google.cloud`` is not enough in full-suite mode — some
    # prior test may have left ``sys.modules['google']`` set to a thing
    # that doesn't expose ``.cloud`` as an attribute. ``importlib`` forces
    # a real module resolution.
    import importlib  # noqa: PLC0415
    google_cloud = importlib.import_module("google.cloud")
    attr_patch = mock.patch.object(google_cloud, "storage", fake_module, create=True)
    return sys_modules_patch, attr_patch


class PersistArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = _import_cache()
        self.cache.reset_state_for_tests()
        # Clear env so no-op tests start from a clean slate.
        self._env_patch = mock.patch.dict(
            "os.environ",
            {
                "YTFACTORY_JOB_ID": "",
                "YTFACTORY_BUCKET": "",
                "YTFACTORY_CACHE_PERSIST": "1",
            },
            clear=False,
        )
        self._env_patch.start()
        for key in ("YTFACTORY_JOB_ID", "YTFACTORY_BUCKET"):
            import os  # noqa: PLC0415
            os.environ.pop(key, None)
        self.addCleanup(self._env_patch.stop)
        self.addCleanup(self.cache.reset_state_for_tests)

    def _make_local(self, tmpdir: Path, name: str = "panel_000.png") -> Path:
        p = tmpdir / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 256)
        return p

    def test_noop_when_job_id_unset(self) -> None:
        """No env → no-op, no SDK touched, no error."""
        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as td:
            p = self._make_local(Path(td))
            # If the SDK were invoked, this would raise AttributeError —
            # we did NOT install a fake.
            self.cache.persist_artifact(p, kind="panels")
        # No-op: disabled-reason should be cached now.
        self.assertIn("YTFACTORY_JOB_ID", self.cache._PERSIST_DISABLED_REASON or "")

    def test_noop_when_bucket_unset(self) -> None:
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "j1"
        with tempfile.TemporaryDirectory() as td:
            p = self._make_local(Path(td))
            self.cache.persist_artifact(p, kind="panels")
        self.assertIn("YTFACTORY_BUCKET", self.cache._PERSIST_DISABLED_REASON or "")

    def test_noop_when_persist_disabled_env(self) -> None:
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "j1"
        os.environ["YTFACTORY_BUCKET"] = "b1"
        os.environ["YTFACTORY_CACHE_PERSIST"] = "0"
        with tempfile.TemporaryDirectory() as td:
            p = self._make_local(Path(td))
            self.cache.persist_artifact(p, kind="panels")
        self.assertIn("YTFACTORY_CACHE_PERSIST", self.cache._PERSIST_DISABLED_REASON or "")

    def test_uploads_to_correct_path_when_enabled(self) -> None:
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "job-abc"
        os.environ["YTFACTORY_BUCKET"] = "bkt-x"

        sink: list[tuple[str, str]] = []
        sys_p, attr_p = _install_fake_storage(sink)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = self._make_local(Path(td), "panel_007.png")
                self.cache.persist_artifact(p, kind="panels")
                # Fire-and-forget: shut down the pool to flush.
                self.cache.shutdown_upload_pool(wait=True, timeout=5.0)
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(len(sink), 1)
        blob_name, local_path = sink[0]
        self.assertEqual(blob_name, "jobs/job-abc/cache/panels/panel_007.png")
        self.assertTrue(local_path.endswith("panel_007.png"))

    def test_explicit_name_kwarg_overrides_filename(self) -> None:
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "j2"
        os.environ["YTFACTORY_BUCKET"] = "b2"

        sink: list[tuple[str, str]] = []
        sys_p, attr_p = _install_fake_storage(sink)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = self._make_local(Path(td), "tmp_name.png")
                self.cache.persist_artifact(p, kind="panels", name="canonical.png")
                self.cache.shutdown_upload_pool(wait=True, timeout=5.0)
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(sink[0][0], "jobs/j2/cache/panels/canonical.png")

    def test_upload_failure_is_swallowed(self) -> None:
        """Upload exception must not propagate — cache is best-effort."""
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "j-fail"
        os.environ["YTFACTORY_BUCKET"] = "bkt"

        sys_p, attr_p = _install_fake_storage([], fail=True)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = self._make_local(Path(td))
                # The render must not crash even if upload fails.
                self.cache.persist_artifact(p, kind="panels")
                # Drain — exception is in the worker thread, surfaces here.
                self.cache.shutdown_upload_pool(wait=True, timeout=5.0)
        finally:
            attr_p.stop()
            sys_p.stop()

    def test_missing_local_path_is_warning_not_raise(self) -> None:
        import os  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "j"
        os.environ["YTFACTORY_BUCKET"] = "b"
        # Don't even install fake storage — we should never get to upload.
        self.cache.persist_artifact(Path("/nonexistent/path/file.png"), kind="panels")
        # No raise == pass.

    def test_disabled_reason_logged_once(self) -> None:
        """The disable reason is logged at INFO exactly once per process."""
        import io  # noqa: PLC0415
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.INFO)
        root_logger = logging.getLogger("pipeline.cloud.cache")
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        try:
            import tempfile  # noqa: PLC0415
            with tempfile.TemporaryDirectory() as td:
                p = self._make_local(Path(td))
                self.cache.persist_artifact(p, kind="panels")
                self.cache.persist_artifact(p, kind="panels")
                self.cache.persist_artifact(p, kind="panels")
        finally:
            root_logger.removeHandler(handler)

        log_text = log_stream.getvalue()
        self.assertEqual(
            log_text.count("cache.persist disabled"),
            1,
            f"expected exactly 1 disable log, got: {log_text!r}",
        )


class ShutdownPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = _import_cache()
        self.cache.reset_state_for_tests()
        # Snapshot the env vars we'll mutate so tearDown can put them
        # back. Tests that flip ``YTFACTORY_JOB_ID`` / ``YTFACTORY_BUCKET``
        # directly via ``os.environ[...] = ...`` leak the values to
        # whatever test runs next in the same process — which is what
        # caused background upload threads from the panel-parallelism
        # tests to start firing "no such file" warnings.
        import os  # noqa: PLC0415
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("YTFACTORY_JOB_ID", "YTFACTORY_BUCKET", "YTFACTORY_CACHE_PERSIST")
        }
        self.addCleanup(self._restore_env)
        self.addCleanup(self.cache.reset_state_for_tests)

    def _restore_env(self) -> None:
        import os  # noqa: PLC0415
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_shutdown_when_pool_uninitialised_is_noop(self) -> None:
        # No prior call — pool is None.
        self.cache.shutdown_upload_pool(wait=True, timeout=5.0)
        # No raise == pass.

    def test_shutdown_after_use_waits_for_pending(self) -> None:
        """Pending uploads should complete (or time out) before return."""
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        os.environ["YTFACTORY_JOB_ID"] = "j-sd"
        os.environ["YTFACTORY_BUCKET"] = "b-sd"

        completed = threading.Event()
        sink: list[tuple[str, str]] = []
        sys_p, attr_p = _install_fake_storage(sink)
        sys_p.start()
        attr_p.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "x.png"
                p.write_bytes(b"data")
                self.cache.persist_artifact(p, kind="panels")
                # Wait via shutdown.
                self.cache.shutdown_upload_pool(wait=True, timeout=5.0)
            self.assertEqual(len(sink), 1, "upload should have completed")
        finally:
            attr_p.stop()
            sys_p.stop()
        del completed  # keep ref to silence unused-var lint


if __name__ == "__main__":
    unittest.main()
