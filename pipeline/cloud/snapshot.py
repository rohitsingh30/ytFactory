"""Daily snapshot orchestrator — feeds the Cloud admin tab.

Calls :mod:`pipeline.cloud.health`, :mod:`.cost`, :mod:`.deploys` once
per day (via :mod:`scripts.cloud_daily_snapshot` + a launchd plist) and
persists the results to ``data/_bench/cloud_{health,cost,deploys}/YYYY-MM-DD.json``.

The FastAPI routes in :mod:`control.routes.cloud_routes` read these
snapshots so the panel renders fast (no live BigQuery query per page
load). Health is also re-probed live on demand because cold-load state
changes minute-to-minute.

Atomicity: writes go to ``<file>.tmp`` then rename, so a panel reading
mid-write never sees a partial file.

Storage backend (FS vs GCS)
---------------------------
Mirrors the convention in :mod:`pipeline.research.youtube`: when
``YTFACTORY_STATE_BUCKET`` is set, snapshots live in GCS at
``gs://$YTFACTORY_STATE_BUCKET/data/_bench/cloud_{health,cost,deploys}/YYYY-MM-DD.json``.
When unset (laptop dev), the on-disk path under ``data/_bench/`` is the
sole backend.

The laptop snapshot cron (see ``control/com.ytfactory.cloud-snapshot.plist``)
sets the bucket env so daily runs ship to GCS too — and prod's web service
(``YTFACTORY_STATE_BUCKET=ytfactory-prod-v2-state`` already wired) reads
from GCS without any code or env change. This is the entire prod path
for the Cloud panel: laptop cron writes → GCS → prod reads.

When the bucket is set, writes hit BOTH GCS and the local FS — so the
laptop operator can still inspect ``data/_bench/`` after a cron tick.

Baseline: this module also updates ``data/_bench/cloud_health_baseline.json``
with rolling p95 latency per service, used by :func:`pipeline.cloud.health._classify`.
The baseline is FS-only (cheap derived data, recomputed every snapshot).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import cost, deploys, health
from .services import list_services
from .. import observability as _obs

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = REPO_ROOT / "data" / "_bench"
HEALTH_DIR = BENCH_ROOT / "cloud_health"
COST_DIR = BENCH_ROOT / "cloud_cost"
DEPLOYS_DIR = BENCH_ROOT / "cloud_deploys"
BASELINE_PATH = BENCH_ROOT / "cloud_health_baseline.json"

# GCS key prefixes mirror the on-disk layout so the same code can
# handle both backends with a one-line switch.
_GCS_KEY_PREFIX = "data/_bench"
_GCS_KIND_TO_DIRNAME = {
    "health": "cloud_health",
    "cost": "cloud_cost",
    "deploys": "cloud_deploys",
}


def _state_bucket() -> str | None:
    """GCS bucket name when running in cloud, else None.

    Resolved on every call so tests can flip the env mid-session — same
    contract as :func:`pipeline.research.youtube._state_bucket`.
    """
    bucket = os.environ.get("YTFACTORY_STATE_BUCKET")
    return bucket or None


def _gcs_blob(bucket: str, key: str):
    """Lazy-imports ``google-cloud-storage`` so laptop dev doesn't need it."""
    from google.cloud import storage  # noqa: PLC0415

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
    client = storage.Client(project=project)
    return client.bucket(bucket).blob(key)


def _gcs_key(kind: str, day: str) -> str:
    return f"{_GCS_KEY_PREFIX}/{_GCS_KIND_TO_DIRNAME[kind]}/{day}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically to disk (temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.rename(path)


def _write_snapshot(kind: str, payload: dict[str, Any]) -> Path:
    """Persist ``payload`` for ``kind`` (health/cost/deploys).

    Always writes to the on-disk path (returned to the caller for log
    messages and for the cron stdout summary). When
    ``YTFACTORY_STATE_BUCKET`` is set, *additionally* uploads to
    ``gs://$bucket/data/_bench/...`` so prod web (which reads from GCS)
    sees the new snapshot immediately.
    """
    day = _today_iso()
    body = json.dumps(payload, indent=2, sort_keys=True)
    dir_map = {"health": HEALTH_DIR, "cost": COST_DIR, "deploys": DEPLOYS_DIR}
    out = dir_map[kind] / f"{day}.json"
    _atomic_write(out, payload)

    bucket = _state_bucket()
    if bucket:
        key = _gcs_key(kind, day)
        try:
            blob = _gcs_blob(bucket, key)
            blob.upload_from_string(body, content_type="application/json")
            logger.info(
                "cloud snapshot %s: wrote gs://%s/%s (%d bytes)",
                kind, bucket, key, len(body),
            )
        except Exception:
            # GCS write failure is non-fatal — the FS write succeeded.
            # Logged at warning so the cron log surfaces it without
            # masking the local write success.
            logger.exception(
                "cloud snapshot %s: GCS upload failed (FS write at %s ok)",
                kind, out,
            )
    return out


def _today_iso() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_health() -> Path:
    """Probe every service and persist today's snapshot."""
    rows = health.sweep()
    payload = {
        "snapshot_at": _now_iso(),
        "summary": health.summary(rows),
        "rows": [asdict(r) for r in rows],
    }
    out = _write_snapshot("health", payload)
    _update_baseline(rows)
    return out


