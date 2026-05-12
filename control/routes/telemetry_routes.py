"""FastAPI routes for the /app/telemetry tab.

What this surfaces:

* ``GET /api/telemetry/overview?hours=24`` — totals + success rate +
  per-channel job counts. Read from the active OTel log buffer in
  ``inmemory`` mode (tests + early dev). When the GCP exporter is
  active the dashboard's Cloud Logging deep-link covers detail; we
  still return summary cards built from any in-process log shadow
  the laptop happens to keep.
* ``GET /api/telemetry/stages?hours=24`` — per-stage p50/p95/error
  rate from the same log stream.
* ``GET /api/telemetry/stage_latency?hours=24`` — p50/p95/mean/max
  for every render-pipeline stage (tts_synth, image_gen, llm_call,
  compose, upload_*, stage.* / render.*). Backs the "where is time
  being spent?" bar chart on /app/telemetry. Excludes background
  bookkeeping events so the chart isn't dominated by 0-ms noise.
* ``GET /api/telemetry/renders?hours=24&limit=30&channel=...`` —
  per-render breakdown (one row per channel + slug + render_kind)
  with total wall-clock duration AND ordered per-stage timings.
  Backs the "Recent renders" table — the operator's first stop for
  "how long did slug X take + where did its time go?".
* ``GET /api/telemetry/timeline?hours=24`` — per-minute event count
  bucket (sparkline data).
* ``GET /api/telemetry/errors?hours=24&limit=50`` — most recent
  unsuccessful events.
* ``GET /api/telemetry/services`` — per-service event volume + p95
  rolling over the last hour. Synthesised from the events stream
  using the ``ytfactory.category`` attribute.
* ``GET /api/telemetry/links`` — pre-baked deep-link URLs into Cloud
  Trace / Cloud Logging / Cloud Monitoring filtered to the project
  + (optionally) the channel/slug query params.
* ``GET /api/telemetry/init_status`` — yes/no init + exporter mode
  for the dashboard to render an "unconfigured" hint when on a fresh
  laptop without GCP creds.

Auth mirrors :mod:`control.routes.cloud_routes._require_auth`.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from pipeline import observability as obs
from pipeline import telemetry as tlm

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_auth(authorization: str | None) -> None:
    """Same auth posture as cloud_routes — Cloud Run trusts IAM,
    laptop checks Bearer when YTFACTORY_AGENT_TOKEN is set."""
    if os.environ.get("K_SERVICE"):
        return
    expected = os.environ.get("YTFACTORY_AGENT_TOKEN")
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(403, "invalid token")


# ---------------------------------------------------------- helpers


def _read_events(hours: int) -> list[dict]:
    since = time.time() - max(1, hours) * 3600
    return tlm.read_events(since_ts=since)


def _percentile(values: list[float], q: float) -> float:
    return tlm.percentile(values, q)


def _gcp_project() -> str | None:
    return (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
    )


def _has_gcp_exporter() -> bool:
    """True when the active OTel mode is the GCP cloud exporter."""
    return obs.current_mode() == "gcp"


# ---------------------------------------------------------- routes


@router.get("/api/telemetry/init_status")
def telemetry_init_status(
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Tell the UI whether telemetry is wired up + which exporter is
    active. Used to render the "deep-link to GCP" buttons only when the
    GCP exporter is actually exporting.

    Also surfaces whether the cross-service Cloud Logging reader is
    available so the dashboard can render an honest hint when the
    in-process buffer is empty (e.g. when the web-server has restarted
    recently and Cloud Logging is the only source of recent events).
    """
    _require_auth(authorization)
    cloudlog_available = False
    cloudlog_reason: str | None = None
    if _has_gcp_exporter():
        try:
            from pipeline.observability.cloud_log_reader import (
                _disabled_via_env,
                get_reader,
            )
            if _disabled_via_env():
                cloudlog_reason = "disabled via YTFACTORY_TELEMETRY_CLOUDLOG_DISABLE"
            elif not _gcp_project():
                cloudlog_reason = "GOOGLE_CLOUD_PROJECT not set"
            else:
                reader = get_reader()
                cloudlog_available = reader is not None
                if reader is None:
                    cloudlog_reason = (
                        "google-cloud-logging package or ADC unavailable"
                    )
        except Exception as e:  # noqa: BLE001
            cloudlog_reason = f"reader init failed: {e}"
    return {
        "initialised": obs.is_initialised(),
        "exporter": obs.current_mode(),
        "gcp_project": _gcp_project(),
        "has_gcp_exporter": _has_gcp_exporter(),
        "cloud_logging_reader_available": cloudlog_available,
        "cloud_logging_reader_reason": cloudlog_reason,
    }


