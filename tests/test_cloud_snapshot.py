"""Tests for the FS↔GCS dispatch in :mod:`pipeline.cloud.snapshot`.

The pre-2026-05-11 snapshot module was FS-only. After making it GCS-aware
(so prod web-server reads what laptop cron writes), we have two paths:

* ``YTFACTORY_STATE_BUCKET`` unset → on-disk under ``data/_bench/``
* ``YTFACTORY_STATE_BUCKET`` set   → ``gs://$bucket/data/_bench/...``
  (writes also keep the FS copy so the laptop operator can inspect)

These tests pin both branches by patching the GCS client surface, in the
same shape as :mod:`tests.test_research_youtube`.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def isolated_bench(tmp_path: Path, monkeypatch):
    """Point the snapshot module's *_DIR globals at a per-test tmp dir.

    Mirrors the conftest pattern for ``pipeline.research.youtube``.
    """
    from pipeline.cloud import snapshot as snap

    bench = tmp_path / "_bench"
    monkeypatch.setattr(snap, "BENCH_ROOT", bench, raising=True)
    monkeypatch.setattr(snap, "HEALTH_DIR", bench / "cloud_health", raising=True)
    monkeypatch.setattr(snap, "COST_DIR", bench / "cloud_cost", raising=True)
    monkeypatch.setattr(snap, "DEPLOYS_DIR", bench / "cloud_deploys", raising=True)
    monkeypatch.setattr(snap, "BASELINE_PATH", bench / "cloud_health_baseline.json", raising=True)
    monkeypatch.delenv("YTFACTORY_STATE_BUCKET", raising=False)
    return snap


# ---------------------------------------------------------------------------
# FS-only path (no bucket env)
# ---------------------------------------------------------------------------


def test_latest_snapshot_returns_none_when_dir_missing(isolated_bench):
    snap = isolated_bench
    assert snap.latest_snapshot("cost") is None
    assert snap.latest_snapshot("deploys") is None
    assert snap.latest_snapshot("health") is None


def test_latest_snapshot_unknown_kind_returns_none(isolated_bench):
    assert isolated_bench.latest_snapshot("totally-not-a-kind") is None


def test_write_snapshot_fs_then_read(isolated_bench):
    snap = isolated_bench
    payload = {"hello": "world", "rows": [1, 2, 3]}
    out = snap._write_snapshot("deploys", payload)
    assert out.exists()
    assert out.parent.name == "cloud_deploys"
    # Filename is today's ISO date.
    assert out.stem == date.today().isoformat()
    # latest_snapshot reads it back.
    got = snap.latest_snapshot("deploys")
    assert got == payload


def test_write_snapshot_does_not_touch_gcs_when_bucket_unset(isolated_bench):
    """Sanity: when bucket env is absent, no google.cloud.storage import.

    We intercept the lazy import and assert it was never called.
    """
    snap = isolated_bench
    with patch.object(snap, "_gcs_blob") as gcs_blob:
        snap._write_snapshot("cost", {"x": 1})
        gcs_blob.assert_not_called()


# ---------------------------------------------------------------------------
# GCS path (bucket env set)
# ---------------------------------------------------------------------------


def _install_fake_gcs(monkeypatch, *, list_blobs_return=None, download_payload=None):
    """Stub google.cloud.storage with a MagicMock-driven Client."""
    fake_storage = types.ModuleType("google.cloud.storage")
    fake_blob = MagicMock()
    if download_payload is not None:
        fake_blob.download_as_bytes.return_value = json.dumps(download_payload).encode()
    fake_blob.upload_from_string = MagicMock()

    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob

    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    fake_client.list_blobs.return_value = list_blobs_return or []

    fake_storage.Client = MagicMock(return_value=fake_client)

    fake_google = types.ModuleType("google")
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google_cloud.storage = fake_storage
    fake_google.cloud = fake_google_cloud

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)

    return fake_client, fake_bucket, fake_blob


def test_write_snapshot_uploads_to_gcs_when_bucket_set(isolated_bench, monkeypatch):
    snap = isolated_bench
    monkeypatch.setenv("YTFACTORY_STATE_BUCKET", "fake-state-bucket")
    _client, _bucket, blob = _install_fake_gcs(monkeypatch)

    out = snap._write_snapshot("cost", {"available": True, "total_today": 4.20})

    # FS write still happens (so laptop operator sees a local file).
    assert out.exists()
    assert json.loads(out.read_text())["total_today"] == 4.20

    # GCS upload happened with the right key + content type.
    blob.upload_from_string.assert_called_once()
    call = blob.upload_from_string.call_args
    body = call.args[0]
    assert json.loads(body)["available"] is True
    assert call.kwargs.get("content_type") == "application/json"


def test_write_snapshot_gcs_failure_does_not_raise(isolated_bench, monkeypatch, caplog):
    """GCS-side failure must not mask the local FS write success."""
    snap = isolated_bench
    monkeypatch.setenv("YTFACTORY_STATE_BUCKET", "fake-state-bucket")
    _client, _bucket, blob = _install_fake_gcs(monkeypatch)
    blob.upload_from_string.side_effect = RuntimeError("GCS go boom")

    out = snap._write_snapshot("deploys", {"rows": []})

    assert out.exists()  # FS path persisted
    assert "GCS upload failed" in caplog.text


def test_latest_snapshot_reads_from_gcs_when_bucket_set(isolated_bench, monkeypatch):
    snap = isolated_bench
    monkeypatch.setenv("YTFACTORY_STATE_BUCKET", "fake-state-bucket")

    older = MagicMock()
    older.name = "data/_bench/cloud_deploys/2026-05-09.json"
    newer = MagicMock()
    newer.name = "data/_bench/cloud_deploys/2026-05-11.json"
    newer.download_as_bytes.return_value = json.dumps({"rows": ["from-gcs"]}).encode()

    _install_fake_gcs(monkeypatch, list_blobs_return=[older, newer])
    # Re-stub the ``newer`` blob's download to actually be the one returned —
    # _install_fake_gcs created its own MagicMock; here we want list_blobs[]
    # entries to expose download_as_bytes themselves.

    got = snap.latest_snapshot("deploys")
    assert got == {"rows": ["from-gcs"]}


def test_latest_snapshot_gcs_empty_returns_none(isolated_bench, monkeypatch):
    snap = isolated_bench
    monkeypatch.setenv("YTFACTORY_STATE_BUCKET", "fake-state-bucket")
    _install_fake_gcs(monkeypatch, list_blobs_return=[])

    assert snap.latest_snapshot("cost") is None


def test_latest_snapshot_gcs_list_failure_returns_none(isolated_bench, monkeypatch, caplog):
    snap = isolated_bench
    monkeypatch.setenv("YTFACTORY_STATE_BUCKET", "fake-state-bucket")
    client, _bucket, _blob = _install_fake_gcs(monkeypatch)
    client.list_blobs.side_effect = RuntimeError("network died")

    assert snap.latest_snapshot("deploys") is None
    assert "GCS list failed" in caplog.text
