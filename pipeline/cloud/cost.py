"""Daily Cloud Run cost ingest from the BigQuery Billing Export.

Setup (one-time, documented in ``docs/cloudrun_admin_panel.md``):

1. Enable the Cloud Billing → BigQuery export from the GCP console:
     Billing → Billing export → Detailed usage cost → BigQuery export
   pointed at dataset ``billing_export`` in project ``ytfactory-prod-v2``.
   This is **Console-only** — no API or ``gcloud`` command exists for
   creating the export config (verified 2026-05-11). The dataset itself
   can be pre-created with ``bq mk --location=US billing_export``.
2. The standard export table is named
   ``gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`` (dashes →
   underscores). For ``ytfactory-prod-v2`` the billing account id is
   ``012FF7-AF3923-1A94C4`` (so the table is ``..._012FF7_AF3923_1A94C4``).
3. The service account running this code (laptop user creds in dev,
   ``tts-runner@ytfactory-prod-v2`` for the prod web service) needs
   ``roles/bigquery.dataViewer`` on that dataset.

What we query
-------------
For each Cloud Run service we own (filtered to ``service.description =
'Cloud Run'`` AND ``resource.name LIKE 'projects/.../services/<name>'``),
sum ``cost`` grouped by day and service over the requested window.

Returns a structure the panel renders directly (per-service sparkline +
today / MTD totals + drift flag if today's burn > 2x the 30-day median).

Graceful degradation
--------------------
If ``google-cloud-bigquery`` isn't installed or the export table doesn't
exist yet, :func:`fetch_cost_window` returns an empty payload with
``available: False`` and a one-line ``reason``. The panel renders a
"Cost data not yet wired — see docs/cloudrun_admin_panel.md" banner
rather than crashing.
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .services import Service, list_services
from .. import observability as _obs

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
DEFAULT_DATASET = os.environ.get("BIGQUERY_BILLING_DATASET", "billing_export")
DEFAULT_TABLE_PREFIX = "gcp_billing_export_resource_v1_"


@dataclass
class CostPoint:
    """One day, one service, one cost figure (USD)."""

    day: str  # YYYY-MM-DD
    service_short: str  # chatterbox / flux2-klein / ...
    cost_usd: float


@dataclass
class CostWindow:
    """Per-service cost over the requested window — what the API returns."""

    available: bool
    reason: Optional[str] = None
    days: int = 0
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    points: list[CostPoint] = field(default_factory=list)
    per_service_today: dict[str, float] = field(default_factory=dict)
    per_service_mtd: dict[str, float] = field(default_factory=dict)
    per_service_30d_median: dict[str, float] = field(default_factory=dict)
    drift_flags: dict[str, str] = field(default_factory=dict)
    total_today: float = 0.0
    total_mtd: float = 0.0


def _table_id(billing_account_id: str) -> str:
    """Format the standard billing-export table id (dashes → underscores)."""
    suffix = billing_account_id.replace("-", "_")
    return f"{DEFAULT_PROJECT}.{DEFAULT_DATASET}.{DEFAULT_TABLE_PREFIX}{suffix}"


def _bq_client():
    """Import bigquery lazily so the module stays importable without the dep."""
    from google.cloud import bigquery  # type: ignore  # noqa: PLC0415

    return bigquery.Client(project=DEFAULT_PROJECT)


@_obs.traced("cloud.cost.fetch_cost_window", category="cloud",
             capture=["days"])
def fetch_cost_window(
    days: int = 30,
    *,
    billing_account_id: Optional[str] = None,
    services: Optional[list[Service]] = None,
) -> CostWindow:
    """Pull per-service per-day cost for the last ``days`` days.

    ``billing_account_id`` defaults to the ``BILLING_ACCOUNT_ID`` env, then
    to the hard-coded id for ``ytfactory-prod-v2``: ``012FF7-AF3923-1A94C4``
    (verified via ``gcloud beta billing projects describe ytfactory-prod-v2``
    on 2026-05-11). The old v1-era id ``012E39-E4ECEB-7F119F`` referenced in
    ``docs/cloudrun_tts.md`` belongs to ``ytfactory-prod`` (v1) and is no
    longer accessible.
    """
    targets = services if services is not None else list_services()
    name_to_short = {s.name: s.short for s in targets}
    today = date.today()
    start = today - timedelta(days=days - 1)

    window = CostWindow(available=False, days=days,
                        range_start=start.isoformat(),
                        range_end=today.isoformat())

    bid = billing_account_id or os.environ.get("BILLING_ACCOUNT_ID") or "012FF7-AF3923-1A94C4"
    if not bid:
        window.reason = "BILLING_ACCOUNT_ID env not set"
        return window

    try:
        client = _bq_client()
    except ImportError:
        window.reason = "google-cloud-bigquery not installed (pip install google-cloud-bigquery)"
        return window
    except Exception as e:  # noqa: BLE001
        window.reason = f"BigQuery client init failed: {type(e).__name__}: {e}"
        return window

    table = _table_id(bid)
    sql = f"""
        SELECT
          DATE(usage_start_time, 'UTC') AS day,
          REGEXP_EXTRACT(resource.name, r'/services/([^/]+)$') AS svc_name,
          SUM(cost) AS cost_usd
        FROM `{table}`
        WHERE service.description = 'Cloud Run'
          AND DATE(usage_start_time, 'UTC') BETWEEN @start AND @end
          AND resource.name LIKE 'projects/%/services/%'
        GROUP BY day, svc_name
        ORDER BY day, svc_name
    """
    try:
        from google.cloud import bigquery  # type: ignore  # noqa: PLC0415

        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("start", "DATE", start.isoformat()),
                    bigquery.ScalarQueryParameter("end", "DATE", today.isoformat()),
                ]
            ),
        )
        rows = list(job.result(timeout=30))
    except Exception as e:  # noqa: BLE001
        window.reason = f"BigQuery query failed: {type(e).__name__}: {e}"
        return window

    by_short_by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        full = r.svc_name or ""
        short = name_to_short.get(full)
        if not short:
            # Cloud Run service we don't track — bucket as 'other' so
            # totals stay accurate.
            short = "other"
        d = r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day)
        cost = float(r.cost_usd or 0.0)
        window.points.append(CostPoint(day=d, service_short=short, cost_usd=cost))
        by_short_by_day.setdefault(short, {})[d] = by_short_by_day.get(short, {}).get(d, 0.0) + cost

    today_iso = today.isoformat()
    mtd_start = today.replace(day=1)
    mtd_dates = {(mtd_start + timedelta(days=i)).isoformat()
                 for i in range((today - mtd_start).days + 1)}

    for short, by_day in by_short_by_day.items():
        window.per_service_today[short] = round(by_day.get(today_iso, 0.0), 4)
        mtd_total = sum(c for d, c in by_day.items() if d in mtd_dates)
        window.per_service_mtd[short] = round(mtd_total, 4)
        if by_day:
            median = statistics.median(by_day.values())
            window.per_service_30d_median[short] = round(median, 4)
            today_v = by_day.get(today_iso, 0.0)
            if median > 0 and today_v > 2 * median:
                window.drift_flags[short] = f"today ${today_v:.2f} > 2× median ${median:.2f}"

    window.total_today = round(sum(window.per_service_today.values()), 2)
    window.total_mtd = round(sum(window.per_service_mtd.values()), 2)
    window.available = True
    return window


def to_dict(window: CostWindow) -> dict[str, Any]:
    """JSON-friendly view of a :class:`CostWindow`."""
    return {
        "available": window.available,
        "reason": window.reason,
        "days": window.days,
        "range_start": window.range_start,
        "range_end": window.range_end,
        "points": [
            {"day": p.day, "service_short": p.service_short, "cost_usd": p.cost_usd}
            for p in window.points
        ],
        "per_service_today": window.per_service_today,
        "per_service_mtd": window.per_service_mtd,
        "per_service_30d_median": window.per_service_30d_median,
        "drift_flags": window.drift_flags,
        "total_today": window.total_today,
        "total_mtd": window.total_mtd,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