@router.get("/api/telemetry/overview")
def telemetry_overview(
    hours: int = 24,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Coarse totals + success rate + per-channel counts."""
    _require_auth(authorization)
    events = _read_events(hours)
    total = len(events)
    successes = sum(1 for e in events if e.get("success"))
    failures = total - successes

    # Per-category breakdown.
    by_cat: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "success": 0, "failed": 0},
    )
    for e in events:
        c = e.get("category") or "?"
        by_cat[c]["total"] += 1
        if e.get("success"):
            by_cat[c]["success"] += 1
        else:
            by_cat[c]["failed"] += 1

    # Per-channel rollup (channel comes from metadata).
    by_channel: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "success": 0, "failed": 0},
    )
    for e in events:
        md = e.get("metadata") or {}
        ch = md.get("channel") or md.get("niche") or "?"
        by_channel[ch]["total"] += 1
        if e.get("success"):
            by_channel[ch]["success"] += 1
        else:
            by_channel[ch]["failed"] += 1

    durations = [e["duration_ms"] for e in events
                 if e.get("duration_ms") is not None]

    return {
        "hours": hours,
        "total_events": total,
        "successes": successes,
        "failures": failures,
        "success_rate": round(successes / total * 100, 1) if total else None,
        "p50_ms": round(_percentile(durations, 0.5)) if durations else 0,
        "p95_ms": round(_percentile(durations, 0.95)) if durations else 0,
        "by_category": {k: dict(v) for k, v in by_cat.items()},
        "by_channel": {k: dict(v) for k, v in by_channel.items()},
        "exporter": obs.current_mode(),
    }


@router.get("/api/telemetry/stages")
def telemetry_stages(
    hours: int = 24,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Per-event-name p50/p95/error-count rollup."""
    _require_auth(authorization)
    events = _read_events(hours)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_name[e.get("event") or "?"].append(e)

    out = []
    for name, evts in by_name.items():
        durs = [e["duration_ms"] for e in evts
                if e.get("duration_ms") is not None]
        fails = sum(1 for e in evts if not e.get("success"))
        out.append({
            "name": name,
            "count": len(evts),
            "failed": fails,
            "p50_ms": round(_percentile(durs, 0.5)) if durs else 0,
            "p95_ms": round(_percentile(durs, 0.95)) if durs else 0,
            "max_ms": int(max(durs)) if durs else 0,
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return {"hours": hours, "stages": out}


@router.get("/api/telemetry/services")
def telemetry_services(
    hours: int = 1,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Per-service / per-category rollup over the recent window.

    'Service' is a synthesis: if metadata.provider is set we use that
    (e.g. ``cloudrun_chatterbox``); otherwise we fall back to the
    event's category (``llm`` / ``upload`` / ``image`` / ``tts`` /
    ``render``). Gives the dashboard one coherent table without
    waiting on Cloud Monitoring's metric ingestion lag.
    """
    _require_auth(authorization)
    events = _read_events(hours)
    by_svc: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        md = e.get("metadata") or {}
        svc = md.get("provider") or e.get("category") or "?"
        by_svc[svc].append(e)
    out = []
    for svc, evts in by_svc.items():
        durs = [e["duration_ms"] for e in evts
                if e.get("duration_ms") is not None]
        fails = sum(1 for e in evts if not e.get("success"))
        out.append({
            "service": svc,
            "count": len(evts),
            "failed": fails,
            "error_rate": round(fails / len(evts) * 100, 1) if evts else 0,
            "p50_ms": round(_percentile(durs, 0.5)) if durs else 0,
            "p95_ms": round(_percentile(durs, 0.95)) if durs else 0,
            "last_seen_ts": max((e.get("ts") or 0) for e in evts),
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return {"hours": hours, "services": out}


# Stage-level events the dashboard cares about. Anything matching
# either the explicit set or the ``stage.*`` / ``render.*`` prefixes
# is a "render-pipeline" event whose duration belongs in the latency
# charts. Everything else (cache_hit, http requests, llm tokens,
# health probes) is excluded so the bar chart isn't dominated by
# 0-ms bookkeeping events.
_STAGE_EVENT_NAMES: set[str] = {
    "tts_synth", "image_gen", "llm_call",
    "asr_transcribe", "compose", "upload_short", "youtube_upload",
    "x_post", "post_short", "authenticate",
    "cast_author", "shotlist_author", "rewrite", "critic", "imitate",
}


def _is_stage_event(name: str | None) -> bool:
    if not name:
        return False
    if name in _STAGE_EVENT_NAMES:
        return True
    return name.startswith("stage.") or name.startswith("render.")


@router.get("/api/telemetry/stage_latency")
def telemetry_stage_latency(
    hours: int = 24,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Per-stage latency for the dashboard's "where is time being
    spent?" bar chart.

    Returns one row per distinct stage name with count, mean / p50 /
    p95 / max duration in ms. Sorted by p95 descending so the chart
    foregrounds the slowest stages — that's the operator's first
    "what should I optimise?" signal.

    Filters to `render-pipeline` events (`tts_synth`, `image_gen`,
    `llm_call`, `compose`, `upload_*`, plus anything matching
    ``stage.*`` / ``render.*``). Excludes background bookkeeping
    (`cache_hit`, `cloud.health.sweep`, etc.) so the chart actually
    answers "where did the render spend its time?".
    """
    _require_auth(authorization)
    events = _read_events(hours)
    by_stage: dict[str, list[int]] = defaultdict(list)
    fails: dict[str, int] = defaultdict(int)
    for e in events:
        name = e.get("event")
        if not _is_stage_event(name):
            continue
        d = e.get("duration_ms")
        if d is None:
            continue
        by_stage[name].append(int(d))
        if not e.get("success"):
            fails[name] += 1

    out = []
    for name, durs in by_stage.items():
        out.append({
            "stage": name,
            "count": len(durs),
            "failed": fails.get(name, 0),
            "mean_ms": round(sum(durs) / len(durs)) if durs else 0,
            "p50_ms": round(_percentile(durs, 0.5)) if durs else 0,
            "p95_ms": round(_percentile(durs, 0.95)) if durs else 0,
            "max_ms": int(max(durs)) if durs else 0,
            "total_ms": int(sum(durs)),
        })
    out.sort(key=lambda r: r["p95_ms"], reverse=True)
    return {"hours": hours, "stages": out}


@router.get("/api/telemetry/renders")
def telemetry_renders(
    hours: int = 24,
    limit: int = 30,
    channel: str | None = None,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Per-render breakdown — one row per (channel, slug, render_kind)
    with total duration AND per-stage time spent.

    This is the "how long did each video take + where did the time
    go?" view that turns `cloud.health.sweep`-only dashboards into
    something you can act on. Each row carries:

    * ``channel`` / ``slug`` / ``render_kind`` (short / long_form /
      footage_only / sports_doc).
    * ``total_ms`` — wall-clock duration of the ``render.<kind>``
      envelope span. Falls back to the sum of stage durations when
      the envelope event isn't in the window.
    * ``stages`` — ordered list of ``{name, duration_ms, success}``
      for every stage event tied to this render via
      ``metadata.channel + metadata.slug``.
    * ``success`` — false if ANY child stage or the envelope failed.
    * ``started_at`` / ``ended_at`` — earliest / latest stage ts.

    Sorted newest-first. ``limit`` caps the number of renders
    returned. Optional ``channel=`` filter narrows to one channel
    when the dashboard wants per-channel detail.
    """
    _require_auth(authorization)
    events = _read_events(hours)

    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for e in events:
        md = e.get("metadata") or {}
        ch = md.get("channel")
        slug = md.get("slug")
        rk = md.get("render_kind") or ""
        if not ch or not slug:
            continue
        if channel and ch != channel:
            continue
        if not _is_stage_event(e.get("event")) and \
                not str(e.get("event") or "").startswith("render."):
            continue
        grouped[(ch, slug, rk)].append(e)

    out = []
    for (ch, slug, rk), evts in grouped.items():
        envelope = next(
            (e for e in evts
             if str(e.get("event") or "").startswith("render.")),
            None,
        )
        stage_evts = [e for e in evts
                      if not str(e.get("event") or "").startswith("render.")]
        # Sort stages by start time (oldest first) for a stable
        # left-to-right reading order in the UI.
        stage_evts.sort(key=lambda e: e.get("ts") or 0)
        stages = []
        for e in stage_evts:
            stages.append({
                "name": e.get("event"),
                "duration_ms": e.get("duration_ms"),
                "success": bool(e.get("success", True)),
                "ts": e.get("ts"),
                "provider": (e.get("metadata") or {}).get("provider"),
            })
        sum_stage_ms = sum(int(s["duration_ms"] or 0) for s in stages)
        total_ms = (
            int(envelope["duration_ms"])
            if envelope and envelope.get("duration_ms") is not None
            else sum_stage_ms
        )
        all_success = all(e.get("success", True) for e in evts)
        ts_values = [e.get("ts") for e in evts if e.get("ts")]
        started_at = min(ts_values) if ts_values else None
        ended_at = max(ts_values) if ts_values else None

        out.append({
            "channel": ch,
            "slug": slug,
            "render_kind": rk or (envelope and (envelope.get("metadata") or {}).get("render_kind")) or "?",
            "total_ms": total_ms,
            "stage_total_ms": sum_stage_ms,
            "stages": stages,
            "success": all_success,
            "started_at": started_at,
            "ended_at": ended_at,
            "has_envelope": envelope is not None,
        })

    out.sort(key=lambda r: r["started_at"] or 0, reverse=True)
    out = out[:max(1, min(limit, 200))]
    return {"hours": hours, "renders": out}


@router.get("/api/telemetry/timeline")
def telemetry_timeline(
    hours: int = 24,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Per-bucket event counts for sparkline rendering.

    Bucket granularity scales with window: 1h → 1-min, 24h → 15-min,
    week+ → 1-hour. Keeps each response under a few KB.
    """
    _require_auth(authorization)
    events = _read_events(hours)
    if hours <= 1:
        bucket_s = 60
    elif hours <= 24:
        bucket_s = 15 * 60
    else:
        bucket_s = 3600
    now = time.time()
    n_buckets = max(1, int(hours * 3600 / bucket_s))
    series: list[dict] = []
    counts = [0] * n_buckets
    errors = [0] * n_buckets
    start = now - n_buckets * bucket_s
    for e in events:
        ts = e.get("ts") or 0
        if ts < start:
            continue
        i = min(int((ts - start) / bucket_s), n_buckets - 1)
        counts[i] += 1
        if not e.get("success"):
            errors[i] += 1
    for i in range(n_buckets):
        series.append({
            "bucket_start": int(start + i * bucket_s),
            "count": counts[i],
            "errors": errors[i],
        })
    return {"hours": hours, "bucket_s": bucket_s, "series": series}


@router.get("/api/telemetry/errors")
def telemetry_errors(
    hours: int = 24,
    limit: int = 50,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Most recent failures."""
    _require_auth(authorization)
    events = _read_events(hours)
    fails = [e for e in events if not e.get("success")]
    fails.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return {"hours": hours, "errors": fails[:max(1, min(limit, 200))]}


@router.get("/api/telemetry/links")
def telemetry_links(
    channel: str | None = None,
    slug: str | None = None,
    job_id: str | None = None,
    hours: int = 1,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Return deep-link URLs into Cloud Trace / Logging / Monitoring,
    pre-filtered to the active project + (optionally) channel + slug.

    The dashboard renders each as a one-click button. We compose them
    here (not in the Next.js page) so the project ID + filter syntax
    stay in one place.
    """
    _require_auth(authorization)
    project = _gcp_project()
    if not project:
        return {"available": False, "reason": "GOOGLE_CLOUD_PROJECT not set"}

    # Trace explorer — by service + custom attribute filter.
    trace_filter_parts = []
    if channel:
        trace_filter_parts.append(f"ytfactory.channel={channel}")
    if slug:
        trace_filter_parts.append(f"ytfactory.slug={slug}")
    if job_id:
        trace_filter_parts.append(f"ytfactory.job_id={job_id}")
    trace_query = "%20".join(
        urllib.parse.quote(p, safe="=") for p in trace_filter_parts
    )
    trace_url = (
        f"https://console.cloud.google.com/traces/list?project={project}"
        + (f"&tfilter={trace_query}" if trace_query else "")
    )

    # Logging — Cloud Logging Query syntax.
    log_filter_parts = ['logName=~"ytfactory"']
    if channel:
        log_filter_parts.append(
            f'jsonPayload."ytfactory.channel"="{channel}"',
        )
    if slug:
        log_filter_parts.append(
            f'jsonPayload."ytfactory.slug"="{slug}"',
        )
    if job_id:
        log_filter_parts.append(
            f'jsonPayload."ytfactory.job_id"="{job_id}"',
        )
    log_filter = " AND ".join(log_filter_parts)
    log_url = (
        "https://console.cloud.google.com/logs/query;"
        f"query={urllib.parse.quote(log_filter)}"
        f"?project={project}"
    )

    # Monitoring — Metrics Explorer with the ytfactory custom metrics.
    mon_url = (
        f"https://console.cloud.google.com/monitoring/metrics-explorer"
        f"?project={project}"
    )

    return {
        "available": True,
        "project": project,
        "channel": channel,
        "slug": slug,
        "job_id": job_id,
        "hours": hours,
        "links": {
            "trace": trace_url,
            "logging": log_url,
            "monitoring": mon_url,
        },
    }


__all__ = ["router"]
