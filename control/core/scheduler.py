"""Round-robin auto-scheduler for 24/7 Short production.

Every tick:
  1. Bail if ANY heavy task (RENDER_SHORT etc.) is queued or leased — only
     one short renders at a time so the M2 Max GPU never gets a second
     mflux/Z-Image-Turbo session and triggers a Metal OOM.
     (See historyrecapped/learnings/… and the broader
     feedback_gpu_one_render_at_a_time rule.)
  2. Round-robin across the 5 channels: pick the next channel after the
     last one we enqueued for. If that channel has no pending work, try
     the next, until we either find work or run out of channels.
  3. "Pending work" = a narration JSON with no matching upload record
     AND no recent successful RENDER for the same slug (the "topic
     uniqueness window" added 2026-05-14 to fix the 9-cake-AITA / 6-baghdad /
     5-ronaldinho duplicate-render bug from the 2026-05-13 audit).
     Looks at both top-level (`<channel>/narrations/<slug>.json`) AND
     niche-nested (`<channel>/<niche>/narrations/<slug>.json`) layouts.
  4. On match, enqueue a RENDER_SHORT task via control.core.jobs._enqueue_render_job.
     The agent on the laptop leases it, runs make_shorts.py, and ships
     to YouTube.

State (last_channel, last_slug, last_enqueued_at) persists in Firestore
collection ``scheduler_state`` (or in-process memory for the dev backend).

Cloud Scheduler triggers POST /api/scheduler/tick every 30 min.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from control.core.jobs import _enqueue_render_job
from control.core.queue import get_queue
from control.core.schema import HEAVY_KINDS, ShortProposal, TaskStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULER_STATE_DOC = "scheduler_state/main"

# Stable order for round-robin. Channels not in this list never get
# auto-scheduled even if they have a backlog.
#
# Source of truth: pipeline.channels.channel_rotation() — derived from
# the per-channel ``in_rotation`` flag in pipeline/channels.py:CHANNELS.
# When the user merged sportsrecapped + scrollpulse into
# sportstoriesanimated (2026-05-10), this list silently kept the old
# name and cron skipped the merged channel for a release. Single-source-
# of-truth via channels.py prevents that recurring.
from pipeline.channels import channel_rotation as _channel_rotation  # noqa: E402

CHANNEL_ROTATION: list[str] = _channel_rotation()

# How many heavy tasks the scheduler is willing to keep in pipeline
# (queued + leased). The agent still runs ONE at a time on the GPU
# (running 2 concurrent mflux sessions crashes Metal — per the
# `gpu_one_render_at_a_time` rule), but having a second task already
# leased-or-queued means zero idle gap between renders. Set higher if
# you ever add a second laptop/agent.
MAX_HEAVY_IN_FLIGHT = int(os.environ.get("YTFACTORY_MAX_HEAVY_IN_FLIGHT", "2"))

# Topic-uniqueness window for the dedupe guard added 2026-05-14.
# A topic that has been successfully rendered (status=done) within
# this many days is excluded from re-rendering. Set to 0 to disable
# (re-renders allowed without limit). Per the 2026-05-13 audit found
# 9 cake-AITA / 6 baghdad / 5 ronaldinho re-renders within 6 days
# because the only check was "is upload missing?" — never satisfied
# while uploads are paused. The window-based check is independent of
# upload status.
DEDUPE_WINDOW_DAYS = int(os.environ.get("YTFACTORY_TOPIC_DEDUPE_DAYS", "30"))

logger = logging.getLogger(__name__)


# ---------- Test-fixture topic detector --------------------------------
#
# Added 2026-05-14 after the 27-render audit found 2 jobs leaked to
# prod with obviously-internal topic names ('AITA descriptor-registry
# smoke test all knobs', 'AITA slice 4 + 5 verify'). These are dev
# fixtures that the agent runs to validate pipeline branches; they
# should never appear on the dashboard or get auto-uploaded.
#
# The detector below is regex-based and intentionally aggressive on
# false positives. Better to mark a real topic as internal_only
# (operator can flip the flag manually) than to ship a smoke-test to
# YouTube. False negatives (real test fixtures slipping through) are
# the actually-bad case the 2026-05-14 audit caught us on.

_TEST_FIXTURE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bsmoke[- ]?test\b", re.IGNORECASE),
    re.compile(r"\bdescriptor[- ]?registry\b", re.IGNORECASE),
    re.compile(r"\b(slice|verify|verification)\s+\d", re.IGNORECASE),
    re.compile(r"\bregress(ion)?\s+(test|fixture)\b", re.IGNORECASE),
    re.compile(r"\bdebug[- ](render|test|build)\b", re.IGNORECASE),
    re.compile(r"\b(internal|dev|debug|qa)[- ](only|fixture|test)\b", re.IGNORECASE),
    re.compile(r"\b(test|fixture)\s+all\s+knobs\b", re.IGNORECASE),
    # Slug forms — kebab-case with the same hints.
    re.compile(r"\b(smoke-test|debug-render|dev-fixture|qa-test)\b", re.IGNORECASE),
)


def is_test_fixture_topic(topic: str | None) -> bool:
    """True if ``topic`` looks like a dev / smoke-test fixture.

    Used by ``control/core/jobs.py::_enqueue_render_job`` to auto-flag
    proposals as ``internal_only=True`` so they don't reach the
    production renders dashboard or get auto-uploaded.

    Returns False for falsy inputs (None / "") — defensive default,
    a missing topic is a different bug class.
    """
    if not topic:
        return False
    return any(p.search(topic) for p in _TEST_FIXTURE_PATTERNS)


# ---------- Recently-rendered topic dedupe -----------------------------


def _firestore_client_safe():
    """Best-effort Firestore client; returns None if not configured.

    The scheduler's dedupe guard degrades gracefully when Firestore is
    unavailable (laptop dev without GOOGLE_CLOUD_PROJECT, unit tests
    without a real client) — we log + skip the dedupe rather than
    blocking renders entirely.
    """
    try:
        from google.cloud import firestore  # noqa: PLC0415
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            return None
        return firestore.Client(project=project)
    except Exception:  # noqa: BLE001
        logger.debug("scheduler dedupe: firestore client unavailable", exc_info=True)
        return None


def _recently_rendered_slugs(channel: str, *, since_days: int = DEDUPE_WINDOW_DAYS) -> set[str]:
    """Slugs (= proposal.topic) that have been rendered (status=done) for
    ``channel`` in the last ``since_days`` days.

    Returns an empty set if Firestore is unavailable OR ``since_days``
    is 0 (dedupe disabled). Errors are logged + swallowed — degrading
    to "no dedupe" is preferable to halting the scheduler tick when
    Firestore has a hiccup.

    Used by ``_next_unrendered`` to filter out narrations whose slug
    has already been successfully rendered recently. Critical fix for
    the 9-cake-AITA / 6-baghdad / 5-ronaldinho re-render bug surfaced
    by the 2026-05-13 audit.
    """
    if since_days <= 0:
        return set()
    cli = _firestore_client_safe()
    if cli is None:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    out: set[str] = set()
    try:
        # Query: all jobs for this channel, status=done, created within
        # the window. Project-wide collection group not needed; jobs
        # live in a flat collection.
        q = (
            cli.collection("jobs")
            .where("channel", "==", channel)
            .where("status", "==", "done")
            .where("created_at", ">=", cutoff)
        )
        for doc in q.stream():
            d = doc.to_dict() or {}
            topic = d.get("topic")
            if topic:
                out.add(topic)
    except Exception:  # noqa: BLE001
        logger.warning(
            "scheduler dedupe: firestore query failed for channel=%s; "
            "proceeding without dedupe (may re-render recent topics)",
            channel, exc_info=True,
        )
    return out

_MEM_STATE: dict = {}


def _backend() -> str:
    return os.environ.get("YTFACTORY_QUEUE_BACKEND", "firestore")


# -------- in-flight check ----------------------------------------------------

def _count_in_flight_heavy() -> int:
    """How many HEAVY tasks (queued or leased) are in the queue right now."""
    if _backend() == "memory":
        q = get_queue()
        return sum(
            1 for t in getattr(q, "_tasks", {}).values()
            if t.kind in HEAVY_KINDS and t.status in (TaskStatus.QUEUED, TaskStatus.LEASED)
        )

    from google.cloud import firestore  # noqa: PLC0415
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    col = client.collection("tasks")
    heavy_kind_values = [k.value for k in HEAVY_KINDS]
    n = 0
    for status_val in (TaskStatus.QUEUED.value, TaskStatus.LEASED.value):
        q = (col.where("status", "==", status_val)
                .where("kind", "in", heavy_kind_values)
                .limit(MAX_HEAVY_IN_FLIGHT + 1))
        n += sum(1 for _ in q.stream())
        if n >= MAX_HEAVY_IN_FLIGHT:
            break
    return n


def _has_in_flight_heavy() -> bool:
    """Back-compat shim — True iff at least 1 heavy task is in flight."""
    return _count_in_flight_heavy() > 0


# -------- backlog walk -------------------------------------------------------

def _state_bucket() -> str | None:
    """GCS bucket holding per-channel narrations / uploads when running
    in cloud. Empty when on laptop (filesystem fallback)."""
    return os.environ.get("YTFACTORY_STATE_BUCKET") or None


def _gcs_client():
    from google.cloud import storage  # noqa: PLC0415
    return storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3"))


def _uploaded_slugs(channel_dir: Path) -> set[str]:
    """All slug stems that already have an upload record.

    On cloud: lists `gs://<state-bucket>/<channel>/uploads/*.json` and
    `gs://<state-bucket>/<channel>/<niche>/uploads/*.json`.
    On laptop: globs both flat and niched layouts via the recursive
    `<channel>/**/uploads/*.json` pattern. Skips `*.x.json` X sidecars.
    """
    bucket = _state_bucket()
    if bucket:
        return _uploaded_slugs_gcs(bucket, channel_dir.name)
    out: set[str] = set()
    for f in channel_dir.rglob("uploads/*.json"):
        if f.name.endswith(".x.json"):
            continue
        out.add(f.stem)
    return out


def _uploaded_slugs_gcs(bucket: str, channel: str) -> set[str]:
    out: set[str] = set()
    try:
        cli = _gcs_client()
        for blob in cli.list_blobs(bucket, prefix=f"{channel}/"):
            # Match <channel>/uploads/<slug>.json AND
            # <channel>/<niche>/uploads/<slug>.json — anything ending in
            # /uploads/<slug>.json with no other intermediate uploads dir.
            parts = blob.name.split("/")
            if len(parts) >= 3 and parts[-2] == "uploads" and blob.name.endswith(".json"):
                if blob.name.endswith(".x.json"):
                    continue
                out.add(Path(parts[-1]).stem)
    except Exception:
        logger.warning("scheduler: GCS uploads list failed", exc_info=True)
    return out


def _next_unrendered(channel: str) -> Optional[Tuple[str, Path]]:
    """Oldest unrendered narration for ``channel``, or None.

    Returns (slug, narration_path). "Unrendered" = the narration JSON
    exists but no upload record under <channel>/uploads/**/<slug>.json.
    Looks at <channel>/narrations/ AND <channel>/<niche>/narrations/.

    On cloud, sources both the narration list and upload list from
    `gs://$YTFACTORY_STATE_BUCKET/`. The returned path is a
    `gs://...` URI wrapped as Path so downstream consumers can stringify
    it; the actual narration content is fetched by the render-worker.
    """
    bucket = _state_bucket()
    if bucket:
        return _next_unrendered_gcs(bucket, channel)

    chan_dir = PROJECT_ROOT / channel
    if not chan_dir.exists():
        return None

    uploaded = _uploaded_slugs(chan_dir)
    # Topic-uniqueness window — also exclude slugs that have a recent
    # successful render (regardless of upload status). Fixes the
    # 9-cake-AITA / 6-baghdad / 5-ronaldinho re-render bug from the
    # 2026-05-13 audit (uploads were paused so the upload-only check
    # never fired). See ``DEDUPE_WINDOW_DAYS``.
    recently_rendered = _recently_rendered_slugs(channel)

    # Top-level + niche-nested narration buckets.
    candidates: list[tuple[str, Path]] = []
    top_narr = chan_dir / "narrations"
    if top_narr.exists():
        for f in top_narr.glob("*.json"):
            if f.stem in uploaded or f.stem in recently_rendered:
                continue
            candidates.append((f.stem, f))
    for niche_narr in chan_dir.glob("*/narrations/*.json"):
        if niche_narr.stem in uploaded or niche_narr.stem in recently_rendered:
            continue
        candidates.append((niche_narr.stem, niche_narr))

    if not candidates:
        return None
    # Oldest first (FIFO = curator submitted first goes first).
    candidates.sort(key=lambda x: x[1].stat().st_mtime)
    return candidates[0]


def _next_unrendered_gcs(bucket: str, channel: str) -> Optional[Tuple[str, Path]]:
    """GCS-backed equivalent of _next_unrendered."""
    try:
        cli = _gcs_client()
        uploaded = _uploaded_slugs_gcs(bucket, channel)
        # Topic-uniqueness window — see _next_unrendered for rationale.
        recently_rendered = _recently_rendered_slugs(channel)
        candidates: list[tuple[str, str, float]] = []  # (slug, gs_uri, mtime)
        for blob in cli.list_blobs(bucket, prefix=f"{channel}/"):
            parts = blob.name.split("/")
            # Match <channel>/narrations/<slug>.json AND
            # <channel>/<niche>/narrations/<slug>.json.
            if len(parts) >= 3 and parts[-2] == "narrations" and blob.name.endswith(".json"):
                slug = Path(parts[-1]).stem
                if slug in uploaded or slug in recently_rendered:
                    continue
                ts = blob.updated.timestamp() if blob.updated else 0.0
                candidates.append((slug, f"gs://{bucket}/{blob.name}", ts))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[2])  # oldest first
        slug, uri, _ = candidates[0]
        return (slug, Path(uri))
    except Exception:
        logger.warning("scheduler: GCS narrations list failed", exc_info=True)
        return None


# -------- state I/O ----------------------------------------------------------

def _read_state() -> dict:
    if _backend() == "memory":
        return dict(_MEM_STATE)
    from google.cloud import firestore  # noqa: PLC0415
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    snap = client.collection("scheduler_state").document("main").get()
    return (snap.to_dict() or {}) if snap.exists else {}


def read_state() -> dict:
    """Public alias for ``_read_state``.

    Audit S1.9 — cross-module callers (``control/routes/scheduler_routes.py``)
    were reaching into the underscore-prefixed helper, which is brittle:
    a refactor that renames or restructures the state-read path would
    silently break those importers. The public alias gives external
    callers a stable name; the underscore version stays as the internal
    in-module entry point so existing tests that monkey-patch
    ``scheduler._read_state`` keep working.
    """
    return _read_state()


def _save_state(state: dict) -> None:
    if _backend() == "memory":
        _MEM_STATE.clear()
        _MEM_STATE.update(state)
        return
    from google.cloud import firestore  # noqa: PLC0415
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    client.collection("scheduler_state").document("main").set(state, merge=True)


# -------- public API ---------------------------------------------------------

def tick() -> dict:
    """Run one scheduler tick. Returns a small dict describing what
    happened — surfaced in the /api/scheduler/tick HTTP response.

    Pipeline depth is bounded by ``MAX_HEAVY_IN_FLIGHT`` (default 2):
    queue stays topped up so the agent has the next task waiting the
    instant it finishes the current one, but never enough in flight
    to make a second GPU process try to grab Metal.
    """
    in_flight = _count_in_flight_heavy()
    if in_flight >= MAX_HEAVY_IN_FLIGHT:
        return {
            "action": "skipped",
            "reason": "pipeline_full",
            "in_flight": in_flight,
            "max_in_flight": MAX_HEAVY_IN_FLIGHT,
            "explain": (
                f"{in_flight} heavy task(s) already queued/leased — capped at "
                f"{MAX_HEAVY_IN_FLIGHT} so the GPU doesn't OOM. Tick again when one finishes."
            ),
        }

    state = _read_state()
    last_channel = state.get("last_channel", "")
    if last_channel in CHANNEL_ROTATION:
        start = (CHANNEL_ROTATION.index(last_channel) + 1) % len(CHANNEL_ROTATION)
    else:
        start = 0

    tried: list[str] = []
    for offset in range(len(CHANNEL_ROTATION)):
        ch = CHANNEL_ROTATION[(start + offset) % len(CHANNEL_ROTATION)]
        tried.append(ch)
        work = _next_unrendered(ch)
        if work is None:
            continue
        slug, narr_path = work
        logger.info("scheduler enqueueing channel=%s slug=%s", ch, slug)
        # narr_path is either a local repo-relative path (laptop dev)
        # or a gs:// URI (cloud — _next_unrendered_gcs wraps the URI
        # in Path(), which mangles it slightly but the round-tripped
        # str still starts with "gs:/"). Don't relative_to() in the
        # cloud case because gs:// URIs aren't repo subpaths.
        narr_str = str(narr_path)
        if narr_str.startswith("gs:/"):
            source_ref = narr_str  # GCS URI as-is
        else:
            source_ref = str(narr_path.relative_to(PROJECT_ROOT))
        proposal = ShortProposal(
            channel=ch,
            format="auto",
            topic=slug,
            source_kind="manual_backlog",
            source_ref=source_ref,
            length_s=55,
            notes="auto-scheduled from narration backlog",
        )
        resp = _enqueue_render_job(proposal)
        state["last_channel"] = ch
        state["last_slug"] = slug
        state["last_enqueued_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        return {
            "action": "enqueued",
            "channel": ch,
            "slug": slug,
            "job_id": resp.job_id,
            "task_id": resp.task_id,
            "tried": tried,
        }

    return {
        "action": "skipped",
        "reason": "no_unrendered_scripts",
        "explain": "No channel has a narration without a matching upload record. Author more scripts.",
        "tried": tried,
    }
