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

Baseline: this module also updates ``data/_bench/cloud_health_baseline.json``
with rolling p95 latency per service, used by :func:`pipeline.cloud.health._classify`.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import cost, deploys, health
from .services import list_services

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = REPO_ROOT / "data" / "_bench"
HEALTH_DIR = BENCH_ROOT / "cloud_health"
COST_DIR = BENCH_ROOT / "cloud_cost"
DEPLOYS_DIR = BENCH_ROOT / "cloud_deploys"
BASELINE_PATH = BENCH_ROOT / "cloud_health_baseline.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.rename(path)


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
    out = HEALTH_DIR / f"{_today_iso()}.json"
    _atomic_write(out, payload)
    _update_baseline(rows)
    return out


def snapshot_cost(days: int = 30, billing_account_id: Optional[str] = None) -> Path:
    """Pull the BigQuery cost window and persist today's snapshot."""
    window = cost.fetch_cost_window(days=days, billing_account_id=billing_account_id)
    payload = {"snapshot_at": _now_iso(), **cost.to_dict(window)}
    out = COST_DIR / f"{_today_iso()}.json"
    _atomic_write(out, payload)
    return out


def snapshot_deploys(project: str = "ytfactory-prod-v2") -> Path:
    """List recent gcloud builds + prep status, persist today's snapshot."""
    rows = deploys.collect(project=project)
    payload = {
        "snapshot_at": _now_iso(),
        "project": project,
        "rows": [asdict(r) for r in rows],
    }
    out = DEPLOYS_DIR / f"{_today_iso()}.json"
    _atomic_write(out, payload)
    return out


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


def latest_snapshot(kind: str) -> Optional[dict[str, Any]]:
    """Read the newest snapshot for ``kind`` (health/cost/deploys), or None."""
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
