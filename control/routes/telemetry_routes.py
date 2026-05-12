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
* ``GET /api/telemetry/jobs?hours=24&limit=100&status=...&channel=...``
  — Firestore-backed per-job view. Sees jobs the Cloud Logging
  /renders view CANNOT — including dispatch failures (gcloud / IAM /
  quota errors before the worker ever ran), cancelled jobs, and jobs
  whose worker crashed before emitting any ``obs.timed()`` event.
  Together with /renders this gives the operator full ground-truth.
* ``GET /api/telemetry/llm_costs?hours=24`` — aggregate llm_call
  events grouped by tier and backend. Tracks calls / failed /
  input_tokens / output_tokens / mean+p95 latency. Catches cost
  regressions when the dispatcher routes to a paid SDK instead of
  the free CLI.
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


# ---- /api/telemetry/jobs (Firestore-backed control-plane view) ----------


def _synthesize_stage_durations(timeline: list[dict] | None) -> list[dict]:
    """Build a render-section-shaped stages list from the Firestore
    job's timeline.

    The cloud render-worker's `_set_stage` writes one entry per stage
    transition with `ts` (ISO-8601 UTC). Sequential `ts` deltas give
    us per-stage durations even for jobs that ran BEFORE the
    Cloud-Run-shaped JSON log exporter was deployed (i.e. before
    Cloud Logging had any structured `ytfactory.event` records).

    Output shape matches what `/api/telemetry/renders` returns for
    each `stages[]` entry, so the dashboard's `<StageWaterfall />`
    component can render Firestore-backfilled jobs identically to
    Cloud-Logging-backed ones.
    """
    if not timeline or not isinstance(timeline, list):
        return []
    rows: list[dict] = []
    # Pair each `done` event with the most recent `running` event for
    # the same stage. Falls back to "ts only" when only one phase was
    # captured (legacy dispatcher writes that didn't include both).
    last_running_ts: dict[str, float] = {}
    for entry in timeline:
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        status = entry.get("status")
        ts_iso = entry.get("ts")
        ts = _parse_iso(str(ts_iso)) if ts_iso else None
        if not stage or ts is None:
            continue
        if status == "running":
            last_running_ts[stage] = ts
        elif status in ("done", "failed", "skipped"):
            start = last_running_ts.pop(stage, None)
            duration_ms = int((ts - start) * 1000) if start else None
            rows.append({
                "name": f"stage.{stage}",
                "duration_ms": duration_ms,
                "success": status != "failed",
                "ts": ts,
                "provider": entry.get("msg"),
            })
    # Surface stages still in-flight at last update so an in-flight
    # render's waterfall isn't empty.
    for stage, start in last_running_ts.items():
        rows.append({
            "name": f"stage.{stage}",
            "duration_ms": None,
            "success": True,
            "ts": start,
            "provider": "in_flight",
        })
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def _job_doc_to_view(job_id: str, doc: dict) -> dict:
    """Distill a Firestore job doc to the fields the dashboard needs.

    Source of truth for "what did the operator submit + how did it
    end up?". This is DISTINCT from the Cloud Logging /renders view
    above, which only sees jobs that actually emitted ``obs.timed()``
    events from inside the worker. Jobs that failed during DISPATCH
    (gcloud command rejected, IAM denied, quota exceeded) never run
    a render envelope and are invisible to /renders — but they ARE
    visible here because Firestore is the control-plane source of
    truth.

    Includes synthesized stage durations from the doc's ``timeline``
    array so historical jobs (predating the Cloud-Run-shaped JSON log
    exporter) come back with a renderable stage waterfall.
    """
    created = doc.get("created_at")
    updated = doc.get("updated_at")
    duration_ms: int | None = None
    if created and updated:
        try:
            # Firestore SDK returns datetime; raw REST returns ISO str.
            created_ts = created.timestamp() if hasattr(created, "timestamp") \
                else _parse_iso(str(created))
            updated_ts = updated.timestamp() if hasattr(updated, "timestamp") \
                else _parse_iso(str(updated))
            if created_ts and updated_ts:
                duration_ms = max(0, int((updated_ts - created_ts) * 1000))
        except Exception:  # noqa: BLE001
            pass

    proposal = doc.get("proposal") or {}
    timeline = doc.get("timeline") or []
    stages = _synthesize_stage_durations(timeline)
    stage_total_ms = sum(int(s["duration_ms"] or 0) for s in stages)
    return {
        "job_id": job_id,
        "channel": doc.get("channel") or proposal.get("channel"),
        "topic": doc.get("topic") or proposal.get("topic"),
        "status": doc.get("status", "pending"),
        "stage": doc.get("stage"),
        "render_kind": (doc.get("render_spec") or {}).get("kind")
            or proposal.get("render_kind") or proposal.get("format"),
        "error": doc.get("error"),
        "created_at": str(created) if created else None,
        "updated_at": str(updated) if updated else None,
        "duration_ms": duration_ms,
        "stage_total_ms": stage_total_ms,
        "stages": stages,
        "youtube_url": doc.get("youtube_url"),
        "cloud_execution": doc.get("cloud_execution"),
    }


def _parse_iso(s: str) -> float | None:
    """Lenient ISO-8601 → unix seconds; returns None on parse failure."""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


