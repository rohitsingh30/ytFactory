"""Tests for ``POST /api/jobs/{job_id}/retry`` (B4 in 2026-05-23 docket).

The route rebuilds a failed/cancelled job with a fresh job_id, copies
the persisted cache (B2) from the old job_id's GCS prefix to the new
one, and triggers a Cloud Run JOB execution against the new id. The
test exercises:

* 404 when the job doesn't exist
* 409 when the job is still rendering (don't race the worker)
* 422 when proposal is missing
* happy path: new doc created with ``retry_of`` → cache copy invoked →
  Cloud Run triggered → back-reference ``retried_as`` written
* GCS copy failure must NOT block the retry — empty cache is acceptable
* dispatch failure surfaces 502 and marks the new doc failed
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient


# In-memory Firestore-substitute for the test ----------------------------------


class _FakeJobs:
    """Stand-in for ``control.core.jobs.get_jobs()`` — dict-backed."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    def get(self, job_id: str) -> dict | None:
        d = self.store.get(job_id)
        return dict(d) if d else None

    def create(self, job_id: str, **fields) -> None:
        self.store[job_id] = dict(fields)

    def update(self, job_id: str, **fields) -> None:
        if job_id not in self.store:
            self.store[job_id] = {}
        self.store[job_id].update(fields)


# Fake GCS storage --------------------------------------------------------------


class _FakeBlob:
    def __init__(self, name: str, payload: bytes):
        self.name = name
        self.payload = payload


class _FakeBucket:
    def __init__(self, blobs: list[_FakeBlob], *, list_raises: bool = False,
                 copy_raises: bool = False):
        self._blobs = blobs
        self._list_raises = list_raises
        self._copy_raises = copy_raises
        self.copy_calls: list[tuple[str, str]] = []

    def list_blobs(self, prefix: str):
        if self._list_raises:
            raise RuntimeError("simulated gcs list failure")
        return [b for b in self._blobs if b.name.startswith(prefix)]

    def copy_blob(self, src_blob, dest_bucket, *, new_name: str):
        if self._copy_raises:
            raise RuntimeError("simulated copy failure")
        self.copy_calls.append((src_blob.name, new_name))
        self._blobs.append(_FakeBlob(new_name, src_blob.payload))


class _FakeStorageClient:
    def __init__(self, bucket: _FakeBucket):
        self._bucket = bucket

    def bucket(self, _name: str) -> _FakeBucket:
        return self._bucket


def _install_fake_storage(bucket: _FakeBucket):
    mod = types.ModuleType("google.cloud.storage")
    mod.Client = lambda: _FakeStorageClient(bucket)  # type: ignore[attr-defined]
    # ``import google.cloud`` is not enough in full-suite mode — some
    # prior test may have left ``sys.modules['google']`` set to a thing
    # that doesn't expose ``.cloud`` as an attribute. Use importlib so
    # we force a real resolution of the ``google.cloud`` package.
    import importlib  # noqa: PLC0415
    google_cloud = importlib.import_module("google.cloud")
    sys_p = mock.patch.dict(sys.modules, {"google.cloud.storage": mod})
    attr_p = mock.patch.object(google_cloud, "storage", mod, create=True)
    return sys_p, attr_p


# Fake cloud_run ----------------------------------------------------------------


class _FakeExecutionRef:
    def __init__(self, name: str = "execution-xyz"):
        self.execution_name = name


# Test harness ------------------------------------------------------------------


