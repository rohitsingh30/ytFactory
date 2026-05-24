#!/usr/bin/env python3
"""Telemetry-driven GCP cost report — reconciles the real GCP bill against
ytFactory's stage-level telemetry so it's clear WHERE the burn goes.

Why this exists
---------------
The billing CSV tells us "Cloud Run cost ₹9,456 in 9 days" but not "render
job X cost ₹Y". Cloud Logging tells us "image.server.gen ran for 5,620s"
but not "your real GCP invoice was $135". This script joins the two: for
each day it shows real spend (from the CSV) next to telemetry-attributed
spend (per render category + per top job), and surfaces the GAP as the
things telemetry doesn't see (min-instance idle, builds, egress, storage).

Inputs
~~~~~~
* ``--bill PATH`` — GCP billing-export CSV downloaded from the console
  (columns: Date, Service description, SKU description, Cost (₹), ...).
  The script reads the **gross** ``Cost (₹)`` column, not ``Subtotal``,
  because subtotals are zeroed by credits and gross is the only number
  that reflects actual resource consumption.
* ``--days N`` — telemetry window. Defaults to 7.
* ``--project ID`` — GCP project for the Cloud Logging query. Defaults to
  ``GOOGLE_CLOUD_PROJECT`` or ``ytfactory-prod-v3``.
* ``--inr-per-usd 83`` — FX rate for currency conversion. Bill is in INR.

What it does NOT cover
~~~~~~~~~~~~~~~~~~~~~~
* **LLM costs**: chat models live on Azure, NOT in the GCP bill. We
  emit ``llm.call`` telemetry but the dollars hit a different invoice.
  The script prints a sidebar with token counts + Azure-side $ estimate
  for visibility but does NOT mix that figure into the GCP totals.
* **Cold-start / model-load time**: per ``cost_rates.py`` notes, the
  per-request ``gpu_seconds`` excludes model-load on first request.
  Treat the gap as the upper bound on cold-start cost.

Reading the output
~~~~~~~~~~~~~~~~~~
Three sections:

1. **Daily ledger** — real bill total vs telemetry-attributed total per
   day, with gap.
2. **Today's category breakdown** — image GPU, TTS GPU, ASR GPU, worker
   wallclock + their per-second confidence level.
3. **Top-10 expensive jobs today** — job_id → channel → slug → derived $.
4. **Bill-side hotspots** — top SKUs by ₹, with action hints when the
   SKU matches a known cost-guardrail violation (min-instances, inter-
   region egress, Cloud Build pool).

Usage
~~~~~
.. code-block:: shell

   .venv/bin/python scripts/cost_report.py \\
       --bill "/Users/rohit/Downloads/My Billing Account_Reports, 2026-05-01 — 2026-05-24.csv" \\
       --days 7
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running as a script: ``python scripts/cost_report.py``
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.observability.cost_rates import (  # noqa: E402
    cloud_run_rate,
    llm_call_cost_usd,
)


PROJECT_DEFAULT = "ytfactory-prod-v3"
INR_PER_USD_DEFAULT = 83.0

# Event names that carry per-call GPU/CPU duration we can attribute.
_IMAGE_SERVER_EVENT = "image.server.gen"
_TTS_WORKER_EVENT = "tts_synth"
_ASR_CHUNK_EVENT = "asr.chunk"
_LLM_EVENTS = ("llm.call", "llm_call")
_STAGE_END_EVENTS = ("stage.end", "stage.failed")

# Cloud Logging filter: only structured ytFactory events. Querying
# day-by-day instead of one big query keeps us under the 50k page cap
# and avoids silent truncation of high-traffic days (rubber-duck
# feedback #6).
_LOG_FILTER_TEMPLATE = (
    '(resource.type="cloud_run_revision" OR resource.type="cloud_run_job") AND '
    'jsonPayload."ytfactory.event"!="" AND '
    'timestamp >= "{start}" AND timestamp < "{end}"'
)


# ---------------------------------------------------------------------------
# Bill CSV ingestion
# ---------------------------------------------------------------------------


@dataclass
class BillSummary:
    by_day_inr: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_service_inr: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_sku_inr: dict[tuple[str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    by_day_sku_inr: dict[tuple[str, str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    total_inr: float = 0.0
    earliest_day: str = ""
    latest_day: str = ""


def load_bill_csv(path: Path) -> BillSummary:
    """Parse the GCP billing-export CSV. Uses the gross ``Cost (₹)`` column
    so credits-applied $0 rows don't hide real usage."""
    summary = BillSummary()
    cost_col = "Cost (\u20b9)"
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            day = (row.get("Date") or "").strip()
            if not day:
                continue
            svc = (row.get("Service description") or "").strip() or "Unknown"
            sku = (row.get("SKU description") or "").strip() or "Unknown"
            try:
                cost = float((row.get(cost_col) or "0").strip() or 0)
            except ValueError:
                cost = 0.0
            summary.by_day_inr[day] += cost
            summary.by_service_inr[svc] += cost
            summary.by_sku_inr[(svc, sku)] += cost
            summary.by_day_sku_inr[(day, svc, sku)] += cost
            summary.total_inr += cost
            if not summary.earliest_day or day < summary.earliest_day:
                summary.earliest_day = day
            if not summary.latest_day or day > summary.latest_day:
                summary.latest_day = day
    return summary