def snapshot_cost(days: int = 30, billing_account_id: Optional[str] = None) -> Path:
    """Pull the BigQuery cost window and persist today's snapshot."""
    window = cost.fetch_cost_window(days=days, billing_account_id=billing_account_id)
    payload = {"snapshot_at": _now_iso(), **cost.to_dict(window)}
    return _write_snapshot("cost", payload)


def snapshot_deploys(project: str = "ytfactory-prod-v2") -> Path:
    """List recent gcloud builds + prep status, persist today's snapshot."""
    rows = deploys.collect(project=project)
    payload = {
        "snapshot_at": _now_iso(),
        "project": project,
        "rows": [asdict(r) for r in rows],
    }
    return _write_snapshot("deploys", payload)


@_obs.traced("cloud.snapshot.snapshot_all", category="cloud",
             capture=["cost_days", "project"])
def snapshot_all(
    *,
    cost_days: int = 30,
    project: str = "ytfactory-prod-v2",
    billing_account_id: Optional[str] = None,
) -> dict[str, str]:
    """Run all three snapshots — what the cron + Refresh button call."""
    paths = {
        "health": str(snapshot_health()),
        "cost": str(snapshot_cost(days=cost_days, billing_account_id=billing_account_id)),
        "deploys": str(snapshot_deploys(project=project)),
    }
    logger.info("snapshot_all wrote: %s", paths)
    return paths


def _update_baseline(rows: list[health.HealthRow], window_days: int = 7) -> None:
    """Maintain a rolling per-service latency_p95_ms over the last N daily snapshots."""
    by_short_latencies: dict[str, list[float]] = {}
    if HEALTH_DIR.exists():
        files = sorted(HEALTH_DIR.glob("*.json"))[-window_days:]
        for f in files:
            try:
                snap = json.loads(f.read_text())
            except Exception:
                continue
            for r in snap.get("rows", []):
                lat = r.get("latency_ms")
                if isinstance(lat, (int, float)):
                    by_short_latencies.setdefault(r["short"], []).append(float(lat))
    for r in rows:
        if r.latency_ms is not None:
            by_short_latencies.setdefault(r.short, []).append(float(r.latency_ms))

    baseline: dict[str, dict[str, Any]] = {}
    for short, lats in by_short_latencies.items():
        if not lats:
            continue
        try:
            p95 = statistics.quantiles(lats, n=20)[-1]  # 95th
        except statistics.StatisticsError:
            p95 = max(lats)
        baseline[short] = {
            "latency_p95_ms": round(p95, 1),
            "samples": len(lats),
            "updated_at": _now_iso(),
        }
    _atomic_write(BASELINE_PATH, baseline)


def _latest_from_fs(kind: str) -> Optional[dict[str, Any]]:
    """Read the newest on-disk snapshot for ``kind``, or None."""
    dir_map = {"health": HEALTH_DIR, "cost": COST_DIR, "deploys": DEPLOYS_DIR}
    d = dir_map.get(kind)
    if not d or not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception as e:  # noqa: BLE001
        logger.warning("latest_snapshot(%s) failed to parse %s: %s", kind, files[-1], e)
        return None


def _latest_from_gcs(kind: str, bucket: str) -> Optional[dict[str, Any]]:
    """Read the newest snapshot for ``kind`` from
    ``gs://$bucket/data/_bench/cloud_<kind>/`` (the largest YYYY-MM-DD.json),
    or None on miss / error.
    """
    try:
        from google.cloud import storage  # noqa: PLC0415
    except ImportError:
        logger.warning("latest_snapshot(%s): google-cloud-storage missing, can't read GCS", kind)
        return None

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
    try:
        client = storage.Client(project=project)
        prefix = f"{_GCS_KEY_PREFIX}/{_GCS_KIND_TO_DIRNAME[kind]}/"
        blobs = list(client.list_blobs(bucket, prefix=prefix))
    except Exception as e:  # noqa: BLE001
        logger.warning("latest_snapshot(%s): GCS list failed: %s", kind, e)
        return None
    if not blobs:
        return None
    # Names are <prefix>YYYY-MM-DD.json — lexical max == newest.
    blob = max(blobs, key=lambda b: b.name)
    try:
        raw = blob.download_as_bytes()
    except Exception as e:  # noqa: BLE001
        logger.warning("latest_snapshot(%s): GCS download failed for %s: %s", kind, blob.name, e)
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("latest_snapshot(%s): %s is not valid JSON: %s", kind, blob.name, e)
        return None


def latest_snapshot(kind: str) -> Optional[dict[str, Any]]:
    """Read the newest snapshot for ``kind`` (health/cost/deploys), or None.

    Backend dispatch matches :func:`_state_bucket`: GCS when bucket env
    is set, otherwise on-disk under ``data/_bench/``.
    """
    if kind not in _GCS_KIND_TO_DIRNAME:
        return None
    bucket = _state_bucket()
    if bucket:
        return _latest_from_gcs(kind, bucket)
    return _latest_from_fs(kind)


def cost_history(days: int = 30) -> dict[str, Any]:
    """Stitch together up to ``days`` daily cost snapshots into one chart-ready payload.

    Falls back to the most recent snapshot's own per-day rollup when
    only one daily snapshot exists yet (the BigQuery query already
    returns 30 days of data).
    """
    latest = latest_snapshot("cost")
    if not latest:
        return {"available": False, "reason": "no cost snapshot yet"}
    return latest
