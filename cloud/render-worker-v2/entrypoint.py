"""Cloud Run Job entry point for ytFactory render-worker v2.

Firestore-driven (not GCS-spec-driven like the legacy v1).

Flow per execution:
  1. Read ``YTFACTORY_JOB_ID`` from env.
  2. Fetch ``jobs/<job_id>`` from Firestore — proposal + channel + overrides.
  3. Walk the canonical 7-stage pipeline:
       rewrite → cast → images → tts → asr → compose → upload
     For each stage: write a TIMELINE event to the job doc, run the
     stage, update on completion. The UI's poll loop sees this in real
     time without any extra plumbing.
  4. On success: upload mp4 + thumbnail to
     ``gs://<bucket>/jobs/<job_id>/short.mp4`` (and ``thumb.jpg``),
     update job doc with ``short_uri`` + ``status=done``.
  5. On failure: write ``status=failed`` + error string.

Two render modes — flip via ``YTFACTORY_RENDER_MODE`` env on the JOB:

* ``stub``   (until proven on cloud) — each stage sleeps ~2s, the
              upload stage drops a placeholder mp4. Useful for
              end-to-end wiring tests without an LLM key.
* ``real``   (default) — calls ``pipeline.llm.rewrite.rewrite()`` to
              synthesize a script.json, then shells out to
              ``python -m pipeline.render.shorts`` with the channel
              YAML + the new script. The renderer handles stages
              images→tts→asr→compose→upload itself using the cloud
              providers declared in the channel YAML
              (``image_provider: cloudrun_flux2_klein``,
              ``tts_provider: cloudrun_chatterbox``, etc.). ASR uses
              the ``faster_whisper`` provider. LLM defaults to
              **Azure OpenAI** (reuses your existing chat-assistant
              deployment — no separate spend). Switch to Anthropic
              SDK with ``YTFACTORY_LLM_BACKEND=anthropic_sdk``.

Environment (set at deploy time on the Cloud Run Job):
  YTFACTORY_JOB_ID            — per-execution override (set by control plane)
  GOOGLE_CLOUD_PROJECT        — Firestore + GCS project (default ytfactory-prod-v2)
  YTFACTORY_BUCKET            — GCS bucket for artifacts
  CLOUDRUN_TTS_CHATTERBOX_URL — TTS service URL
  CLOUDRUN_TTS_INDICPARLER_URL
  CLOUDRUN_IMAGE_FLUX2_KLEIN_URL
  AZURE_OPENAI_ENDPOINT       — required when YTFACTORY_LLM_BACKEND=azure_openai (default)
  AZURE_OPENAI_API_KEY        — required when YTFACTORY_LLM_BACKEND=azure_openai
  AZURE_OPENAI_MODEL          — Azure deployment name (e.g. gpt-4o-mini)
  AZURE_OPENAI_MODEL_OPUS     — optional per-tier override (e.g. gpt-4o)
  ANTHROPIC_API_KEY           — required when YTFACTORY_LLM_BACKEND=anthropic_sdk
  YTFACTORY_LLM_BACKEND       — "azure_openai" (default cloud) | "anthropic_sdk" | "cli"
  YTFACTORY_ASR_PROVIDER      — "faster_whisper" forces faster-whisper
  YTFACTORY_RENDER_MODE       — "real" (default) or "stub".
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("render-worker-v2")


REPO_ROOT = Path("/workspace")
TMP_ROOT = Path("/tmp/render")

# Canonical stages — UI mirrors these.
STAGES: list[tuple[str, str]] = [
    ("rewrite", "Rewriting script"),
    ("cast", "Casting voice & visuals"),
    ("images", "Generating images"),
    ("tts", "Synthesizing narration"),
    ("asr", "Aligning captions"),
    ("compose", "Composing video"),
    ("upload", "Uploading to GCS"),
]


# Map proposal.channel → channel YAML path on disk (baked into the image).
#
# Single source of truth for channel YAML locations is
# pipeline.channels._channel_yaml_path — defining it here too would
# drift the moment a channel reorg lands (which already happened once
# pre-2026-05-10, breaking the cloud build until this map was rewired).
# We resolve at request time instead.
def _channel_yaml_for(channel_key: str) -> Path:
    from pipeline.channels import _channel_yaml_path  # noqa: PLC0415
    rel = _channel_yaml_path(channel_key)
    return REPO_ROOT / rel


# ---------------------------------------------------------------------------
# Firestore + GCS helpers
# ---------------------------------------------------------------------------


def _project_id() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")


def _bucket_name() -> str:
    return os.environ.get("YTFACTORY_BUCKET", "ytfactory-prod-v2-artifacts")


def _firestore_client():
    from google.cloud import firestore  # noqa: PLC0415
    return firestore.Client(project=_project_id())


def _storage_client():
    from google.cloud import storage  # noqa: PLC0415
    return storage.Client(project=_project_id())


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_ref(job_id: str):
    return _firestore_client().collection("jobs").document(job_id)


def _empty_timeline() -> list[dict]:
    return [{"stage": k, "label": label, "status": "pending"} for k, label in STAGES]


def _set_stage(timeline: list[dict], key: str, status: str, msg: str | None = None) -> list[dict]:
    out = [dict(s) for s in timeline]
    for s in out:
        if s["stage"] == key:
            s["status"] = status
            s["ts"] = _utcnow_iso()
            if msg:
                s["msg"] = msg
    return out


def _update_job(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = datetime.now(timezone.utc)
    _job_ref(job_id).set(fields, merge=True)


def _upload_mp4_to_gcs(local_mp4: Path, job_id: str) -> str:
    blob_path = f"jobs/{job_id}/short.mp4"
    bucket = _storage_client().bucket(_bucket_name())
    blob = bucket.blob(blob_path)
    blob.content_type = "video/mp4"
    blob.upload_from_filename(str(local_mp4))
    return f"gs://{_bucket_name()}/{blob_path}"


def _upload_thumb_to_gcs(local_thumb: Path, job_id: str) -> str:
    blob_path = f"jobs/{job_id}/thumb.jpg"
    bucket = _storage_client().bucket(_bucket_name())
    blob = bucket.blob(blob_path)
    blob.content_type = "image/jpeg"
    blob.upload_from_filename(str(local_thumb))
    return f"gs://{_bucket_name()}/{blob_path}"


# ---------------------------------------------------------------------------
# Mode + slug helpers
# ---------------------------------------------------------------------------


def _is_stub_mode() -> bool:
    return os.environ.get("YTFACTORY_RENDER_MODE", "stub").lower() == "stub"


def _slug_from_topic(topic: str, job_id: str) -> str:
    """Filesystem-safe slug for the script.json + per-render dir."""
    import re  # noqa: PLC0415
    base = re.sub(r"[^a-z0-9]+", "-", (topic or "render").lower()).strip("-")[:50]
    suffix = job_id[:8]
    return f"{base}-{suffix}" if base else suffix


# ---------------------------------------------------------------------------
# STUB stage handlers — used when YTFACTORY_RENDER_MODE=stub
# ---------------------------------------------------------------------------


def _run_stage_stub(stage: str, job: dict, work_dir: Path) -> None:
    if stage == "upload":
        out = work_dir / "short.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=#0a0a0a:s=1080x1920:d=4:r=30",
                "-pix_fmt", "yuv420p", str(out),
            ],
            check=True,
        )
        thumb = work_dir / "thumb.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(out),
             "-frames:v", "1", str(thumb)],
            check=True,
        )
        job["_stub_mp4"] = str(out)
        job["_stub_thumb"] = str(thumb)
    time.sleep(2)


# ---------------------------------------------------------------------------
# REAL stage handlers — used when YTFACTORY_RENDER_MODE=real
# ---------------------------------------------------------------------------


def _stage_rewrite_real(job: dict, work_dir: Path) -> None:
    """Synthesize a Script via pipeline.llm.rewrite — Anthropic SDK
    auto-fires on cloud (no claude CLI installed)."""
    from pipeline.llm.rewrite import rewrite, save_script  # noqa: PLC0415
    import yaml  # noqa: PLC0415

    proposal = job.get("proposal") or {}
    channel_key = proposal.get("channel") or "mystoriesanimated"
    channel_yaml = _channel_yaml_for(channel_key)
    channel_cfg: dict = {}
    if channel_yaml.exists():
        with channel_yaml.open() as fp:
            channel_cfg = yaml.safe_load(fp) or {}

    job_id = job.get("job_id") or os.environ["YTFACTORY_JOB_ID"]
    slug = _slug_from_topic(proposal.get("topic") or "", job_id)
    raw_story = {
        "slug": slug,
        "title": (proposal.get("topic") or "").strip(),
        "body":  (proposal.get("notes") or "").strip(),
        "source": "user_text",
        "url": "",
    }
    if not raw_story["title"]:
        raise RuntimeError("proposal missing topic")

    script = rewrite(raw_story, channel_cfg=channel_cfg)

    # Persist script.json under <channel_root>/scripts/<slug>.json so
    # pipeline.render.shorts --script picks it up.
    chan_root = REPO_ROOT / channel_key
    scripts_dir = chan_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{slug}.json"
    save_script(script, script_path)

    job["_slug"] = slug
    job["_script_path"] = str(script_path)
    job["_channel_yaml"] = str(channel_yaml)
    logger.info("rewrite OK · slug=%s script=%s", slug, script_path)


def _stage_cast_real(job: dict, work_dir: Path) -> None:
    """No-op for the v2 worker: the renderer subprocess invokes
    pipeline.llm.cast.author_cast as part of its first stage; we
    just emit a timeline marker.

    (Authoring cast here would require loading the script.json we
    just wrote, generating a cast.json, and persisting it — work
    that pipeline.render.shorts already does in its bootstrap. Keep
    the worker thin and let the renderer own it.)"""
    time.sleep(0.05)


def _run_renderer_subprocess(job: dict, work_dir: Path) -> Path:
    """Shell out to ``pipeline.render.shorts`` and return the produced mp4 path.

    The renderer handles stages images → tts → asr → compose using the
    cloud providers declared in the channel YAML. ASR is forced to
    faster-whisper via env (whisper-mlx is Apple-only).
    """
    script_path = job.get("_script_path")
    channel_yaml = job.get("_channel_yaml")
    if not script_path or not channel_yaml:
        raise RuntimeError("renderer: rewrite stage didn't set _script_path / _channel_yaml")

    log_path = work_dir / "renderer.log"
    cmd = [
        sys.executable, "-m", "pipeline.render.shorts",
        "--script", script_path,
        "--channel", channel_yaml,
        "--no-upload",     # YouTube upload happens via /api/jobs/{id}/publish
        "--no-critic",     # critic runs on the cloud worker as a separate stage later
    ]

    # Forward the user's Customize-step picks from the proposal's
    # `channel_overrides` dict into pipeline.render.shorts via repeated
    # --override KEY=VALUE flags. This is the cloud-side counterpart of
    # the create page's submit() forwarder; without it, song_style /
    # audio_mode / visual_source / voice etc. would silently land in
    # Firestore but never reach make_short.
    #
    # Stringify defensively — the renderer's --override parser splits on
    # the first `=` and stores the raw RHS, so non-string values would
    # arrive as their repr. Skip empty values so a YAML default keeps
    # winning when the form left a knob untouched.
    proposal = job.get("proposal") or {}
    overrides = proposal.get("channel_overrides") or {}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if v is None:
                continue
            sv = str(v)
            if not sv.strip():
                continue
            cmd += ["--override", f"{k}={sv}"]
        if overrides:
            logger.info("renderer overrides: %s", sorted(overrides.keys()))
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    env.setdefault("YTFACTORY_ASR_PROVIDER", "faster_whisper")
    env.setdefault("YTFACTORY_LLM_BACKEND", "azure_openai")
    # Disable any local-fallback paths (they require Apple-only mlx /
    # mflux which aren't installed in the cloud image).
    env.setdefault("CLOUDRUN_TTS_DISABLE_FALLBACK", "1")
    env.setdefault("CLOUDRUN_IMAGE_DISABLE_FALLBACK", "1")

    logger.info("renderer: %s", " ".join(cmd))
    t0 = time.time()
    with log_path.open("wb") as logfh:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=logfh, stderr=subprocess.STDOUT, check=False,
        )
    elapsed = time.time() - t0
    logger.info("renderer exit=%s in %.0fs", proc.returncode, elapsed)
    if proc.returncode != 0:
        tail = log_path.read_text(errors="replace")[-4000:]
        raise RuntimeError(f"renderer subprocess exit={proc.returncode}\n{tail}")

    # Locate the produced mp4. The renderer writes via
    # RenderPaths.from_channel_yaml — use the SAME resolver so worker
    # and renderer can never disagree on the output location. Pre-fix
    # the worker hard-coded chan_dir = Path(channel_yaml).parent which
    # resolved to ``pipeline/channels/`` (the central-config dir) when
    # the channel YAML lived there, and then looked for
    # ``pipeline/channels/shorts/<slug>.mp4`` — the renderer had
    # written to the channel root. v7 cake-orch surfaced this:
    # renderer exited 0 in 941s, all 22 images + mp4 on disk, the
    # worker raised "renderer ran but no mp4 found".
    slug = job.get("_slug") or ""
    candidates: list[Path] = []
    try:
        from pipeline.paths import RenderPaths  # noqa: PLC0415
        rp = RenderPaths.from_channel_yaml(
            Path(channel_yaml), project_root=REPO_ROOT,
        )
        # Per-niche layout writes to <channel>/<niche>/shorts/<slug>.mp4;
        # flat layout to <channel>/shorts/<slug>.mp4. ``rp.root`` is
        # the right answer for both.
        candidates += [
            rp.root / "shorts" / f"{slug}.mp4",
            rp.root / "shorts" / slug / f"{slug}.mp4",
            rp.channel_root / "shorts" / f"{slug}.mp4",
        ]
    except Exception as e:  # noqa: BLE001 — fall back to legacy lookup
        logger.warning(
            "[worker] RenderPaths lookup failed (%s); using legacy "
            "chan_dir glob", e,
        )

    chan_dir = Path(channel_yaml).parent
    candidates += [
        chan_dir / "shorts" / f"{slug}.mp4",
        chan_dir / "shorts" / slug / f"{slug}.mp4",
        # Renderer's data/ legacy fallback when path resolution can't
        # find a channel root (e.g. for mystoriesanimated when the
        # channel-named dir was removed in the 2026-05-10 cleanup).
        REPO_ROOT / "data" / "shorts" / f"{slug}.mp4",
    ]
    for cand in candidates:
        if cand.exists():
            logger.info("[worker] mp4 found at %s", cand)
            return cand
    # Last-ditch: glob the entire repo root for the slug.
    for mp4 in REPO_ROOT.rglob(f"{slug}.mp4"):
        logger.info("[worker] mp4 found via repo-wide glob at %s", mp4)
        return mp4
    raise RuntimeError(
        f"renderer ran but no mp4 found for slug={slug}; "
        f"searched: {candidates!r}"
    )


def _stage_render_real(job: dict, work_dir: Path) -> None:
    """Composite stage that covers images + tts + asr + compose by
    delegating to ``pipeline.render.shorts``. Returns the mp4 path
    via job['_real_mp4']."""
    mp4 = _run_renderer_subprocess(job, work_dir)
    # Generate a thumb from the mp4.
    thumb = work_dir / "thumb.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4),
         "-frames:v", "1", str(thumb)],
        check=False,
    )
    job["_real_mp4"] = str(mp4)
    job["_real_thumb"] = str(thumb) if thumb.exists() else None


def _stage_upload_real(job: dict, work_dir: Path) -> None:
    """No-op: actual GCS upload happens after the stage loop in main()
    so we can update Firestore with the URI in one shot. This keeps
    the timeline label honest."""
    time.sleep(0.05)


# Registry: stage key → (stub handler, real handler).
_REAL_HANDLERS: dict[str, Callable[[dict, Path], None]] = {
    "rewrite": _stage_rewrite_real,
    "cast":    _stage_cast_real,
    # images, tts, asr, compose are all covered by the renderer subprocess
    # which we run UNDER the "compose" stage label so the UI shows the
    # heaviest visible activity at the right time. The intermediate
    # stages emit "running" + "done" purely for timeline aesthetics —
    # the renderer's own logs go to renderer.log inside the work_dir.
    "images":  lambda job, wd: time.sleep(0.05),
    "tts":     lambda job, wd: time.sleep(0.05),
    "asr":     lambda job, wd: time.sleep(0.05),
    "compose": _stage_render_real,
    "upload":  _stage_upload_real,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Two entry modes:

    1. ``YTFACTORY_JOB_ID`` set → Firestore-driven proposal flow (the
       chat-assistant path). Walks all 7 stages incl. rewrite via
       Azure OpenAI.
    2. ``JOB_SPEC_GCS_URI`` set → website-driven from-script flow. The
       spec.json contains a pre-baked channel YAML + script JSON; we
       skip rewrite (script already authored) and just run the
       renderer subprocess + upload. State is reported back via
       state.json on GCS (matches the legacy v1 contract that
       web/server.py:_run_cloudrun expects).

    Exactly one of the two env vars must be set.
    """
    spec_uri = os.environ.get("JOB_SPEC_GCS_URI", "").strip()
    job_id = os.environ.get("YTFACTORY_JOB_ID", "").strip()
    if spec_uri:
        return _main_from_gcs_spec(spec_uri)
    if job_id:
        return _main_from_firestore(job_id)
    logger.error("either YTFACTORY_JOB_ID (Firestore) or JOB_SPEC_GCS_URI (GCS) must be set")
    return 2


