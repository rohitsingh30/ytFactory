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

from workers.agent.runner import TaskContext, register
from control import jobs as jobs_mod
from control import storage
from control.queue import get_queue, new_task_id
from control.schema import TaskEnvelope, TaskKind

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Mapping from chat-side channel keys → channel YAML path on disk.
# Update when the channel reorg (#4) lands; keys stay stable.
_CHANNEL_YAML: dict[str, str] = {
    "mystoriesanimated": "mystoriesanimated/config.yaml",
    "sportstoriesanimated": "sportstoriesanimated/config.yaml",
    "historyrecapped": "historyrecapped/config.yaml",
    "mahabharathindi": "hindutavaanimated/config.yaml",  # legacy, kept for back-compat
    "auto": "mystoriesanimated/config.yaml",  # default fallback
}


def _resolve_channel_yaml(channel_key: str) -> Path:
    rel = _CHANNEL_YAML.get(channel_key, _CHANNEL_YAML["auto"])
    return PROJECT_ROOT / rel


def _channel_dir_from_yaml(channel_yaml: Path) -> str:
    """data/intermediate/<channel_dir>/ — inferred from YAML stem.

    Matches the existing pipeline convention: mystoriesanimated.yaml →
    mystoriesanimated/.
    """
    return channel_yaml.stem


def _build_raw(payload: dict[str, Any]) -> dict[str, Any]:
    """Synthesize a RawStory dict from the chat proposal payload.

    For source_kind in {"user_text", "auto"}: title=topic, body=notes (or topic).
    For reddit_url / wikipedia_topic / youtube_video: leave fetching to a
    later light worker (#9) — for v1 we accept user_text/auto as the path.
    """
    from pipeline.sources.base import slugify  # noqa: PLC0415 — lightweight

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
    """Run rewrite + cast in parallel (both shell out to claude CLI).

    Writes per-slug intermediates (raw / narration / cast) to the
    canonical per-channel layout via :class:`pipeline.paths.RenderPaths`.
    Replaces the legacy ``data/intermediate/<channel_dir>/{raw,scripts,cast}/``
    location AND fixes the ``scripts`` → ``narrations`` rename gap that
    the 2026-05-03 reorg started but didn't finish on the writer side.
    """
    import yaml as _yaml  # noqa: PLC0415 — lightweight
    from pipeline.paths import RenderPaths, Subdir  # noqa: PLC0415

    cfg = _yaml.safe_load(channel_yaml.read_text()) if channel_yaml.exists() else {}
    paths = RenderPaths.from_channel_dir(channel_dir, project_root=PROJECT_ROOT)
    paths.ensure_dirs(Subdir.RAW, Subdir.NARRATIONS, Subdir.CAST)

    raw_path = paths.raw_for(slug)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))

    script_path = paths.narration_for(slug)
    cast_path = paths.cast_for(slug)

    from pipeline.llm import cast as cast_mod, rewrite as rewrite_mod  # noqa: PLC0415

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
    """Subprocess call to scripts/make_shorts.py. Streams output to log_path.

    `--require-critic` is ALWAYS passed for cron-triggered renders so the
    upload step refuses to ship if the post-render critique didn't run or
    didn't produce a score (per the user's "never auto-upload without a
    critic check" rule).

    Note: scripts/make_shorts.py is now a thin shim around
    pipeline.render.shorts.cli_main (since the 2026-05-05 renderer
    promotion). The shim still accepts the same CLI flags so this
    subprocess contract is unchanged. The shim's cli_main also prints
    an OUTPUT_MANIFEST: {...} line on stdout that ``_find_outputs``
    parses to locate the produced mp4 + thumb.
    """
    cmd = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "make_shorts.py"),
        "--script", str(script_path),
        "--channel", str(channel_yaml),
        "--require-critic",
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


def _parse_output_manifest(log_path: Path) -> dict | None:
    """Pull the most recent ``OUTPUT_MANIFEST: {…}`` line from the render log.

    pipeline.render.shorts.cli_main prints exactly one of these lines on
    successful render. Returns ``{"mp4": str, "thumb": str | None,
    "slug": str, "channel_dir": str}`` or ``None`` if the line wasn't
    found (older renderer versions, render failure, partial log, etc).
    """
    if not log_path.exists():
        return None
    try:
        for line in reversed(log_path.read_text(errors="replace").splitlines()):
            if line.startswith("OUTPUT_MANIFEST: "):
                return json.loads(line[len("OUTPUT_MANIFEST: "):])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("failed to parse OUTPUT_MANIFEST from %s: %s", log_path, e)
    return None


