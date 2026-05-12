"""Unified renderer orchestrator — dispatches by ``RenderSpec.kind``.

Single entry point for any render in ytFactory. Replaces the ad-hoc
"shell out to shorts.py / long_form.py / sports_doc.py" branching the
cloud worker did pre-2026-05-12 (see plan.md, Slice 2).

Today's responsibilities (Slice 2 minimum-viable):

- ``spec.kind == short``     → no-op; the worker continues to use its
                              existing rewrite/cast/compose stages
                              (which already know how to drive
                              ``pipeline.render.shorts``). This module
                              exists to give the worker a single
                              dispatcher symbol; the short path will
                              migrate into here in Slice 5 along with
                              the shim cleanup.

- ``spec.kind == long_form`` → run :func:`render_long_form` which:
    1. Calls :func:`pipeline.llm.rewrite_long_form.rewrite_long_form`
       to author a sectioned ``ScriptEnvelope``.
    2. Writes the envelope's legacy long-form dict shape to
       ``<channel_dir>/narrations/<slug>.json`` (where
       ``pipeline.render.long_form`` reads it).
    3. Writes the merged channel YAML (base + variant overlay) to a
       temporary path so the existing long_form.py loads spec-derived
       overrides (aspect, voice, music_bed, etc) without us having to
       refactor its config-loading.
    4. Shells out to ``python -m pipeline.render.long_form``.
    5. Returns the produced mp4 path.

- ``spec.kind == sports_doc`` / ``footage_only`` → not yet wired
  (the worker doesn't generate these via the form today; they go
  through their own laptop CLIs). Slice 5 lands the wiring.

Why short stays in the worker
-----------------------------

The worker's per-stage timeline emission (rewrite → cast → tts → asr
→ images → compose → upload) is tightly coupled to its Firestore
update loop. Migrating short into ``video.render`` in Slice 2 would
require changing both at once. Slice 5 absorbs short into here once
the envelope + spec foundation has bedded in.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from pipeline.llm.rewrite_long_form import rewrite_long_form
from pipeline.llm.script_schema import ScriptEnvelope
from pipeline.paths import RenderPaths
from pipeline.render.spec import RenderKind, RenderSpec


_logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[str, str], None]
"""``progress_cb(substage, msg)`` lets the caller forward sub-stage
events to its own UI / Firestore. Same shape the worker's
``_compose_progress`` already consumes."""


def render(
    spec: RenderSpec,
    *,
    proposal: dict[str, Any],
    work_dir: Path,
    job_id: str,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Render the spec to an mp4. Returns the produced mp4 path.

    Dispatches by ``spec.kind``. NEVER calls back into the worker's
    Firestore-update loop — the caller wraps progress + persistence.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    _logger.info(
        "video.render: kind=%s channel=%s slug-source=%s aspect=%s res=%s",
        spec.kind.value, spec.channel,
        proposal.get("topic", "")[:40],
        spec.aspect_ratio, spec.output_resolution,
    )

    if spec.kind == RenderKind.LONG_FORM:
        return render_long_form(
            spec=spec,
            proposal=proposal,
            work_dir=work_dir,
            job_id=job_id,
            progress_cb=progress_cb,
        )

    if spec.kind == RenderKind.SHORT:
        # Slice 2: short still goes through the worker's existing
        # stage-by-stage path. Returning a sentinel exception so the
        # worker keeps using its current short logic without us
        # silently bypassing it.
        raise NotImplementedError(
            "video.render: kind=short is still handled by the worker's "
            "existing rewrite/cast/compose stages (Slice 5 will absorb "
            "it). Caller should not invoke video.render(spec) for short."
        )

    raise NotImplementedError(
        f"video.render: kind={spec.kind.value} is not yet wired "
        "(Slice 5 lands sports_doc + footage_only)."
    )


# ---------------------------------------------------------------------------
# Long-form
# ---------------------------------------------------------------------------


def render_long_form(
    *,
    spec: RenderSpec,
    proposal: dict[str, Any],
    work_dir: Path,
    job_id: str,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Author the long-form narration + invoke ``pipeline.render.long_form``.

    Writes the narration to the canonical location the existing
    long_form.py loader reads (``<channel_dir>/narrations/<slug>.json``)
    so we don't have to refactor its config + load path.

    Returns the produced mp4 path
    (``<channel_dir>/long_form/<slug>.mp4``).
    """
    if spec.kind != RenderKind.LONG_FORM:
        raise ValueError(
            f"render_long_form called with spec.kind={spec.kind.value!r}"
        )

    # Stage A: author the long-form ScriptEnvelope via the LLM.
    if progress_cb:
        progress_cb("rewrite", f"long-form rewrite ({spec.duration_target_s}s target)")
    raw_story = _raw_story_from_proposal(proposal)
    cfg = _merged_channel_cfg(spec)

    env = rewrite_long_form(
        raw_story,
        channel_cfg=cfg,
        target_duration_s=spec.duration_target_s or 1800,
        spec=spec,  # enables descriptor-driven prompt patches
    )
    _logger.info(
        "render_long_form: rewrote envelope slug=%s sections=%d panels=%d",
        env.slug,
        len(env.long_form.sections) if env.long_form else 0,
        len(env.long_form.panels) if env.long_form else 0,
    )

    # Stage B: write the legacy narration JSON shape into the channel dir
    # where pipeline.render.long_form will find it.
    paths = _resolve_paths(spec)
    narration_path = paths.narration_for(env.slug)
    narration_path.parent.mkdir(parents=True, exist_ok=True)
    narration_path.write_text(
        json.dumps(env.to_legacy_long_form_dict(), indent=2)
    )
    _logger.info("render_long_form: wrote narration → %s", narration_path)

    # Persist the full envelope alongside (kind=long_form) for audit.
    envelope_path = narration_path.with_suffix(".envelope.json")
    envelope_path.write_text(json.dumps(env.to_dict(), indent=2))

    # Live artifact previews (Slice 4): upload the envelope + narration
    # JSONs to GCS the moment they're authored so the dashboard can
    # show "Script: ready" within ~2 min of starting a long-form render
    # instead of waiting for the entire 30-min render to finish. The
    # narration .wav itself is uploaded by pipeline.render.long_form
    # later; what we emit here is the JSON shape that lists the
    # sections + panels so the user can preview the structure.
    try:
        from pipeline.render.artifacts import emit_artifact  # noqa: PLC0415
        emit_artifact(
            job_id=job_id,
            kind="envelope",
            local_path=envelope_path,
            extras={
                "slug": env.slug,
                "kind": env.kind,
                "n_sections": len(env.long_form.sections) if env.long_form else 0,
                "n_panels": len(env.long_form.panels) if env.long_form else 0,
                "title_options": list(env.title_options),
            },
        )
        emit_artifact(
            job_id=job_id,
            kind="script",
            local_path=narration_path,
            extras={
                "slug": env.slug,
                "kind": env.kind,
                "hook": (env.long_form.hook if env.long_form else "")[:300],
            },
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("emit_artifact(envelope/script) failed: %s", exc)

    # Stage C: invoke pipeline.render.long_form as a subprocess so its
    # internal stage-by-stage prints continue to flow through the
    # worker's existing log tailer (which classifies them into substage
    # progress markers via _classify_renderer_line).
    if progress_cb:
        progress_cb("narrate", "long-form chunked narration starting")

    channel_arg = _channel_arg_for_long_form(spec, paths)

    # Build a per-render YAML overlay from the form-driven RenderSpec
    # (Slice-2.P2 — 2026-05-12). long_form.py picks this up via its new
    # ``--config <path>`` flag and deep-merges it on top of the channel
    # YAML, so the user's form picks (voice, music_bed, output_resolution,
    # render_mode, captions_density, …) actually take effect on the
    # long-form path. Pre-2026-05-12 video.render_long_form shelled out
    # with only ``--channel/--slug`` and EVERY spec field except
    # duration_target_s was silently dropped — long_form.py would read
    # the channel YAML defaults regardless of what the user picked.
    #
    # The overlay is built by :func:`pipeline.render.input_registry.long_form_overlay_from_spec`
    # (P3 — descriptor-driven so adding a new form input is one entry,
    # not five). Until the registry lands, we still write the overlay
    # file (empty dict is harmless) so the wiring is in place.
    overlay_path = work_dir / "long_form_overlay.yaml"
    overlay: dict = {}
    try:
        from pipeline.render.input_registry import long_form_overlay_from_spec  # noqa: PLC0415
        overlay = long_form_overlay_from_spec(spec) or {}
    except ImportError:
        # Registry not yet shipped — keep overlay empty. long_form.py
        # gracefully no-ops on empty overlay.
        overlay = {}
    except Exception as exc:  # noqa: BLE001
        _logger.warning("long_form_overlay_from_spec failed: %s — "
                        "proceeding with channel YAML only", exc)
        overlay = {}
    overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=True))
    _logger.info("render_long_form: overlay → %s (%d top-level keys)",
                 overlay_path, len(overlay))

    cmd = [
        sys.executable, "-m", "pipeline.render.long_form",
        "--channel", channel_arg,
        "--slug", env.slug,
        "--config", str(overlay_path),
    ]
    _logger.info("render_long_form: invoking %s", " ".join(cmd))

    log_path = work_dir / "long_form_renderer.log"
    rc = _stream_subprocess(cmd, log_path=log_path, progress_cb=progress_cb)
    if rc != 0:
        # Surface the last few lines of the log to make the worker's
        # Firestore error field actionable.
        tail = ""
        if log_path.exists():
            try:
                lines = log_path.read_text().splitlines()
                tail = "\n".join(lines[-25:])
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(
            f"pipeline.render.long_form exited with code {rc}.\n"
            f"Last 25 log lines:\n{tail}"
        )

    mp4_path = paths.long_form_for(env.slug)
    if not mp4_path.exists():
        # Fallback: scan the channel dir for the slug mp4 (long_form.py
        # writes to paths.long_form_for(slug) by convention but we
        # cross-check).
        candidates = list(paths.root.rglob(f"{env.slug}.mp4"))
        if candidates:
            mp4_path = candidates[0]
            _logger.warning(
                "render_long_form: expected mp4 at %s missing; "
                "found candidate %s",
                paths.long_form_for(env.slug), mp4_path,
            )
        else:
            raise RuntimeError(
                f"pipeline.render.long_form succeeded but produced no mp4. "
                f"Expected at {mp4_path}; channel root: {paths.root}"
            )

    if progress_cb:
        progress_cb("compose", f"wrote {mp4_path.name}")

    # Live artifact preview (Slice 4): emit the canonical long-form mp4
    # the moment it lands so the dashboard's render-detail page can
    # show the FULL video preview ahead of the worker's final
    # _upload_mp4_to_gcs (which fires after the upload pill flips).
    # The two uploads target different GCS prefixes (artifacts/video/
    # vs short_uri) so they don't collide.
    try:
        from pipeline.render.artifacts import emit_artifact  # noqa: PLC0415
        from pipeline.probe import probe_duration  # noqa: PLC0415
        try:
            dur_s = float(probe_duration(mp4_path))
        except Exception:  # noqa: BLE001
            dur_s = 0.0
        emit_artifact(
            job_id=job_id,
            kind="video",
            local_path=mp4_path,
            extras={
                "slug": env.slug,
                "kind": env.kind,
                "duration_s": round(dur_s, 2),
                "width": spec.output_resolution[0],
                "height": spec.output_resolution[1],
                "fps": spec.output_fps,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("emit_artifact(video) failed: %s", exc)

    return mp4_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_story_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Translate the proposal shape (form input) into the rewriter's
    ``raw_story`` shape ``{slug, title, body, source, url}``.

    Same structural translation the worker's ``_stage_rewrite_real``
    already does for short; consolidated here so long-form picks up the
    same source-fetch hooks (TODO: full source_kind dispatch lives in
    the worker today; Slice 5 absorbs it).
    """
    slug = _slug_from_topic(
        proposal.get("topic") or "untitled",
        proposal.get("job_id") or "",
    )
    return {
        "slug": slug,
        "title": (proposal.get("topic") or "").strip(),
        "body": (proposal.get("notes") or "").strip(),
        "source": proposal.get("source_kind") or "user_text",
        "url": proposal.get("source_ref") or "",
    }


def _slug_from_topic(topic: str, job_id: str) -> str:
    """Mirror of the worker's ``_slug_from_topic`` so envelopes
    produced by the orchestrator land at the same on-disk path the
    worker would've picked. NOT a deep copy — keeps the orchestrator
    decoupled from worker internals."""
    import re
    s = (topic or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:60] if s else "untitled"
    if job_id:
        s = f"{s}-{job_id[:8]}"
    return s


def _merged_channel_cfg(spec: RenderSpec) -> dict[str, Any]:
    """Re-merge the channel + variant YAML for the rewriter.

    Spec already carries the resolved field values, but the rewriter's
    ``_channel_context`` wants the raw cfg dict (image_style_prefix,
    character_description, long_form block, etc). Cheaper to re-parse
    here than to round-trip everything through the spec dataclass."""
    cfg: dict = {}
    if spec.source_channel_yaml:
        try:
            cfg = yaml.safe_load(Path(spec.source_channel_yaml).read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "merged cfg: failed to read channel YAML %s: %s",
                spec.source_channel_yaml, exc,
            )
    if spec.source_variant_yaml:
        try:
            variant = yaml.safe_load(Path(spec.source_variant_yaml).read_text()) or {}
            cfg.update(variant)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "merged cfg: failed to read variant YAML %s: %s",
                spec.source_variant_yaml, exc,
            )
    return cfg


def _resolve_paths(spec: RenderSpec) -> RenderPaths:
    """Build the channel-rooted RenderPaths for this spec.

    Niched channels (mystoriesanimated/<niche>) get the niche-prefixed
    layout; flat channels (historyrecapped) get the flat layout.
    """
    # Use the niche-prefixed channel_dir form if the spec has a niche
    # and the canonical map has a routing for it — otherwise fall back
    # to the bare channel slug.
    channel_dir_arg = spec.channel
    if spec.niche:
        try:
            from pipeline.niches import NICHE_CHANNEL  # noqa: PLC0415
            # NICHE_CHANNEL maps niche → (channel_dir, channel_yaml).
            entry = NICHE_CHANNEL.get(spec.niche)
            if entry:
                channel_dir_arg = entry[0]
        except Exception:  # noqa: BLE001
            pass

    return RenderPaths.from_channel_dir(channel_dir_arg, project_root=REPO_ROOT)


def _channel_arg_for_long_form(spec: RenderSpec, paths: RenderPaths) -> str:
    """The ``--channel`` arg long_form.py expects.

    Today long_form.py's ``RenderPaths.from_channel_dir`` accepts either
    a flat channel slug or a compound ``<channel>/<niche>`` form. We
    pass the same string we used to build ``paths`` so layout stays
    consistent.
    """
    channel_dir_arg = spec.channel
    if spec.niche:
        try:
            from pipeline.niches import NICHE_CHANNEL  # noqa: PLC0415
            entry = NICHE_CHANNEL.get(spec.niche)
            if entry:
                channel_dir_arg = entry[0]
        except Exception:  # noqa: BLE001
            pass
    return channel_dir_arg


def _stream_subprocess(
    cmd: list[str],
    *,
    log_path: Path,
    progress_cb: ProgressCallback | None,
) -> int:
    """Run a subprocess streaming stdout to both log_path and the
    optional progress_cb (classified through the worker's existing
    long-form regex bank)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )

    with log_path.open("w") as logf:
        assert proc.stdout is not None
        for line in proc.stdout:
            logf.write(line)
            logf.flush()
            if progress_cb:
                _maybe_emit_long_form_progress(line, progress_cb)
        rc = proc.wait()

    return rc


def _maybe_emit_long_form_progress(
    line: str, progress_cb: ProgressCallback,
) -> None:
    """Map long_form.py's ``[N/5]`` markers to the worker's substage UI.

    long_form.py emits stages like ``[1/5] chunked TTS``,
    ``[2/5] image_panels``, ``[3/5] music bed``, ``[4/5] caption burn``,
    ``[5/5] final mux``. Map each to the closest worker timeline pill
    so the user sees live progress instead of staring at an opaque
    ``compose: real-mode``.
    """
    s = line.rstrip("\r\n")
    if not s.startswith("[") or "/5]" not in s:
        return
    try:
        chunk = s.split("]", 1)[0]  # "[1/5"
        idx = int(chunk[1:].split("/", 1)[0])
    except Exception:  # noqa: BLE001
        return
    msg = s.split("]", 1)[1].strip() if "]" in s else s
    stage_map = {
        1: "tts",
        2: "images",
        3: "compose",
        4: "compose",
        5: "compose",
    }
    stage = stage_map.get(idx, "compose")
    progress_cb(stage, msg or f"long-form stage {idx}/5")


__all__ = [
    "render",
    "render_long_form",
    "ProgressCallback",
]
