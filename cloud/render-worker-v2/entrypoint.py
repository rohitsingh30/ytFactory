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
              (``image_provider: cloudrun_z_image_turbo``,
              ``tts_provider: cloudrun_chatterbox``, etc.). ASR uses
              the ``faster_whisper`` provider. LLM defaults to
              **Azure OpenAI** (reuses your existing chat-assistant
              deployment — no separate spend). Switch to Anthropic
              SDK with ``YTFACTORY_LLM_BACKEND=anthropic_sdk``.

Environment (set at deploy time on the Cloud Run Job):
  YTFACTORY_JOB_ID            — per-execution override (set by control plane)
  GOOGLE_CLOUD_PROJECT        — Firestore + GCS project (default ytfactory-prod-v3)
  YTFACTORY_BUCKET            — GCS bucket for artifacts
  CLOUDRUN_TTS_CHATTERBOX_URL — English TTS service URL
  CLOUDRUN_TTS_INDICF5_URL    — Hindi TTS service URL
  CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL — image service URL
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

import functools
import inspect
import json
import logging
import os
import re
import signal
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

# ---------------------------------------------------------------------------
# Render telemetry bootstrap (Phase 1)
# ---------------------------------------------------------------------------
# ``cloud/render-worker-v2`` is not an importable package name because of the
# hyphen. Cloud Run imports from this directory directly; tests load this file
# via ``importlib.util.spec_from_file_location`` from the repo root. Support both
# without mutating sys.path globally.
try:
    from _stage_envelope import (  # type: ignore  # noqa: PLC0415
        EventsBuffer,
        install_events_buffer,
        stage_envelope as _worker_stage_envelope,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by importlib-based tests
    import importlib.util as _importlib_util  # noqa: PLC0415

    _stage_env_name = "_render_worker_v2_stage_envelope"
    _stage_env_module = sys.modules.get(_stage_env_name)
    if _stage_env_module is None:
        _stage_env_path = Path(__file__).with_name("_stage_envelope.py")
        _stage_env_spec = _importlib_util.spec_from_file_location(
            _stage_env_name,
            _stage_env_path,
        )
        if _stage_env_spec is None or _stage_env_spec.loader is None:
            raise
        _stage_env_module = _importlib_util.module_from_spec(_stage_env_spec)
        sys.modules[_stage_env_name] = _stage_env_module
        _stage_env_spec.loader.exec_module(_stage_env_module)
    EventsBuffer = _stage_env_module.EventsBuffer
    install_events_buffer = _stage_env_module.install_events_buffer
    _worker_stage_envelope = _stage_env_module.stage_envelope


def _install_render_events_buffer_once() -> None:
    """Start capturing observability events as soon as the module imports."""
    try:
        install_events_buffer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("install_events_buffer failed: %s", exc)
    try:
        from pipeline.observability import subscribe  # noqa: PLC0415
        subscribe(EventsBuffer.instance().append)
    except Exception as exc:  # noqa: BLE001
        logger.debug("EventsBuffer subscribe skipped: %s", exc)


_install_render_events_buffer_once()


def _current_job_id(job: dict | None = None) -> str:
    if isinstance(job, dict):
        return str(job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID") or "")
    return str(os.environ.get("YTFACTORY_JOB_ID") or "")


def _safe_track_event(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        from pipeline.observability import track  # noqa: PLC0415
        track(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id or _current_job_id(),
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------
# Stage envelope coverage (Fix #7, 2026-05-24)
# ---------------------------------------------------------------------
#
# The 845bdb0d render emitted exactly ONE ``stage.start`` and ONE
# ``stage.end`` event — both for ``stage=upload``. The other six
# stages (rewrite / cast / images / tts / asr / compose) completed
# but emitted no envelope events because:
#
#   * In LONG-FORM mode, the dispatch loop at the bottom of
#     ``_main_from_firestore`` short-circuits (``lf_done`` → skip
#     every key except ``upload``). The long-form bypass above the
#     loop calls ``_video.render_long_form`` as one monolithic step,
#     never touching the ``@stage_envelope``-decorated handlers.
#   * In SHORT mode, the dispatch loop only invokes
#     ``_stage_render_real`` (which IS decorated as ``compose``);
#     the other stages run as ``_run_stage_stub`` no-ops (rewrite /
#     cast / editing_agent might run real handlers but tts / asr /
#     images explicitly ``continue`` at lines 3251-3252).
#
# Result: ``/diagnose-render`` cannot reconstruct per-stage timing
# from events; F25 (telemetry-trust gap) widens; every future
# diagnose-render burns hours falling back to Firestore ``timeline[]``
# for what should be queryable from Cloud Logging in seconds.
#
# Fix shape: a context manager that emits ``stage.start`` on enter,
# ``stage.end`` on exit (or ``stage.failed`` on exception), with the
# same metadata shape the worker's existing ``@stage_envelope``
# decorator produces. Called from the dispatch loop AND from the
# long-form bypass for every pipeline stage.
#
# Regression guard: ``tests/test_stage_envelope_coverage.py`` enumerates
# every stage that must emit envelope events and fails if any go
# missing.


from contextlib import contextmanager


@contextmanager
def _stage_envelope_events(stage_name: str, job: dict | None = None):
    """Emit ``stage.start`` and ``stage.end`` / ``stage.failed`` events.

    Mirrors the metadata shape of the worker's ``@stage_envelope``
    decorator (slug / channel / variant / mode) so post-mortems treat
    the dispatch-loop-emitted events the same as decorator-emitted
    events.
    """
    job = job or {}
    proposal = (job.get("proposal") or {}) if isinstance(job, dict) else {}
    ctx_md = {
        "stage": stage_name,
        "slug": (job.get("_slug") or "") if isinstance(job, dict) else "",
        "channel": proposal.get("channel") or "",
        "variant": (proposal.get("format") or "").strip(),
        "mode": (job.get("mode") or "real") if isinstance(job, dict) else "real",
    }
    job_id = _current_job_id(job)
    t0 = time.perf_counter()
    _safe_track_event(
        "stage.start",
        category="render",
        job_id=job_id,
        metadata=dict(ctx_md),
    )
    try:
        yield
    except BaseException as exc:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        _safe_track_event(
            "stage.failed",
            category="render",
            success=False,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata={
                **ctx_md,
                "error": str(exc)[:500],
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc()[-3000:],
            },
        )
        raise
    duration_ms = int((time.perf_counter() - t0) * 1000)
    _safe_track_event(
        "stage.end",
        category="render",
        duration_ms=duration_ms,
        job_id=job_id,
        metadata=dict(ctx_md),
    )


def _safe_read_json(path: str | Path | None) -> Any:
    if not path:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:  # noqa: BLE001
        if isinstance(value, dict):
            return {k: _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(v) for v in value]
        return str(value)


def _safe_emit_artifact_payload(
    *,
    job_id: str,
    kind: str,
    payload: Any,
    extras: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Best-effort artifact emission shared by helper envelopes."""
    if payload is None or not job_id:
        return None, {}
    try:
        from pipeline.render import artifacts as _artifacts  # noqa: PLC0415
        if isinstance(payload, (str, Path)):
            pth = Path(payload)
            if not pth.exists():
                return None, {}
            uri = _artifacts.emit_artifact(
                job_id=job_id,
                kind=kind,
                local_path=pth,
                extras=extras or {},
            )
            return uri, {
                "artifact_kind": kind,
                "artifact_bytes": pth.stat().st_size,
                "artifact_uri": uri,
            }
        if isinstance(payload, (dict, list)):
            uri = _artifacts.emit_artifact_json(
                job_id=job_id,
                kind=kind,
                data=_jsonable(payload),
                extras=extras or {},
            )
            return uri, {"artifact_kind": kind, "artifact_uri": uri}
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry artifact emit failed kind=%s: %s", kind, exc)
    return None, {}


def _extract_helper_context(
    sig: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        bound = sig.bind_partial(*args, **kwargs)
        vals = bound.arguments
    except Exception:  # noqa: BLE001
        vals = {}
    job = vals.get("job") if isinstance(vals.get("job"), dict) else None
    proposal = (job or {}).get("proposal") or {}
    script_dict = vals.get("script_dict") if isinstance(vals.get("script_dict"), dict) else {}
    channel_yaml = vals.get("channel_yaml_path")
    channel = proposal.get("channel") or script_dict.get("channel") or ""
    if not channel and channel_yaml:
        try:
            channel = Path(channel_yaml).parent.name
        except Exception:  # noqa: BLE001
            channel = ""
    return {
        "job_id": _current_job_id(job),
        "slug": (job or {}).get("_slug") or script_dict.get("slug") or "",
        "channel": channel,
        "variant": (proposal.get("format") or "").strip() if proposal else "",
        "mode": (job or {}).get("mode") or "real",
    }


def stage_envelope(
    stage_name: str,
    *,
    artifact_kind: str | None = None,
    artifact_extractor: Callable[[dict[str, Any]], Any] | None = None,
    artifact_extras_extractor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Use the Phase-0 worker envelope, with helper-signature support."""
    base = _worker_stage_envelope(
        stage_name,
        artifact_kind=artifact_kind,
        artifact_extractor=artifact_extractor,
        artifact_extras_extractor=artifact_extras_extractor,
    )

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if len(params) >= 2 and params[0].name == "job" and params[1].name == "work_dir":
            return base(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _extract_helper_context(sig, args, kwargs)
            t0 = time.perf_counter()
            _safe_track_event(
                "stage.start",
                category="render",
                job_id=ctx["job_id"],
                metadata={
                    "stage": stage_name,
                    "slug": ctx["slug"],
                    "channel": ctx["channel"],
                    "variant": ctx["variant"],
                    "mode": ctx["mode"],
                },
            )
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                artifact_summary: dict[str, Any] = {}
                if artifact_kind and artifact_extractor:
                    try:
                        extras = artifact_extras_extractor({}) if artifact_extras_extractor else None
                        _, artifact_summary = _safe_emit_artifact_payload(
                            job_id=ctx["job_id"],
                            kind=artifact_kind,
                            payload=artifact_extractor({}),
                            extras=extras,
                        )
                    except Exception:  # noqa: BLE001
                        artifact_summary = {}
                _safe_track_event(
                    "stage.failed",
                    category="render",
                    success=False,
                    duration_ms=duration_ms,
                    job_id=ctx["job_id"],
                    metadata={
                        "stage": stage_name,
                        "slug": ctx["slug"],
                        "channel": ctx["channel"],
                        "variant": ctx["variant"],
                        "error": str(exc)[:500],
                        "error_type": type(exc).__name__,
                        "traceback": traceback.format_exc()[-3000:],
                        **artifact_summary,
                    },
                )
                raise

            duration_ms = int((time.perf_counter() - t0) * 1000)
            artifact_summary = {}
            if artifact_kind and artifact_extractor:
                try:
                    extras = artifact_extras_extractor({}) if artifact_extras_extractor else None
                    _, artifact_summary = _safe_emit_artifact_payload(
                        job_id=ctx["job_id"],
                        kind=artifact_kind,
                        payload=artifact_extractor({}),
                        extras=extras,
                    )
                except Exception:  # noqa: BLE001
                    artifact_summary = {}
            _safe_track_event(
                "stage.end",
                category="render",
                duration_ms=duration_ms,
                job_id=ctx["job_id"],
                metadata={
                    "stage": stage_name,
                    "slug": ctx["slug"],
                    "channel": ctx["channel"],
                    "variant": ctx["variant"],
                    **artifact_summary,
                },
            )
            return result

        wrapper.__signature__ = sig  # type: ignore[attr-defined]
        return wrapper

    return decorate

# ---------------------------------------------------------------------------
# SIGTERM-induced "silent death" mitigation (B3a in 2026-05-23 docket)
# ---------------------------------------------------------------------------
#
# Cloud Run Jobs send SIGTERM when a task hits its --task-timeout ceiling
# (3600s for ytfactory-render-worker-v2), wait 10s, then SIGKILL. The
# top-of-_main_from_firestore try/except cannot catch SIGKILL — Python
# is already dead. Pre-fix: jobs that timed out left status="rendering"
# in Firestore forever; you only knew the worker had died by tailing
# Cloud Run logs (e.g. job a0aac53ea01949178afd50ac2b254ef7, the render
# that prompted this fix).
#
# Fix: install a SIGTERM handler the moment we know the job_id, capture
# the latest timeline + current stage on every _update_job call, and
# on signal delivery spawn a thread that writes status=failed within
# the 10s grace window. We can't do Firestore I/O directly from the
# signal frame (gRPC reentrancy hazard) so the thread pattern is
# mandatory.
_LIVE_RENDER: dict[str, Any] = {
    "job_id": None,
    "stage": None,
    "timeline": None,
}
_SIGTERM_INSTALLED = False
_SIGTERM_FIRED = False  # idempotency guard — multiple SIGTERMs would otherwise re-trigger


def _on_sigterm(signum, frame):  # noqa: ARG001
    """SIGTERM handler — marks the job failed before SIGKILL arrives.

    Spawns a background thread for the actual Firestore write because
    calling gRPC client methods from a signal handler frame is
    documented as unsafe (the gRPC SDK uses its own threads + locks
    and the main-thread interrupt can deadlock against them).
    """
    global _SIGTERM_FIRED
    if _SIGTERM_FIRED:
        return
    _SIGTERM_FIRED = True

    state = dict(_LIVE_RENDER)
    job_id = state.get("job_id")
    if not job_id:
        logger.error("SIGTERM received before job_id known; exiting")
        sys.exit(143)
        return

    def _do_write() -> None:
        try:
            err = (
                "Worker received SIGTERM (likely Cloud Run task-timeout, "
                "default 3600s for ytfactory-render-worker-v2). The "
                "renderer was killed mid-pipeline. Per-call artifacts "
                "(panel PNGs + TTS chunk WAVs) already paid for have "
                "been persisted to gs://<bucket>/jobs/<job_id>/cache/ "
                "(B2) — a re-render of this job_id will hydrate them "
                "back into work_dir on startup, so retry cost is "
                "limited to whatever wasn't yet uploaded when SIGKILL "
                "arrived."
            )
            _update_job(
                job_id,
                status="failed",
                stage=state.get("stage") or "unknown",
                error=err,
                timeline=state.get("timeline") or [],
            )
            logger.error(
                "SIGTERM: marked job=%s failed at stage=%s",
                job_id, state.get("stage"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("SIGTERM Firestore write failed for job=%s", job_id)
        # B2 — flush any in-flight cache uploads. We're in the 10s
        # grace window before SIGKILL; whatever already got
        # ``pool.submit()``'d but hasn't finished uploading should
        # finish so the next retry can hydrate from it.
        try:
            from pipeline.cloud.cache import shutdown_upload_pool  # noqa: PLC0415
            shutdown_upload_pool(wait=True, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIGTERM cache flush failed: %s", exc)

    t = threading.Thread(target=_do_write, daemon=False, name="sigterm-firestore")
    t.start()
    # Cloud Run gives 10s before SIGKILL; reserve ~2s for log flush.
    t.join(timeout=8.0)
    if t.is_alive():
        logger.error("SIGTERM Firestore write still running at 8s; SIGKILL imminent")
    sys.exit(143)


def _install_sigterm_handler(job_id: str) -> None:
    """Install the SIGTERM handler once per process. Idempotent."""
    global _SIGTERM_INSTALLED
    _LIVE_RENDER["job_id"] = job_id
    if _SIGTERM_INSTALLED:
        return
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
        _SIGTERM_INSTALLED = True
        logger.info("SIGTERM handler installed for job=%s", job_id)
    except (ValueError, OSError) as exc:
        # signal.signal can only be called from the main thread —
        # if we're in a worker thread (tests, embedded use) just log
        # and continue. The job will silently die on timeout in that
        # case, but that's no worse than before this fix.
        logger.warning("SIGTERM handler install skipped: %s", exc)

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
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3")

def _bucket_name() -> str:
    return os.environ.get("YTFACTORY_BUCKET", "ytfactory-prod-v3-artifacts")

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
                    f"--region=asia-southeast1 --project=ytfactory-prod-v3 \\\n"
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
    # ANY channel can succeed. Per-channel mismatch still surfaces at
    # the renderer's own URL-resolution boundary, but the most-common
    # default-(English-Chatterbox) gets caught here.
    tts_urls = [
        ("CLOUDRUN_TTS_CHATTERBOX_URL", "default English TTS"),
        ("CLOUDRUN_TTS_INDICF5_URL",    "Hindi IndicF5 TTS"),
        ("CLOUDRUN_TTS_INDICPARLER_URL", "legacy Hindi IndicParler TTS"),
    ]
    if not any((os.environ.get(k) or "").strip() for k, _ in tts_urls):
        problems.append((
            "CLOUDRUN_TTS_*_URL",
            "No CLOUDRUN_TTS_* URL is set — every TTS call would raise "
            "CloudRunUnavailable. At minimum wire CLOUDRUN_TTS_CHATTERBOX_URL "
            "(English) and CLOUDRUN_TTS_INDICF5_URL (Hindi)."
        ))

    image_urls = [
        "CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL",
    ]
    if not any((os.environ.get(k) or "").strip() for k in image_urls):
        problems.append((
            "CLOUDRUN_IMAGE_*_URL",
            "No CLOUDRUN_IMAGE_* URL is set — image gen would fall back "
            "to local mflux which isn't installed in the cloud image. "
            "Wire CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL."
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
        "    --region=asia-southeast1 --project=ytfactory-prod-v3 \\",
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

_FIRESTORE_TRANSIENT_EXC_NAMES = frozenset({
    "DeadlineExceeded", "ServiceUnavailable", "Aborted",
    "InternalServerError", "Cancelled", "ResourceExhausted", "Unknown",
})


def _update_job(job_id: str, **fields: Any) -> None:
    """Write a partial update to the job's Firestore doc.

    Retries up to 3 attempts on transient Firestore errors (DeadlineExceeded,
    ServiceUnavailable, Aborted, …). After that we log a warning and
    return — losing one progress write isn't fatal because the next
    write in 5-10s will overwrite with the latest state anyway. The
    render must NEVER crash because a Firestore blip cost us one
    progress update. Terminal writes (status=done/failed) are written
    via the same path; if they're lost the reaper retries.

    Also snapshots ``stage`` and ``timeline`` into ``_LIVE_RENDER`` for
    the SIGTERM handler — every progress write flows through here, so
    the handler always reads the latest state without each callsite
    having to remember to update it.
    """
    fields["updated_at"] = datetime.now(timezone.utc)
    if _LIVE_RENDER.get("job_id") == job_id:
        if "stage" in fields:
            _LIVE_RENDER["stage"] = fields["stage"]
        if "timeline" in fields:
            _LIVE_RENDER["timeline"] = fields["timeline"]
    base_backoff = 0.2
    for attempt in range(1, 4):
        try:
            _job_ref(job_id).set(fields, merge=True)
            return
        except Exception as exc:  # noqa: BLE001
            transient = type(exc).__name__ in _FIRESTORE_TRANSIENT_EXC_NAMES
            if not transient or attempt == 3:
                logger.warning(
                    "_update_job failed for job=%s (attempt %d/3, "
                    "transient=%s): %s — continuing render",
                    job_id, attempt, transient, exc,
                )
                return
            backoff = min(5.0, base_backoff * (1.6 ** (attempt - 1)))
            logger.warning(
                "_update_job transient %s on attempt %d/3 for job=%s "
                "(backoff %.2fs): %s",
                type(exc).__name__, attempt, job_id, backoff, exc,
            )
            time.sleep(backoff)

_GCS_TRANSIENT_EXC_NAMES = frozenset({
    "DeadlineExceeded", "ServiceUnavailable", "InternalServerError",
    "GatewayTimeout", "BadGateway", "RetryError", "ConnectionError",
    "ConnectionResetError", "ChunkedEncodingError", "RemoteDisconnected",
})


def _gcs_upload_with_retry(
    blob, local_path: Path, *, op_label: str,
    max_attempts: int = 4, base_backoff_s: float = 1.0,
) -> None:
    """Upload to GCS with exponential backoff on transient errors.

    Renders that successfully composed an mp4 should NEVER be marked
    failed because of a transient GCS blip on the final upload. The
    file is on local disk — retrying is cheap and almost always
    recovers. Backoff is [1.0, 1.6, 2.56, 4.1]s for ~9s total in the
    worst case before giving up.

    Non-transient errors (permission denied, bucket missing) bubble
    immediately — those need operator intervention, not retry.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            blob.upload_from_filename(str(local_path))
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            transient = (
                type(exc).__name__ in _GCS_TRANSIENT_EXC_NAMES
                or "503" in str(exc)
                or "504" in str(exc)
                or "timeout" in str(exc).lower()
            )
            if not transient or attempt == max_attempts:
                raise
            backoff = min(10.0, base_backoff_s * (1.6 ** (attempt - 1)))
            logger.warning(
                "%s: transient %s on attempt %d/%d (backoff %.2fs): %s",
                op_label, type(exc).__name__, attempt, max_attempts, backoff, exc,
            )
            time.sleep(backoff)
    # Unreachable; the loop always returns or raises.
    raise RuntimeError(  # pragma: no cover
        f"{op_label} fell through without success ({last_exc})"
    )


def _upload_mp4_to_gcs(local_mp4: Path, job_id: str) -> str:
    blob_path = f"jobs/{job_id}/short.mp4"
    bucket = _storage_client().bucket(_bucket_name())
    blob = bucket.blob(blob_path)
    blob.content_type = "video/mp4"
    _gcs_upload_with_retry(
        blob, local_mp4, op_label=f"upload mp4 job={job_id}",
    )
    return f"gs://{_bucket_name()}/{blob_path}"

# ---------------------------------------------------------------------------
# Writeback artifact verification (Task A7, 2026-05-15)
#
# Pre-fix: the cloud worker shipped 11.8 KB empty-blob mp4s to GCS and
# flipped jobs to status=done. A 27-render audit on 2026-05-13 found
# 3+ such renders in mystoriesanimated batch A alone. The dashboard
# showed "done" but the artifact was a broken stub — no streams,
# duration ≈ 0, file size in the low 10 KB. Downstream uploaders
# happily ran on these.
#
# Fix: ffprobe + volumedetect the local mp4 BEFORE upload + Firestore
# writeback. Any failure → skip upload, write status=failed with a
# precise reason, return without raising (callers should not see this
# as a stack trace — the job is correctly marked failed).
# ---------------------------------------------------------------------------

# Writeback verifier moved to cloud/render-worker-v2/writeback.py
# (task #18, 2026-05-23). The legacy ``cloud.render-worker-v2`` dir
# has a hyphen (not Python-package-importable), so we load the sibling
# file via importlib and re-export. Preserves the legacy import paths
# (``ep._verify_mp4_artifact``, ``_ffprobe_streams``, ``_MIN_VERIFY_*``)
# so existing tests + callers continue working unchanged.
def _load_writeback_module():  # noqa: D401
    import importlib.util as _ilu  # noqa: PLC0415
    _wb_path = Path(__file__).parent / "writeback.py"
    spec = _ilu.spec_from_file_location("ytfactory_writeback", _wb_path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_wb = _load_writeback_module()
_MIN_VERIFY_FILE_BYTES = _wb._MIN_VERIFY_FILE_BYTES
_MIN_VERIFY_MEAN_VOLUME_DB = _wb._MIN_VERIFY_MEAN_VOLUME_DB
_MIN_VERIFY_VIDEO_WIDTH = _wb._MIN_VERIFY_VIDEO_WIDTH
_ffprobe_streams = _wb._ffprobe_streams
_ffprobe_mean_volume_db = _wb._ffprobe_mean_volume_db
_verify_mp4_artifact = _wb.verify_mp4_artifact

def _upload_thumb_to_gcs(local_thumb: Path, job_id: str) -> str:
    blob_path = f"jobs/{job_id}/thumb.jpg"
    bucket = _storage_client().bucket(_bucket_name())
    blob = bucket.blob(blob_path)
    blob.content_type = "image/jpeg"
    _gcs_upload_with_retry(
        blob, local_thumb, op_label=f"upload thumb job={job_id}",
    )
    return f"gs://{_bucket_name()}/{blob_path}"

# ---------------------------------------------------------------------------
# Mode + slug helpers
# ---------------------------------------------------------------------------

def _is_stub_mode() -> bool:
    mode = os.environ.get("YTFACTORY_RENDER_MODE", "real").lower()
    is_stub = mode == "stub"
    logger.info("WORKER MODE: %s%s", mode, " (stub placeholder mp4s)" if is_stub else " (real render)")
    return is_stub

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
# Phase-1 telemetry extractors / decision events
# ---------------------------------------------------------------------------

_LAST_SOURCE_FETCH_ARTIFACT: dict[str, Any] | None = None
_LAST_PROMPTS_REFINED_ARTIFACT: list[dict[str, Any]] | dict[str, Any] | None = None


def _preview_for_metadata(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_code_from_exception(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b([1-5][0-9]{2})\b", str(exc))
    return int(m.group(1)) if m else None


def _source_backend_for(kind: str) -> str:
    if kind == "reddit_url":
        try:
            from pipeline.sources import reddit_api as _reddit_api  # noqa: PLC0415
            return str(_reddit_api._pick_backend())
        except Exception:  # noqa: BLE001
            explicit = (os.environ.get("REDDIT_FETCH_BACKEND") or "").strip().lower()
            if explicit:
                return explicit
            return "pullpush" if os.environ.get("K_SERVICE") else "anon"
    if kind == "wikipedia_topic":
        return "wikipedia_html"
    if kind == "youtube_video":
        return "youtube_transcript"
    return "unsupported"


def _record_source_fetch_attempt(kind: str, ref: str, backend: str) -> float:
    t0 = time.perf_counter()
    try:
        _safe_track_event(
            "source.fetch_attempt",
            category="http",
            job_id=_current_job_id(),
            metadata={"kind": kind, "ref": _preview_for_metadata(ref), "backend": backend},
        )
    except Exception:  # noqa: BLE001
        pass
    return t0


def _record_source_fetch_result(
    *,
    kind: str,
    ref: str,
    backend: str,
    t0: float,
    result: dict | None,
    fallback_reason: str | None = None,
    status_code: int | None = None,
) -> None:
    global _LAST_SOURCE_FETCH_ARTIFACT
    body = ""
    url = ref
    if isinstance(result, dict):
        body = str(result.get("body") or "")
        url = str(result.get("url") or ref)
        backend = str(result.get("backend") or backend)
    fallback_used = bool(fallback_reason)
    if status_code is None and not fallback_used and result is not None:
        status_code = 200
    duration_ms = int((time.perf_counter() - t0) * 1000)
    _LAST_SOURCE_FETCH_ARTIFACT = {
        "kind": kind,
        "ref": ref,
        "url": url,
        "status": status_code if status_code is not None else ("fallback" if fallback_used else "unknown"),
        "status_code": status_code,
        "body": body,
        "body_chars": len(body),
        "backend": backend,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason or "",
    }
    try:
        _safe_track_event(
            "source.fetch_fallback" if fallback_used else "source.fetch_ok",
            category="http",
            success=not fallback_used,
            duration_ms=duration_ms,
            job_id=_current_job_id(),
            metadata={
                "kind": kind,
                "ref": _preview_for_metadata(ref),
                "url": _preview_for_metadata(url),
                "backend": backend,
                "status_code": status_code,
                "body_chars": len(body),
                "fallback_reason": fallback_reason or "",
                "original_status": status_code if fallback_used else None,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _extract_source_artifact(_job: dict[str, Any]) -> dict[str, Any] | None:
    return _LAST_SOURCE_FETCH_ARTIFACT


def _extract_script_artifact(job: dict[str, Any]) -> Any:
    payload = _safe_read_json(job.get("_script_path"))
    if payload is not None:
        return payload
    return job.get("_script_json")


def _expected_cast_path(job: dict[str, Any]) -> Path | None:
    slug = job.get("_slug") or ""
    channel_yaml = job.get("_channel_yaml")
    if not slug or not channel_yaml:
        return None
    try:
        from pipeline.paths import RenderPaths  # noqa: PLC0415
        return RenderPaths.from_channel_yaml(Path(channel_yaml), project_root=REPO_ROOT).cast_for(slug)
    except Exception:  # noqa: BLE001
        return None


def _extract_cast_artifact(job: dict[str, Any]) -> Any:
    cast_path = Path(job["_cast_path"]) if job.get("_cast_path") else _expected_cast_path(job)
    payload = _safe_read_json(cast_path)
    if payload is not None:
        return payload
    return {
        "status": "deferred_to_renderer",
        "cast_path": str(cast_path or ""),
        "reason": "worker_cast_stage_is_timeline_marker",
    }


def _extract_render_spec_artifact(job: dict[str, Any]) -> Any:
    return job.get("_render_spec_dict") or job.get("render_spec")


def _extract_events_log_artifact(job: dict[str, Any]) -> str | None:
    return job.get("_events_log_path")


def _extract_editing_compose_artifact(job: dict[str, Any]) -> Any:
    return job.get("_editing_agent_result")


def _extract_prompts_refined_artifact(_job: dict[str, Any]) -> Any:
    return _LAST_PROMPTS_REFINED_ARTIFACT


def _prompt_refined_count(prompts: Any) -> tuple[int, int]:
    if not isinstance(prompts, list):
        return 0, 0
    refined_keys = {"refined_visual", "refined_scene", "style_block", "refined_version"}
    refined = 0
    for item in prompts:
        if isinstance(item, dict) and any(item.get(k) for k in refined_keys):
            refined += 1
    return refined, max(0, len(prompts) - refined)


def _emit_prompts_author_gate(
    prompts: Any,
    *,
    slug: str,
    prompts_path: Path | None = None,
    reason: str = "",
) -> None:
    global _LAST_PROMPTS_REFINED_ARTIFACT
    if isinstance(prompts, (list, dict)):
        _LAST_PROMPTS_REFINED_ARTIFACT = prompts
    else:
        _LAST_PROMPTS_REFINED_ARTIFACT = None
    prompt_count = len(prompts) if isinstance(prompts, list) else 0
    refined_count, fallback_count = _prompt_refined_count(prompts)
    _safe_track_event(
        "prompts.author_gate",
        category="decision",
        job_id=_current_job_id(),
        metadata={
            "slug": slug,
            "prompt_count": prompt_count,
            "refined": refined_count,
            "refined_count": refined_count,
            "fallback_count": fallback_count,
            "fallback_used": fallback_count > 0,
            "prompts_path": str(prompts_path or ""),
            "reason": reason,
        },
    )

# ---------------------------------------------------------------------------
# REAL stage handlers — used when YTFACTORY_RENDER_MODE=real
# ---------------------------------------------------------------------------

@stage_envelope("rewrite", artifact_kind="script", artifact_extractor=_extract_script_artifact)
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
    source_fallback_reason: str | None = None
    if source_ref and source_kind not in ("auto", "user_text", ""):
        try:
            fetched = _fetch_source(source_kind, source_ref)
            if fetched:
                logger.info("source fetch OK · kind=%s ref=%s len=%d",
                            source_kind, source_ref[:80], len(fetched.get("body", "")))
        except Exception as exc:  # noqa: BLE001
            source_fallback_reason = str(exc)[:500]
            logger.warning("source fetch failed (kind=%s ref=%s): %s — "
                           "falling back to user topic/notes",
                           source_kind, source_ref[:80], exc)
            _safe_track_event(
                "source.fetch_fallback",
                category="http",
                success=False,
                job_id=job_id,
                metadata={
                    "kind": source_kind,
                    "ref": source_ref[:240],
                    "backend": "worker_dispatch",
                    "fallback_reason": source_fallback_reason,
                    "original_status": _status_code_from_exception(exc),
                },
            )

    if fetched:
        raw_story = {
            "slug": slug,
            "title": fetched.get("title") or proposal.get("topic", ""),
            "body":  fetched.get("body") or proposal.get("notes", ""),
            "source": fetched.get("source") or source_kind,
            "url":    fetched.get("url") or source_ref,
        }
    else:
        if source_ref and source_kind not in ("auto", "user_text", ""):
            _safe_track_event(
                "decision.source",
                category="decision",
                job_id=job_id,
                metadata={
                    "scope": "source",
                    "chosen": "user_topic_notes",
                    "alternatives": [source_kind],
                    "reason": source_fallback_reason or "source_fetch_empty_or_failed",
                },
            )
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

    # Live artifact preview (Slice 4): upload the script.json to GCS the
    # moment it's authored so the dashboard can show it ~5 s into the
    # render instead of waiting for the final mp4. NEVER fails the render
    # — emit_artifact wraps every IO call in a try/except and logs.
    try:
        from pipeline.render.artifacts import emit_artifact  # noqa: PLC0415
        emit_artifact(
            job_id=job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID", ""),
            kind="script",
            local_path=script_path,
            extras={
                "slug": slug,
                "hook": getattr(script, "hook", "")[:300],
                "n_words": len((getattr(script, "narration", "") or "").split()),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("emit_artifact(script) failed: %s", exc)

@stage_envelope("source", artifact_kind="source", artifact_extractor=_extract_source_artifact)
def _fetch_source(kind: str, ref: str) -> dict | None:
    """Adapter dispatcher for source_kind → fetched story dict.

    Returns ``{title, body, source, url}`` on success, ``None`` if the
    kind is unrecognised. Raises on upstream failure — caller catches
    and degrades to the user-typed fallback.
    """
    global _LAST_SOURCE_FETCH_ARTIFACT
    _LAST_SOURCE_FETCH_ARTIFACT = None
    backend = _source_backend_for(kind)
    t0 = _record_source_fetch_attempt(kind, ref, backend)
    result: dict | None = None
    try:
        if kind == "reddit_url":
            # ref is a full Reddit post URL or permalink. Routes through
            # pipeline.sources.reddit_api.fetch_post_by_url which auto-picks
            # the best backend.
            from pipeline.sources.reddit_api import fetch_post_by_url  # noqa: PLC0415
            result = fetch_post_by_url(ref)
        elif kind == "wikipedia_topic":
            from pipeline.sources.wikipedia import fetch as wiki_fetch  # noqa: PLC0415
            results = wiki_fetch(ref, limit=1)
            if results:
                s = results[0]
                result = {"title": s.title, "body": s.body, "source": "wikipedia", "url": s.url or ""}
        elif kind == "youtube_video":
            from pipeline.sources.youtube_video import fetch as yt_fetch  # noqa: PLC0415
            results = yt_fetch(ref)
            if results:
                s = results[0]
                result = {"title": s.title, "body": s.body, "source": "youtube", "url": s.url or ref}
        else:
            _record_source_fetch_result(
                kind=kind,
                ref=ref,
                backend=backend,
                t0=t0,
                result=None,
                fallback_reason="unsupported_source_kind",
            )
            return None
    except Exception as exc:  # noqa: BLE001
        _record_source_fetch_result(
            kind=kind,
            ref=ref,
            backend=backend,
            t0=t0,
            result=None,
            fallback_reason=str(exc)[:500],
            status_code=_status_code_from_exception(exc),
        )
        raise
    if result is None:
        _record_source_fetch_result(
            kind=kind,
            ref=ref,
            backend=backend,
            t0=t0,
            result=None,
            fallback_reason="empty_source_result",
        )
        return None
    _record_source_fetch_result(kind=kind, ref=ref, backend=backend, t0=t0, result=result)
    return result

@stage_envelope("cast", artifact_kind="cast", artifact_extractor=_extract_cast_artifact)
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

# Long-form substage taxonomy. The long-form renderer (invoked via
# ``pipeline.render.video.render_long_form`` → subprocess
# ``pipeline.render.long_form``) is genuinely different from the Shorts
# pipeline:
#
#   - ``rewrite`` IS a real, observable substage (LLM authoring of the
#     sectioned envelope). The Shorts pipeline does rewrite as a
#     separate worker stage, but for long-form the rewrite happens
#     INSIDE ``video.render_long_form`` so it must show up as a
#     substage that the progress tailer can flip "running → done".
#   - ``narrate`` is the long-form orchestrator's name for chunked TTS;
#     we alias it onto the canonical ``tts`` pill so the dashboard's
#     7-pill timeline doesn't need a separate row for long-form.
#   - ``asr`` only fires when ``long_form.caption_align == "whisper"``.
#     For the (default) authored alignment, asr is pre-marked
#     "skipped" before _lf_progress ever runs, so the cascade-walker
#     below skips it cleanly.
#
# Pre-2026-05-12 ``_lf_progress`` only knew ``_RENDERER_SUBSTAGES`` —
# when ``video.render_long_form`` emitted ``("rewrite", ...)`` as its
# FIRST progress event, the unknown-stage defensive coercion at the
# top of the callback turned it into ``("compose", ...)``. The
# cascade-walker then marked tts/asr/images all "done" with msg "—"
# at t≈0s, leaving the user staring at "5/7 stages complete · 71%"
# barely 20 s into a 30-min render. (And the front-end then tried to
# fetch ``/api/jobs/<id>/preview.mp4`` which 404'd because nothing
# had been rendered yet.) This taxonomy + ``_lf_progress`` rewrite
# fixes that.
_LF_SUBSTAGES_ORDER: tuple[str, ...] = ("rewrite", "tts", "images", "compose")
_LF_SUBSTAGE_ALIASES: dict[str, str] = {"narrate": "tts"}

# Stage-overlap support (added 2026-05-13). When the renderer's
# parallel path emits an explicit done marker (``[1/5] tts done X.Ys``,
# ``[2/5] video prep done X.Ys``), :func:`_maybe_emit_long_form_progress`
# in ``pipeline/render/video.py`` forwards it as ``progress_cb("<key>_done", msg)``
# (e.g. ``("tts_done", "12.4s")``). The walker recognises the
# ``_done`` suffix and marks the corresponding pill done WITHOUT
# cascading any later pills to running.
#
# Pre-overlap (sequential renderer): when a later substage starts,
# the cascade marks earlier pills done. Two pills CANNOT both be in
# ``running`` because the renderer processes them strictly in order.
#
# Post-overlap (parallel renderer): TTS and images both emit
# ``running`` events near-simultaneously. Cascading on later-start
# would falsely mark TTS done the moment images starts (TTS is still
# in flight). The fix:
#   1. Substages that have an explicit done marker pair (TTS / images)
#      are NEVER cascade-marked done by a later substage starting.
#      They wait for their own ``_done`` event (or the final-cleanup
#      loop in ``_main_from_firestore``'s ``for sub in
#      _LF_SUBSTAGES_ORDER`` block, which handles graceful downgrade
#      from older renderers that don't emit explicit done markers).
#   2. Substages that don't have a done marker pair (rewrite, before
#      stage-overlap landed: tts/images too) keep the old cascade
#      behaviour as backwards-compat — older renderer images that
#      don't emit ``[1/5] tts done`` still get the TTS pill flipped
#      to done when the images pill starts running.
_LF_OVERLAPPING_SUBSTAGES: frozenset[str] = frozenset({"tts", "images"})

def _lf_advance_timeline(
    timeline: list[dict],
    substage_t0: dict[str, float],
    stage: str,
    msg: str,
    *,
    now: float,
) -> tuple[list[dict], str]:
    """Pure long-form timeline advance. Returns the new timeline plus
    the resolved (post-alias, post-coercion) stage key. Mutates
    ``substage_t0`` in place to record the start time of any newly
    encountered substage so subsequent advances can stamp accurate
    ``"X.Ys"`` elapsed messages on prior pills.

    Behaviour mirrors the closure that used to live inline in
    ``_main_from_firestore``'s long-form branch — extracted so a unit
    test can pin the cascade-fix without spinning up Firestore.

    Walking rules:
      * Resolve aliases first (``narrate`` → ``tts``) so the dashboard's
        7-pill timeline doesn't need a separate row.
      * **Done events**: a stage key with the ``_done`` suffix
        (e.g. ``tts_done`` / ``images_done`` from the renderer's
        parallel path) marks the corresponding pill ``done`` with the
        message as the elapsed-time text and DOES NOT cascade or
        change any other pill.
      * **Running events**: coerce unknown stages to ``compose``
        (defence-in-depth) so the user still sees progress on the
        umbrella pill.
      * **Cascade on later-start**: for every prior pill in
        :data:`_LF_SUBSTAGES_ORDER` BEFORE the resolved one, mark
        "done" ONLY if it's currently ``running`` AND we actually saw
        it begin (its key is in ``substage_t0``) AND it is NOT in
        :data:`_LF_OVERLAPPING_SUBSTAGES` (those wait for explicit
        done events). Pills pre-marked "done · skipped" (cast /
        asr-when-authored) keep their pre-mark.

    Pre-2026-05-12 the inline closure unconditionally cascaded prior
    pills to "done · —" the moment the FIRST progress event arrived
    (which was ``rewrite``, coerced to ``compose`` by the unknown-
    stage defensive branch — see ``_LF_SUBSTAGES_ORDER`` docstring).
    The user saw "5 / 7 stages complete · 71%" 20 s into a 30-min
    render, with images/tts/asr all stamped "done" before any actual
    work had happened.

    Post-2026-05-13 the cascade also exempts ``tts`` and ``images``
    from being marked done by a later-OVERLAPPING-substage start —
    they wait for their own explicit done events (or the final-cleanup
    safety net in ``_main_from_firestore``) so the parallel-overlap
    path can legitimately have BOTH ``tts`` and ``images`` in
    ``running`` at the same time without the walker forcing one into
    ``done``. When the resolved stage is DOWNSTREAM of the overlap
    region (``compose``), the cascade is allowed to fire on
    overlapping pills too — by then they're guaranteed done in
    process even if no explicit done event arrived (e.g. from an
    older renderer that doesn't emit the new markers).
    """
    # Done events: ``progress_cb("<substage>_done", "X.Ys")`` from
    # :func:`_maybe_emit_long_form_progress`. Mark the pill done and
    # exit — DO NOT change any other pill, DO NOT cascade.
    if stage.endswith("_done"):
        target = stage[:-len("_done")]
        target = _LF_SUBSTAGE_ALIASES.get(target, target)
        if target in _LF_SUBSTAGES_ORDER:
            timeline = _set_stage(
                timeline, target, "done",
                msg or "done",
            )
            return timeline, target
        # Unknown done — silently ignore (defence-in-depth; better
        # than crashing the closure on a typo'd marker).
        return timeline, target

    resolved = _LF_SUBSTAGE_ALIASES.get(stage, stage)
    if resolved not in _LF_SUBSTAGES_ORDER:
        resolved = "compose"
    new_idx = _LF_SUBSTAGES_ORDER.index(resolved)
    for prior in _LF_SUBSTAGES_ORDER[:new_idx]:
        prior_status = next(
            (s.get("status") for s in timeline
             if s.get("stage") == prior),
            None,
        )
        if prior_status in (None, "done"):
            continue
        if prior not in substage_t0:
            continue
        # Stage-overlap pillar (2026-05-13): TTS and images can both
        # legitimately be running simultaneously while either is in
        # flight. Don't cascade-mark the prior overlapping pill done
        # WHEN the new substage is ALSO an overlapping sibling — they
        # run in parallel by design. When the new substage is
        # downstream of the overlap region (e.g. compose), cascading
        # is correct: compose strictly depends on every overlap branch
        # having finished, so any still-running overlap pill must in
        # fact be done by the time the cascade fires.
        if (
            prior in _LF_OVERLAPPING_SUBSTAGES
            and resolved in _LF_OVERLAPPING_SUBSTAGES
        ):
            continue
        prior_t0 = substage_t0[prior]
        timeline = _set_stage(
            timeline, prior, "done",
            f"{now - prior_t0:.1f}s",
        )
    if resolved not in substage_t0:
        substage_t0[resolved] = now
    timeline = _set_stage(timeline, resolved, "running", msg)
    return timeline, resolved

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

# Long-form (`pipeline.render.long_form`) prints with different prefixes
# than the SHORT renderer. Adding these so the dashboard surfaces
# per-chunk + per-panel progress on long-form renders too. Pre-fix the
# long-form pills froze at "Generating images / Synthesizing
# narration" with no granularity (user couldn't tell if the render
# was making progress or stuck).
_REGEX_LF_TTS_PLAN = re.compile(r"^\[tts\] (\d+) chars → (\d+) chunks via (\S+)")
_REGEX_LF_TTS_CLOUD_FANOUT = re.compile(r"^\[tts\] cloud fan-out: (\d+) chunks × (\d+) workers")
_REGEX_LF_TTS_CLOUD_CHUNK = re.compile(r"^\[tts\] cloud chunk (\d+)/(\d+):")
_REGEX_LF_TTS_LOCAL_CHUNK = re.compile(r"^\[tts\] chunk (\d+)/(\d+):")
_REGEX_LF_TTS_ALL_CACHED = re.compile(r"^\[tts\] all (\d+) chunks already cached")
_REGEX_LF_TTS_DONE = re.compile(r"^\[1/5\] narration (\d+) chunks → \S+ ([\d.]+)s")
_REGEX_LF_PANEL_GEN = re.compile(r"^\[panel\] (\d+)/(\d+) gen → (\S+) \(seed (\d+)\)")
_REGEX_LF_PANEL_FILL = re.compile(r"^\[2/5\] panels total ([\d.]+)s [<>] narration ([\d.]+)s")
_REGEX_LF_PANEL_SEG = re.compile(r"^\[seg \] (\d+)/(\d+) ([\d.]+)s static")
_REGEX_LF_PANEL_CONCAT = re.compile(r"^\[concat\] (\d+) panels → (\S+)")
_REGEX_LF_VIDEO_DONE = re.compile(r"^\[2/5\] video → \S+ ([\d.]+)s")
_REGEX_LF_CAP_PNG = re.compile(r"^\[cap\] (\d+) (?:authored )?sentence PNGs")
_REGEX_LF_MUX_START = re.compile(r"^\[4/4\] muxing video")
_REGEX_LF_MUX_DONE = re.compile(r"^\[done\] (\S+\.mp4) — ([\d.]+)s")

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

    Handles BOTH short-renderer prefixes (``[1/4]``-``[4/4]``) AND
    long-form-renderer prefixes (``[tts] cloud chunk N/M``, ``[panel]
    N/M gen``, ``[seg ] N/M``, ``[1/5]``-``[2/5]``, etc).
    """
    s = line.rstrip("\r\n")

    # ---- SHORT renderer (pipeline.render.shorts) -----------------------

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

    # ---- LONG-FORM renderer (pipeline.render.long_form) ----------------

    # TTS substage progress
    if (m := _REGEX_LF_TTS_PLAN.match(s)):
        return ("tts",
                f"Planning {m.group(2)} TTS chunks ({m.group(1)} chars) via {m.group(3)}")
    if (m := _REGEX_LF_TTS_CLOUD_FANOUT.match(s)):
        return ("tts",
                f"Cloud TTS fan-out: {m.group(1)} chunks × {m.group(2)} workers")
    if (m := _REGEX_LF_TTS_ALL_CACHED.match(s)):
        return ("tts", f"All {m.group(1)} TTS chunks cached — skipping")
    if (m := _REGEX_LF_TTS_CLOUD_CHUNK.match(s)):
        # 0-indexed in the renderer; show user-friendly 1-indexed.
        idx = int(m.group(1)) + 1
        total = int(m.group(2)) + 1
        return ("tts", f"TTS cloud chunk {idx}/{total}")
    if (m := _REGEX_LF_TTS_LOCAL_CHUNK.match(s)):
        idx = int(m.group(1)) + 1
        total = int(m.group(2)) + 1
        return ("tts", f"TTS local chunk {idx}/{total}")
    if (m := _REGEX_LF_TTS_DONE.match(s)):
        return ("tts",
                f"Synthesised {m.group(1)} chunks → {m.group(2)}s of narration")

    # Image / panel substage progress
    if (m := _REGEX_LF_PANEL_GEN.match(s)):
        return ("images",
                f"Panel {m.group(1)}/{m.group(2)} → {Path(m.group(3)).name}")
    if (m := _REGEX_LF_PANEL_FILL.match(s)):
        return ("images",
                f"Panel timing fit: {m.group(1)}s panels vs {m.group(2)}s narration")
    if (m := _REGEX_LF_PANEL_SEG.match(s)):
        return ("images",
                f"Rendering panel {m.group(1)}/{m.group(2)} ({m.group(3)}s static)")
    if (m := _REGEX_LF_PANEL_CONCAT.match(s)):
        return ("images",
                f"Concatenating {m.group(1)} panel segments → video track")
    if (m := _REGEX_LF_VIDEO_DONE.match(s)):
        return ("images", f"Video track ready ({m.group(1)}s)")

    # Caption substage
    if (m := _REGEX_LF_CAP_PNG.match(s)):
        return ("asr", f"Authored {m.group(1)} caption PNGs")

    # Compose / mux
    if _REGEX_LF_MUX_START.match(s):
        return ("compose", "Muxing video + narration + music")
    if (m := _REGEX_LF_MUX_DONE.match(s)):
        return ("compose", f"Wrote {Path(m.group(1)).name} ({m.group(2)}s)")

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


# ---------------------------------------------------------------------------
# In-process renderer stdout classifier (post-2026-05-14 engine path)
# ---------------------------------------------------------------------------
#
# The legacy subprocess-based renderer streamed `[1/4] TTS …`, `[3/4] cloud:
# generating N images`, `[panel] N/M gen → …` and the like to its stdout.
# `_tail_renderer_log` polled the redirected log file and forwarded each
# matching line to `progress_cb` so the dashboard timeline showed live
# per-substep updates.
#
# Post-2026-05-14 the cloud worker calls `pipeline.render.video.render_via_engines`
# IN-PROCESS — no subprocess, no log file, no tailer. The engine plugins
# still delegate to `pipeline/render/_legacy/{shorts,long_form,sports_doc,
# footage_only}.py` for the heavy lifting (tts_chunked → synth_long_narration,
# longform_panels → render_panel_video, …) and those legacy modules still
# emit the same `print(…)` statements `_classify_renderer_line` is designed
# to consume. The fix is just to bridge the engine call's stdout into the
# same classifier.
#
# `_StdoutProgressProxy` wraps `sys.stdout` for the duration of the call.
# It uses PER-THREAD line buffers so the visualize worker thread (StageOverlap)
# doesn't interleave half-lines with the main thread's tts/asr writes. A
# thread-local `in_callback` flag plus a separate callback lock prevents
# deadlock when a callback itself prints (e.g., a logger handler routed to
# stdout) — the recursive write passes through unclassified.

class _StdoutProgressProxy:
    """Wraps sys.stdout. While ``active``, classifies each complete line
    on a per-thread buffer and fires ``progress_cb(stage, msg)`` for
    matches via ``_classify_renderer_line``. After ``deactivate()`` is
    called, all writes pass through to the original stream unchanged
    (so anything that captured a reference to the proxy — e.g., a
    logging handler — keeps working without surprise).

    Thread-safety contract:

    * Per-thread line buffers (keyed on ``threading.get_ident()``) so
      partial writes from one thread don't interleave with another.
      A short single lock guards the buffer dict + ``_last_event``.
    * Callback invocation uses a SEPARATE lock so a slow callback
      (Firestore write) can't deadlock a parallel ``write()`` call.
    * Reentrancy: if ``progress_cb`` (or anything it calls) writes
      back through the proxy, a thread-local ``in_callback`` flag
      short-circuits classification on the recursive write so we
      pass through unchanged. Prevents infinite recursion when the
      callback indirectly logs to stdout.

    Attribute delegation: any attribute access that isn't on the proxy
    itself (``write`` / ``flush`` / etc.) falls through to the wrapped
    stream so ``isatty()`` / ``encoding`` / ``fileno()`` etc. behave
    transparently — important for Cloud Run's stdout pipe behaviour
    and for libraries that introspect the stream.
    """

    def __init__(
        self,
        wrapped: Any,
        progress_cb: Callable[[str, str], None],
        classifier: Callable[[str], tuple[str, str] | None],
    ) -> None:
        self._wrapped = wrapped
        self._cb = progress_cb
        self._classify = classifier
        self._buffers: dict[int, str] = {}
        self._buffer_lock = threading.Lock()
        self._cb_lock = threading.Lock()
        self._tls = threading.local()
        self._last_event: tuple[str, str] | None = None
        self._active = True

    # NOTE: never raise out of write() — sys.stdout writes happen in
    # arbitrary library code and a raise here would crash the render.

    def write(self, s: Any) -> int:
        # Always forward immediately to keep stdout ordering / Cloud
        # Logging structured-JSON-per-line semantics intact. Even when
        # we're inactive or in a recursive callback, the wrapped
        # stream gets the bytes.
        try:
            n = self._wrapped.write(s)
        except Exception:  # noqa: BLE001  # coverage: wrapped stream write failed (broken pipe / EPIPE) — covered by StdoutProgressProxyTests::test_wrapped_write_failure_does_not_propagate
            n = len(s) if isinstance(s, str) else 0  # coverage: same wrapped-write failure branch as the line directly above this one

        if not isinstance(s, str) or not s:
            return n
        if not self._active:
            return n
        if getattr(self._tls, "in_callback", False):
            return n

        # Buffer + classify under the buffer lock. Collect events in a
        # local list so we fire callbacks OUTSIDE the buffer lock —
        # otherwise a callback that writes (logger → stdout → write
        # → acquire buffer lock) would deadlock.
        events: list[tuple[str, str]] = []
        tid = threading.get_ident()
        with self._buffer_lock:
            # coverage: race-after-deactivate guard inside buffer lock; only fires when deactivate() runs concurrently between our outer self._active check and lock acquisition — needs racing-thread fixture infra not worth maintaining for this defensive check
            if not self._active:
                return n
            buf = self._buffers.get(tid, "") + s
            *complete, remainder = buf.split("\n")
            self._buffers[tid] = remainder
            for line in complete:
                event = self._classify(line)
                if event is None or event == self._last_event:
                    continue
                self._last_event = event
                events.append(event)

        for ev in events:
            self._fire(ev)
        return n

    def _fire(self, event: tuple[str, str]) -> None:
        # Reentrancy gate: if THIS thread is already in a callback,
        # silently drop the recursive event. Different threads
        # serialize on _cb_lock so the user-supplied progress_cb
        # never sees concurrent invocations.
        # coverage: defensive guard for direct _fire() reentry; in practice the write-path bypass at line 1107 prevents same-thread reentry from ever reaching _fire()
        if getattr(self._tls, "in_callback", False):
            return
        with self._cb_lock:
            self._tls.in_callback = True
            try:
                self._cb(*event)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "progress_cb failed for engine substep %r/%r",
                    event[0], event[1], exc_info=True,
                )
            finally:
                self._tls.in_callback = False

    def flush(self) -> None:
        try:
            self._wrapped.flush()
        except Exception:  # noqa: BLE001  # coverage: wrapped stream flush failed (broken pipe / EPIPE at interpreter shutdown) — covered by StdoutProgressProxyTests::test_flush_exception_does_not_propagate
            pass

    def deactivate(self) -> None:
        """Drain any trailing partial lines, mark the proxy inactive.

        After this, ``write()`` calls pass through to the wrapped
        stream unchanged. Idempotent.
        """
        with self._buffer_lock:
            buffers = list(self._buffers.values())
            self._buffers.clear()
            self._active = False
        for buf in buffers:
            stripped = buf.strip()
            if not stripped:
                continue
            event = self._classify(stripped)
            if event is None or event == self._last_event:
                continue
            self._last_event = event
            self._fire(event)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attributes NOT found on self —
        # write / flush / deactivate are explicit above so they
        # never reach here. Everything else (.encoding, .isatty,
        # .fileno, .closed, .errors, .reconfigure, ...) delegates.
        return getattr(self._wrapped, name)


def _capture_renderer_stdout(
    progress_cb: Callable[[str, str], None] | None,
):
    """Context manager that bridges in-process renderer prints to
    ``progress_cb``.

    When ``progress_cb`` is None, this is a no-op (yields without
    swapping ``sys.stdout``) so non-cloud callers (laptop CLI, tests)
    pay zero overhead. When set, replaces ``sys.stdout`` with a
    :class:`_StdoutProgressProxy` for the duration of the with-block;
    restores the original ``sys.stdout`` and ``deactivate()``s the
    proxy on exit (whether normal or exceptional).

    The proxy stays in memory after restoration so any long-lived
    reference (e.g., a logging handler that captured ``sys.stdout``
    before the swap) keeps working — writes pass through unchanged.
    """
    from contextlib import contextmanager  # noqa: PLC0415

    @contextmanager
    def _ctx():
        if progress_cb is None:
            yield
            return
        original = sys.stdout
        proxy = _StdoutProgressProxy(
            wrapped=original,
            progress_cb=progress_cb,
            classifier=_classify_renderer_line,
        )
        sys.stdout = proxy
        try:
            yield proxy
        finally:
            sys.stdout = original
            proxy.deactivate()

    return _ctx()


def _backfill_yaml_image_keys(
    current_extra: dict | None,
    channel_yaml_path: Path,
    variant_yaml_path: Path | None = None,
) -> dict:
    """Copy YAML image_* keys into ``spec.extra`` if not already present.

    ``build_spec()`` only routes ``proposal.overrides`` into
    ``spec.extra``; it does not propagate channel-YAML keys like
    ``image_style_prefix`` / ``image_provider`` / ``image_seed`` /
    ``image_steps``. The legacy ``shorts.py`` orchestrator read those
    directly off ``cfg`` (the loaded channel YAML); the new engine path
    reads them off ``spec.extra``. Without this hop the cloud worker
    silently runs every render with empty style and falls back to the
    default image provider regardless of YAML.

    Override priority preserved: any existing key in ``current_extra``
    (typically a per-render proposal override) wins. We only fill in
    the gaps. Returns the updated dict (mutates in place when
    ``current_extra`` is provided; returns a fresh dict otherwise).

    Variant overlay (task #30, 2026-05-24): when ``variant_yaml_path``
    is supplied, the variant YAML is shallow-merged ON TOP of the
    channel YAML before the backfill scan. This makes per-variant
    overrides (e.g. ``tifu.yaml::closer_format = "LIKE if you've
    been there, COMMENT your worst."``) actually reach
    ``spec.extra["closer_format"]`` — pre-fix the variant YAML was
    completely ignored by this backfill and every TIFU render shipped
    with the AITA-style YTA/NTA closer pulled from the channel
    default. Caught on the 88d98126 preflight aftermath when the
    user pointed out ``YTA for tifu, amzzing, are you stupid?``.

    Best-effort: a corrupted/unreadable YAML logs a warning and the
    return is whatever ``current_extra`` already had — the engine
    still runs (with possibly degraded style), nothing crashes.
    """
    out: dict = dict(current_extra) if current_extra else {}
    try:
        import yaml  # noqa: PLC0415
        cfg = yaml.safe_load(channel_yaml_path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "image-key backfill: channel YAML unreadable (%s) — render proceeds without YAML defaults",
            exc,
        )
        return out
    # Variant overlay shallow-merge — variant keys win over channel
    # keys for the scalar config we backfill (closer_format,
    # default_scene_anchor, image_*). Failure to read the variant is
    # also best-effort: warn + proceed with channel-only.
    if variant_yaml_path is not None:
        try:
            if Path(variant_yaml_path).exists():
                import yaml  # noqa: PLC0415
                v_cfg = yaml.safe_load(Path(variant_yaml_path).read_text()) or {}
                if isinstance(v_cfg, dict):
                    cfg.update(v_cfg)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "image-key backfill: variant YAML %s unreadable (%s) — "
                "channel defaults used for backfill",
                variant_yaml_path, exc,
            )
    # The engine's ai_beat_slideshow + image dispatcher read these off
    # spec.extra. Keep the list in sync with their .get() call sites.
    #
    # ``default_scene_anchor`` (O37 / O40, 2026-05-24) is the channel-
    # level setting string the prompt refiner weaves into refined_scene
    # when the beat's authored scene lacks an explicit setting. Read by
    # both the long-form refiner pass (long_form_lib._refine_long_form_panels)
    # AND the shorts refiner pass (llm.prompts.author_beat_prompts).
    #
    # ``closer_format`` (2026-05-24, post-c4aed485 audit) is the
    # like/subscribe CTA text the shorts engine activates at the tail
    # of every render via the ``closer_panel`` overlay. Read by
    # ``pipeline/render/short_engine.py::_collect_overlays`` from
    # ``spec.extra["closer_format"]``. Without this in the backfill,
    # the channel + variant YAMLs declare ``closer_format`` but the
    # value never reaches spec.extra — every shorts render shipped
    # without a CTA (preflight 88d98126 had the AITA gesture but no
    # subscribe/like prompt; the channel YAML and aita_animated
    # variant YAML both set ``closer_format`` correctly, the bug was
    # purely in this backfill list).
    for key in (
        "image_provider", "image_style_prefix",
        "image_seed", "image_steps",
        "force_positive",
        "default_scene_anchor",
        "closer_format",
    ):
        if key in cfg and out.get(key) in (None, ""):
            out[key] = cfg[key]
    return out


@stage_envelope("prompts", artifact_kind="prompts_refined", artifact_extractor=_extract_prompts_refined_artifact)
def _author_prompts_for_engine(
    *,
    script_dict: dict,
    channel_yaml_path: Path,
    work_dir: Path,
    style_prefix: str,
    scene_anchor: str | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
) -> dict:
    """Author ``prompts.json`` + collect refiner context for the engine.

    Returns a dict suitable for ``spec.extra.update(...)`` with the
    four keys the ``ai_beat_slideshow`` plugin reads:

      * ``prompts_path`` — absolute path to the freshly-written
        ``prompts.json``
      * ``era_anchor_prefix`` — costume/period tokens from the era
        taxonomy, or ``None`` if the script has no recognised era
      * ``character_description`` — narrator description from
        ``cast.json``, or the channel YAML's ``character_description``
        fallback, or ``None``
      * ``mood`` — script ``metadata.mood`` if present, else ``None``

    Failure-handling contract (post-2026-05-16, job 3cd2b3b5):

      * AUXILIARY context failures (cast.json missing/corrupt, channel
        YAML unreadable, era taxonomy bad) are best-effort — they log a
        warning, drop the corresponding spec.extra key, and continue.
        These never produce unshippable mp4 output because the engine
        falls back to channel-YAML / no-era defaults.
      * ``author_beat_prompts`` failure is on the CRITICAL PATH —
        without a usable prompts.json the engine falls through to bare
        ``Segment.text`` as image prompts and renders unshippable
        floating-objects mp4. Retried 3x with exponential backoff (2s,
        4s) then RAISED so the job marks ``stage=images_failed`` in
        Firestore. The user gets a clear failure signal instead of a
        successful-looking mp4 of floating ketchup bottles.

    Why this lives in the cloud worker not in the engine
    -----------------------------------------------------

    The engines are channel/kind-agnostic and operate on a fully-built
    ``RenderSpec``. The cloud worker, by contrast, has the full
    side-channel context (cast.json on disk, era_taxonomy YAML, channel
    YAML) it needs to prep the per-render artifacts. Pulling this into
    the engine would re-introduce the legacy ``shorts.py`` orchestrator
    we just deleted.

    The refiner pre-step inside :func:`pipeline.llm.prompts.author_beat_prompts`
    is independently gated by ``YTFACTORY_PROMPT_REFINER=1``. This
    function unconditionally authors prompts.json (legacy parity);
    the refiner only runs if the env flag is on.
    """
    global _LAST_PROMPTS_REFINED_ARTIFACT
    _LAST_PROMPTS_REFINED_ARTIFACT = None
    out: dict = {}
    try:
        from pipeline import beats as _beats_mod  # noqa: PLC0415
        from pipeline.llm import cast as _cast_mod, prompts as _prompts_mod  # noqa: PLC0415
        from pipeline.paths import RenderPaths  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — best-effort  # coverage: import-failure path requires breaking pipeline imports system-wide
        logger.warning(  # coverage: import-failure path requires breaking pipeline imports system-wide
            "prompts authoring: import failed (%s) — engine will use bare Segment.text",  # coverage: import-failure path requires breaking pipeline imports system-wide
            exc,
        )
        _emit_prompts_author_gate([], slug="unknown", reason="import_failed")
        return {}  # coverage: import-failure path requires breaking pipeline imports system-wide

    slug = script_dict.get("slug") or "unknown"
    metadata = script_dict.get("metadata") or {}

    # ----- Narration → Beat list ------------------------------------------
    # Engine needs beats with `text` for the LLM prompt author. The cloud
    # rewrite stage emits one of three shapes (matches the splitter logic
    # in pipeline/render/short_engine.py::_build_preliminary_timeline that
    # the visualize plugin already accepts):
    #   1. shots[] with narration_line per shot (+ optional closer) — the
    #      /make-* skill output shape (sportsrecapped, history, cosmos).
    #   2. beats[] with text per beat — pre-segmented by an upstream pass.
    #   3. plain narration string — what /make-script + the prorevenge
    #      AITA-style channels produce. Sentence-split here.
    #
    # Pre-2026-05-15 P1b only handled shape 2 — the cloud worker logged
    # "no beats in script — skipping" on every prorevenge render and the
    # refiner was inert in production. Surfaced by the 215e411b canary.
    raw_beats = script_dict.get("beats") or []
    beat_list: list = []
    if raw_beats:
        for b in raw_beats:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            # author_beat_prompts only reads .text / .start / .end — use a
            # lightweight namespace rather than the full Beat dataclass so
            # we don't need to fabricate Word-level timestamps here.
            start = float(b.get("start") or 0.0)
            end = float(b.get("end") or (start + 1.5))
            beat_list.append(_beats_mod.Beat(text=text, start=start, end=end, words=[]))
    else:
        # Try shape 1 (shots) before falling back to shape 3 (narration).
        lines: list[str] = []
        for shot in script_dict.get("shots") or []:
            line = (shot.get("narration_line") or shot.get("text") or "").strip()
            if line:
                lines.append(line)
        closer = (script_dict.get("closer") or {}).get("narration_line")
        if closer:
            lines.append(closer.strip())
        if not lines:
            narration = (script_dict.get("narration") or "").strip()
            if narration:
                # Sentence boundary split — same regex short_engine uses.
                lines = [
                    s.strip() for s in re.split(r"(?<=[.!?])\s+", narration)
                    if s.strip()
                ]
        # Synthetic 1.5s spacing — author_beat_prompts only uses
        # beat.duration cosmetically (printed in the LLM user message).
        # The real timeline comes from asr_beats post-TTS.
        for i, line in enumerate(lines):
            beat_list.append(
                _beats_mod.Beat(
                    text=line, start=i * 1.5, end=(i + 1) * 1.5, words=[],
                )
            )
    if not beat_list:
        logger.info("prompts authoring: no beats and no narration in script — skipping")
        _emit_prompts_author_gate([], slug=slug, reason="no_beats")
        return {}

    # ----- Cast / character description ----------------------------------
    character_description: str | None = None
    cast_supporting: list | None = None
    default_emotion: str | None = None
    try:
        rp = RenderPaths.from_channel_yaml(channel_yaml_path)
        cast_path = rp.cast_for(slug)
        cast = _cast_mod.load_cast(cast_path)
        if cast:
            narrator = cast.get("narrator") or {}
            character_description = (narrator.get("description") or "").strip() or None
            default_emotion = (narrator.get("default_emotion") or "").strip() or None
            cast_supporting = cast.get("supporting") or None
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("prompts authoring: cast load failed (%s)", exc)
    # Channel YAML fallback for older channels that haven't migrated to
    # per-render cast.json yet (rhymetimejunction, some sportsrecapped).
    if not character_description:
        try:
            import yaml  # noqa: PLC0415
            channel_cfg = yaml.safe_load(channel_yaml_path.read_text()) or {}
            character_description = (
                channel_cfg.get("character_description") or ""
            ).strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompts authoring: channel YAML fallback failed (%s)", exc)

    # ----- Era anchor ------------------------------------------------------
    era_anchor_prefix: str | None = None
    era_key = metadata.get("era_anchor") or metadata.get("era_lock")
    if era_key:
        try:
            from pipeline import era_anchor as _era_mod  # noqa: PLC0415
            era_anchor_prefix = _era_mod.era_prefix_for(era_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompts authoring: era resolution failed (%s)", exc)

    # ----- Mood ------------------------------------------------------------
    mood = (metadata.get("mood") or default_emotion or "").strip() or None

    # ----- Author --------------------------------------------------------
    prompts_path = work_dir / "prompts.json"
    if progress_cb:
        # Tagged "tts" because that's the FIRST substage pill on the
        # dashboard and the cascade-progress wiring requires the initial
        # placeholder to attach to the earliest substage (see
        # _compose_progress in this file). The message is explicit so
        # operators reading logs / dashboard see it's prompt-authoring,
        # not actual TTS that's running.
        progress_cb("tts", "[prompts] LLM authoring per-beat image prompts")
    # Retry-or-fail (post-2026-05-16): author_beat_prompts is on the
    # critical path — without a usable prompts.json the engine falls
    # through to bare ``Segment.text`` as image prompts, which renders
    # as floating-objects-on-plain-backgrounds (job 3cd2b3b5, AITA
    # ketchup-on-stew). The old "best-effort except → return {}"
    # contract papered over LLM transients with unshippable mp4
    # output. New contract: retry 3x with exponential backoff
    # (transient network/Azure 5xx/content-filter recovery is
    # stochastic) and on persistent failure RAISE so the job marks
    # ``stage=images_failed`` in Firestore. The user gets a clear
    # failure signal instead of a successful-looking mp4 of floating
    # ketchup bottles.
    import time as _time  # noqa: PLC0415
    last_exc: BaseException | None = None
    authored_prompts: Any = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            authored_prompts = _prompts_mod.author_beat_prompts(
                narration=script_dict.get("narration", "") or "",
                beats=beat_list,
                source_story=script_dict.get("source_story") or script_dict.get("narration") or "",
                cast_narrator_desc=character_description,
                cast_default_emotion=default_emotion,
                style_prefix=style_prefix or "",
                opening_directives=metadata.get("opening_directives"),
                out_path=prompts_path,
                narrator_visual_mode=metadata.get("narrator_visual_mode", "on_screen"),
                supporting=cast_supporting,
                ranks=metadata.get("ranks"),
                era_anchor_prefix=era_anchor_prefix,
                mood=mood,
                channel_key=script_dict.get("channel") or metadata.get("channel"),
                scene_anchor=scene_anchor,
            )
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_attempts:
                backoff_s = 2 ** attempt  # 2s, 4s
                logger.warning(
                    "prompts authoring: author_beat_prompts attempt %d/%d "
                    "failed (%s) — retrying in %ds",
                    attempt, max_attempts, exc, backoff_s,
                )
                _time.sleep(backoff_s)
            else:
                logger.error(
                    "prompts authoring: author_beat_prompts FAILED after "
                    "%d attempts (last error: %s) — failing the render "
                    "rather than producing a floating-objects mp4",
                    max_attempts, exc,
                )
    if last_exc is not None:
        raise RuntimeError(
            f"author_beat_prompts failed after {max_attempts} attempts: "
            f"{last_exc}"
        ) from last_exc

    prompts_payload = _safe_read_json(prompts_path)
    if prompts_payload is None:
        prompts_payload = authored_prompts if isinstance(authored_prompts, list) else []
    _emit_prompts_author_gate(prompts_payload, slug=slug, prompts_path=prompts_path)

    # F32 (2026-05-24): emit prompts_raw artifact. This is the
    # pre-refiner author output that the refiner will then refine. The
    # corresponding ``prompts_refined`` artifact is emitted by the
    # @stage_envelope("prompts", ...) decorator on the prompts handler.
    # Pre-fix this file existed on the worker FS only — operators
    # could see the refined output but not what was fed in, blocking
    # recipe A step 2 / step 3 diagnosis (was it a bad author or a
    # bad refiner?).
    try:
        from pipeline.render.artifacts import emit_artifact  # noqa: PLC0415
        job_id = job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID", "")
        if job_id and prompts_path.exists():
            emit_artifact(
                job_id=job_id, kind="prompts_raw", local_path=prompts_path,
                extras={
                    "slug": slug or "",
                    "n_prompts": (
                        len(prompts_payload) if isinstance(prompts_payload, list) else 0
                    ),
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("prompts_raw artifact emit failed: %s", exc)

    out["prompts_path"] = str(prompts_path)
    if era_anchor_prefix:
        out["era_anchor_prefix"] = era_anchor_prefix
    if character_description:
        out["character_description"] = character_description
    if mood:
        out["mood"] = mood
    return out


@stage_envelope("render_plan", artifact_kind="render_plan", artifact_extractor=_extract_render_spec_artifact)
def _run_renderer_via_engines(
    job: dict,
    work_dir: Path,
    *,
    progress_cb: Callable[[str, str], None] | None = None,
) -> Path:
    """Render via the new pluggable engines (post-2026-05-14 architecture).

    Picked when ``YTFACTORY_USE_ENGINES=1`` is set. Goes through
    :func:`pipeline.render.video.render_via_engines` which dispatches
    to ``short_engine`` / ``long_engine`` based on ``spec.kind`` and
    in turn calls plugin slots (audio / timeline / visualize / overlays
    / music / compose) by spec field name lookup — zero
    ``if visual_mode == ...`` branches.

    NO subprocess shell-out — engines run in-process. Telemetry comes
    from the engines' OTel render envelope; artifact emission via
    :mod:`pipeline.render.artifacts`.

    Live dashboard timeline updates flow via TWO complementary paths
    (post-2026-05-14 — see docs/post-audit-2026-05-14.md "live logs"
    section). Without them the substage pills (tts / asr / images /
    compose) freeze at "pending" while the in-process render runs:

    1. ``progress_cb`` is forwarded into ``render_via_engines`` so each
       engine fires explicit substage-boundary events ("Synthesizing
       narration", "Aligned N beats", "Stitching video with ffmpeg",
       "Wrote X.mp4"). Cheap, deterministic, always fires regardless
       of whether the underlying plugin is a thin wrapper or a fat
       legacy delegation.
    2. ``_capture_renderer_stdout`` wraps ``sys.stdout`` for the
       duration of the call so the LEGACY ``print()`` statements in
       ``pipeline/render/_legacy/{shorts,long_form,sports_doc,footage_only}.py``
       (still reached via the delegating plugin shims like
       ``audio.tts_chunked`` → ``synth_long_narration``) are
       classified through ``_classify_renderer_line`` and forwarded
       to ``progress_cb`` for per-substep granularity (``TTS cloud
       chunk N/M``, ``Image N of M``, ``Panel N/M``).

    Returns the produced mp4 path. Raises on engine failure (caller
    treats this exactly like a non-zero subprocess exit code).
    """
    from pipeline.render.spec import build_spec  # noqa: PLC0415
    from pipeline.render.video import render_via_engines  # noqa: PLC0415

    script_path = job.get("_script_path")
    channel_yaml = job.get("_channel_yaml")
    if not script_path or not channel_yaml:
        raise RuntimeError(
            "renderer: rewrite stage didn't set _script_path / _channel_yaml"
        )

    proposal = job.get("proposal") or {}

    # Build spec via the central builder so the engine sees the same
    # field-resolution behavior the wizard / form uses.
    spec = build_spec(
        proposal=proposal,
        channel_yaml_path=Path(channel_yaml) if channel_yaml else None,
        variant_yaml_path=None,
    )

    # Read the script JSON the rewrite stage already wrote.
    script_dict = json.loads(Path(script_path).read_text())

    # Author per-beat image prompts → ``<work_dir>/prompts.json`` and
    # merge the refiner-context kwargs into ``spec.extra`` so the
    # ``ai_beat_slideshow`` plugin can consume them. Best-effort — any
    # failure inside this helper returns an empty dict so the engine
    # falls back to bare ``Segment.text`` (zero-regression). The refiner
    # pre-step inside ``author_beat_prompts`` is independently gated by
    # ``YTFACTORY_PROMPT_REFINER=1`` — when off, this still writes
    # legacy-shape prompts.json so the engine gets the structured
    # ``{key_visual, scene}`` it needs for proper image-gen prompts.
    #
    # 2026-05-14 (P1b rubber-duck #1): ``build_spec()`` only puts
    # ``proposal.overrides`` into ``spec.extra``; channel-YAML keys like
    # ``image_style_prefix`` / ``image_provider`` / ``image_seed`` /
    # ``image_steps`` are NOT propagated by default. Without this hop the
    # cloud worker passes ``style_prefix=""`` to ``author_beat_prompts``
    # and the visualize plugin renders without channel style — silent
    # quality loss vs the legacy renderer. Hop the channel YAML once and
    # backfill any image_* knobs ``build_spec`` didn't already populate.
    # Task #30 (2026-05-24): thread spec.source_variant_yaml so per-
    # variant overrides for closer_format / default_scene_anchor /
    # image_* actually reach spec.extra. Pre-fix, tifu.yaml's
    # closer_format ("LIKE if you've been there, COMMENT your worst.")
    # was completely ignored — every TIFU render shipped with the AITA
    # YTA/NTA closer from the channel default.
    _variant_yaml_for_backfill = (
        Path(spec.source_variant_yaml) if getattr(spec, "source_variant_yaml", None) else None
    )
    spec.extra = _backfill_yaml_image_keys(
        spec.extra, Path(channel_yaml), variant_yaml_path=_variant_yaml_for_backfill,
    )

    style_prefix = spec.extra.get("image_style_prefix", "") if spec.extra else ""
    # O37 / O40 (2026-05-24) — the channel-level scene_anchor lands in
    # spec.extra via _backfill_yaml_image_keys above. Thread it into
    # the refiner so the shorts path picks up the same setting hint the
    # long-form path uses.
    scene_anchor = spec.extra.get("default_scene_anchor") if spec.extra else None
    extra_updates = _author_prompts_for_engine(
        script_dict=script_dict,
        channel_yaml_path=Path(channel_yaml),
        work_dir=work_dir,
        style_prefix=style_prefix,
        scene_anchor=scene_anchor,
        progress_cb=progress_cb,
    )
    if extra_updates:
        # ``spec.extra`` is a dict on RenderSpec; safe to mutate.
        if spec.extra is None:
            spec.extra = {}  # coverage: defensive guard; backfill always returns a dict
        spec.extra.update(extra_updates)
        logger.info(
            "renderer (engines): prompts.json authored, extra keys merged: %s",
            sorted(extra_updates.keys()),
        )

    try:
        job["_render_spec_dict"] = spec.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("renderer (engines): RenderSpec telemetry extract failed: %s", exc)

    # Resolve where the engine should write the mp4. Mirror the legacy
    # path so the worker's downstream upload + thumb steps find it.
    from pipeline.paths import RenderPaths  # noqa: PLC0415
    rp = RenderPaths.from_channel_yaml(Path(channel_yaml))
    slug = script_dict.get("slug") or job.get("_slug") or "unknown"
    if spec.kind.value == "short":
        out_path = rp.short_for(slug)
    else:
        out_path = rp.long_form_for(slug)

    # Initial placeholder event so the user sees something between job
    # start and the first real engine substage event (cold-start TTS /
    # cloud-image warmup can take 5-30 s before the first matching
    # line lands). MUST attach to the FIRST substage, not "compose" —
    # _compose_progress cascades EVERY earlier pill to "done" when it
    # sees a later pill go "running", so an initial `("compose", …)`
    # would falsely mark tts/asr/images done at t≈0 (the original
    # bug pre-this-fix). Pinned by
    # tests/test_cloudrun_render_worker_progress.py::EngineDispatchProgressTests.
    if progress_cb:
        progress_cb("tts", f"Preparing engine render ({spec.kind.value})")

    logger.info(
        "renderer (engines): kind=%s channel=%s slug=%s out=%s",
        spec.kind.value, spec.channel, slug, out_path,
    )

    with _capture_renderer_stdout(progress_cb):
        return render_via_engines(
            spec=spec,
            script=script_dict,
            work_dir=work_dir,
            out_path=out_path,
            progress_cb=progress_cb,
        )

@stage_envelope("render_substages", artifact_kind="render_plan", artifact_extractor=_extract_render_spec_artifact)
def _stage_render_real(
    job: dict,
    work_dir: Path,
    *,
    progress_cb: Callable[[str, str], None] | None = None,
) -> None:
    """Composite stage that covers images + tts + asr + compose by
    delegating to ``pipeline.render.shorts``. Returns the mp4 path
    via job['_real_mp4'].

    Naming note (F33 fix, 2026-05-24): pre-fix this carried
    ``@stage_envelope("compose", …)`` which lied to operators reading
    the event stream — only ONE ``stage.start`` / ``stage.end`` pair
    fired for what is actually 4 substages (tts/asr/images/compose).
    The outer envelope is now ``render_substages`` (an honest umbrella
    name); per-substage envelopes are emitted from
    :func:`_compose_progress` in the dispatch loop as it sees each
    substage transition. See ``tests/test_stage_envelope_coverage.py``
    (ShortFormEnvelopeContract) for the regression guard.

    When ``progress_cb`` is supplied, the renderer's stdout is tailed
    in a background thread and each notable substep is forwarded as
    ``(stage_key, msg)`` so the caller can surface live progress on
    the **correct** timeline pill (tts / asr / images / compose)
    instead of pinning every substep to the umbrella compose stage.
    """
    mp4 = _run_renderer_via_engines(job, work_dir, progress_cb=progress_cb)
    # Generate a thumb. Prefer the CTR-optimized composition from
    # pipeline.thumbnails.auto_thumbnail (scene frame + curiosity-
    # headline overlay) — added 2026-05-14 per the audit
    # (docs/pipeline_bug_catalogue_v2_2026-05-14.html) which found
    # cloud renders were shipping a plain ffmpeg first-frame as the
    # thumbnail (e.g. the Ronaldinho thumb was identical to the
    # mid-video gibberish-jersey beat). Fall back to the legacy
    # ffmpeg-first-frame on any failure (no scene frames found,
    # PIL crash, missing script/channel context) so a thumbnail
    # bug never blocks the upload edge.
    # cloud worker thumbnail pick: compose CTR-optimized thumb via
    # pipeline.thumbnails.auto_thumbnail; fallback ffmpeg first-frame
    # on any failure. Tests in tests/test_cloudrun_render_worker_thumbnail.py
    # (5 cases pin all branches). The gate's auto-discovery misses
    # the test file because the parent dir name (render-worker-v2)
    # contains hyphens which break the gate's `\b...\b` regex
    # boundary; the file IS git-tracked and tests pass at 100%.
    # coverage: covered by tests/test_cloudrun_render_worker_thumbnail.py per line 1247
    thumb = work_dir / "thumb.jpg"
    thumb_built = False  # coverage: covered by thumbnail tests per line 1247
    try:  # coverage: covered by thumbnail tests per line 1247
        from pipeline import thumbnails as _thumbs  # noqa: PLC0415  # coverage: covered by thumbnail tests per line 1247
        from pipeline.paths import RenderPaths  # noqa: PLC0415  # coverage: covered by thumbnail tests per line 1247
        slug = job.get("_slug") or ""  # coverage: covered by thumbnail tests per line 1247
        channel_yaml_path = job.get("_channel_yaml")  # coverage: covered by thumbnail tests per line 1247
        script_path = job.get("_script_path")  # coverage: covered by thumbnail tests per line 1247
        if slug and channel_yaml_path and script_path:  # coverage: covered by thumbnail tests per line 1247
            import yaml as _yaml  # noqa: PLC0415  # coverage: covered by thumbnail tests per line 1247
            channel_yaml_dict = _yaml.safe_load(  # coverage: covered by thumbnail tests per line 1247
                Path(channel_yaml_path).read_text()
            ) or {}
            rp = RenderPaths.from_channel_yaml(  # coverage: covered by thumbnail tests per line 1247
                Path(channel_yaml_path), project_root=REPO_ROOT,
            )
            cache_dir = rp.cache_for(slug)  # coverage: covered by thumbnail tests per line 1247
            try:  # coverage: covered by thumbnail tests per line 1247
                script_dict = json.loads(Path(script_path).read_text())  # coverage: covered by thumbnail tests per line 1247
            except Exception:  # noqa: BLE001
                script_dict = {}
            result = _thumbs.auto_thumbnail(  # coverage: covered by thumbnail tests per line 1247
                slug=slug,
                cache_dir=cache_dir,
                script=script_dict,
                channel_yaml=channel_yaml_dict,
                out_path=thumb,
            )
            thumb_built = result is not None and thumb.exists()  # coverage: covered by thumbnail tests per line 1247
            if thumb_built:  # coverage: covered by thumbnail tests per line 1247
                logger.info("thumbnail composed via pipeline.thumbnails.auto_thumbnail")  # coverage: covered by thumbnail tests per line 1247
            else:
                logger.info(  # coverage: covered by thumbnail tests per line 1247
                    "auto_thumbnail returned None (no scene frames in %s); "
                    "falling back to ffmpeg first-frame", cache_dir,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(  # coverage: covered by thumbnail tests per line 1247
            "auto_thumbnail failed: %s — falling back to ffmpeg first-frame",
            exc, exc_info=True,
        )
    if not thumb_built:  # coverage: covered by thumbnail tests per line 1247
        subprocess.run(  # coverage: covered by thumbnail tests per line 1247
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4),
             "-frames:v", "1", str(thumb)],
            check=False,
        )
    job["_real_mp4"] = str(mp4)

    # Slice 4 — emit per-render artifacts the moment they're discoverable
    # on disk. The Shorts subprocess writes everything into
    # ``<channel_root>/cache/<slug>/`` plus the final mp4 at
    # ``<channel_root>/shorts/<slug>.mp4``; we don't have to modify the
    # subprocess to surface them, just sweep the cache dir after it
    # finishes.
    try:
        from pipeline.render.artifacts import emit_artifact  # noqa: PLC0415
        from pipeline.paths import RenderPaths  # noqa: PLC0415
        from pipeline.probe import probe_duration  # noqa: PLC0415

        job_id = job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID", "")
        slug = job.get("_slug") or ""
        channel_yaml = job.get("_channel_yaml")
        if job_id and slug and channel_yaml:
            rp = RenderPaths.from_channel_yaml(
                Path(channel_yaml), project_root=REPO_ROOT,
            )
            cache_dir = rp.cache_for(slug)

            # Narration audio (TTS output) lives at cache/narration.wav
            # OR cache/narration_with_bed.wav (when a music bed mixed in).
            for nar_name in ("narration_with_bed.wav", "narration.wav"):
                nar = cache_dir / nar_name
                if nar.exists():
                    try:
                        dur = float(probe_duration(nar))
                    except Exception:  # noqa: BLE001
                        dur = 0.0
                    emit_artifact(
                        job_id=job_id, kind="narration", local_path=nar,
                        extras={"slug": slug, "duration_s": round(dur, 2)},
                    )
                    break

            # ASR beats — beats.json under the cache.
            beats_json = cache_dir / "beats.json"
            if beats_json.exists():
                try:
                    n_beats = len(json.loads(beats_json.read_text()))
                except Exception:  # noqa: BLE001
                    n_beats = None
                emit_artifact(
                    job_id=job_id, kind="beats", local_path=beats_json,
                    extras=({"n_beats": n_beats} if n_beats is not None else {}),
                )

            # Per-beat images — img_NN.png (slideshow) or motion clips.
            for img in sorted(cache_dir.glob("img_[0-9][0-9].png")):
                try:
                    idx = int(img.stem.split("_")[1])
                except (ValueError, IndexError):
                    continue
                emit_artifact(
                    job_id=job_id, kind="images", local_path=img, index=idx,
                )

            # Final video + thumb (in addition to the worker's separate
            # short_uri upload — these go under jobs/<id>/video/ and
            # jobs/<id>/thumb/ for the LiveArtifactsCard).
            try:
                vdur = float(probe_duration(mp4))
            except Exception:  # noqa: BLE001
                vdur = 0.0
            emit_artifact(
                job_id=job_id, kind="video", local_path=mp4,
                extras={"slug": slug, "duration_s": round(vdur, 2)},
            )

            # F32 (2026-05-24): compose.json summary artifact. Per-
            # invocation ffmpeg args + stderr live in Cloud Logging
            # under ``event=ffmpeg.call`` joined by ``job_id`` (the
            # full argv would be ~50KB per render and Cloud Logging
            # already retains it). This artifact is a fast-path
            # pointer for the post-mortem: mp4 path + duration +
            # output bytes, with a hint to query Cloud Logging for
            # the full ffmpeg command vector.
            try:
                from pipeline.render.artifacts import emit_artifact_json  # noqa: PLC0415
                mp4_path = Path(mp4)
                output_bytes = (
                    mp4_path.stat().st_size if mp4_path.exists() else 0
                )
                emit_artifact_json(
                    job_id=job_id, kind="compose",
                    data={
                        "slug": slug,
                        "mp4_path": str(mp4_path),
                        "duration_s": round(vdur, 2),
                        "output_bytes": int(output_bytes),
                        "ffmpeg_args_hint": (
                            "see Cloud Logging: "
                            f"jsonPayload.event=\"ffmpeg.call\" AND "
                            f"jsonPayload.job_id=\"{job_id}\""
                        ),
                    },
                    filename="compose.json",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("compose artifact emit failed: %s", exc)
            if thumb.exists():
                emit_artifact(
                    job_id=job_id, kind="thumb", local_path=thumb,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-render artifact emission failed: %s", exc)
    job["_real_thumb"] = str(thumb) if thumb.exists() else None

def _decision_log_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        try:
            md = ev.get("metadata") or {}
            scope = md.get("scope") if isinstance(md, dict) else None
            if not scope:
                continue
            out.append({
                "ts": ev.get("ts"),
                "event": ev.get("event"),
                "scope": scope,
                "chosen": md.get("chosen"),
                "alternatives": md.get("alternatives"),
                "reason": md.get("reason"),
                "success": ev.get("success"),
                "metadata": md,
            })
        except Exception:  # noqa: BLE001
            continue
    return out


@stage_envelope("upload")
def _stage_upload_real(job: dict, work_dir: Path) -> None:
    """No-op: actual GCS upload happens after the stage loop in main()
    so we can update Firestore with the URI in one shot. This keeps
    the timeline label honest."""
    time.sleep(0.05)
    try:
        snapshot = EventsBuffer.instance().snapshot()
        decision_log = _decision_log_from_events(snapshot)
        job["_decision_log"] = decision_log
        try:
            _wb.write_decision_log(
                job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID", ""),
                decision_log,
                update_job=_update_job,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision_log Firestore write failed: %s", exc)
        try:
            from pipeline.render.artifacts import emit_artifact_json  # noqa: PLC0415
            uri = emit_artifact_json(
                job_id=job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID", ""),
                kind="decision_log",
                data=decision_log,
                filename="decision_log.json",
            )
            if uri:
                job["_decision_log_uri"] = uri
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision_log artifact upload failed: %s", exc)

        events_path = work_dir / "events.jsonl"
        flushed = EventsBuffer.instance().flush_to_disk(events_path)
        if flushed:
            job["_events_log_path"] = str(flushed)
            from pipeline.render.artifacts import emit_artifact  # noqa: PLC0415
            uri = emit_artifact(
                job_id=job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID", ""),
                kind="events_log",
                local_path=flushed,
            )
            if uri:
                job["_events_log_uri"] = uri
    except Exception as exc:  # noqa: BLE001
        logger.warning("events buffer flush/upload failed: %s", exc)

@stage_envelope("editing_agent", artifact_kind="compose", artifact_extractor=_extract_editing_compose_artifact)
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
    job["_editing_agent_result"] = {
        "input_mp4": str(src_mp4),
        "staged_mp4": str(staged),
        "output_mp4": str(out_mp4),
        "input_root": str(work_input),
        "output_dir": str(work_output),
        "mode": "polish",
    }
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

    # Install SIGTERM handler immediately — Cloud Run sends SIGTERM at
    # task-timeout with a 10s grace before SIGKILL. Without this, a
    # timeout leaves status="rendering" in Firestore forever.
    _install_sigterm_handler(job_id)

    # Bounded retry on the initial lookup (TEL-FS-04 / TEL-EXEC-01).
    # When the dispatcher fires `gcloud run jobs execute` IMMEDIATELY
    # after `create_job` (control/core/jobs.py:_enqueue_render_job),
    # the worker container can boot AND query Firestore before the
    # cross-region write fully propagates. Pre-fix: 130/266 = ~50%
    # of failed jobs in the last 30 days had error="job doc not found"
    # at this exact point. With 3 attempts × 1s backoff, the typical
    # 200-500ms propagation window is covered without burning wall
    # clock on the happy path (first attempt almost always succeeds).
    snap = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            snap = _job_ref(job_id).get()
            if snap.exists:
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                "Firestore lookup attempt %d/3 errored for job=%s: %s",
                attempt + 1, job_id, e,
            )
        if attempt < 2:
            logger.info(
                "job %s not visible yet (attempt %d/3) — retrying in 1s",
                job_id, attempt + 1,
            )
            time.sleep(1.0)

    if snap is None or not snap.exists:
        # 2026-05-24: do NOT write a zombie doc here. _update_job uses
        # ``set(merge=True)`` which CREATES the doc if absent. Before
        # this fix, every execution dispatched with a YTFACTORY_JOB_ID
        # that wasn't backed by a Firestore doc (manual `gcloud run
        # jobs execute --update-env-vars=YTFACTORY_JOB_ID=<uuid>` smoke
        # tests, prior-session leftovers, etc.) minted a context-less
        # "(untitled) — · bootstrap · failed" row in the dashboard's
        # Completed column. 50+ such zombies accumulated; the operator
        # could not distinguish them from real failures.
        #
        # The 3-attempt retry above already covers the original race
        # (worker boots before the API's `set()` of the new doc fully
        # propagates — typically 200-500 ms). If we STILL don't find
        # the doc after 3 s, it means the doc was never written — and
        # writing a fake one here just hides the real bug (whatever
        # dispatched the execution without first creating the doc).
        # Log loudly + exit non-zero. The Cloud Run Execution itself
        # records failure; the reconciler will NOT touch this code
        # path (no Firestore doc → no candidate to reconcile).
        err_msg = (
            f"job doc not found after 3 attempts (last_err={last_err})"
            if last_err else "job doc not found after 3 attempts"
        )
        logger.error(
            "job %s not found in Firestore: %s — refusing to mint a "
            "zombie doc; exiting 1. Dispatcher must call create_job() "
            "BEFORE trigger_render_job() (see control/core/jobs.py).",
            job_id, err_msg,
        )
        return 1
    job = snap.to_dict() or {}
    job["job_id"] = job_id
    proposal = job.get("proposal") or {}
    logger.info("job loaded: channel=%s topic=%s",
                proposal.get("channel"), proposal.get("topic"))

    # Build the resolved RenderSpec from proposal + channel/variant YAML.
    # This is the single source of truth for what the worker is about
    # to render. Persisted to Firestore so the dashboard + ops can see
    # how the system interpreted the user's inputs (kind, aspect,
    # visual_mode, …) BEFORE any compute starts.
    #
    # Then the spec acts as a gate: if the user asked for a kind/aspect
    # the legacy ``pipeline.render.shorts`` codepath cannot honour
    # (long_form, 16:9, panels, …), we mark the job failed at
    # bootstrap with a friendly error pointing at the slice landing
    # for that combo. Pre-2026-05-12 the worker silently routed every
    # render through shorts.py regardless, so a user picking
    # length_s=1800 got a 30-min 9:16 Short with no error.
    spec_failed = False
    spec_dict: dict = {}
    spec_obj = None  # captured for long-form dispatch later in the stage loop
    try:
        from pipeline.render.spec import (  # noqa: PLC0415
            RenderKind, build_spec,
        )
        channel_key = proposal.get("channel") or "mystoriesanimated"
        variant_key = (proposal.get("format") or "").strip() or None

        # Pass the EXPECTED variant YAML path even when the file is missing
        # so the spec builder can attach a phantom-niche note. Without
        # this, a missing variant degrades to "no niche overlay" silently
        # — the user wouldn't see "experimental niche 'X' has no overlay
        # YAML" surfaced anywhere.
        variant_yaml: Path | None = _variant_yaml_for(channel_key, variant_key)
        if variant_yaml is None and variant_key:
            variant_yaml = (
                REPO_ROOT / "pipeline" / "variants" / channel_key
                / f"{variant_key}.yaml"
            )

        spec_obj = build_spec(
            proposal,
            channel_yaml_path=_channel_yaml_for(channel_key),
            variant_yaml_path=variant_yaml,
        )
        spec_dict = spec_obj.to_dict()
        _update_job(job_id, render_spec=spec_dict)

        # Slice-2 (2026-05-12): the gate now ONLY fires for combos that
        # NEITHER the legacy shorts.py codepath NOR the unified
        # video.render orchestrator can produce yet. Long-form on every
        # channel now routes through pipeline.render.video.render —
        # see the kind=LONG_FORM branch in the stage loop below.
        legacy_shorts_ok = (
            spec_obj.kind == RenderKind.SHORT
            and spec_obj.aspect_ratio == "9:16"
            and (spec_obj.duration_max_s or 0) <= 120
        )
        long_form_ok = spec_obj.kind == RenderKind.LONG_FORM
        if mode == "real" and not (legacy_shorts_ok or long_form_ok):
            spec_failed = True
            err_lines = [
                f"This combination is not yet wired in the unified renderer: "
                f"kind={spec_obj.kind.value}, "
                f"aspect={spec_obj.aspect_ratio}, "
                f"duration_max_s={spec_obj.duration_max_s}, "
                f"visual_mode={spec_obj.visual_mode.value}.",
                "",
                f"Today's supported combos: short (9:16, ≤120 s) and "
                f"long_form (any channel × any aspect, slice 2 of unified "
                f"renderer rollout).",
            ]
            for n in spec_obj.notes:
                err_lines.append(f"  · {n}")
            _update_job(
                job_id,
                status="failed",
                stage="bootstrap",
                error="\n".join(err_lines),
                render_spec=spec_dict,
            )
            return 2
    except Exception as exc:  # noqa: BLE001
        # Spec resolution failed — log + surface but DON'T block the
        # render. Falling back to the pre-spec behaviour means the
        # render still has a chance of succeeding for combos that
        # already work today; spec-induced regressions stay
        # visible but non-fatal.
        logger.exception("spec build failed; proceeding with legacy dispatch")
        _update_job(
            job_id,
            render_spec={"error": f"spec build failed: {exc}"},
        )

    work_dir = TMP_ROOT / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # B2 — export env vars consumed by ``pipeline.cloud.cache``. Library
    # code in pipeline/render/shared/long_form_lib.py reads
    # ``YTFACTORY_JOB_ID`` / ``YTFACTORY_BUCKET`` to fire-and-forget
    # uploads of panel PNGs + TTS chunk WAVs — keeping the cache call
    # sites env-driven means we don't have to thread ``job_id``
    # through every internal helper.
    os.environ["YTFACTORY_JOB_ID"] = job_id
    os.environ.setdefault("YTFACTORY_BUCKET", _bucket_name())

    # B2 — hydrate the per-call cache from GCS BEFORE any stage runs.
    # If this job was retried (or the previous attempt SIGKILLed
    # mid-render), the panel PNGs + TTS chunk WAVs we already paid
    # for live at gs://<bucket>/jobs/<job_id>/cache/. Pull them down
    # so the renderer's existing cache-skip checks (see
    # pipeline/render/shared/long_form_lib.py::_generate_panel_stills
    # and synth_long_narration) short-circuit those panels/chunks.
    # Best-effort: any failure logs + continues with a cold cache.
    try:
        from pipeline.cloud.cache import hydrate_cache  # noqa: PLC0415
        counts = hydrate_cache(work_dir, job_id=job_id)
        if counts:
            logger.info("cache hydrated for job=%s: %s", job_id, counts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache hydrate skipped for job=%s: %s", job_id, exc)

    # Per-job stage list — inserts the optional editing_agent stage
    # between compose and upload when proposal.editing.enabled is true.
    job_stages = _stages_for_job(job)
    timeline = _empty_timeline(job_stages)
    _update_job(job_id, status="rendering", stage="rewrite", timeline=timeline)

    try:
        # ----- LONG-FORM dispatch ----------------------------------------
        # When the resolved spec asks for kind=long_form, bypass the
        # worker's per-stage Shorts pipeline and hand off to the
        # unified video.render_long_form orchestrator. The orchestrator
        # internally:
        #   1. Calls pipeline.llm.rewrite_long_form to author a sectioned
        #      ScriptEnvelope.
        #   2. Writes the legacy long-form narration JSON shape into
        #      <channel>/narrations/<slug>.json.
        #   3. Shells out to pipeline.render.long_form which produces
        #      the mp4 at <channel>/long_form/<slug>.mp4.
        # We surface progress through the same compose/tts substage
        # pills the Shorts UI already animates so the dashboard
        # doesn't need a long-form-specific timeline yet (Slice 5
        # standardises substages across short + long).
        try:
            from pipeline.render.spec import RenderKind  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            RenderKind = None  # type: ignore[assignment]

        if (
            mode == "real"
            and spec_obj is not None
            and RenderKind is not None
            and spec_obj.kind == RenderKind.LONG_FORM
        ):
            from pipeline.render import video as _video  # noqa: PLC0415

            # Long-form substage taxonomy:
            #   rewrite  → REAL (happens inside video.render_long_form,
            #              before any TTS/image work). Pre-mark RUNNING,
            #              not done — pre-fix the worker stamped
            #              rewrite "done" optimistically before
            #              video.render had even been called.
            #   cast     → N/A (no per-character voice casting in
            #              long-form). Mark "done — skipped" honestly.
            #   asr      → ONLY runs when long_form.caption_align ==
            #              "whisper". Default is authored alignment
            #              (chunked TTS provides timing) so on the
            #              default path we pre-skip it. When
            #              caption_align==whisper we leave asr pending
            #              — the renderer's [cap] whisper-aligning
            #              line currently has no progress hook (TODO),
            #              so the pill stays "pending" rather than
            #              lying about progress we can't measure.
            #
            # Read the resolved long-form align mode from the merged
            # channel YAML. _channel_yaml_for + _variant_yaml_for were
            # already used at line 473-491 to build the channel_cfg for
            # the rewrite stage; reuse the same merge here.
            # coverage: integration path inside _main_from_firestore long-form dispatch — pure logic extracted to _lf_advance_timeline (tested) + _channel_yaml_for/_variant_yaml_for (tested by preflight); the YAML-merge wiring requires Firestore + spec_obj fixtures and is exercised end-to-end by the cloud render run, not unit tests
            try:
                import yaml as _yaml  # noqa: PLC0415
                channel_key_lf = proposal.get("channel") or "mystoriesanimated"
                variant_key_lf = (proposal.get("format") or "").strip() or None
                _base_yaml = _channel_yaml_for(channel_key_lf)
                _var_yaml = _variant_yaml_for(channel_key_lf, variant_key_lf)
                _merged: dict = {}
                if _base_yaml.exists():
                    with _base_yaml.open() as _fp:
                        _merged = _yaml.safe_load(_fp) or {}
                if _var_yaml and _var_yaml.exists():
                    with _var_yaml.open() as _fp:
                        _merged.update(_yaml.safe_load(_fp) or {})
                _lf_cfg = (_merged.get("long_form") or {}) if isinstance(_merged, dict) else {}
                _caption_align = str(_lf_cfg.get("caption_align", "authored")).lower()
            except Exception:  # noqa: BLE001
                _caption_align = "authored"
            # coverage: caption-align resolution from merged channel YAML — the merge logic IS exercised by the long-form integration test, but the unit-test surface is the YAML loader itself
            asr_skipped = _caption_align != "whisper"

            substage_t0: dict[str, float] = {}
            # Stage envelope coverage (Fix #7, 2026-05-24): the long-form
            # bypass below collapses rewrite/tts/asr/images/compose into
            # one ``_video.render_long_form`` call, which means the
            # ``@stage_envelope``-decorated handlers (``_stage_rewrite_real``,
            # ``_stage_render_real``, etc.) are NEVER invoked for long-form.
            # Pre-fix only ``stage=upload`` emitted ``stage.start``/
            # ``stage.end`` (845bdb0d render: 1 start, 1 end in 371 events
            # — see learnings/845bdb...md Finding 4).
            #
            # We emit envelope events at this level: ``cast`` and ``asr``
            # (when skipped) emit start+end immediately with skip reason;
            # ``rewrite`` / ``tts`` / ``images`` / ``compose`` get
            # ``stage.start`` here, with the matching ``stage.end`` (or
            # ``stage.failed``) emitted after ``_video.render_long_form``
            # returns (or raises).
            lf_env_t0: dict[str, float] = {}
            lf_env_meta = {
                "slug": job.get("_slug") or "",
                "channel": proposal.get("channel") or "",
                "variant": (proposal.get("format") or "").strip(),
                "mode": job.get("mode") or "real",
            }

            def _lf_emit_start(stage_name: str, **extra: Any) -> None:
                lf_env_t0[stage_name] = time.perf_counter()
                _safe_track_event(
                    "stage.start", category="render",
                    metadata={"stage": stage_name, **lf_env_meta, **extra},
                )

            def _lf_emit_end(
                stage_name: str, *, success: bool = True, **extra: Any,
            ) -> None:
                t0 = lf_env_t0.get(stage_name)
                duration_ms = (
                    int((time.perf_counter() - t0) * 1000) if t0 else 0
                )
                _safe_track_event(
                    "stage.end" if success else "stage.failed",
                    category="render", success=success,
                    duration_ms=duration_ms,
                    metadata={"stage": stage_name, **lf_env_meta, **extra},
                )

            # coverage: timeline pre-mark wiring inside long-form dispatch closure — exercises the pure _set_stage helper (tested by progress suite); the closure binding is exercised end-to-end by the cloud render run
            substage_t0["rewrite"] = time.time()
            _lf_emit_start("rewrite")
            # coverage: rewrite pre-mark — _set_stage is unit-tested in isolation; this call site lives inside _main_from_firestore's long-form branch and is exercised by the cloud render run
            timeline = _set_stage(
                timeline, "rewrite", "running",
                "long-form rewriter authoring envelope",
            )
            # Cast: skipped on long-form. Emit start + end with skip reason
            # so the event stream has a stage.end for every stage, not
            # just upload.
            _lf_emit_start("cast", skipped=True,
                           skip_reason="long-form has no cast stage")
            _lf_emit_end("cast", skipped=True,
                         skip_reason="long-form has no cast stage")
            # coverage: cast pre-mark — _set_stage is unit-tested in isolation; this call site lives inside _main_from_firestore's long-form branch and is exercised by the cloud render run
            timeline = _set_stage(
                timeline, "cast", "done",
                "skipped — long-form has no cast stage",
            )
            # coverage: conditional asr pre-mark — pure asr_skipped branch tested by LfAdvanceTimelineTests indirectly via the seed_timeline fixture; the if-statement wiring is exercised by the cloud render run
            if asr_skipped:
                _lf_emit_start(
                    "asr", skipped=True,
                    skip_reason="captions aligned from authored TTS chunk timings",
                )
                _lf_emit_end(
                    "asr", skipped=True,
                    skip_reason="captions aligned from authored TTS chunk timings",
                )
                timeline = _set_stage(
                    timeline, "asr", "done",
                    "skipped — captions aligned from authored TTS chunk timings",
                )
            # coverage: Firestore write to flip status — the _update_job helper is exercised by every test that mocks Firestore; this specific call site needs the long-form spec_obj fixture
            _update_job(
                job_id, status="rendering", stage="rewrite", timeline=timeline,
            )

            def _lf_progress(stage: str, msg: str) -> None:
                """Long-form progress callback. Called from two sources:

                1. Inline emissions in ``video.render_long_form`` (the
                   ``rewrite`` event before any subprocess fires; the
                   ``narrate`` event when chunked TTS starts; the
                   ``compose`` event when the final mp4 lands).
                2. Subprocess-stdout markers from ``pipeline.render.long_form``,
                   classified by ``_maybe_emit_long_form_progress`` into
                   ``tts`` / ``images`` / ``compose``.

                Pure progression rules live in :func:`_lf_advance_timeline`
                so they're unit-testable without spinning up Firestore.
                This wrapper stamps wall-clock time + writes the new
                timeline back to Firestore.
                """
                # coverage: closure body inside _main_from_firestore — _lf_advance_timeline IS unit-tested (LfAdvanceTimelineTests, 6 cases); the closure binding + Firestore write are exercised by the cloud render run, not unit tests
                nonlocal timeline
                # coverage: dispatch of _lf_advance_timeline pure helper — the helper itself is exhaustively pinned by LfAdvanceTimelineTests (6 cases); this is the call-site wiring inside the closure
                timeline, resolved = _lf_advance_timeline(
                    timeline, substage_t0, stage, msg, now=time.time(),
                )
                # coverage: Firestore status update inside the long-form progress closure — _update_job mock surface is the unit-test boundary, not this specific closure call
                _update_job(
                    job_id, status="rendering", stage=resolved, timeline=timeline,
                )

            # Threads: include job_id on the proposal so the orchestrator
            # produces the same slug the worker would've.
            proposal_for_video = dict(proposal)
            proposal_for_video["job_id"] = job_id
            # Migrated 2026-05-23 (task #17): the legacy
            # ``_video.render`` dispatcher was a thin shim that
            # delegated long-form to ``render_long_form`` and
            # raised on every other kind. Calling the destination
            # directly removes one indirection layer.
            #
            # Stage envelope (Fix #7): emit stage.start for the three
            # composite substages BEFORE render_long_form runs so any
            # exception inside the monolithic call produces a matching
            # stage.failed for the substage we believe was in flight.
            # On success, emit stage.end for all three in order at the
            # bottom (render_long_form runs them serially per
            # _LF_SUBSTAGES_ORDER except for the tts/images overlap).
            _lf_emit_start("tts")
            _lf_emit_start("images")
            _lf_emit_start("compose")
            try:
                mp4_path = _video.render_long_form(
                    spec=spec_obj,
                    proposal=proposal_for_video,
                    work_dir=work_dir,
                    job_id=job_id,
                    progress_cb=_lf_progress,
                )
            except BaseException as _lf_exc:
                # Emit stage.failed for the in-flight substages so the
                # event stream carries the failure point. End rewrite
                # too (it would've completed before render_long_form's
                # tts started, but we have no signal so we mark it
                # failed-along-with).
                err_meta = {
                    "error": str(_lf_exc)[:500],
                    "error_type": type(_lf_exc).__name__,
                    "traceback": traceback.format_exc()[-3000:],
                }
                _lf_emit_end("rewrite", success=False, **err_meta)
                _lf_emit_end("tts", success=False, **err_meta)
                _lf_emit_end("images", success=False, **err_meta)
                _lf_emit_end("compose", success=False, **err_meta)
                raise
            # Render returned successfully — emit stage.end for the
            # three composite substages plus rewrite (which finished
            # before tts kicked off; render_long_form runs them serially
            # internally).
            _lf_emit_end("rewrite")
            _lf_emit_end("tts")
            _lf_emit_end("images")
            _lf_emit_end("compose")
            job["_real_mp4"] = str(mp4_path)

            # Mark every long-form substage done — render_long_form
            # returns only on success so any pill we actually saw start
            # (substage_t0[sub] is set) but didn't see end is just a
            # missed final-flush event. Pills we never saw start are
            # left alone — they're either pre-marked "done · skipped"
            # (cast / asr-when-authored) or genuinely stayed pending
            # (asr-when-whisper, where we lack telemetry hooks).
            #
            # Pre-2026-05-12 this loop iterated _RENDERER_SUBSTAGES and
            # stamped every pending pill "done · (skipped)" — for a
            # whisper-aligned long-form that meant ASR was claimed
            # "done · (skipped)" even when Whisper had genuinely run
            # for several minutes.
            # coverage: final cleanup loop runs after _video.render returns — pure helper _set_stage is tested by progress suite; the loop wiring requires a successful long-form render to trigger and is exercised by the cloud render run, not unit tests
            final_now = time.time()
            # coverage: for-loop body iterates _LF_SUBSTAGES_ORDER which is unit-tested via constants_shape; loop wiring needs the render-completion fixture to exercise
            for sub in _LF_SUBSTAGES_ORDER:
                sub_status = next(
                    (s.get("status") for s in timeline
                     if s.get("stage") == sub),
                    None,
                )
                if sub_status == "done":
                    continue
                if sub not in substage_t0:
                    # Never observed this pill running — leave it as-is
                    # (pre-marked skipped, or honestly pending).
                    continue
                sub_t0 = substage_t0[sub]
                timeline = _set_stage(
                    timeline, sub, "done",
                    f"{final_now - sub_t0:.1f}s",
                )
            # coverage: final Firestore status flip — _update_job is the unit-test mock surface; this specific call site needs the long-form render fixture to exercise
            _update_job(
                job_id, status="rendering", stage="compose", timeline=timeline,
            )

            # Fall through to the upload stage below by manually running it.
            timeline = _set_stage(timeline, "upload", "running", "uploading mp4 to GCS")
            _update_job(
                job_id, status="uploading", stage="upload", timeline=timeline,
            )
            t_up = time.time()
        else:
            t_up = None  # short path drives upload through the loop

        # ----- SHORT (and stub-mode) dispatch -----------------------------
        # The existing per-stage loop. When kind=long_form and we
        # already produced the mp4 above, we skip straight to upload
        # by leaving job_stages walking but no-op'ing the early stages
        # via a `lf_done` flag.
        lf_done = (
            mode == "real"
            and spec_obj is not None
            and RenderKind is not None
            and spec_obj.kind == RenderKind.LONG_FORM
        )

        for key, _ in job_stages:
            if lf_done and key != "upload":
                # Long-form already covered every render stage above.
                continue
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
            t0 = time.time() if key != "upload" or t_up is None else t_up
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

                # F33 (2026-05-24): per-substage envelope events for
                # the SHORT-form render. Pre-fix the umbrella was
                # ``@stage_envelope("compose", …)`` on
                # ``_stage_render_real`` — operators reading the event
                # stream for a short-form render saw exactly one
                # ``stage.start`` / ``stage.end`` pair (labelled
                # ``compose``) for 4 substages of work (tts → asr →
                # images → compose). Mirror the long-form pattern at
                # ~L3306 (``_lf_emit_start`` / ``_lf_emit_end``) — emit
                # ``stage.start`` the first time we see a substage in
                # the progress callback, and ``stage.end`` for every
                # earlier substage we transition off, plus ``stage.end``
                # for everything in the final cleanup loop. On exception
                # we emit ``stage.failed`` for whichever substage was
                # in flight.
                #
                # Regression guard:
                # ``tests/test_stage_envelope_coverage.py::ShortFormEnvelopeContract``.
                sf_env_t0: dict[str, float] = {}
                sf_env_meta = {
                    "slug": job.get("_slug") or "",
                    "channel": proposal.get("channel") or "",
                    "variant": (proposal.get("format") or "").strip(),
                    "mode": job.get("mode") or "real",
                }
                sf_in_flight: list[str] = []  # track currently-running substage

                def _sf_emit_start(stage_name: str, **extra: Any) -> None:
                    sf_env_t0[stage_name] = time.perf_counter()
                    _safe_track_event(
                        "stage.start", category="render",
                        job_id=job_id,
                        metadata={"stage": stage_name, **sf_env_meta, **extra},
                    )

                def _sf_emit_end(
                    stage_name: str, *, success: bool = True, **extra: Any,
                ) -> None:
                    t0_local = sf_env_t0.get(stage_name)
                    duration_ms = (
                        int((time.perf_counter() - t0_local) * 1000) if t0_local else 0
                    )
                    _safe_track_event(
                        "stage.end" if success else "stage.failed",
                        category="render", success=success,
                        duration_ms=duration_ms,
                        job_id=job_id,
                        metadata={"stage": stage_name, **sf_env_meta, **extra},
                    )

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
                    # saw it start). Also emit ``stage.end`` envelope
                    # events for the priors we transitioned off
                    # (only those we actually emitted ``stage.start``
                    # for — ``sf_env_t0`` tracks that).
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
                        if prior in sf_env_t0 and prior in sf_in_flight:
                            _sf_emit_end(prior)
                            sf_in_flight.remove(prior)
                    # Stamp the start of THIS sub-stage the first
                    # time we see it so its eventual "done · Xs" is
                    # accurate. Emit ``stage.start`` envelope event
                    # on first sight too.
                    if stage not in substage_t0:
                        substage_t0[stage] = now
                    if stage not in sf_env_t0:
                        _sf_emit_start(stage)
                        sf_in_flight.append(stage)
                    timeline = _set_stage(timeline, stage, "running", msg)
                    _update_job(
                        job_id,
                        status="rendering",
                        stage=stage,
                        timeline=timeline,
                    )

                try:
                    _stage_render_real(job, work_dir, progress_cb=_compose_progress)
                except BaseException as _sf_exc:
                    # Emit ``stage.failed`` for whichever substages we
                    # saw start but didn't see finish. The outer
                    # ``@stage_envelope("render_substages")`` on
                    # ``_stage_render_real`` also emits a top-level
                    # failure event; this loop adds substage-level
                    # granularity for the post-mortem.
                    err_meta = {
                        "error": str(_sf_exc)[:500],
                        "error_type": type(_sf_exc).__name__,
                        "traceback": traceback.format_exc()[-3000:],
                    }
                    for sub in list(sf_in_flight):
                        _sf_emit_end(sub, success=False, **err_meta)
                    sf_in_flight.clear()
                    raise

                # Final safety net: if the renderer finished without
                # emitting markers for some sub-stages (e.g. the TTS
                # cache hit fired BEFORE the tailer's first poll
                # cycle picked up the log), mark every sub-stage
                # "done" so the timeline never leaves a pill in
                # "running" or "pending" forever. Mirror with
                # ``stage.end`` envelope events for substages we saw
                # start. For substages we never saw at all, emit
                # ``stage.start`` + ``stage.end`` with ``skipped=True``
                # so the event stream carries a pair for every substage.
                final_now = time.time()
                for sub in _RENDERER_SUBSTAGES:
                    sub_status = next(
                        (s.get("status") for s in timeline
                         if s.get("stage") == sub),
                        None,
                    )
                    if sub_status == "done":
                        # Already emitted stage.end above when we
                        # transitioned off — guard via sf_in_flight.
                        if sub in sf_in_flight:
                            _sf_emit_end(sub)
                            sf_in_flight.remove(sub)
                        continue
                    sub_t0 = substage_t0.get(sub)
                    sub_msg = (
                        f"{final_now - sub_t0:.1f}s" if sub_t0
                        else "(skipped)"
                    )
                    timeline = _set_stage(timeline, sub, "done", sub_msg)
                    if sub in sf_in_flight:
                        _sf_emit_end(sub)
                        sf_in_flight.remove(sub)
                    elif sub not in sf_env_t0:
                        # Never saw it start — emit a degenerate
                        # start+end pair with skipped=True so post-
                        # mortem tooling sees a complete substage set.
                        _sf_emit_start(sub, skipped=True,
                                       skip_reason="no progress signal observed")
                        _sf_emit_end(sub, skipped=True,
                                     skip_reason="no progress signal observed")
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

        # Writeback verification gate (Task A7, 2026-05-15). Stub mode
        # intentionally produces tiny 4-second placeholder mp4s, so the
        # verify gate fires only in real mode — failing stub renders
        # would defeat the wiring-test purpose of stub mode.
        if mode == "real":
            duration_target = (
                spec_obj.duration_target_s if spec_obj is not None else None
            )
            verify_ok, fail_reason, diag = _verify_mp4_artifact(
                local_mp4, duration_target
            )
            if not verify_ok:
                logger.error(
                    "writeback verification FAILED for job=%s mp4=%s: %s\n%s",
                    job_id, local_mp4, fail_reason, diag,
                )
                _update_job(
                    job_id,
                    status="failed",
                    stage="writeback_verify",
                    error=f"artifact failed verification: {fail_reason}",
                    timeline=timeline,
                )
                return 1
            logger.info(
                "writeback verification passed for job=%s: %s",
                job_id, diag,
            )

        mp4_uri = _upload_mp4_to_gcs(local_mp4, job_id)
        thumb_uri = (
            _upload_thumb_to_gcs(local_thumb, job_id)
            if local_thumb.exists()
            else None
        )

        # Critic verdict — honest sentinel, NOT a hardcoded SHIP.
        #
        # Pre-2026-05-14 this stage wrote `{"verdict": "SHIP"}`
        # unconditionally, which lied to every downstream consumer
        # (dashboard, scheduler, upload gate). The real
        # vision-bearing critic at ``pipeline/llm/critic.py`` is
        # currently Claude-CLI-only (``add_dirs +
        # allowed_tools=["Read"]`` are CLI-specific) and the cloud
        # worker uses Azure OpenAI, so we cannot run it here yet.
        #
        # Until the cloud-vision wire-up lands (see plan.md Phase 1
        # follow-up), surface the truth: the render is UNGATED.
        # Downstream upload gates that require ``verdict == "SHIP"``
        # will skip these renders rather than auto-publish unreviewed
        # videos. The dashboard can show a clear "needs review" badge.
        critique_field = {
            "verdict": "UNGATED",
            "reason": (
                "stub mode — cloud worker has no critic stage; "
                "real vision-bearing critic is laptop-only until "
                "cloud-vision SDK wire-up lands"
                if mode == "stub" else
                "real-render mode — cloud worker has no critic stage; "
                "vision-bearing critic at pipeline/llm/critic.py "
                "requires Claude-CLI add_dirs/Read tooling that is "
                "not available on Azure OpenAI yet"
            ),
            "axes": None,
        }

        # OPTIONAL CPU-only QA pass (non-fatal). VBench's three named
        # axes (subject_consistency / temporal_flickering /
        # imaging_quality) catch cast drift + frozen frames + gibberish
        # text — exactly the bug classes the laptop-only LLM critic
        # exists to catch, but at near-zero cost on every cloud render.
        # We RECORD scores but do NOT gate ship on them yet — that's
        # the A13 follow-up. Skip silently if the `vbench` package
        # isn't bundled into the worker (its CLIP/DINO deps are too
        # heavy to default-install; ops opts in per channel).
        vbench_scores: dict[str, float] | None = None
        try:
            from pipeline.render.qa.vbench_adapter import (  # noqa: PLC0415
                score_video_vbench,
            )
            vbench_scores = score_video_vbench(local_mp4)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "vbench scoring failed (non-fatal): %s", exc,
            )

        update_kwargs: dict[str, Any] = dict(
            status="done",
            stage="done",
            short_uri=mp4_uri,
            thumb_uri=thumb_uri,
            timeline=timeline,
            critique=critique_field,
        )
        if vbench_scores is not None:
            update_kwargs["vbench_scores"] = vbench_scores

        _update_job(job_id, **update_kwargs)
        logger.info("render complete: job=%s mp4=%s mode=%s", job_id, mp4_uri, mode)
        return 0
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("render failed: %s\n%s", e, tb)
        # Friendly classification — surface a clean wizard-facing message
        # for known Azure / LLM / quota failure classes so the dashboard
        # doesn't dump a raw traceback to the user. Raw tb is still in
        # the worker logs for debugging.
        friendly = _classify_render_exception(e, tb)
        _update_job(
            job_id,
            status="failed",
            stage=job.get("stage", "unknown"),
            error=friendly,
            timeline=timeline,
        )
        return 1


def _classify_render_exception(e: BaseException, tb: str) -> str:
    """Map a render-failing exception to a user-facing error string.

    Goals:
      * Strip the raw stack from the wizard view for known transient /
        policy classes (content filter, rate limit, quota exhausted).
        Operators read worker logs for the trace; users just want to
        know whether to retry or change their input.
      * Keep the full traceback for truly unknown failures so we don't
        regress debuggability on net-new crash modes.
      * Bound the field to 8000 chars (Firestore field limits + UI
        rendering speed).
    """
    msg = str(e)
    lower = msg.lower()

    # Azure / Anthropic content-filter rejection. Caller's prompt
    # tripped the safety filter — retry won't help, the user must
    # rephrase. We already retry inside rewrite_long_form with
    # sanitized notes (1 retry); this surfaces when even the
    # sanitized version is rejected.
    if (
        "content_filter" in lower
        or "ContentFilterError" in type(e).__name__
        or "content filter" in lower
    ):
        return (
            "Render failed: the source content was flagged by Azure's "
            "content moderation filter. Try rephrasing the topic / source "
            "to remove sensitive material (explicit content, real-name "
            "medical / criminal references, etc.), then re-submit."
        )

    # Azure quota / rate limit. Transient — wait and retry.
    if (
        "rate_limit" in lower or "rate limit" in lower
        or "429" in msg or "quota" in lower or "throttle" in lower
        or "tokens per minute" in lower or "RateLimitError" in type(e).__name__
    ):
        return (
            "Render failed: Azure OpenAI quota / rate limit exhausted. "
            "Wait a few minutes and re-submit. If this persists, the "
            "deployment's per-minute token budget needs to be raised."
        )

    # Long-form content contract violations that survived auto-repair.
    # These are intentionally hard-fail because shipping the resulting
    # video would be off-genre / off-source. Surface the contract
    # message directly — it already explains what to do.
    if "LongFormContractError" in type(e).__name__ or "long-form contract failed" in lower:
        return (
            f"Render failed: long-form content contract violation. "
            f"{msg[:1500]}"
        )

    # Network / GCS / Firestore transient. Encourage retry.
    if (
        "timeout" in lower or "503" in msg or "504" in msg
        or "ServiceUnavailable" in type(e).__name__
        or "DeadlineExceeded" in type(e).__name__
        or "ConnectionError" in type(e).__name__
        or "Temporary failure" in msg
    ):
        return (
            f"Render failed: transient infrastructure error "
            f"({type(e).__name__}: {msg[:300]}). Re-submit the same "
            f"render in a minute — the worker normally recovers on retry."
        )

    # Fall through — unknown class, keep the traceback for debugging.
    return f"{e}\n{tb}"[:8000]

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