# ---------------------------------------------------------------------------
# Telemetry ingestion — Cloud Logging, day-by-day
# ---------------------------------------------------------------------------


@dataclass
class Event:
    ts: datetime
    name: str
    job_id: str | None
    service: str | None  # cloud_run_revision/job service_name
    duration_ms: float | None
    meta: dict[str, Any]
    success: bool | None


def _event_iter(
    *,
    project: str,
    day_start: datetime,
    day_end: datetime,
    page_size: int = 1000,
) -> Iterable[Event]:
    from google.cloud import logging as gcl  # local import — only needed when actually fetching

    client = gcl.Client(project=project)
    flt = _LOG_FILTER_TEMPLATE.format(
        start=day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    # Recursively split if any 6-h block hits the page cap. For now
    # 24h-windows are fine for ytFactory volume but the entry point
    # is here if it changes.
    for entry in client.list_entries(filter_=flt, page_size=page_size):
        payload = entry.payload if isinstance(entry.payload, dict) else None
        if not payload:
            continue
        name = payload.get("ytfactory.event")
        if not name:
            continue
        ts = getattr(entry, "timestamp", None)
        if isinstance(ts, datetime):
            ts_norm = ts
        else:
            ts_norm = datetime.now(timezone.utc)
        meta: dict[str, Any] = {}
        for k, v in payload.items():
            if not isinstance(k, str):
                continue
            if k.startswith("ytfactory.meta."):
                meta[k[len("ytfactory.meta."):]] = v
        duration_ms_raw = payload.get("ytfactory.duration_ms") or meta.get("duration_ms")
        try:
            duration_ms = float(duration_ms_raw) if duration_ms_raw is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        # Resource: prefer service_name (revisions) else job_name
        svc = None
        if getattr(entry, "resource", None) is not None:
            labels = entry.resource.labels or {}
            svc = labels.get("service_name") or labels.get("job_name")
        success = payload.get("ytfactory.success")
        if isinstance(success, str):
            success = success.lower() in ("true", "1", "yes")
        yield Event(
            ts=ts_norm,
            name=name,
            job_id=payload.get("ytfactory.job_id"),
            service=svc,
            duration_ms=duration_ms,
            meta=meta,
            success=bool(success) if success is not None else None,
        )


def fetch_telemetry_window(
    *, project: str, days: int
) -> tuple[list[Event], date, date]:
    """Return all ytFactory events from the last N days plus the actual
    [start, end) UTC dates that were queried."""
    end_dt = datetime.now(timezone.utc).replace(microsecond=0)
    start_dt = end_dt - timedelta(days=days)
    events: list[Event] = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(days=1), end_dt)
        for ev in _event_iter(project=project, day_start=cur, day_end=nxt):
            events.append(ev)
        cur = nxt
    return events, start_dt.date(), end_dt.date()