def _find_outputs(
    slug: str, channel_dir: str, log_path: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Return ``(mp4, thumb)`` paths produced by the render.

    Resolution precedence:

    1. **OUTPUT_MANIFEST stdout line** — printed by
       ``pipeline.render.shorts.cli_main`` (since 2026-05-05 renderer
       promotion). Authoritative when present; covers per-channel
       layouts that don't follow the legacy ``data/`` convention.

    2. **Per-channel scan** — ``<channel_dir>/shorts/<slug>.mp4`` is
       the per-channel layout (memory: feedback_channels_subdir_layout)
       that landed in the 2026-05-05 reorg. Used as a fallback if the
       manifest is missing AND ``channel_dir`` is something other than
       the legacy ``data`` (i.e. the channel reorg actually applied).

    3. **Legacy fallback** — ``data/shorts/<slug>.mp4``. Pre-2026-05-05
       behaviour. Kept only because some half-migrated runs may still
       land mp4s here while the cron picks up.
    """
    if log_path is not None:
        manifest = _parse_output_manifest(log_path)
        if manifest is not None:
            mp4_str = manifest.get("mp4")
            thumb_str = manifest.get("thumb")
            mp4_p = Path(mp4_str) if mp4_str else None
            thumb_p = Path(thumb_str) if thumb_str else None
            if mp4_p is not None and not mp4_p.is_absolute():
                mp4_p = PROJECT_ROOT / mp4_p
            if thumb_p is not None and not thumb_p.is_absolute():
                thumb_p = PROJECT_ROOT / thumb_p
            return (
                mp4_p if mp4_p and mp4_p.exists() else None,
                thumb_p if thumb_p and thumb_p.exists() else None,
            )

    # Per-channel scan: tries the new layout first via paths.py
    # (canonical: <channel>/[<niche>/]/shorts/<slug>.mp4 +
    # <channel>/[<niche>/]/shorts/<slug>.thumb.png).
    from pipeline.paths import RenderPaths  # noqa: PLC0415

    paths = RenderPaths.from_channel_dir(channel_dir, project_root=PROJECT_ROOT)
    per_channel_mp4 = paths.short_for(slug)
    per_channel_thumb_candidates = [
        paths.short_thumb_for(slug),
        paths.root / "thumbs" / f"{slug}.png",  # older variant
        paths.root / "thumb" / f"{slug}.png",   # older variant
    ]

    # Legacy data/ fallback for half-migrated runs.
    legacy_mp4 = PROJECT_ROOT / "data" / "shorts" / f"{slug}.mp4"
    legacy_thumb_candidates = [
        PROJECT_ROOT / "data" / "intermediate" / channel_dir / "thumbs" / f"{slug}.png",
        PROJECT_ROOT / "data" / "intermediate" / channel_dir / "thumb" / f"{slug}.png",
        PROJECT_ROOT / "data" / "shorts" / f"{slug}.thumb.png",
    ]

    mp4 = per_channel_mp4 if per_channel_mp4.exists() else (
        legacy_mp4 if legacy_mp4.exists() else None
    )
    thumb = next(
        (c for c in per_channel_thumb_candidates + legacy_thumb_candidates if c.exists()),
        None,
    )
    return mp4, thumb


def _cleanup_intermediate(slug: str, channel_dir: str) -> None:
    """Best-effort: remove per-slug intermediates after a successful render.

    Removes the per-slug raw / narration / cast / thumb / voice JSONs +
    the per-slug image cache. Keeps the laptop's per-channel ``cache/``
    parent dir from accumulating stale per-render state across many runs.
    Channel-wide things (config.yaml, learnings/, scripts/, branding/,
    music/) are NEVER touched. ML model weights under
    ``data/cache/`` (Kokoro / F5 / Whisper checkpoints) also stay put.

    Cleans BOTH the canonical per-channel layout (since the 2026-05-05
    layout cleanup) AND the legacy ``data/intermediate/<channel_dir>/``
    location for half-migrated runs.
    """
    from pipeline.paths import RenderPaths  # noqa: PLC0415

    paths = RenderPaths.from_channel_dir(channel_dir, project_root=PROJECT_ROOT)

    # Per-channel canonical layout: delete per-slug JSON / PNG / WAV.
    canonical_files = [
        paths.raw_for(slug),
        paths.narration_for(slug),
        paths.cast_for(slug),
        paths.short_thumb_for(slug),
    ]
    for p in canonical_files:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    # Per-slug image cache (canonical: <channel>/[<niche>/]/cache/<slug>/).
    canonical_cache = paths.cache_for(slug)
    if canonical_cache.exists():
        try:
            shutil.rmtree(canonical_cache, ignore_errors=True)
        except OSError:
            pass

    # Legacy fallback: data/intermediate/<channel_dir>/{raw,scripts,cast,thumbs,thumb,voices}/<slug>.{json,png,wav}
    inter_root = PROJECT_ROOT / "data" / "intermediate" / channel_dir
    for sub in ("raw", "scripts", "cast", "thumbs", "thumb", "voices"):
        for ext in (".json", ".png", ".wav"):
            p = inter_root / sub / f"{slug}{ext}"
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
    # Legacy global per-slug image cache (data/cache/<slug>/).
    legacy_cache = PROJECT_ROOT / "data" / "cache" / slug
    if legacy_cache.exists():
        try:
            shutil.rmtree(legacy_cache, ignore_errors=True)
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

    # Per-channel render style. footage_only channels (historyrecapped) skip
    # image-gen entirely and run the dedicated render_footage_only.py path.
    import yaml as _yaml  # noqa: PLC0415
    cfg = _yaml.safe_load(channel_yaml.read_text()) if channel_yaml.exists() else {}
    render_style = cfg.get("render_style", "image_gen")
    if render_style == "footage_only":
        # The cron's payload is "topic + channel". For footage_only, the
        # actual slug is whatever the scheduler picked (a pre-authored
        # narration). Pass it through via topic — the slug is already on
        # disk under <channel>/narrations/.
        scheduled_slug = payload.get("topic") or slug
        logger.info("RENDER_SHORT (footage_only) job=%s channel=%s slug=%s",
                    job_id, channel_dir, scheduled_slug)
        jobs_mod.mark_stage(job_id, status=jobs_mod.STATUS_RENDERING,
                            stage="footage_render", slug=scheduled_slug)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(PROJECT_ROOT / "scripts" / channel_dir / "render_footage_only.py"),
            "--channel", channel_dir,
            "--slug", scheduled_slug,
            "--upload",
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ},
        )
        log_path = ctx.scratch / "render_footage_only.log"
        with log_path.open("wb") as f:
            async for line in proc.stdout:
                f.write(line)
        rc = await proc.wait()
        if rc != 0:
            tail = ""
            try:
                tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
            except OSError:
                pass
            err = f"render_footage_only.py exited {rc}\n--- last 40 lines ---\n{tail}"
            jobs_mod.mark_failed(job_id, stage="footage_render", error=err)
            raise RuntimeError(err)
        out = PROJECT_ROOT / channel_dir / "shorts" / f"{scheduled_slug}.mp4"
        jobs_mod.mark_done(job_id, short_uri=str(out))
        logger.info("RENDER_SHORT (footage_only) done job=%s out=%s", job_id, out)
        return str(out)

    logger.info("RENDER_SHORT job=%s slug=%s channel=%s", job_id, slug, channel_yaml.name)
    jobs_mod.mark_stage(job_id, status=jobs_mod.STATUS_RENDERING, stage="rewrite_cast", slug=slug)

    # 1. Rewrite + cast (claude CLI, in-process).
    try:
        script_path, cast_path = await _rewrite_and_cast(raw, channel_yaml, slug, channel_dir)
    except Exception as e:
        jobs_mod.mark_failed(job_id, stage="rewrite_cast", error=f"{type(e).__name__}: {e}")
        raise

    # 2. Heavy render via subprocess.
    jobs_mod.mark_stage(job_id, status=jobs_mod.STATUS_RENDERING, stage="render")
    log_path = ctx.scratch / "make_shorts.log"
    rc = await _run_make_shorts(script_path, channel_yaml, log_path)
    if rc != 0:
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
        except OSError:
            pass
        err = f"make_shorts.py exited {rc}\n--- last 40 log lines ---\n{tail}"
        jobs_mod.mark_failed(job_id, stage="render", error=err)
        raise RuntimeError(err)

    # 3. Upload outputs to GCS.
    jobs_mod.mark_stage(job_id, status=jobs_mod.STATUS_UPLOADING, stage="gcs_upload")
    # Pass log_path so _find_outputs can prefer the OUTPUT_MANIFEST line
    # (printed by pipeline.render.shorts.cli_main) over path guessing.
    mp4_path, thumb_path = _find_outputs(slug, channel_dir, log_path=log_path)
    if mp4_path is None:
        err = f"render reported success but no mp4 found at {slug}.mp4"
        jobs_mod.mark_failed(job_id, stage="gcs_upload", error=err)
        raise RuntimeError(err)

    short_uri = storage.job_uri(job_id, "short.mp4")
    storage.upload(mp4_path, short_uri, content_type="video/mp4")

    thumb_uri: str | None = None
    if thumb_path:
        thumb_uri = storage.job_uri(job_id, "thumb.png")
        storage.upload(thumb_path, thumb_uri, content_type="image/png")

    storage.upload_bytes(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        storage.job_uri(job_id, "proposal.json"),
        content_type="application/json",
    )

    # Mark the render itself done — YT upload/research happen on follow-up
    # tasks and update the job further. Surface short_uri now so the UI
    # can offer a download link as soon as render finishes, even if YT
    # upload is still in flight.
    jobs_mod.mark_done(job_id, short_uri=short_uri, thumb_uri=thumb_uri)

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
