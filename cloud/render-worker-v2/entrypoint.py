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
import re
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
#
# The optional 8th stage ``editing_agent`` is INSERTED between
# ``compose`` and ``upload`` at runtime when ``proposal.editing.enabled``
# is truthy on the job doc. We don't add it to the static STAGES list
# because most jobs don't opt in — appending it unconditionally would
# fill every dashboard with a "skipped" pill on the 8th column. See
# :func:`_stages_for_job` below.
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


def _variant_yaml_for(channel_key: str, variant: str | None) -> Path | None:
    """Return the variant overlay YAML path if it exists, else None.

    The form's ``format`` field carries the variant slug (e.g.
    ``aita_animated``, ``aita_cliffhanger_text``, ``today_in_history``).
    Variant YAMLs live at ``pipeline/variants/<channel>/<variant>.yaml``
    (canonical 2026-05-05 layout) or ``<channel>/variants/<variant>.yaml``
    (legacy). Pre-2026-05-11 the cloud worker ignored ``format`` and
    always used the bare channel YAML — so picking "AITA Animated" vs
    "AITA Text" produced identical renders. Now we resolve the overlay
    and pass IT as ``--channel`` to ``pipeline.render.shorts``, which
    uses ``RenderPaths.from_channel_yaml`` to merge channel + variant.
    """
    if not variant or variant in ("auto", ""):
        return None
    candidates = [
        REPO_ROOT / "pipeline" / "variants" / channel_key / f"{variant}.yaml",
        REPO_ROOT / channel_key / "variants" / f"{variant}.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


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


# ---------------------------------------------------------------------------
# Preflight — validate env BEFORE we touch Firestore.
#
# Pre-2026-05-11 a missing AZURE_OPENAI_ENDPOINT would surface as a
# ClaudeCLIError mid-rewrite; the worker had already marked the
# Firestore job ``status=rendering, stage=rewrite`` so the user-visible
# job died with a giant Python traceback. The real cause — operator
# forgot to wire env after a redeploy — was buried in the
# ``handler(job, work_dir)`` stack trace. Preflight runs before any
# Firestore write, surfaces a one-line cause, exits clean.
# ---------------------------------------------------------------------------


_PreflightError = tuple[str, str]  # (env_key, friendly_message)


def _preflight() -> list[_PreflightError]:
    """Return a list of missing/invalid env entries — empty list = OK.

    Validates only the env that this worker actually needs in the
    current mode. Stub mode skips LLM + cloud-service checks (they
    aren't called). Real mode is strict.
    """
    problems: list[_PreflightError] = []

    # Always required.
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        problems.append(
            ("GOOGLE_CLOUD_PROJECT",
             "GCP project id missing — Firestore + GCS clients can't initialise.")
        )
    if not os.environ.get("YTFACTORY_BUCKET"):
        problems.append(
            ("YTFACTORY_BUCKET",
             "Artifacts bucket missing — mp4/thumbnail upload would fail at the end.")
        )

    if _is_stub_mode():
        # Stub mode is fine without LLM / cloud TTS / cloud image env —
        # it ffmpeg-generates a placeholder mp4 and doesn't call any
        # real pipeline. Useful for end-to-end wiring smoke tests.
        return problems

    # ----- Real mode below — LLM backend env -----
    backend = (os.environ.get("YTFACTORY_LLM_BACKEND") or "").strip().lower()
    if backend == "azure_openai" or (
        not backend and os.environ.get("AZURE_OPENAI_ENDPOINT")
    ):
        for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"):
            if not (os.environ.get(k) or "").strip():
                problems.append((
                    k,
                    f"{k} not set — azure_openai LLM backend can't authenticate. "
                    f"Wire via:\n"
                    f"  gcloud run jobs update ytfactory-render-worker-v2 "
                    f"--region=asia-southeast1 --project=ytfactory-prod-v2 \\\n"
                    f"    --update-env-vars=AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com,"
                    f"AZURE_OPENAI_API_VERSION=2025-04-01-preview,AZURE_OPENAI_MODEL=gpt-5.3-chat \\\n"
                    f"    --update-secrets=AZURE_OPENAI_API_KEY=azure-openai-key:latest"
                ))
        if not (os.environ.get("AZURE_OPENAI_MODEL")
                or os.environ.get("AZURE_OPENAI_MODEL_HAIKU")):
            problems.append((
                "AZURE_OPENAI_MODEL",
                "AZURE_OPENAI_MODEL (or per-tier AZURE_OPENAI_MODEL_HAIKU/SONNET/OPUS) "
                "not set — every rewrite call would 404 from Azure with 'deployment not found'."
            ))
    elif backend == "anthropic_sdk":
        if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            problems.append((
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_API_KEY not set — anthropic_sdk LLM backend can't authenticate."
            ))
    elif backend == "cli":
        # cli backend shells out to the `claude` binary which doesn't
        # exist in the cloud image — error early instead of failing
        # mid-rewrite.
        problems.append((
            "YTFACTORY_LLM_BACKEND",
            "YTFACTORY_LLM_BACKEND=cli is laptop-only (the `claude` binary "
            "isn't in the cloud image). Switch to azure_openai or anthropic_sdk."
        ))

    # ----- Cloud TTS / image services -----
    # We don't know the channel yet (Firestore lookup happens AFTER
    # preflight), so we can't pick the exact provider. Validate that
    # AT LEAST one TTS URL + the image URL are wired so renders for
    # ANY channel can succeed. Per-channel mismatch (e.g. Hindi job
    # but indicparler URL missing) still surfaces at the renderer's
    # own URL-resolution boundary, but the most-common
    # default-(English-Chatterbox) gets caught here.
    tts_urls = [
        ("CLOUDRUN_TTS_CHATTERBOX_URL",  "default English TTS"),
        ("CLOUDRUN_TTS_INDICPARLER_URL", "default Hindi TTS"),
        ("CLOUDRUN_TTS_INDICF5_URL",     "Hindi IndicF5 TTS"),
        ("CLOUDRUN_TTS_F5_URL",          "F5-TTS (legacy default)"),
        ("CLOUDRUN_TTS_URL",             "generic TTS fallback URL"),
    ]
    if not any((os.environ.get(k) or "").strip() for k, _ in tts_urls):
        problems.append((
            "CLOUDRUN_TTS_*_URL",
            "No CLOUDRUN_TTS_* URL is set — every TTS call would raise "
            "CloudRunUnavailable. At minimum wire CLOUDRUN_TTS_CHATTERBOX_URL "
            "(English) and CLOUDRUN_TTS_INDICPARLER_URL (Hindi)."
        ))

    image_urls = [
        "CLOUDRUN_IMAGE_FLUX2_KLEIN_URL",
        "CLOUDRUN_IMAGE_Z_TURBO_URL",
        "CLOUDRUN_IMAGE_QWEN_URL",
        "CLOUDRUN_IMAGE_HIDREAM_URL",
    ]
    if not any((os.environ.get(k) or "").strip() for k in image_urls):
        problems.append((
            "CLOUDRUN_IMAGE_*_URL",
            "No CLOUDRUN_IMAGE_* URL is set — image gen would fall back "
            "to local mflux which isn't installed in the cloud image. "
            "Wire CLOUDRUN_IMAGE_FLUX2_KLEIN_URL at minimum."
        ))

    # ----- ASR provider sanity -----
    asr = (os.environ.get("YTFACTORY_ASR_PROVIDER") or "").strip().lower()
    if asr in ("whisper_mlx", "parakeet_mlx"):
        # Apple-only providers will crash on Linux with a ModuleNotFoundError
        # for mlx_whisper. The renderer reads this env and uses it; clear-fail
        # at preflight so the operator sees the cause without searching logs.
        problems.append((
            "YTFACTORY_ASR_PROVIDER",
            f"YTFACTORY_ASR_PROVIDER={asr} is Apple-only (mlx_whisper). "
            f"Set to 'faster_whisper' for the cloud image."
        ))

    return problems


def _format_preflight_error(problems: list[_PreflightError]) -> str:
    lines = [
        "Cloud Run render worker preflight failed — refusing to consume work.",
        "",
        f"{len(problems)} env problem(s) detected:",
    ]
    for k, msg in problems:
        lines.append("")
        lines.append(f"  ✗ {k}")
        for ml in msg.splitlines():
            lines.append(f"      {ml}")
    lines += [
        "",
        "After fixing env, this job will run again on the next dispatch.",
        "Smoke-test the wiring without consuming work:",
        "  gcloud run jobs execute ytfactory-render-worker-v2 \\",
        "    --region=asia-southeast1 --project=ytfactory-prod-v2 \\",
        "    --update-env-vars=YTFACTORY_PREFLIGHT_ONLY=1",
    ]
    return "\n".join(lines)


def _run_preflight_or_die(job_id: str | None) -> None:
    """Validate env. On failure: log + (optionally) mark the Firestore
    job as ``failed`` with a friendly error, then ``sys.exit(2)``.

    Honoured exit semantics: ``YTFACTORY_PREFLIGHT_ONLY=1`` exits 0 on
    success, 2 on failure — useful as a post-deploy smoke test.
    """
    problems = _preflight()
    preflight_only = (os.environ.get("YTFACTORY_PREFLIGHT_ONLY") or "").strip() in ("1", "true", "yes")

    if not problems:
        if preflight_only:
            logger.info("preflight OK — env wiring valid (YTFACTORY_PREFLIGHT_ONLY=1, exiting clean).")
            sys.exit(0)
        logger.info("preflight OK — env wiring valid.")
        return

    msg = _format_preflight_error(problems)
    # Always log to stderr so the Cloud Logging entry is grep-friendly.
    for line in msg.splitlines():
        logger.error("%s", line)

    # If we know the user-facing job id, flag the failure on Firestore
    # so the dashboard shows a useful error instead of "rendering" forever.
    if job_id:
        try:
            short_msg = "; ".join(f"{k} missing" for k, _ in problems)
            _update_job(
                job_id,
                status="failed",
                stage="bootstrap",
                error=(
                    "Cloud worker preflight failed — operator action required. "
                    f"{short_msg}. Full env-fix instructions in the worker logs "
                    f"(execution: {os.environ.get('CLOUD_RUN_EXECUTION', 'unknown')})."
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("preflight: also failed to mark Firestore job as failed: %s", e)

    sys.exit(2)


def _job_ref(job_id: str):
    return _firestore_client().collection("jobs").document(job_id)


def _empty_timeline(stages: list[tuple[str, str]] | None = None) -> list[dict]:
    """Build the initial timeline from a stage list.

    Defaults to the canonical 7-stage ``STAGES``. The Firestore-driven
    main loop passes :func:`_stages_for_job` so opted-in jobs see the
    8th ``editing_agent`` pill in the UI from the moment the job is
    accepted, not when the stage starts."""
    src = stages if stages is not None else STAGES
    return [{"stage": k, "label": label, "status": "pending"} for k, label in src]


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
    variant_key = (proposal.get("format") or "").strip() or None

    # Resolve variant overlay if the form specified one. Falls back to
    # the bare channel YAML when no variant is set or the variant file
    # is missing — keeps unrelated channels working.
    variant_yaml = _variant_yaml_for(channel_key, variant_key)
    base_channel_yaml = _channel_yaml_for(channel_key)
    # The renderer (RenderPaths.from_channel_yaml) accepts either the
    # base channel YAML OR a variant YAML and resolves the overlay
    # itself. Pass the variant YAML when we have one so the renderer
    # picks up the variant's tts_voice, image_style, etc.
    channel_yaml = variant_yaml or base_channel_yaml

    # Build channel_cfg for the rewrite stage by merging base + variant.
    channel_cfg: dict = {}
    if base_channel_yaml.exists():
        with base_channel_yaml.open() as fp:
            channel_cfg = yaml.safe_load(fp) or {}
    if variant_yaml and variant_yaml.exists():
        with variant_yaml.open() as fp:
            variant_cfg = yaml.safe_load(fp) or {}
        # Variant keys override channel keys (consistent with
        # RenderPaths.from_channel_yaml's overlay semantics).
        channel_cfg.update(variant_cfg)
        logger.info("variant overlay applied: %s", variant_yaml.relative_to(REPO_ROOT))

    job_id = job.get("job_id") or os.environ["YTFACTORY_JOB_ID"]
    slug = _slug_from_topic(proposal.get("topic") or "", job_id)

    # Source dispatch — when source_kind ∈ {reddit_url, wikipedia_topic,
    # youtube_video} AND source_ref is set, fetch the upstream content
    # and use IT as the rewrite input. Otherwise fall back to using the
    # user's typed `topic` + `notes` directly. The fetch is best-effort:
    # any failure logs a warning and degrades to the user-typed path so
    # the render never gets stuck on an upstream API hiccup.
    #
    # Known hazard: ``reddit_api.fetch`` is BLOCKED from Cloud Run egress
    # IPs (see docs/cloud_egress_blocked_apis.md). The fallback below
    # catches the 403 and uses the user's topic/notes instead — the
    # render still completes, just without the auto-fetched body.
    source_kind = (proposal.get("source_kind") or "auto").strip()
    source_ref = (proposal.get("source_ref") or "").strip()
    fetched: dict | None = None
    if source_ref and source_kind not in ("auto", "user_text", ""):
        try:
            fetched = _fetch_source(source_kind, source_ref)
            if fetched:
                logger.info("source fetch OK · kind=%s ref=%s len=%d",
                            source_kind, source_ref[:80], len(fetched.get("body", "")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("source fetch failed (kind=%s ref=%s): %s — "
                           "falling back to user topic/notes",
                           source_kind, source_ref[:80], exc)

    if fetched:
        raw_story = {
            "slug": slug,
            "title": fetched.get("title") or proposal.get("topic", ""),
            "body":  fetched.get("body") or proposal.get("notes", ""),
            "source": fetched.get("source") or source_kind,
            "url":    fetched.get("url") or source_ref,
        }
    else:
        raw_story = {
            "slug": slug,
            "title": (proposal.get("topic") or "").strip(),
            "body":  (proposal.get("notes") or "").strip(),
            "source": "user_text",
            "url": "",
        }
    if not raw_story["title"] and not raw_story["body"]:
        raise RuntimeError("proposal missing topic and source content")

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
    logger.info("rewrite OK · slug=%s script=%s channel_yaml=%s",
                slug, script_path,
                channel_yaml.relative_to(REPO_ROOT))


def _fetch_source(kind: str, ref: str) -> dict | None:
    """Adapter dispatcher for source_kind → fetched story dict.

    Returns ``{title, body, source, url}`` on success, ``None`` if the
    kind is unrecognised. Raises on upstream failure — caller catches
    and degrades to the user-typed fallback.
    """
    if kind == "reddit_url":
        # ref is a full Reddit post URL or permalink. Routes through
        # pipeline.sources.reddit_api.fetch_post_by_url which auto-picks
        # the best backend:
        #   1. OAuth (oauth.reddit.com) when REDDIT_CLIENT_ID + _SECRET set.
        #   2. Pullpush (api.pullpush.io) when running on Cloud Run
        #      without OAuth — open-source Pushshift fork, no auth,
        #      hours-delayed archival data.
        #   3. Anonymous JSON (works on laptop, 403s on Cloud Run).
        # See docs/cloud_egress_blocked_apis.md and
        # pipeline/sources/reddit_api.py::_pick_backend for the full
        # selection logic.
        from pipeline.sources.reddit_api import fetch_post_by_url  # noqa: PLC0415
        return fetch_post_by_url(ref)
    if kind == "wikipedia_topic":
        from pipeline.sources.wikipedia import fetch as wiki_fetch  # noqa: PLC0415
        results = wiki_fetch(ref, limit=1)
        if not results:
            return None
        s = results[0]
        return {
            "title": s.title,
            "body": s.body,
            "source": "wikipedia",
            "url": s.url or "",
        }
    if kind == "youtube_video":
        from pipeline.sources.youtube_video import fetch as yt_fetch  # noqa: PLC0415
        results = yt_fetch(ref)
        if not results:
            return None
        s = results[0]
        return {
            "title": s.title,
            "body": s.body,
            "source": "youtube",
            "url": s.url or ref,
        }
    return None


def _stage_cast_real(job: dict, work_dir: Path) -> None:
    """No-op for the v2 worker: the renderer subprocess invokes
    pipeline.llm.cast.author_cast as part of its first stage; we
    just emit a timeline marker.

    (Authoring cast here would require loading the script.json we
    just wrote, generating a cast.json, and persisting it — work
    that pipeline.render.shorts already does in its bootstrap. Keep
    the worker thin and let the renderer own it.)"""
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# Renderer-log → user-visible substep
# ---------------------------------------------------------------------------
#
# The renderer subprocess (``pipeline.render.shorts``) prints a
# well-known set of phase markers to stdout: ``[1/4] TTS``, ``[2/4]
# … timestamps``, ``[3/4] z_image_turbo: generating 22 images``,
# ``[3/4] [12/22]``, ``[image-done] beat 12 of 22``, ``[4/4] ffmpeg
# compose``, ``[compose] wrote slug.mp4`` and friends.
#
# Pre-2026-05-11 the cloud worker piped all of that to a log file and
# only emitted a coarse ``"real-mode"`` pill on the compose stage —
# leaving the user staring at "Composing video / compose / real-mode"
# for the entire 5-15 min substage. The matching laptop server
# (``web/server.py:_classify_line``) has parsed the same lines for
# years; we mirror just the ones a human cares about so the cloud
# render-detail page can show:
#
#     Composing video    compose    Image 12 of 22
#
# instead. Deliberately a small subset — keep this in sync with
# ``web/server.py`` regex bank when adding new markers.

# Sub-stages of the renderer subprocess in the order
# pipeline.render.shorts emits them. Used by ``_compose_progress`` to
# walk the timeline forward — when the renderer reaches images, tts /
# asr are marked done, etc.
_RENDERER_SUBSTAGES: tuple[str, ...] = ("tts", "asr", "images", "compose")

_REGEX_TTS_START = re.compile(r"^\[1/4\] TTS(?:\s*\(([^)]+)\))?")
_REGEX_TTS_CACHED = re.compile(r"^\[1/4\] TTS cached")
_REGEX_BEATS_START = re.compile(r"^\[2/4\] (\S+).+timestamps")
_REGEX_BEATS_CACHED = re.compile(r"^\[2/4\] beats cached")
_REGEX_BEATS_DONE = re.compile(r"^\s+(\d+) beats, total ([\d.]+)s")
_REGEX_PROMPTS_START = re.compile(r"^\[prompts\] authoring (\d+) beat prompts")
_REGEX_IMG_START = re.compile(r"^\[3/4\] (\S+): generating (\d+) (?:images|clips)")
_REGEX_IMG_BEAT = re.compile(r"^\s+\[(\d+)/(\d+)\]")
_REGEX_IMG_DONE = re.compile(r"^\[image-done\] beat (\d+) of (\d+)")
_REGEX_COMPOSE_START = re.compile(r"^\[4/4\] ffmpeg compose")
_REGEX_COMPOSE_RECOMPOSE = re.compile(r"^\[critic\] recomposing")
_REGEX_COMPOSE_DONE = re.compile(r"^\[compose\] wrote (.+\.mp4)")


def _classify_renderer_line(line: str) -> tuple[str, str] | None:
    """Translate a single renderer-stdout line into ``(stage_key,
    substep_msg)``, or ``None`` if the line carries no user-visible
    progress signal.

    ``stage_key`` is one of :data:`_RENDERER_SUBSTAGES` (``tts`` /
    ``asr`` / ``images`` / ``compose``) so the caller can attach the
    msg to the correct timeline pill instead of pinning every
    substep to ``compose``. Pre 2026-05-11 this returned just the
    msg, and the outer loop pinned everything to ``compose`` — so a
    user saw "Composing video / compose / Synthesizing narration"
    while ffmpeg hadn't started yet, plus images/tts/asr pills all
    marked "done" at t≈0s before any real work.

    Pure function — no I/O, no globals — so it's trivially testable
    and safe to call from the tailer thread.
    """
    s = line.rstrip("\r\n")

    if _REGEX_TTS_CACHED.match(s):
        return ("tts", "Reusing cached narration")
    if (m := _REGEX_TTS_START.match(s)):
        prov = (m.group(1) or "").strip()
        return ("tts",
                f"Synthesizing narration ({prov})" if prov
                else "Synthesizing narration")

    if (m := _REGEX_BEATS_START.match(s)):
        return ("asr", f"Aligning captions ({m.group(1)})")
    if _REGEX_BEATS_CACHED.match(s):
        return ("asr", "Reusing cached caption alignment")
    if (m := _REGEX_BEATS_DONE.match(s)):
        return ("asr", f"Aligned {m.group(1)} beats — {m.group(2)}s of audio")

    if (m := _REGEX_PROMPTS_START.match(s)):
        return ("images", f"Authoring {m.group(1)} image prompts")
    if (m := _REGEX_IMG_START.match(s)):
        return ("images",
                f"Generating {m.group(2)} images via {m.group(1)}")
    if (m := _REGEX_IMG_BEAT.match(s)):
        return ("images", f"Image {m.group(1)} of {m.group(2)}")
    if (m := _REGEX_IMG_DONE.match(s)):
        return ("images", f"Image {m.group(1)} of {m.group(2)} done")

    if _REGEX_COMPOSE_START.match(s):
        return ("compose", "Stitching video with ffmpeg")
    if _REGEX_COMPOSE_RECOMPOSE.match(s):
        return ("compose", "Recomposing after critic patch")
    if (m := _REGEX_COMPOSE_DONE.match(s)):
        return ("compose", f"Wrote {Path(m.group(1)).name}")

    return None


def _tail_renderer_log(
    log_path: Path,
    progress_cb: Callable[[str, str], None],
    stop_event: threading.Event,
    *,
    poll_interval: float = 1.5,
) -> None:
    """Background tailer: poll ``log_path`` for new complete lines,
    classify each, and invoke ``progress_cb(stage, msg)`` whenever the
    classified ``(stage, msg)`` tuple changes.

    ``progress_cb`` receives the renderer sub-stage key (``tts`` /
    ``asr`` / ``images`` / ``compose``) alongside the human-friendly
    msg so the caller can surface live progress on the **correct**
    timeline pill instead of pinning every substep to ``compose``.

    Polling (instead of inotify or a streaming Popen pipe) keeps the
    worker portable across the Cloud Run VM kernels we have no control
    over and avoids interleaving the subprocess's binary log writer
    with our own readers. The 1.5 s default cadence gives the user
    near-realtime feedback while costing ≤ 40 Firestore writes per
    minute even when the renderer fires a substep every other line.

    Always exits cleanly when ``stop_event`` is set so the calling
    thread can ``join()`` it without leaking the worker process.
    """
    pos = 0
    last_event: tuple[str, str] | None = None
    pending = b""
    while not stop_event.is_set():
        try:
            with log_path.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            pending += chunk
            # Hold back any trailing partial line until the next poll —
            # avoids classifying half-written substep markers that the
            # subprocess hasn't flushed yet.
            *complete, pending = pending.split(b"\n")
            for raw in complete:
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                event = _classify_renderer_line(line)
                if event is None or event == last_event:
                    continue
                last_event = event
                stage, msg = event
                try:
                    progress_cb(stage, msg)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "progress_cb failed for substep %r/%r",
                        stage, msg, exc_info=True,
                    )
        # Sleep in small slices so stop_event is honoured promptly.
        stop_event.wait(timeout=poll_interval)


def _run_renderer_subprocess(
    job: dict,
    work_dir: Path,
    *,
    progress_cb: Callable[[str, str], None] | None = None,
) -> Path:
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
    # Long-form rendering would normally dispatch to
    # ``pipeline.render.long_form`` (chunked TTS, archival footage matching,
    # 60-min sleep narrator). The cloud worker doesn't have that
    # dispatch yet — it always invokes ``pipeline.render.shorts``. When
    # the user picks "Long form" we still respect the duration cap
    # (forwarded as duration_max_s via _apply_form_overrides) but the
    # output remains an extended Shorts-style render. Surface this
    # clearly so an operator scanning the worker log doesn't expect a
    # 60-min sleep video.
    if isinstance(overrides, dict) and overrides.get("length_kind") == "long":
        ls = overrides.get("length_s")
        logger.warning(
            "length_kind=long requested (length_s=%s) — cloud worker dispatches "
            "to pipeline.render.shorts regardless; the output will be an "
            "EXTENDED Shorts render, not a true long-form sleep video. "
            "True long-form dispatch in cloud is tracked separately.",
            ls,
        )
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
    # Force the child Python to flush prints immediately. Without this
    # the renderer's [1/4] / [3/4] / [4/4] phase markers stay in the
    # interpreter's block buffer and only land in renderer.log when
    # the subprocess exits — defeating the substep tailer below.
    env.setdefault("PYTHONUNBUFFERED", "1")

    logger.info("renderer: %s", " ".join(cmd))
    t0 = time.time()
    # Pre-create the log so the tailer doesn't race the subprocess
    # creating it; FileNotFoundError on the first poll cycle would
    # otherwise drop the first batch of substep markers.
    log_path.touch()
    stop_event = threading.Event()
    tailer: threading.Thread | None = None
    if progress_cb is not None:
        tailer = threading.Thread(
            target=_tail_renderer_log,
            args=(log_path, progress_cb, stop_event),
            name="renderer-log-tailer",
            daemon=True,
        )
        tailer.start()
    try:
        with log_path.open("wb") as logfh:
            proc = subprocess.run(
                cmd, cwd=str(REPO_ROOT), env=env,
                stdout=logfh, stderr=subprocess.STDOUT, check=False,
            )
    finally:
        if tailer is not None:
            stop_event.set()
            tailer.join(timeout=5)
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


def _stage_render_real(
    job: dict,
    work_dir: Path,
    *,
    progress_cb: Callable[[str, str], None] | None = None,
) -> None:
    """Composite stage that covers images + tts + asr + compose by
    delegating to ``pipeline.render.shorts``. Returns the mp4 path
    via job['_real_mp4'].

    When ``progress_cb`` is supplied, the renderer's stdout is tailed
    in a background thread and each notable substep is forwarded as
    ``(stage_key, msg)`` so the caller can surface live progress on
    the **correct** timeline pill (tts / asr / images / compose)
    instead of pinning every substep to the umbrella compose stage.
    """
    mp4 = _run_renderer_subprocess(job, work_dir, progress_cb=progress_cb)
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


def _stage_editing_agent_real(job: dict, work_dir: Path) -> None:
    """Optional 8th stage: post-compose cinematic polish.

    Runs ONLY when ``proposal.editing.enabled`` is truthy on the job
    doc — the runtime stage list (:func:`_stages_for_job`) inserts
    this stage between ``compose`` and ``upload`` for opted-in jobs
    only.

    Reads the mp4 the compose stage produced (``job['_real_mp4']``),
    runs it through the editing-agent in polish mode (single-mp4
    auto-detect), and replaces ``job['_real_mp4']`` with the polished
    output so the upload stage picks the new file.

    Falls back gracefully: if the editing-agent service URL isn't
    configured OR the cloud call fails AND
    ``CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK`` is unset, the
    pipeline.editing.cloudrun client routes to the local executor
    (which uses the same ffmpeg already in this image).
    """
    from pipeline.editing.planner import plan_edit, manifest_for_inputs  # noqa: PLC0415
    from pipeline.editing.cloudrun import execute_edit  # noqa: PLC0415

    proposal = job.get("proposal") or {}
    editing_cfg = proposal.get("editing") or {}
    if not editing_cfg.get("enabled"):
        # Defensive: if we got dispatched anyway, no-op cleanly.
        logger.info("editing_agent stage requested but proposal.editing.enabled=false; skipping")
        return

    src_mp4 = Path(job.get("_real_mp4") or "")
    if not src_mp4.exists():
        raise RuntimeError(
            "editing_agent: compose stage didn't produce _real_mp4 — "
            "cannot polish. (Did the renderer subprocess fail silently?)"
        )

    work_input = work_dir / "editing_input"
    work_output = work_dir / "editing_output"
    work_input.mkdir(parents=True, exist_ok=True)
    work_output.mkdir(parents=True, exist_ok=True)
    staged = work_input / src_mp4.name
    if not staged.exists():
        # Hard-link if same FS, else copy.
        try:
            staged.hardlink_to(src_mp4)
        except (OSError, AttributeError):
            import shutil as _sh  # noqa: PLC0415
            _sh.copy2(src_mp4, staged)

    manifest = manifest_for_inputs([staged])
    director_notes = (editing_cfg.get("director_notes") or "").strip() or (
        f"Polish pass on a finished ytFactory Short. Channel: "
        f"{proposal.get('channel', 'unknown')}. Topic: "
        f"{(proposal.get('topic') or '').strip() or 'unspecified'}. "
        "Keep cuts conservative — trim only obvious dead air. "
        "Apply a subtle cinematic grade. Preserve burned-in captions."
    )
    target_duration = float(
        manifest[0].get("duration_s") or proposal.get("target_duration_s") or 60.0
    )
    aspect = editing_cfg.get("aspect") or "9:16"
    lut = editing_cfg.get("lut") or "cinematic.cube"
    edl = plan_edit(
        input_manifest=manifest,
        mode="polish",
        director_notes=director_notes,
        channel_profile={
            "channel": proposal.get("channel"),
            "lut_pref": lut,
        },
        target_duration_s=target_duration,
        aspect=aspect,
        duration_band=f"{int(target_duration)-3}-{int(target_duration)+3}s",
        model=editing_cfg.get("model") or "sonnet",
    )

    out_mp4 = execute_edit(
        edl,
        input_root=work_input,
        output_dir=work_output,
        output_name=f"{src_mp4.stem}__edited.mp4",
    )
    logger.info("editing_agent: %s → %s", src_mp4, out_mp4)

    # Replace the upload-stage's mp4 with the polished version. Leave
    # the original around in work_dir for debug.
    job["_real_mp4_pre_edit"] = str(src_mp4)
    job["_real_mp4"] = str(out_mp4)


# Registry: stage key → real handler. The renderer subprocess
# (``_stage_render_real``) covers tts / asr / images / compose as one
# umbrella step — that's why those three keys aren't in here. The main
# loop SKIPS them in real mode and lets ``_compose_progress`` walk the
# timeline through them as the renderer reaches each one. Pre 2026-05-11
# they were no-op ``time.sleep(0.05)`` lambdas that flashed each pill
# "running → done · 0.0s" before the real work started, leaving the
# user staring at "Composing video / compose / Synthesizing narration"
# with no signal that TTS was actually happening RIGHT NOW.
_REAL_HANDLERS: dict[str, Callable[[dict, Path], None]] = {
    "rewrite": _stage_rewrite_real,
    "cast":    _stage_cast_real,
    "compose": _stage_render_real,
    "editing_agent": _stage_editing_agent_real,
    "upload":  _stage_upload_real,
}


# Stub handler for the optional 8th stage — sleeps a bit so the
# timeline pill isn't suspiciously instant in stub mode.
def _stub_editing_agent_handler(key: str, job: dict, work_dir: Path) -> None:
    time.sleep(2)
    logger.info("editing_agent (stub) — would polish %s", job.get("_stub_mp4"))


def _stages_for_job(job: dict) -> list[tuple[str, str]]:
    """Return the stage list for THIS job. Inserts ``editing_agent``
    between compose and upload when ``proposal.editing.enabled`` is
    truthy. Default is the 7-stage canonical list (no editing pass).
    Pre-fix the worker hard-coded ``STAGES`` everywhere; that meant
    the optional stage either had to be in the canonical list (and
    silently skipped on every non-opted job, polluting the dashboard)
    or required forking the worker. Per-job stage list is the clean
    out."""
    proposal = job.get("proposal") or {}
    editing = proposal.get("editing") or {}
    if not editing.get("enabled"):
        return list(STAGES)
    out: list[tuple[str, str]] = []
    for key, label in STAGES:
        out.append((key, label))
        if key == "compose":
            out.append(("editing_agent", "Cinematic polish pass"))
    return out


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

    Both modes run :func:`_run_preflight_or_die` first — missing env
    surfaces as a clean exit(2) BEFORE any Firestore mark, so the
    user-visible job either runs cleanly or fails with a single-line
    "operator action required" error instead of a wall of traceback.
    """
    # OTel SDK boot — exports spans / metrics / logs to GCP. The
    # control plane scheduling this JOB sets YTFACTORY_TRACEPARENT
    # via gcloud --update-env-vars so the JOB's root span links back
    # to the chat-request span that started it.
    try:
        from otel_init import init as _otel_init, attach_traceparent_from_env
        _otel_init("render-worker-v2")
        attach_traceparent_from_env()
    except Exception:  # noqa: BLE001
        pass
    spec_uri = os.environ.get("JOB_SPEC_GCS_URI", "").strip()
    job_id = os.environ.get("YTFACTORY_JOB_ID", "").strip()

    # Preflight runs even when neither entry-mode env is set so
    # ``YTFACTORY_PREFLIGHT_ONLY=1`` works as a standalone smoke test
    # (no job to consume — just validate wiring + exit).
    _run_preflight_or_die(job_id or None)

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

    # Per-job stage list — inserts the optional editing_agent stage
    # between compose and upload when proposal.editing.enabled is true.
    job_stages = _stages_for_job(job)
    timeline = _empty_timeline(job_stages)
    _update_job(job_id, status="rendering", stage="rewrite", timeline=timeline)

    try:
        for key, _ in job_stages:
            # In real mode the renderer subprocess covers tts / asr /
            # images / compose as a single umbrella step (see
            # _RENDERER_SUBSTAGES). Skip the early "running" stamp for
            # the three NON-compose substages — _compose_progress will
            # flip them to "running" → "done" live as the renderer
            # actually reaches each one. Pre 2026-05-11 this loop
            # marked tts/asr/images "running" → handler (no-op
            # time.sleep(0.05)) → "done · 0.1s" all in the first 200
            # ms, then ran compose for 5-15 min — making the timeline
            # claim TTS finished at t≈0s with the user staring at
            # "Composing video / compose / Synthesizing narration"
            # while ffmpeg hadn't started yet.
            if mode == "real" and key in ("tts", "asr", "images"):
                continue

            timeline = _set_stage(timeline, key, "running",
                                  "real-mode" if mode == "real" else "stub-mode")
            _update_job(
                job_id,
                status="rendering" if key != "upload" else "uploading",
                stage=key,
                timeline=timeline,
            )
            t0 = time.time()
            if mode == "stub":
                # Stub path doesn't have a real editing handler — use
                # the dedicated stub so the timeline pill shows
                # progress for opted-in jobs.
                if key == "editing_agent":
                    _stub_editing_agent_handler(key, job, work_dir)
                else:
                    _run_stage_stub(key, job, work_dir)
            elif key == "compose":
                # Live substep reporting: the renderer subprocess
                # carries TTS → ASR → image-gen → ffmpeg under one
                # umbrella. The progress callback walks the timeline
                # forward — when a higher-numbered sub-stage starts,
                # all earlier ones are marked "done" with their own
                # elapsed time; the new one flips to "running" with
                # the substep msg. Updates Firestore on each substep
                # transition (≤ 40 writes/min per the tailer).
                substage_t0: dict[str, float] = {}

                def _compose_progress(stage: str, msg: str) -> None:
                    nonlocal timeline
                    if stage not in _RENDERER_SUBSTAGES:
                        # Defence-in-depth: classifier should never
                        # emit an unknown sub-stage, but if upstream
                        # adds one we fall back to attaching to
                        # compose so the user still sees progress.
                        stage = "compose"
                    now = time.time()
                    new_idx = _RENDERER_SUBSTAGES.index(stage)
                    # Mark every earlier sub-stage that's still
                    # "running" or "pending" as "done", stamped with
                    # how long it actually ran (or "—" if we never
                    # saw it start).
                    for prior in _RENDERER_SUBSTAGES[:new_idx]:
                        prior_status = next(
                            (s.get("status") for s in timeline
                             if s.get("stage") == prior),
                            None,
                        )
                        if prior_status in (None, "done"):
                            continue
                        prior_t0 = substage_t0.get(prior)
                        prior_msg = (
                            f"{now - prior_t0:.1f}s" if prior_t0
                            else "—"
                        )
                        timeline = _set_stage(timeline, prior, "done", prior_msg)
                    # Stamp the start of THIS sub-stage the first
                    # time we see it so its eventual "done · Xs" is
                    # accurate.
                    if stage not in substage_t0:
                        substage_t0[stage] = now
                    timeline = _set_stage(timeline, stage, "running", msg)
                    _update_job(
                        job_id,
                        status="rendering",
                        stage=stage,
                        timeline=timeline,
                    )
                _stage_render_real(job, work_dir, progress_cb=_compose_progress)
                # Final safety net: if the renderer finished without
                # emitting markers for some sub-stages (e.g. the TTS
                # cache hit fired BEFORE the tailer's first poll
                # cycle picked up the log), mark every sub-stage
                # "done" so the timeline never leaves a pill in
                # "running" or "pending" forever.
                final_now = time.time()
                for sub in _RENDERER_SUBSTAGES:
                    sub_status = next(
                        (s.get("status") for s in timeline
                         if s.get("stage") == sub),
                        None,
                    )
                    if sub_status == "done":
                        continue
                    sub_t0 = substage_t0.get(sub)
                    sub_msg = (
                        f"{final_now - sub_t0:.1f}s" if sub_t0
                        else "(skipped)"
                    )
                    timeline = _set_stage(timeline, sub, "done", sub_msg)
                _update_job(
                    job_id,
                    status="rendering",
                    stage="compose",
                    timeline=timeline,
                )
            else:
                handler = _REAL_HANDLERS.get(key, _run_stage_stub)
                handler(job, work_dir)  # type: ignore[arg-type]
            elapsed = time.time() - t0
            logger.info("stage %s done in %.1fs", key, elapsed)
            # The compose block already wrote each sub-stage's "done"
            # with its own per-stage elapsed. Don't overwrite the
            # umbrella key with the wall-clock total — that would
            # claim "compose" took the full TTS+ASR+images+compose
            # time, which the per-substage rows already account for.
            if mode == "real" and key == "compose":
                continue
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
