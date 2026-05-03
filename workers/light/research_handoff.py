"""RESEARCH_HANDOFF light worker.

Once a Short is published to YouTube, this worker hands the video off to
the research / analytics pipeline. After this point the source media
artifacts are GC'd from the bucket and only the YouTube video itself
exists; the research pipeline pulls stats (views, retention, comments)
from the YouTube Data API on a separate cron schedule.

This worker is intentionally thin — the heavy lifting lives in
pipeline/research.py and pipeline/youtube_stats.py (built by the
research-dashboard effort in parallel). We just:

1. Persist the video metadata so future research runs can find it.
2. Trigger pipeline.research.rebuild() to refresh the flat index.
3. Optionally fetch initial stats via pipeline.youtube_stats.

If pipeline.research / pipeline.youtube_stats aren't available (or have
moved), we log and no-op rather than fail the chain.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from workers.agent.runner import TaskContext, register
from control.schema import TaskKind

logger = logging.getLogger(__name__)


async def research_handoff(ctx: TaskContext) -> str | None:
    payload: dict[str, Any] = ctx.task.payload
    job_id = payload.get("job_id") or ctx.task.job_id
    video_id = payload.get("youtube_video_id")
    if not video_id:
        logger.warning("RESEARCH_HANDOFF job=%s missing youtube_video_id; no-op", job_id)
        return None

    # 1. Firestore: youtube_videos/<video_id> with the handoff metadata.
    # 2. Refresh the research dashboard index (best-effort).
    # 3. Pull initial stats so the dashboard has something to show
    #    before the next cron run.
    #
    # YT is already published — we MUST NOT raise out of this worker just
    # because a research-side helper blew up. Wrap each step defensively
    # so a single broken helper doesn't requeue the published video.
    for step in (lambda: _record_handoff(payload),
                 lambda: _try_rebuild_research_index(),
                 lambda: _try_fetch_initial_stats(video_id)):
        try:
            step()
        except Exception:  # noqa: BLE001
            logger.warning("research-handoff step failed (ignored)", exc_info=True)

    return f"https://youtu.be/{video_id}"


def _record_handoff(payload: dict[str, Any]) -> None:
    if os.environ.get("YTFACTORY_QUEUE_BACKEND", "memory").lower() != "firestore":
        return
    try:
        from google.cloud import firestore  # noqa: PLC0415
        db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
        video_id = payload["youtube_video_id"]
        db.collection("youtube_videos").document(video_id).set({
            "video_id": video_id,
            "youtube_url": payload.get("youtube_url"),
            "channel": payload.get("channel"),
            "topic": payload.get("topic"),
            "slug": payload.get("slug"),
            "job_id": payload.get("job_id"),
            "handoff_at": datetime.now(timezone.utc),
        }, merge=True)
    except Exception:  # noqa: BLE001
        logger.warning("research-handoff Firestore write failed", exc_info=True)


def _try_rebuild_research_index() -> None:
    """pipeline.research.rebuild() refreshes the flat index used by /api/research/*."""
    try:
        from pipeline import research as _research  # noqa: PLC0415

        rebuild = getattr(_research, "rebuild", None)
        if rebuild is None:
            logger.debug("pipeline.research has no rebuild(); skipping")
            return
        rebuild(quiet=True)
    except Exception:  # noqa: BLE001
        logger.warning("pipeline.research.rebuild failed", exc_info=True)


def _try_fetch_initial_stats(video_id: str) -> None:
    """pipeline.youtube_stats.fetch_all() pulls live counters from YT Data API."""
    try:
        from pipeline import youtube_stats as _yt  # noqa: PLC0415

        fetch_all = getattr(_yt, "fetch_all", None)
        if fetch_all is None:
            logger.debug("pipeline.youtube_stats has no fetch_all(); skipping")
            return
        # Fetch_all is bulk; pass a single-video filter if it supports one.
        try:
            fetch_all(video_ids=[video_id])  # if supported
        except TypeError:
            fetch_all()  # fallback to bulk refresh
    except Exception:  # noqa: BLE001
        logger.warning("pipeline.youtube_stats.fetch_all failed", exc_info=True)


register(TaskKind.RESEARCH_HANDOFF, research_handoff)