# ---------------------------------------------------------------------------
# Cost attribution
# ---------------------------------------------------------------------------


@dataclass
class DailyCost:
    image_usd: float = 0.0           # high confidence — server-side gpu_seconds
    tts_chatterbox_usd: float = 0.0  # medium — worker-side wallclock (no server gpu_seconds)
    tts_indicf5_usd: float = 0.0     # medium
    asr_usd: float = 0.0             # medium — chunk count × est duration
    editing_agent_usd: float = 0.0   # medium
    worker_usd: float = 0.0          # high — stage.end durations
    image_events: int = 0
    tts_events: int = 0
    asr_events: int = 0
    stage_events: int = 0
    # off-bill (Azure) LLM sidebar
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_usd_estimate: float = 0.0
    llm_events: int = 0
    # job-level for top-N
    per_job_usd: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    per_job_meta: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def total_gcp_usd(self) -> float:
        return (
            self.image_usd
            + self.tts_chatterbox_usd
            + self.tts_indicf5_usd
            + self.asr_usd
            + self.editing_agent_usd
            + self.worker_usd
        )


def _service_for_image() -> str:
    return "ytfactory-image-z-image-turbo"


def _service_for_tts(provider: str | None) -> str:
    if provider and "indicf5" in provider:
        return "ytfactory-tts-indicf5"
    return "tts-chatterbox"


def _service_for_asr() -> str:
    return "ytfactory-asr-whisper"


def _service_for_worker() -> str:
    return "ytfactory-render-worker-v2"