@router.get("/api/telemetry/jobs")
def telemetry_jobs(
    hours: int = 24,
    limit: int = 100,
    status: str | None = None,
    channel: str | None = None,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Firestore-backed per-job view over the requested window.

    Sees jobs the Cloud Logging /renders view CANNOT — including
    dispatch failures (gcloud / IAM / quota errors before the worker
    ever ran), cancelled jobs, and jobs whose worker crashed before
    emitting any ``obs.timed()`` event. Together with /renders this
    gives the operator full ground-truth: /jobs answers "which jobs
    did I submit and what was their final status?", /renders answers
    "for the jobs that actually ran, where did the time go?".

    Each row also includes a ``stages`` array synthesised from the
    Firestore job's ``timeline`` field — sequential
    ``{stage, status: "running"|"done"|"failed", ts}`` entries are
    paired into per-stage durations. This means historical jobs
    (predating the Cloud-Run-shaped JSON log exporter) come back
    with a renderable stage waterfall, NOT just a status badge —
    full backfill of operator-visible render history.

    Returns one row per Firestore job doc, sorted newest-first by
    updated_at. Optional filters:
    * ``status=failed|done|cancelled|pending|rendering|uploading`` —
      narrow to one outcome.
    * ``channel=mystoriesanimated|...`` — narrow to one channel.
    * ``hours`` — only jobs whose created_at >= now - hours.
      ``hours=0`` returns ALL jobs (no time filter) — capped by
      ``limit`` so a runaway query can't blow the response.
    * ``limit`` — caps the row count (default 100, max 1000).

    Per-job rollup carries: status / stage / channel / topic /
    render_kind / error / wall-clock duration / youtube_url when
    published / synthesised stage waterfall.
    """
    _require_auth(authorization)
    project = _gcp_project()
    rows: list[dict] = []
    error: str | None = None
    if not project:
        return {"hours": hours, "jobs": [], "error": "GOOGLE_CLOUD_PROJECT not set"}

    try:
        from datetime import datetime, timezone
        from google.cloud import firestore  # noqa: PLC0415
        db = firestore.Client(project=project)
        capped_limit = min(max(1, limit), 1000)
        # ``hours=0`` = "give me everything" backfill mode. Skip the
        # created_at filter and rely on the limit + reverse sort to
        # bound the response. Useful for one-shot dashboard backfills
        # ("show me all 100 historical jobs even though most are
        # >24h old").
        q = db.collection("jobs")
        if hours > 0:
            cutoff = datetime.fromtimestamp(
                time.time() - hours * 3600, tz=timezone.utc,
            )
            q = q.where("created_at", ">=", cutoff)
        # Single-field sort uses Firestore's auto-created index.
        q = q.order_by("created_at", direction=firestore.Query.DESCENDING) \
             .limit(capped_limit)
        for snap in q.stream():
            doc = snap.to_dict() or {}
            view = _job_doc_to_view(snap.id, doc)
            if status and view["status"] != status:
                continue
            if channel and view["channel"] != channel:
                continue
            rows.append(view)
    except Exception as e:  # noqa: BLE001
        logger.warning("telemetry_jobs Firestore query failed: %s", e)
        error = f"{type(e).__name__}: {str(e)[:200]}"

    return {"hours": hours, "jobs": rows, "error": error}


@router.get("/api/telemetry/llm_costs")
def telemetry_llm_costs(
    hours: int = 24,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Aggregate llm_call events for the LLM cost / usage chart.

    Reads ``llm_call`` events from the same source ``read_events()``
    consults (in-process buffer for laptop, Cloud Logging for cloud).
    Each event has metadata: ``model``, ``tier``, ``input_tokens``,
    ``output_tokens``, ``backend`` (cli / azure_openai / anthropic_sdk).

    Returns:
    * ``totals`` — total calls, failures, total input/output tokens
      across the window.
    * ``by_tier`` — per-tier rollup (small / medium / large): calls,
      failed, input_tokens, output_tokens, mean_latency_ms,
      p95_latency_ms.
    * ``by_backend`` — same shape grouped by backend (helps catch
      cost regressions when the dispatcher routes to a paid SDK
      instead of the free CLI).
    """
    _require_auth(authorization)
    events = [e for e in _read_events(hours) if e.get("event") == "llm_call"]

    def _zero_row() -> dict:
        return {
            "calls": 0, "failed": 0,
            "input_tokens": 0, "output_tokens": 0,
            "_durs": [],
        }

    by_tier: dict[str, dict] = defaultdict(_zero_row)
    by_backend: dict[str, dict] = defaultdict(_zero_row)
    totals = _zero_row()

    for e in events:
        md = e.get("metadata") or {}
        tier = md.get("tier") or "unknown"
        backend = md.get("backend") or "unknown"
        success = bool(e.get("success", True))
        in_t = int(md.get("input_tokens") or 0)
        out_t = int(md.get("output_tokens") or 0)
        d = e.get("duration_ms")
        for bucket in (totals, by_tier[tier], by_backend[backend]):
            bucket["calls"] += 1
            if not success:
                bucket["failed"] += 1
            bucket["input_tokens"] += in_t
            bucket["output_tokens"] += out_t
            if d is not None:
                bucket["_durs"].append(int(d))

    def _finalise(row: dict) -> dict:
        durs = row.pop("_durs")
        row["mean_latency_ms"] = round(sum(durs) / len(durs)) if durs else 0
        row["p95_latency_ms"] = round(_percentile(durs, 0.95)) if durs else 0
        return row

    return {
        "hours": hours,
        "totals": _finalise(totals),
        "by_tier": {k: _finalise(v) for k, v in by_tier.items()},
        "by_backend": {k: _finalise(v) for k, v in by_backend.items()},
    }


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