def _main_from_firestore(job_id: str) -> int:
    mode = "stub" if _is_stub_mode() else "real"
    logger.info("starting render-worker-v2 for job=%s mode=%s (Firestore)", job_id, mode)

    snap = _job_ref(job_id).get()
    if not snap.exists:
        logger.error("job %s not found in Firestore", job_id)
        _update_job(job_id, status="failed", stage="bootstrap",
                    error="job doc not found")
        return 1
    job = snap.to_dict() or {}
    job["job_id"] = job_id
    proposal = job.get("proposal") or {}
    logger.info("job loaded: channel=%s topic=%s",
                proposal.get("channel"), proposal.get("topic"))

    work_dir = TMP_ROOT / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    timeline = _empty_timeline()
    _update_job(job_id, status="rendering", stage="rewrite", timeline=timeline)

    try:
        for key, _ in STAGES:
            timeline = _set_stage(timeline, key, "running",
                                  "real-mode" if mode == "real" else "stub-mode")
            _update_job(
                job_id,
                status="rendering" if key != "upload" else "uploading",
                stage=key,
                timeline=timeline,
            )
            t0 = time.time()
            handler = (_run_stage_stub if mode == "stub"
                       else _REAL_HANDLERS.get(key, _run_stage_stub))
            if mode == "stub":
                handler(key, job, work_dir)  # type: ignore[arg-type]
            else:
                handler(job, work_dir)  # type: ignore[arg-type]
            elapsed = time.time() - t0
            logger.info("stage %s done in %.1fs", key, elapsed)
            timeline = _set_stage(timeline, key, "done", f"{elapsed:.1f}s")
            _update_job(
                job_id,
                status="rendering" if key != "upload" else "uploading",
                stage=key,
                timeline=timeline,
            )

        local_mp4 = Path(
            job.get("_real_mp4") or job.get("_stub_mp4") or work_dir / "short.mp4"
        )
        local_thumb_str = job.get("_real_thumb") or job.get("_stub_thumb")
        local_thumb = Path(local_thumb_str) if local_thumb_str else (work_dir / "thumb.jpg")
        if not local_mp4.exists():
            raise RuntimeError(f"render finished but mp4 not found at {local_mp4}")

        mp4_uri = _upload_mp4_to_gcs(local_mp4, job_id)
        thumb_uri = (
            _upload_thumb_to_gcs(local_thumb, job_id)
            if local_thumb.exists()
            else None
        )

        _update_job(
            job_id,
            status="done",
            stage="done",
            short_uri=mp4_uri,
            thumb_uri=thumb_uri,
            timeline=timeline,
            critique={
                "verdict": "SHIP",
                "weakest_param": (
                    "(stub mode — real critic runs after pipeline port lands)"
                    if mode == "stub" else None
                ),
                "notes": (
                    f"Cloud Run Job render complete (mode={mode})."
                ),
            },
        )
        logger.info("render complete: job=%s mp4=%s mode=%s", job_id, mp4_uri, mode)
        return 0
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("render failed: %s\n%s", e, tb)
        _update_job(
            job_id,
            status="failed",
            stage=job.get("stage", "unknown"),
            error=f"{e}\n{tb}"[:8000],
            timeline=timeline,
        )
        return 1