def attribute(events: Iterable[Event]) -> dict[str, DailyCost]:
    """Bucket events by UTC date, derive USD per category."""
    by_day: dict[str, DailyCost] = defaultdict(DailyCost)

    for ev in events:
        day_str = ev.ts.date().isoformat()
        d = by_day[day_str]

        if ev.name == _IMAGE_SERVER_EVENT:
            # Highest-confidence path — meta.gpu_seconds is recorded
            # server-side, after model-load, capturing real inference time.
            seconds = float(ev.meta.get("gpu_seconds") or 0.0)
            if not seconds and ev.duration_ms:
                seconds = ev.duration_ms / 1000.0
            usd = cloud_run_rate(_service_for_image()) * seconds / 3600.0
            d.image_usd += usd
            d.image_events += 1
            if ev.job_id:
                d.per_job_usd[ev.job_id] += usd

        elif ev.name == _TTS_WORKER_EVENT:
            # Worker-side wallclock — includes HTTP overhead but is the
            # only TTS timing we currently emit. ``tts.server`` lacks
            # ``meta.gpu_seconds`` so this is the practical fallback.
            seconds = (ev.duration_ms or 0.0) / 1000.0
            provider = ev.meta.get("provider")
            svc = _service_for_tts(provider)
            usd = cloud_run_rate(svc) * seconds / 3600.0
            if "indicf5" in svc:
                d.tts_indicf5_usd += usd
            else:
                d.tts_chatterbox_usd += usd
            d.tts_events += 1
            if ev.job_id:
                d.per_job_usd[ev.job_id] += usd

        elif ev.name == _ASR_CHUNK_EVENT:
            # ASR neither chunk nor server currently emits gpu_seconds
            # in our sample. Faster-whisper turbo runs ~40× real-time
            # on an L4, so an audio chunk of ``audio_seconds`` consumes
            # roughly audio_seconds/40 of GPU. Conservative ×2 margin
            # → audio_seconds/20.
            audio_s = float(ev.meta.get("audio_seconds") or 0.0)
            est_gpu_s = audio_s / 20.0
            usd = cloud_run_rate(_service_for_asr()) * est_gpu_s / 3600.0
            d.asr_usd += usd
            d.asr_events += 1
            if ev.job_id:
                d.per_job_usd[ev.job_id] += usd

        elif ev.name in _STAGE_END_EVENTS:
            seconds = (ev.duration_ms or 0.0) / 1000.0
            usd = cloud_run_rate(_service_for_worker()) * seconds / 3600.0
            d.worker_usd += usd
            d.stage_events += 1
            if ev.job_id:
                d.per_job_usd[ev.job_id] += usd
                # Cache channel / slug for the top-N table.
                meta = d.per_job_meta.setdefault(ev.job_id, {})
                meta.setdefault("channel", ev.meta.get("channel"))
                meta.setdefault("slug", ev.meta.get("slug"))

        elif ev.name in _LLM_EVENTS:
            input_tokens = int(float(ev.meta.get("input_tokens") or 0))
            output_tokens = int(float(ev.meta.get("output_tokens") or 0))
            model = ev.meta.get("model")
            tier = ev.meta.get("tier")
            llm_usd = llm_call_cost_usd(
                model=model,
                tier=tier,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            d.llm_input_tokens += input_tokens
            d.llm_output_tokens += output_tokens
            d.llm_usd_estimate += llm_usd
            d.llm_events += 1

    return dict(by_day)


# ---------------------------------------------------------------------------
# Bill-side hotspot classifier — points at known guardrail violations
# ---------------------------------------------------------------------------


@dataclass
class Hotspot:
    sku: str
    service: str
    cost_inr: float
    cost_usd: float
    action: str | None  # actionable hint if the SKU maps to a known leak


_SKU_ACTION_HINTS: list[tuple[str, str]] = [
    # (substring match on SKU description, action hint shown next to row)
    (
        "Min Instance",
        "Some service has min-instances>=1. Run "
        "`python scripts/audit_idle_costs.py` to find it.",
    ),
    (
        "Inter Region Egress Intercontinental",
        "A `gcloud builds submit` is missing `--region=asia-southeast1`. "
        "Builds default to the global pool (us-central1) and the push to "
        "asia-southeast1 AR crosses the Pacific. Grep deploy.sh files.",
    ),
    (
        "Internet Egress Intercontinental",
        "Some Cloud Run service is calling an out-of-region URL. Check "
        "external-service env vars for non-asia URLs.",
    ),
    (
        "with zonal redundancy",
        "Zonal-redundant L4 is ~50% more expensive than no-redundancy. "
        "Use `--no-gpu-zonal-redundancy` in the relevant deploy.sh.",
    ),
    (
        "Cloud Build",
        "Cloud Build bills CPU+memory per second. Frequent rebuilds add up "
        "fast. Cache layers aggressively and audit which `deploy.sh` is "
        "rebuilding from scratch.",
    ),
]


def classify_hotspots(
    bill: BillSummary,
    *,
    inr_per_usd: float,
    top_n: int = 15,
) -> list[Hotspot]:
    rows: list[Hotspot] = []
    for (svc, sku), inr in sorted(bill.by_sku_inr.items(), key=lambda x: -x[1])[:top_n]:
        if inr <= 0:
            continue
        action = None
        for needle, hint in _SKU_ACTION_HINTS:
            if needle.lower() in sku.lower():
                action = hint
                break
        rows.append(
            Hotspot(
                sku=sku,
                service=svc,
                cost_inr=inr,
                cost_usd=inr / inr_per_usd,
                action=action,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt_money_pair(usd: float, inr_per_usd: float) -> str:
    return f"${usd:>7,.2f} (\u20b9{usd*inr_per_usd:>9,.2f})"


def render_report(
    *,
    bill: BillSummary | None,
    telemetry: dict[str, DailyCost],
    project: str,
    tele_start: date,
    tele_end: date,
    inr_per_usd: float,
    top_jobs: int = 10,
) -> str:
    out: list[str] = []
    out.append("=" * 80)
    out.append(f"  ytFactory cost report   project={project}")
    out.append(
        f"  telemetry window: {tele_start.isoformat()} → {tele_end.isoformat()} (UTC)"
    )
    if bill:
        out.append(
            f"  billing CSV:      {bill.earliest_day} → {bill.latest_day}  "
            f"(\u20b9{bill.total_inr:,.2f} ≈ ${bill.total_inr/inr_per_usd:,.2f} gross)"
        )
    out.append(f"  FX rate:          \u20b9{inr_per_usd:.2f}/$ (override via --inr-per-usd)")
    out.append("=" * 80)
    out.append("")

    # ----------------------------------------------------------------------
    # Section 1 — Daily ledger: bill vs telemetry
    # ----------------------------------------------------------------------
    out.append("DAILY LEDGER  (gross — credits not deducted)")
    out.append("-" * 80)
    header = f"{'Date':<12} {'Real bill':>22} {'Telemetry-attrib':>22} {'Gap':>22}"
    out.append(header)
    out.append("-" * len(header))
    all_days = sorted(set(telemetry.keys()) | (set(bill.by_day_inr.keys()) if bill else set()))
    for day in all_days:
        real_inr = bill.by_day_inr.get(day, 0.0) if bill else 0.0
        real_usd = real_inr / inr_per_usd
        tele_usd = telemetry.get(day, DailyCost()).total_gcp_usd
        gap_usd = real_usd - tele_usd
        real_str = _fmt_money_pair(real_usd, inr_per_usd) if real_inr else "       —"
        tele_str = _fmt_money_pair(tele_usd, inr_per_usd) if tele_usd else "       —"
        if real_inr and tele_usd:
            gap_str = _fmt_money_pair(gap_usd, inr_per_usd)
        else:
            gap_str = "       —"
        out.append(f"{day:<12} {real_str:>22} {tele_str:>22} {gap_str:>22}")
    out.append("")
    out.append(
        "Gap = real bill - telemetry-attributed. Contains: min-instance idle, "
        "Cloud Build, Artifact Registry storage + egress, cold starts, "
        "web/web-next, GCS, logging ingestion."
    )
    out.append("")

    # ----------------------------------------------------------------------
    # Section 2 — Today's category breakdown
    # ----------------------------------------------------------------------
    today = sorted(telemetry.keys())[-1] if telemetry else None
    if today:
        d = telemetry[today]
        out.append(f"CATEGORY BREAKDOWN — {today}")
        out.append("-" * 80)
        rows = [
            ("image GPU       (z-image-turbo)", d.image_usd, d.image_events, "high  (server-side gpu_seconds)"),
            ("tts GPU         (chatterbox)",    d.tts_chatterbox_usd, d.tts_events, "med   (worker wallclock, includes HTTP overhead)"),
            ("tts GPU         (indicf5)",       d.tts_indicf5_usd, 0, "med   (worker wallclock, includes HTTP overhead)"),
            ("asr GPU         (whisper)",       d.asr_usd, d.asr_events, "low   (estimated: audio_seconds/20 — no server gpu_seconds)"),
            ("editing-agent",                   d.editing_agent_usd, 0, "med"),
            ("worker wallclock (render-worker-v2)", d.worker_usd, d.stage_events, "high  (stage.end + stage.failed durations)"),
        ]
        for label, usd, n_events, conf in rows:
            if usd == 0 and n_events == 0:
                continue
            ev_str = f"n={n_events:>4}" if n_events else ""
            out.append(
                f"  {label:<44} {_fmt_money_pair(usd, inr_per_usd):>22}  {ev_str:<10}  {conf}"
            )
        out.append("-" * 80)
        out.append(
            f"  {'GCP TOTAL':<44} {_fmt_money_pair(d.total_gcp_usd, inr_per_usd):>22}"
        )
        out.append("")
        if d.llm_events:
            out.append(
                f"  Azure LLM sidebar (NOT on GCP bill): n={d.llm_events} "
                f"input={d.llm_input_tokens:,} output={d.llm_output_tokens:,} "
                f"≈ ${d.llm_usd_estimate:.2f}"
            )
            out.append("")

    # ----------------------------------------------------------------------
    # Section 3 — Top-N expensive jobs today
    # ----------------------------------------------------------------------
    if today:
        d = telemetry[today]
        if d.per_job_usd:
            out.append(f"TOP {top_jobs} EXPENSIVE JOBS — {today}")
            out.append("-" * 80)
            ranked = sorted(d.per_job_usd.items(), key=lambda x: -x[1])[:top_jobs]
            for job_id, usd in ranked:
                meta = d.per_job_meta.get(job_id, {})
                channel = meta.get("channel") or "?"
                slug = meta.get("slug") or "?"
                out.append(
                    f"  {job_id[:12]}  {_fmt_money_pair(usd, inr_per_usd):>22}  "
                    f"{channel:<22} {slug}"
                )
            out.append("")

    # ----------------------------------------------------------------------
    # Section 4 — Bill-side hotspots with guardrail-violation hints
    # ----------------------------------------------------------------------
    if bill:
        hotspots = classify_hotspots(bill, inr_per_usd=inr_per_usd, top_n=12)
        out.append("BILL HOTSPOTS  (top SKUs, period " + bill.earliest_day + " → " + bill.latest_day + ")")
        out.append("-" * 80)
        for h in hotspots:
            out.append(
                f"  {_fmt_money_pair(h.cost_usd, inr_per_usd):>22}  "
                f"{h.service:<22} {h.sku}"
            )
            if h.action:
                out.append(f"      ↳ ACTION: {h.action}")
        out.append("")

    # ----------------------------------------------------------------------
    # Footer with action surface
    # ----------------------------------------------------------------------
    out.append("=" * 80)
    out.append("Next actions if today's GCP total > budget:")
    out.append("  1. Check top-N jobs above — are they failures? Failures pay full GPU.")
    out.append("  2. Look at the gap line — is it growing? min-instances or build drift.")
    out.append("  3. Hotspot table calls out specific SKU → fix mapping.")
    out.append("=" * 80)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Persistence (Phase 2 — optional)
# ---------------------------------------------------------------------------


def persist_to_firestore(
    *,
    project: str,
    telemetry: dict[str, DailyCost],
    bill: BillSummary | None,
    inr_per_usd: float,
) -> None:
    """Write each day to ``cost_log/<YYYY-MM-DD>`` so we preserve daily
    totals beyond Cloud Logging's 30-day retention AND beyond the CSV
    download (which is one-off and easy to lose).

    The doc captures BOTH sides of the reconciliation:

    * ``real_bill_usd`` — gross from the GCP billing CSV (None if no CSV)
    * ``telemetry_usd`` — sum of attributed events
    * ``gap_usd`` — real minus telemetry (the "uncaptured" tier — min-instance
      idle, builds, AR egress/storage, cold-start, web/web-next, GCS, …)
    * ``bill_by_service_usd`` — top services from the CSV for hotspot history
    * ``bill_top_skus_usd`` — top 5 SKUs by spend so we keep the
      action-hint context (which the SKU classifier needs for trend analysis)

    Idempotent — overwrites existing docs. Days seen only in the bill
    (no telemetry) still get persisted with telemetry_usd=0."""
    from google.cloud import firestore  # local import — optional path

    db = firestore.Client(project=project)

    # Build the set of days to persist: union of telemetry + bill days.
    days: set[str] = set(telemetry.keys())
    if bill is not None:
        days |= set(bill.by_day_inr.keys())

    written = 0
    for day in sorted(days):
        d = telemetry.get(day)
        real_bill_usd: float | None = None
        bill_by_service: dict[str, float] = {}
        bill_top_skus: list[dict[str, Any]] = []
        if bill is not None and day in bill.by_day_inr:
            real_bill_usd = bill.by_day_inr[day] / inr_per_usd
            # Per-service breakdown for the day.
            day_services: dict[str, float] = defaultdict(float)
            day_skus: list[tuple[str, str, float]] = []
            for (d_day, svc, sku), inr in bill.by_day_sku_inr.items():
                if d_day != day:
                    continue
                day_services[svc] += inr
                day_skus.append((svc, sku, inr))
            bill_by_service = {
                svc: round(inr / inr_per_usd, 4) for svc, inr in day_services.items()
            }
            day_skus.sort(key=lambda x: x[2], reverse=True)
            bill_top_skus = [
                {
                    "service": svc,
                    "sku": sku,
                    "usd": round(inr / inr_per_usd, 4),
                }
                for svc, sku, inr in day_skus[:5]
            ]

        telemetry_usd = d.total_gcp_usd if d else 0.0
        gap_usd: float | None = None
        if real_bill_usd is not None:
            gap_usd = real_bill_usd - telemetry_usd

        doc: dict[str, Any] = {
            "date": day,
            "computed_at": firestore.SERVER_TIMESTAMP,
            "fx_inr_per_usd": inr_per_usd,
            # Reconciliation core — the three numbers the user actually asked for.
            "real_bill_usd": real_bill_usd,
            "telemetry_usd": telemetry_usd,
            "gap_usd": gap_usd,
            # Bill-side detail (gone once the CSV is rotated out).
            "bill_by_service_usd": bill_by_service,
            "bill_top_skus_usd": bill_top_skus,
            # Telemetry-side detail.
            "by_category_usd": {
                "image": d.image_usd if d else 0.0,
                "tts_chatterbox": d.tts_chatterbox_usd if d else 0.0,
                "tts_indicf5": d.tts_indicf5_usd if d else 0.0,
                "asr": d.asr_usd if d else 0.0,
                "editing_agent": d.editing_agent_usd if d else 0.0,
                "worker": d.worker_usd if d else 0.0,
            },
            "event_counts": {
                "image": d.image_events if d else 0,
                "tts": d.tts_events if d else 0,
                "asr": d.asr_events if d else 0,
                "stage": d.stage_events if d else 0,
                "llm": d.llm_events if d else 0,
            },
            "llm_off_gcp": {
                "input_tokens": d.llm_input_tokens if d else 0,
                "output_tokens": d.llm_output_tokens if d else 0,
                "estimated_usd": d.llm_usd_estimate if d else 0.0,
            },
            "render_count": len(d.per_job_usd) if d else 0,
        }
        db.collection("cost_log").document(day).set(doc, merge=True)
        written += 1
    print(f"[persist] wrote {written} doc(s) to cost_log/")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--bill",
        type=Path,
        help="Path to GCP billing-export CSV (download from Console → Billing → Reports → Export).",
    )
    p.add_argument("--days", type=int, default=7, help="Telemetry window (UTC days). Default 7.")
    p.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", PROJECT_DEFAULT),
        help=f"GCP project. Default {PROJECT_DEFAULT}.",
    )
    p.add_argument(
        "--inr-per-usd",
        type=float,
        default=float(os.environ.get("YTFACTORY_FX_INR_PER_USD", INR_PER_USD_DEFAULT)),
        help=f"INR/USD FX rate. Default {INR_PER_USD_DEFAULT}.",
    )
    p.add_argument(
        "--persist",
        action="store_true",
        help="After computing, write daily totals to Firestore cost_log/<date>.",
    )
    p.add_argument(
        "--top-jobs",
        type=int,
        default=10,
        help="How many expensive jobs to show in section 3. Default 10.",
    )
    args = p.parse_args(argv)

    bill: BillSummary | None = None
    if args.bill:
        if not args.bill.exists():
            print(f"ERROR: bill file not found: {args.bill}", file=sys.stderr)
            return 2
        bill = load_bill_csv(args.bill)

    print(
        f"[telemetry] fetching last {args.days} day(s) of Cloud Logging events for "
        f"project={args.project} …",
        file=sys.stderr,
    )
    events, tele_start, tele_end = fetch_telemetry_window(
        project=args.project, days=args.days
    )
    print(f"[telemetry] {len(events):,} event(s) ingested", file=sys.stderr)

    telemetry = attribute(events)

    report = render_report(
        bill=bill,
        telemetry=telemetry,
        project=args.project,
        tele_start=tele_start,
        tele_end=tele_end,
        inr_per_usd=args.inr_per_usd,
        top_jobs=args.top_jobs,
    )
    print(report)

    if args.persist:
        persist_to_firestore(
            project=args.project,
            telemetry=telemetry,
            bill=bill,
            inr_per_usd=args.inr_per_usd,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
