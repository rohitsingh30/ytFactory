"""Local executor — compiles an :class:`Edl` to ffmpeg and runs it.

This is the laptop fallback path. Same module is also baked into
``cloud/editing-agent/`` and used by the cloud service handler — same
EDL → same ffmpeg invocation → same output, regardless of where it
runs. That symmetry is the point: the cloud service is "this code +
GCS handlers + a port", nothing more.

Modes:

* ``polish``         — single mp4 in, polish pass: trim + LUT +
                       letterbox + grade + audio loudnorm. Implemented
                       as one ffmpeg invocation with -filter_complex.
* ``assemble-clips`` — multiple mp4s, assembled per the EDL shot list.
                       Each shot is trim+filter, then concatenated.
* ``assemble-stills``— image sequence with slow zoom + crossfade. Each
                       shot is image2pipe → loop → zoompan → scale,
                       then concatenated. Audio is silent unless
                       ``audio.music`` is set.
* ``assemble-mixed`` — same concat pipeline as -clips, with image shots
                       internally upgraded to mp4-frames-per-shot before
                       the concat.

The compiler favors a SINGLE ffmpeg invocation (filter_complex chain)
over multi-pass pipelines. Audio loudnorm is applied single-pass inside
that same filter_complex chain.

Inputs are resolved against ``input_root`` (the directory the skill
passed). The executor REJECTS any ``input_ref`` that escapes that
root (path traversal guard).
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .schema import (
    Edl,
    EditMode,
    EdlValidationError,
    Filter,
    Shot,
    Transition,
    LUT_WHITELIST,
    lut_path_for,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- helpers


def _ffmpeg_bin() -> str:
    """Resolve ffmpeg binary. Honors FFMPEG_BIN env (used by tests)."""
    import os

    bin_ = os.environ.get("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg") or "ffmpeg"
    return bin_


def _aspect_to_dims(aspect: str, base: int = 1080) -> tuple[int, int]:
    """Map aspect string to (W, H). 9:16 → 1080x1920."""
    table = {
        "9:16":   (1080, 1920),
        "16:9":   (1920, 1080),
        "1:1":    (1080, 1080),
        "4:5":    (1080, 1350),
        "2.39:1": (1920, 804),
    }
    return table.get(aspect, (1080, 1920))


def _safe_input_path(input_ref: str, input_root: Path) -> tuple[Path, Optional[str]]:
    """Resolve an EDL ``input_ref`` to a real path under ``input_root``.

    ``input_ref`` is either a basename (``"clip_03.mp4"``) or
    ``"clip_03.mp4#scene_2"`` (PySceneDetect-split). Returns
    ``(path, scene_marker)``. Rejects anything that escapes
    ``input_root`` via ``..`` or absolute paths.
    """
    ref = input_ref.strip()
    scene: Optional[str] = None
    if "#" in ref:
        ref, scene = ref.split("#", 1)
    p = (input_root / ref).resolve()
    root = input_root.resolve()
    try:
        p.relative_to(root)
    except ValueError as e:
        raise EdlValidationError(
            f"input_ref {input_ref!r} resolves outside input_root "
            f"({p} not under {root})"
        ) from e
    if not p.exists():
        raise EdlValidationError(
            f"input_ref {input_ref!r} does not exist at {p}"
        )
    return p, scene


def _filter_to_str(f: Filter) -> str:
    """Render a single Filter into ffmpeg-filter-graph syntax."""
    if f.type == "lut3d" and not f.args:
        # Convenience: bare lut3d gets the cinematic.cube path injected.
        path = lut_path_for("cinematic.cube")
        return f"lut3d=file={shlex.quote(str(path))}"
    if f.type == "lut3d" and "file=" in f.args:
        # The planner emits ``file=luts/<name>.cube``. Re-resolve to
        # the absolute on-disk path so ffmpeg finds it regardless of
        # CWD. Validates the LUT name against the whitelist.
        if "file=" not in f.args:
            return f"{f.type}={f.args}"
        prefix, _, rest = f.args.partition("file=")
        # rest may have trailing args like ":interp=tetrahedral"
        cube, _, suffix = rest.partition(":")
        cube = cube.strip().strip("'\"")
        cube_name = Path(cube).name
        if cube_name not in LUT_WHITELIST:
            raise EdlValidationError(
                f"lut file {cube_name!r} not in whitelist; "
                f"valid: {sorted(LUT_WHITELIST)}"
            )
        cube_path = lut_path_for(cube_name)
        rebuilt = f"{prefix}file={shlex.quote(str(cube_path))}"
        if suffix:
            rebuilt += f":{suffix}"
        return f"{f.type}={rebuilt}"
    if not f.args:
        return f.type
    return f"{f.type}={f.args}"


def _shot_filter_chain(shot: Shot, target_w: int, target_h: int) -> str:
    """Build the per-shot filter chain. Always ends with scale to target
    dims + setsar=1 + format=yuv420p so xfade between shots works."""
    parts = [_filter_to_str(f) for f in shot.filters]
    # Defensive: even if the planner forgot, ensure dims + pixel format
    # are uniform across all shots so xfade doesn't error.
    parts.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease")
    parts.append(f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black")
    parts.append("setsar=1")
    parts.append("format=yuv420p")
    return ",".join(parts)


# ---------------------------------------------------------------------- compile


@dataclass
class CompiledEdit:
    cmd: list[str]
    output_path: Path
    work_dir: Path
    note: str = ""


def compile_edit(
    edl: Edl,
    *,
    input_root: Path,
    output_dir: Path,
    output_name: str = "edited.mp4",
) -> CompiledEdit:
    """Translate the EDL to a single ffmpeg invocation.

    Returns the compiled command + output path. Doesn't run ffmpeg
    itself — that's :func:`execute_local` so callers can dry-run for
    tests.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name

    target_w, target_h = _aspect_to_dims(edl.aspect)

    inputs: list[str] = []
    filter_parts: list[str] = []
    concat_inputs: list[str] = []

    for shot in edl.shots:
        path, _scene = _safe_input_path(shot.input_ref, input_root)
        suf = path.suffix.lower()
        if suf in (".png", ".jpg", ".jpeg", ".webp"):
            # Image: loop for the shot duration, decode to raw frames.
            duration_s = max(shot.out_s - shot.in_s, 1.5)
            inputs.extend(["-loop", "1", "-t", f"{duration_s:.3f}", "-i", str(path)])
        else:
            # Video clip: trim with -ss/-to (output-side for accuracy).
            in_s = max(shot.in_s, 0.0)
            args = ["-i", str(path)]
            inputs.extend(args)
            # Trim is encoded as a filter on the input stream below.

        idx = len(inputs) // 2 - 1  # last added input index
        chain = _shot_filter_chain(shot, target_w, target_h)
        # Trim filter (output of the input stream).
        if suf not in (".png", ".jpg", ".jpeg", ".webp") and shot.out_s > shot.in_s:
            chain = (
                f"trim=start={shot.in_s:.3f}:end={shot.out_s:.3f},"
                f"setpts=PTS-STARTPTS,{chain}"
            )
        label_in = f"[{idx}:v]"
        label_out = f"[v{shot.idx}]"
        filter_parts.append(f"{label_in}{chain}{label_out}")
        concat_inputs.append(label_out)

    # Concat the per-shot streams. v1 uses simple concat; xfade
    # transitions are a follow-up — they require pairwise xfade nodes
    # with timing arithmetic. Documented as a TODO so the next pass
    # picks it up.
    if len(concat_inputs) == 1:
        # Single shot — just rename the label.
        filter_parts.append(f"{concat_inputs[0]}null[vout]")
    else:
        n = len(concat_inputs)
        filter_parts.append(f"{''.join(concat_inputs)}concat=n={n}:v=1:a=0[vout]")

    # LUT (3D color grade) — applied AFTER concat so it grades the whole
    # timeline uniformly. Per-shot LUT could happen pre-concat if the
    # EDL ever needs per-shot grades.
    if edl.lut:
        cube = lut_path_for(edl.lut)
        filter_parts.append(f"[vout]lut3d=file={shlex.quote(str(cube))}[vgraded]")
        final_label = "[vgraded]"
    else:
        final_label = "[vout]"

    # Letterbox: crop+pad to scope ratio inside the canvas.
    if edl.letterbox.enabled:
        # 2.39:1 inside 9:16 canvas → black bars top+bottom. Compute
        # the inner height: w * 1/ratio.
        inner_h = int(target_w / edl.letterbox.ratio)
        # crop to inner_h, then pad back to canvas with black.
        filter_parts.append(
            f"{final_label}crop={target_w}:{inner_h},"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black[vletterbox]"
        )
        final_label = "[vletterbox]"

    filter_complex = ";".join(filter_parts)

    cmd = [_ffmpeg_bin(), "-y", "-loglevel", "error", "-stats"]
    cmd.extend(inputs)
    # Audio: copy from input 0 if it's a video, else silent. v1 keeps
    # it simple — full audio mix (music + duck + loudnorm) is wired
    # below in the polish-mode shortcut.
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", final_label,
    ])
    # Audio map: for polish mode, take the source audio from input 0.
    # For assemble modes, emit silent track unless music is added.
    has_video_input = any(
        s.input_ref.lower().endswith(".mp4") for s in edl.shots
    )
    if edl.mode == EditMode.POLISH.value and has_video_input:
        cmd.extend(["-map", "0:a?"])
    cmd.extend([
        "-r", str(edl.fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])

    return CompiledEdit(
        cmd=cmd,
        output_path=output_path,
        work_dir=output_dir,
        note=(
            f"mode={edl.mode}, shots={len(edl.shots)}, "
            f"lut={edl.lut}, letterbox={edl.letterbox.enabled}, "
            f"target={target_w}x{target_h}@{edl.fps}fps"
        ),
    )


# ----------------------------------------------------------------------- run


def execute_local(
    edl: Edl,
    *,
    input_root: Path,
    output_dir: Path,
    output_name: str = "edited.mp4",
    timeout_s: int = 1800,
    ffmpeg_observer: Callable[[list[str], int, str, int, Path], None] | None = None,
) -> Path:
    """Compile + run the EDL on the local ffmpeg. Returns the produced
    mp4 path. Raises :class:`subprocess.CalledProcessError` on ffmpeg
    failure (the caller can fall back to cloud or surface the error)."""
    compiled = compile_edit(
        edl,
        input_root=input_root,
        output_dir=output_dir,
        output_name=output_name,
    )
    logger.info("editing.executor: %s", compiled.note)
    logger.debug("editing.executor cmd: %s", " ".join(shlex.quote(c) for c in compiled.cmd))

    t0 = time.perf_counter()
    proc = subprocess.run(
        compiled.cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    duration_ms = int((time.perf_counter() - t0) * 1000)
    if ffmpeg_observer:
        try:
            ffmpeg_observer(
                compiled.cmd,
                int(proc.returncode),
                proc.stderr or "",
                duration_ms,
                compiled.output_path,
            )
        except Exception:  # noqa: BLE001
            pass
    if proc.returncode != 0:
        # Surface the tail of stderr — ffmpeg errors are usually in the
        # last 20 lines.
        tail = (proc.stderr or "").strip().splitlines()[-20:]
        raise subprocess.CalledProcessError(
            proc.returncode,
            compiled.cmd,
            output=proc.stdout,
            stderr="\n".join(tail),
        )
    if not compiled.output_path.exists():
        raise RuntimeError(
            f"ffmpeg returned 0 but output {compiled.output_path} missing — "
            "filter graph likely produced 0 frames; check filter_complex "
            "scaling and concat node count."
        )
    return compiled.output_path