class RetryJobRouteTest(unittest.TestCase):
    def _build_app(self, fake_jobs: _FakeJobs, *,
                   backend: str = "cloudrun",
                   trigger_raises: bool = False,
                   ) -> TestClient:
        from control.routes import render_routes  # noqa: PLC0415

        # Patch the in-process jobs store.
        self._jobs_patch = mock.patch.object(
            render_routes.jobs_mod, "get_jobs", return_value=fake_jobs,
        )
        self._get_job_patch = mock.patch.object(
            render_routes.jobs_mod, "get_job",
            side_effect=lambda jid: fake_jobs.get(jid),
        )
        # Skip the per-route owner enforcement — we test that
        # elsewhere; here we want the focused retry-flow asserts.
        self._owner_patch = mock.patch.object(
            render_routes, "_job_owner_check", return_value=None,
        )
        # Bypass require_pin (uses env-based PIN check that's not set in tests).
        from control.routes import auth_pin  # noqa: PLC0415
        self._pin_patch = mock.patch.object(
            auth_pin, "require_pin", return_value=None,
        )
        # Patch the cloud_run module (deferred-imported inside the route).
        fake_cloud_run = types.SimpleNamespace(
            render_backend=lambda: backend,
            trigger_render_job=(
                mock.Mock(side_effect=RuntimeError("dispatch failed"))
                if trigger_raises
                else mock.Mock(return_value=_FakeExecutionRef())
            ),
        )
        # Two patches needed because the route does
        # ``from control.core import cloud_run``. Once ``control.core``
        # has been touched in any earlier test, ``cloud_run`` is set as
        # an attribute on it — at which point Python's import machinery
        # resolves the ``from ... import`` form via ``getattr`` and
        # never consults ``sys.modules``. We patch both so the test
        # works whether or not the attribute is preloaded.
        import control.core  # noqa: PLC0415
        self._cloud_run_patch = mock.patch.dict(
            sys.modules, {"control.core.cloud_run": fake_cloud_run},
        )
        self._cloud_run_attr_patch = mock.patch.object(
            control.core, "cloud_run", fake_cloud_run, create=True,
        )

        self._jobs_patch.start()
        self._get_job_patch.start()
        self._owner_patch.start()
        self._pin_patch.start()
        self._cloud_run_patch.start()
        self._cloud_run_attr_patch.start()

        # Use FastAPI's dependency overrides for require_pin — patching
        # the imported symbol doesn't affect a Depends() reference that
        # was captured at route-registration time.
        app = FastAPI()
        app.include_router(render_routes.router)
        from control.routes.auth_pin import require_pin  # noqa: PLC0415
        app.dependency_overrides[require_pin] = lambda: None

        self.addCleanup(self._cloud_run_attr_patch.stop)
        self.addCleanup(self._cloud_run_patch.stop)
        self.addCleanup(self._pin_patch.stop)
        self.addCleanup(self._owner_patch.stop)
        self.addCleanup(self._get_job_patch.stop)
        self.addCleanup(self._jobs_patch.stop)
        self._fake_cloud_run = fake_cloud_run
        return TestClient(app)

    # ──────────────────────────────────────────────────────────────────
    # 404 / 409 / 422 path
    # ──────────────────────────────────────────────────────────────────

    def test_retry_404_when_job_missing(self) -> None:
        client = self._build_app(_FakeJobs())
        resp = client.post("/api/jobs/missing-id/retry")
        self.assertEqual(resp.status_code, 404)

    def test_retry_409_when_status_still_rendering(self) -> None:
        jobs = _FakeJobs()
        jobs.create("j-running", status="rendering", proposal={
            "channel": "historyrecapped", "topic": "x",
        })
        client = self._build_app(jobs)
        resp = client.post("/api/jobs/j-running/retry")
        self.assertEqual(resp.status_code, 409)
        # Ensure no new doc was created.
        self.assertEqual(set(jobs.store.keys()), {"j-running"})

    def test_retry_409_when_status_done(self) -> None:
        jobs = _FakeJobs()
        jobs.create("j-done", status="done", proposal={
            "channel": "historyrecapped", "topic": "x",
        })
        client = self._build_app(jobs)
        resp = client.post("/api/jobs/j-done/retry")
        self.assertEqual(resp.status_code, 409)

    def test_retry_422_when_proposal_missing(self) -> None:
        jobs = _FakeJobs()
        jobs.create("j-no-proposal", status="failed", proposal=None)
        client = self._build_app(jobs)
        resp = client.post("/api/jobs/j-no-proposal/retry")
        self.assertEqual(resp.status_code, 422)

    # ──────────────────────────────────────────────────────────────────
    # Happy path
    # ──────────────────────────────────────────────────────────────────

    def test_retry_happy_path_copies_cache_and_dispatches(self) -> None:
        jobs = _FakeJobs()
        jobs.create("j-old", status="failed", channel="historyrecapped",
                    topic="The Climb", slug="the-climb-j-old",
                    render_kind="long_form",
                    proposal={"channel": "historyrecapped", "topic": "The Climb",
                              "length_s": 600})

        # Seed three cache blobs under the old job's prefix.
        blobs = [
            _FakeBlob("jobs/j-old/cache/panels/panel_000.png", b"P0"),
            _FakeBlob("jobs/j-old/cache/panels/panel_001.png", b"P1"),
            _FakeBlob("jobs/j-old/cache/tts_chunks/chunk_0000.wav", b"C0"),
        ]
        bucket = _FakeBucket(blobs)
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            client = self._build_app(jobs)
            resp = client.post("/api/jobs/j-old/retry")
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()

        # 3 cache objects copied to the new prefix.
        self.assertEqual(body["cache_objects_copied"], 3)
        self.assertEqual(body["retry_of"], "j-old")
        new_id = body["retry_job_id"]
        self.assertTrue(new_id and new_id != "j-old")

        # Cache copy targets are derived from the new id, not the old.
        copy_names = {dst for (_src, dst) in bucket.copy_calls}
        self.assertIn(f"jobs/{new_id}/cache/panels/panel_000.png", copy_names)
        self.assertIn(f"jobs/{new_id}/cache/panels/panel_001.png", copy_names)
        self.assertIn(f"jobs/{new_id}/cache/tts_chunks/chunk_0000.wav", copy_names)

        # The new doc carries retry_of, proposal preserved verbatim.
        new_doc = jobs.get(new_id)
        self.assertIsNotNone(new_doc)
        self.assertEqual(new_doc["retry_of"], "j-old")
        self.assertEqual(new_doc["proposal"]["channel"], "historyrecapped")
        self.assertEqual(new_doc["proposal"]["topic"], "The Climb")
        self.assertEqual(new_doc["channel"], "historyrecapped")

        # Old doc gets the backref.
        self.assertEqual(jobs.get("j-old")["retried_as"], new_id)

        # Cloud Run was triggered against the new id, not the old.
        self._fake_cloud_run.trigger_render_job.assert_called_once_with(new_id)

    def test_retry_with_empty_source_cache_still_succeeds(self) -> None:
        """First-attempt failure (worker died before any panel) → empty
        cache prefix → cache_objects_copied=0 → retry still dispatches."""
        jobs = _FakeJobs()
        jobs.create("j-empty", status="failed", proposal={
            "channel": "historyrecapped", "topic": "x",
        })

        bucket = _FakeBucket([])
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            client = self._build_app(jobs)
            resp = client.post("/api/jobs/j-empty/retry")
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cache_objects_copied"], 0)
        # Dispatch still happened.
        self._fake_cloud_run.trigger_render_job.assert_called_once()

    def test_retry_with_gcs_copy_failure_still_succeeds_with_count_zero(self) -> None:
        """GCS unavailable during copy → 0 objects copied → retry still
        dispatches. The render will just pay full cost; we don't want
        to fail the retry because of a transient bucket hiccup."""
        jobs = _FakeJobs()
        jobs.create("j-gcs-fail", status="failed", proposal={
            "channel": "historyrecapped", "topic": "x",
        })

        bucket = _FakeBucket([], list_raises=True)
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            client = self._build_app(jobs)
            resp = client.post("/api/jobs/j-gcs-fail/retry")
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cache_objects_copied"], 0)
        self._fake_cloud_run.trigger_render_job.assert_called_once()

    def test_retry_dispatch_failure_surfaces_502_and_marks_failed(self) -> None:
        jobs = _FakeJobs()
        jobs.create("j-disp", status="failed", proposal={
            "channel": "historyrecapped", "topic": "x",
        })

        bucket = _FakeBucket([])
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            client = self._build_app(jobs, trigger_raises=True)
            resp = client.post("/api/jobs/j-disp/retry")
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(resp.status_code, 502)
        # New doc was created and then marked failed.
        new_docs = [k for k in jobs.store if k not in ("j-disp",)]
        self.assertEqual(len(new_docs), 1)
        new_doc = jobs.store[new_docs[0]]
        # The retry-of stays — operator can see what was attempted.
        self.assertEqual(new_doc.get("retry_of"), "j-disp")

    def test_retry_of_cancelled_is_allowed(self) -> None:
        jobs = _FakeJobs()
        jobs.create("j-cancel", status="cancelled", proposal={
            "channel": "historyrecapped", "topic": "x",
        })

        bucket = _FakeBucket([])
        sys_p, attr_p = _install_fake_storage(bucket)
        sys_p.start()
        attr_p.start()
        try:
            client = self._build_app(jobs)
            resp = client.post("/api/jobs/j-cancel/retry")
        finally:
            attr_p.stop()
            sys_p.stop()

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
