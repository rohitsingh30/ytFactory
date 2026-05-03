"""YOUTUBE_UPLOAD light worker.

Downloads the rendered Short from GCS, uploads it to YouTube via the
existing pipeline.upload module, and on success:

1. Records the YouTube video URL on the job doc in Firestore.
2. Calls storage.gc_heavy_artifacts(job_id) — wipes beats/, voice.wav,
   captions.srt, prompts.json, script.json, cast.json, footage/ from
   the bucket. Lifecycle rules are a backstop; this is the explicit GC
   the user asked for.
3. Enqueues a RESEARCH_HANDOFF task so the published video moves into
   the analytics-only pipeline.

Payload from the predecessor RENDER_SHORT task:
    job_id, channel, format, topic, source_kind, source_ref, length_s,
    notes, slug (added by render_short), short_uri (gs://... mp4)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent.runner import TaskContext, register
from control import storage
from control.queue import get_queue, new_task_id
from shared.schema import TaskEnvelope, TaskKind

logger = logging.getLogger(__name__)


def _fail(msg: str) -> None:
    raise RuntimeError(msg)


async def youtube_upload(ctx: TaskContext) -> str | None:
    payload: dict[str, Any] = ctx.task.payload
    job_id = payload.get("job_id") or ctx.task.job_id
    short_uri: str | None = payload.get("short_uri")
    if not short_uri:
        _fail("payload missing 'short_uri' (the rendered mp4 in GCS)")

    # 1. Pull the mp4 from GCS to scratch.
    local_mp4 = ctx.scratch / "short.mp4"
    storage.download(short_uri, local_mp4)
    logger.info("YOUTUBE_UPLOAD job=%s downloaded %s → %s", job_id, short_uri, local_mp4)

    # 2. Upload to YouTube via the existing module.
    # pipeline.upload.upload_video has the auth + retry logic already.
    from pipeline import upload as upload_mod  # noqa: PLC0415 — lazy

    title = payload.get("topic") or "Untitled"
    description = payload.get("notes") or ""
    channel_key = payload.get("channel", "auto")

    video_id = await _do_youtube_upload(upload_mod, local_mp4, title, description, channel_key)
    youtube_url = f"https://youtu.be/{video_id}"
    logger.info("YOUTUBE_UPLOAD job=%s published %s", job_id, youtube_url)

    # 3. Update the job doc in Firestore.
    _record_published(job_id, video_id, youtube_url)

    # 4. Post-upload GC — explicit deletion of heavy intermediates.
    deleted = storage.gc_heavy_artifacts(job_id)
    logger.info("YOUTUBE_UPLOAD job=%s gc'd %d objects", job_id, len(deleted))

    # 5. Hand off to the research/analytics pipeline.
    handoff = TaskEnvelope(
        task_id=new_task_id(),
        job_id=job_id,
        kind=TaskKind.RESEARCH_HANDOFF,
        payload={
            "job_id": job_id,
            "youtube_video_id": video_id,
            "youtube_url": youtube_url,
            "channel": channel_key,
            "topic": payload.get("topic"),
            "slug": payload.get("slug"),
        },
    )
    get_queue().enqueue(handoff)

    return youtube_url


async def _do_youtube_upload(upload_mod, mp4_path: Path, title: str, description: str, channel_key: str) -> str:
    """Wrap pipeline.upload to return the YouTube video id.

    The existing module is called from the legacy CLI; surface its
    `upload_video` (or equivalent) and propagate exceptions as failures.
    """
    import asyncio  # noqa: PLC0415

    # The existing module's exact API may be sync; run in a thread.
    fn = getattr(upload_mod, "upload_video", None)
    if fn is None:
        _fail("pipeline.upload has no upload_video(); cannot publish")
    video_id = await asyncio.to_thread(
        fn,
        str(mp4_path),
        title=title,
        description=description,
        channel=channel_key,
    )
    if not video_id:
        _fail("pipeline.upload.upload_video returned empty video_id")
    return str(video_id)


def _record_published(job_id: str, video_id: str, youtube_url: str) -> None:
    """Best-effort write to Firestore jobs/<job_id>. Never fail the worker on this."""
    import os  # noqa: PLC0415
    if os.environ.get("YTFACTORY_QUEUE_BACKEND", "memory").lower() != "firestore":
        return  # tests or local dev — nothing to write
    try:
        from datetime import datetime, timezone  # noqa: PLC0415
        from google.cloud import firestore  # noqa: PLC0415

        db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
        db.collection("jobs").document(job_id).set({
            "status": "done",
            "youtube_video_id": video_id,
            "youtube_url": youtube_url,
            "published_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }, merge=True)
    except Exception:  # noqa: BLE001
        logger.warning("failed to record published job %s in Firestore", job_id, exc_info=True)


register(TaskKind.YOUTUBE_UPLOAD, youtube_upload)