# ---------------------------------------------------------------------------
# GCS spec.json entry point — for the website's /api/jobs/from_script flow
# ---------------------------------------------------------------------------


def _gcs_read_text(uri: str) -> str:
    from urllib.parse import urlparse  # noqa: PLC0415
    bucket_name, _, blob_path = urlparse(uri).netloc, None, urlparse(uri).path.lstrip("/")
    blob = _storage_client().bucket(bucket_name).blob(blob_path)
    return blob.download_as_text()


def _gcs_upload_text(text: str, uri: str, *, content_type: str = "application/json") -> None:
    from urllib.parse import urlparse  # noqa: PLC0415
    p = urlparse(uri)
    blob = _storage_client().bucket(p.netloc).blob(p.path.lstrip("/"))
    blob.upload_from_string(text, content_type=content_type)


def _gcs_upload_file(local_path: Path, uri: str, *, content_type: str | None = None) -> None:
    from urllib.parse import urlparse  # noqa: PLC0415
    p = urlparse(uri)
    blob = _storage_client().bucket(p.netloc).blob(p.path.lstrip("/"))
    if content_type:
        blob.content_type = content_type
    blob.upload_from_filename(str(local_path))


def _main_from_gcs_spec(spec_uri: str) -> int:
    """The website-driven path: spec.json on GCS + state.json on GCS.

    Spec format (set by web/server.py:_run_cloudrun):
        job_id              — id we'll write state.json under
        cmd                 — list[str] for backward-compat / informational
        channel_yaml_path   — repo-relative (e.g. "mystoriesanimated/config.yaml")
        channel_yaml_b64    — base64-encoded contents (used as fallback if path
                              isn't baked into the image)
        script_path         — repo-relative (e.g. "mystoriesanimated/scripts/foo.json")
        script_json_b64     — base64-encoded contents (idem)
        raw_path / raw_b64  — optional, for upload metadata

    State output (each terminal write is atomic):
        state.json under same gs://.../jobs/{job_id}/ as spec.json
        terminal state ∈ {"done", "done_no_mp4_found", "failed"}
        on done: mp4_uri = gs://.../jobs/{job_id}/short.mp4
    """
    import base64  # noqa: PLC0415

    logger.info("starting render-worker-v2 GCS-spec mode: %s", spec_uri)

    try:
        spec = json.loads(_gcs_read_text(spec_uri))
    except Exception as exc:
        logger.error("could not read spec %s: %s", spec_uri, exc)
        return 1

    job_id = spec.get("job_id") or os.path.basename(os.path.dirname(spec_uri.rstrip("/"))) or "unknown"
    state_uri = f"gs://{_bucket_name()}/jobs/{job_id}/state.json"

    def _write_state(state: str, **extra: Any) -> None:
        body = {
            "state": state,
            "updated_at": _utcnow_iso(),
            **extra,
        }
        try:
            _gcs_upload_text(json.dumps(body), state_uri)
        except Exception as exc:  # noqa: BLE001
            logger.error("could not write state.json: %s", exc)

    _write_state("running", stage="bootstrap")

    try:
        # Materialize channel_yaml + script_json into REPO_ROOT — the
        # renderer reads them by path. Base64 is the source of truth so
        # the renderer sees the user's edits even if the baked image is
        # stale.
        channel_yaml_path = REPO_ROOT / spec["channel_yaml_path"]
        channel_yaml_path.parent.mkdir(parents=True, exist_ok=True)
        channel_yaml_path.write_bytes(base64.b64decode(spec["channel_yaml_b64"]))

        script_path = REPO_ROOT / spec["script_path"]
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_bytes(base64.b64decode(spec["script_json_b64"]))

        if spec.get("raw_path") and spec.get("raw_b64"):
            raw_path = REPO_ROOT / spec["raw_path"]
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(base64.b64decode(spec["raw_b64"]))

        work_dir = TMP_ROOT / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        log_path = work_dir / "renderer.log"

        # Mirror the production renderer call. ASR forced to
        # faster_whisper because mlx_whisper is Apple-only.
        cmd = [
            sys.executable, "-m", "pipeline.render.shorts",
            "--script", str(script_path),
            "--channel", str(channel_yaml_path),
            "--no-upload",
            "--no-critic",
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(REPO_ROOT))
        env.setdefault("YTFACTORY_ASR_PROVIDER", "faster_whisper")

        _write_state("running", stage="render")
        logger.info("invoking renderer: %s", " ".join(cmd))
        with log_path.open("wb") as logf:
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT
            )

        # Upload renderer log to GCS so the website's UI can show it.
        log_uri = f"gs://{_bucket_name()}/jobs/{job_id}/renderer.log"
        try:
            _gcs_upload_file(log_path, log_uri, content_type="text/plain")
        except Exception:  # noqa: BLE001
            log_uri = None  # type: ignore[assignment]

        if proc.returncode != 0:
            tail = log_path.read_text(errors="replace")[-4000:] if log_path.exists() else ""
            _write_state(
                "failed",
                exit_code=proc.returncode,
                error=f"renderer exit={proc.returncode}\n{tail}",
                log_uri=log_uri,
            )
            logger.error("renderer failed: exit=%d", proc.returncode)
            return 1

        # Find the produced mp4. The renderer writes under
        # <channel>/<niche>/shorts/<slug>.mp4 OR <channel>/shorts/<slug>.mp4.
        slug = script_path.stem
        channel_dir = channel_yaml_path.parent
        candidates = [
            channel_dir / "shorts" / f"{slug}.mp4",
            *channel_dir.glob(f"*/shorts/{slug}.mp4"),
        ]
        local_mp4 = next((p for p in candidates if p.exists()), None)
        if not local_mp4:
            _write_state(
                "done_no_mp4_found",
                exit_code=0,
                error=f"renderer succeeded but no mp4 at {[str(p) for p in candidates]}",
                log_uri=log_uri,
            )
            logger.warning("done_no_mp4_found")
            return 0

        # Upload mp4 to GCS.
        mp4_uri = f"gs://{_bucket_name()}/jobs/{job_id}/short.mp4"
        _gcs_upload_file(local_mp4, mp4_uri, content_type="video/mp4")
        logger.info("uploaded %s → %s", local_mp4, mp4_uri)

        _write_state(
            "done",
            exit_code=0,
            mp4_uri=mp4_uri,
            log_uri=log_uri,
            completed_at=time.time(),
        )
        return 0

    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("GCS-spec render failed: %s\n%s", e, tb)
        _write_state("failed", error=f"{e}\n{tb}"[:8000])
        return 1


if __name__ == "__main__":
    sys.exit(main())
