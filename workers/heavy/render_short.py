"""RENDER_SHORT mega-task — wraps the existing make_shorts.py pipeline.

v1 approach: one task per Short, runs the entire pipeline on the laptop.
Rewrite + cast happen in-process (lightweight, claude CLI only). The
heavy rendering shells out to make_shorts.py so the agent process
doesn't have to import torch / diffusers / kokoro at module-load time.

A v2 fine-grained split (separate tasks per stage) is future work.

Payload shape (from chat_routes.confirm):
    job_id, channel, format, topic, source_kind, source_ref, length_s, notes
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent.runner import TaskContext, register
from control import storage
from control.queue import get_queue, new_task_id
from shared.schema import TaskEnvelope, TaskKind

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Mapping from chat-side channel keys → channel YAML path on disk.
# Update when the channel reorg (#4) lands; keys stay stable.
_CHANNEL_YAML: dict[str, str] = {
    "mystoriesanimated": "channels/mystoriesanimated.yaml",
    "sportstoriesanimated": "channels/sportstoriesanimated.yaml",
    "mahabharathindi": "channels/mahabharat_hindi.yaml",
    "auto": "channels/mystoriesanimated.yaml",  # default fallback
}


def _resolve_channel_yaml(channel_key: str) -> Path:
    rel = _CHANNEL_YAML.get(channel_key, _CHANNEL_YAML["auto"])
    return PROJECT_ROOT / rel


def _channel_dir_from_yaml(channel_yaml: Path) -> str:
    """data/intermediate/<channel_dir>/ — inferred from YAML stem.

    Matches the existing pipeline convention: mystoriesanimated.yaml →
    data/intermediate/mystoriesanimated/.
    """
    return channel_yaml.stem


def _build_raw(payload: dict[str, Any]) -> dict[str, Any]:
    """Synthesize a RawStory dict from the chat proposal payload.

    For source_kind in {"user_text", "auto"}: title=topic, body=notes (or topic).
    For reddit_url / wikipedia_topic / youtube_video: leave fetching to a
    later light worker (#9) — for v1 we accept user_text/auto as the path.
    """
    from sources.base import slugify  # noqa: PLC0415 — lightweight

    topic = (payload.get("topic") or "").strip()
    notes = (payload.get("notes") or "").strip()
    if not topic:
        raise ValueError("payload missing 'topic'")

    base_slug = slugify(topic, max_len=50)
    job_suffix = (payload.get("job_id") or "")[:8]
    slug = f"{base_slug}-{job_suffix}" if job_suffix else base_slug

    return {
        "slug": slug,
        "title": topic[:100],
        "body": notes if notes else topic,
        "source": f"chat:{payload.get('source_kind') or 'auto'}",
        "url": payload.get("source_ref") or "",
        "metadata": {
            "chat_job_id": payload.get("job_id", ""),
            "format": payload.get("format", "auto"),
            "length_s": payload.get("length_s", 55),
            "channel_key": payload.get("channel", "auto"),
        },
    }


async def _rewrite_and_cast(raw: dict, channel_yaml: Path, slug: str, channel_dir: str) -> tuple[Path, Path]:
    """Run rewrite + cast in parallel (both shell out to claude CLI)."""
    import yaml as _yaml  # noqa: PLC0415 — lightweight

    cfg = _yaml.safe_load(channel_yaml.read_text()) if channel_yaml.exists() else {}

    inter_root = PROJECT_ROOT / "data" / "intermediate" / channel_dir
    raw_path = inter_root / "raw" / f"{slug}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))

    script_path = inter_root / "scripts" / f"{slug}.json"
    cast_path = inter_root / "cast" / f"{slug}.json"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    cast_path.parent.mkdir(parents=True, exist_ok=True)

    from pipeline import cast as cast_mod, rewrite as rewrite_mod  # noqa: PLC0415

    async def _do_rewrite() -> None:
        script = await asyncio.to_thread(rewrite_mod.rewrite, raw, cfg)
        rewrite_mod.save_script(script, script_path)

    async def _do_cast() -> None:
        await asyncio.to_thread(
            cast_mod.author_cast,
            raw_story=raw, channel_cfg=cfg, out_path=cast_path,
        )

    await asyncio.gather(_do_rewrite(), _do_cast())
    return script_path, cast_path


async def _run_make_shorts(script_path: Path, channel_yaml: Path, log_path: Path) -> int:
    """Subprocess call to make_shorts.py. Streams output to log_path."""
    cmd = [
        sys.executable, str(PROJECT_ROOT / "make_shorts.py"),
        "--script", str(script_path),
        "--channel", str(channel_yaml),
    ]
    logger.info("running %s", " ".join(cmd))
    log_f = log_path.open("w", encoding="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
            env={**os.environ},
        )
        rc = await proc.wait()
        return rc
    finally:
        log_f.close()


def _find_outputs(slug: str, channel_dir: str) -> tuple[Path | None, Path | None]:
    """Return (mp4, thumb) paths if they exist on disk."""
    mp4 = PROJECT_ROOT / "data" / "shorts" / f"{slug}.mp4"
    # Thumb path varies by pipeline; check the common locations.
    for candidate in [
        PROJECT_ROOT / "data" / "intermediate" / channel_dir / "thumbs" / f"{slug}.png",
        PROJECT_ROOT / "data" / "intermediate" / channel_dir / "thumb" / f"{slug}.png",
        PROJECT_ROOT / "data" / "shorts" / f"{slug}.thumb.png",
    ]:
        if candidate.exists():
            return mp4 if mp4.exists() else None, candidate
    return mp4 if mp4.exists() else None, None


def _cleanup_intermediate(slug: str, channel_dir: str) -> None:
    """Best-effort: remove per-slug files in data/intermediate after success.

    Keeps the laptop's data/intermediate from accumulating per-render state.
    Model weights in data/cache and channel-level branding stay put.
    """
    inter_root = PROJECT_ROOT / "data" / "intermediate" / channel_dir
    for sub in ("raw", "scripts", "cast", "thumbs", "thumb", "voices"):
        for ext in (".json", ".png", ".wav"):
            p = inter_root / sub / f"{slug}{ext}"
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
    # Per-slug image cache.
    cache_dir = PROJECT_ROOT / "data" / "cache" / slug
    if cache_dir.exists():
        try:
            shutil.rmtree(cache_dir, ignore_errors=True)
        except OSError:
            pass
    # Per-slug shorts mp4 stays on disk briefly for inspection but goes
    # to GCS as the source of truth. Lifecycle GC is the laptop sweeper (#19).


async def render_short(ctx: TaskContext) -> str | None:
    payload = ctx.task.payload
    job_id = payload.get("job_id") or ctx.task.job_id

    raw = _build_raw(payload)
    slug = raw["slug"]
    channel_yaml = _resolve_channel_yaml(payload.get("channel", "auto"))
    channel_dir = _channel_dir_from_yaml(channel_yaml)

    logger.info("RENDER_SHORT job=%s slug=%s channel=%s", job_id, slug, channel_yaml.name)

    # 1. Rewrite + cast (claude CLI, in-process).
    script_path, cast_path = await _rewrite_and_cast(raw, channel_yaml, slug, channel_dir)

    # 2. Heavy render via subprocess.
    log_path = ctx.scratch / "make_shorts.log"
    rc = await _run_make_shorts(script_path, channel_yaml, log_path)
    if rc != 0:
        # Surface the last 40 lines of the log so Firestore.error has context.
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
        except OSError:
            pass
        raise RuntimeError(f"make_shorts.py exited {rc}\n--- last 40 log lines ---\n{tail}")

    # 3. Upload outputs to GCS.
    mp4_path, thumb_path = _find_outputs(slug, channel_dir)
    if mp4_path is None:
        raise RuntimeError(f"render reported success but no mp4 found at data/shorts/{slug}.mp4")

    short_uri = storage.job_uri(job_id, "short.mp4")
    storage.upload(mp4_path, short_uri, content_type="video/mp4")

    if thumb_path:
        storage.upload(thumb_path, storage.job_uri(job_id, "thumb.png"), content_type="image/png")

    storage.upload_bytes(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        storage.job_uri(job_id, "proposal.json"),
        content_type="application/json",
    )

    # 4. Wipe per-slug intermediates so the laptop disk stays bounded.
    _cleanup_intermediate(slug, channel_dir)

    # 5. Enqueue YOUTUBE_UPLOAD so the next stage takes over once acked.
    #    Light workers run on the laptop in v1 (same caps registry); they
    #    move to Cloud Run jobs when the migration finishes.
    handoff_payload = {**payload, "slug": slug, "short_uri": short_uri}
    get_queue().enqueue(TaskEnvelope(
        task_id=new_task_id(),
        job_id=job_id,
        kind=TaskKind.YOUTUBE_UPLOAD,
        payload=handoff_payload,
    ))

    logger.info("RENDER_SHORT done job=%s short=%s → enqueued YOUTUBE_UPLOAD", job_id, short_uri)
    return short_uri


register(TaskKind.RENDER_SHORT, render_short)
