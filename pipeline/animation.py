"""Stage 6 (alt) — continuous 2D animation per beat.

Replaces the slideshow-style "one static image per beat" with an
actual short animated clip per beat. The default backend is
**AnimateDiff + ToonYou**, which is the only stack today that:

  - is genuinely cartoon-trained (not photoreal + bolted-on LoRA)
  - fits in ~5 GB of model weights
  - runs in single-digit minutes per Short on M-series MPS

Each beat becomes a 16–24 frame clip at 8 fps (AnimateDiff's training
rate). compose.py's ``compose_clips`` stitches them with crossfades.

For beats longer than ~3s, AnimateDiff alone runs out of motion — the
``extend_with_tooncrafter`` helper below is the v1 path that chains
chunks via ``Doubiiu/ToonCrafter`` interpolation. v0.5 just clamps
beats to <=3s (consistent with ``beat_max_s`` in channel YAMLs).

All heavy imports and ``from_pretrained`` calls are lazy. Importing
this module never triggers a model download.

Models pulled on first call (cached under
``~/.cache/huggingface/hub/``):

  * ``frankjoshua/toonyou_beta6`` (~2 GB)        — SD1.5 toon checkpoint
  * ``guoyww/animatediff-motion-adapter-v1-5-3`` (~1.7 GB) — motion adapter

Optional v1 add-on (only if you call ``extend_with_tooncrafter``):

  * ``Doubiiu/ToonCrafter`` (~10 GB)             — cartoon interpolation

Provider keys exposed via channel config:

  * ``motion_provider: animatediff_toonyou``     — this module's default
  * ``motion_provider: animatediff_lcm``         — fast 4-step variant
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any


# Default model ids per provider key. Channel YAMLs can override.
# animatediff_lcm pairs the AnimateLCM motion adapter + LCM LoRA with
# the SAME toon base as animatediff_toonyou — keeps the cartoon look,
# adds 4-step LCM inference. ~5x faster than animatediff_toonyou.
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "animatediff_toonyou": {
        "base": "frankjoshua/toonyou_beta6",
        "motion_adapter": "guoyww/animatediff-motion-adapter-v1-5-3",
    },
    "animatediff_lcm": {
        "base": "frankjoshua/toonyou_beta6",
        "motion_adapter": "wangfuyun/AnimateLCM",
    },
}

# 8 fps is what AnimateDiff was trained at. Don't change unless you know
# what you're doing — different fps wrecks motion quality.
FPS = 8

# Cap frames at 16 (= 2s @ 8fps) to keep MPS memory in check. Beats
# longer than 2s loop the clip in compose; >3s would need ToonCrafter.
MAX_FRAMES = 16
MIN_FRAMES = 8  # below this AnimateDiff produces choppy clips

# Output 9:16 portrait at a small resolution that MPS can actually run
# in float16 — ffmpeg upscales to 1080x1920 in compose_clips. Going
# lower than this gives noticeable line-art artefacts on AnimateDiff.
WIDTH = 320
HEIGHT = 576

_PIPE_CACHE: dict[str, Any] = {}


def _build_pipe(provider: str, base_model: str, motion_model: str) -> Any:
    """Lazy-build (and cache) an AnimateDiffPipeline.

    For ``animatediff_lcm``: also fuses the AnimateLCM LCM LoRA from
    ``wangfuyun/AnimateLCM`` and swaps the scheduler to ``LCMScheduler``,
    so ``generate_clip`` can run in 4-6 steps at guidance ~1.5 instead
    of 25 steps at guidance 7.5. ~5x faster on MPS, with quality close
    enough for Shorts.
    """
    cache_key = f"{provider}::{base_model}::{motion_model}"
    if cache_key in _PIPE_CACHE:
        return _PIPE_CACHE[cache_key]

    import torch  # type: ignore
    from diffusers import (  # type: ignore
        AnimateDiffPipeline,
        DDIMScheduler,
        LCMScheduler,
        MotionAdapter,
    )

    print(f"[animation] loading motion adapter: {motion_model}")
    adapter = MotionAdapter.from_pretrained(motion_model, torch_dtype=torch.float16)

    print(f"[animation] loading base toon checkpoint: {base_model}")
    pipe = AnimateDiffPipeline.from_pretrained(
        base_model,
        motion_adapter=adapter,
        torch_dtype=torch.float16,
    )

    if provider == "animatediff_lcm":
        # Latent Consistency: 4-step inference path. Scheduler + LoRA from
        # the same wangfuyun/AnimateLCM repo as the motion adapter.
        pipe.scheduler = LCMScheduler.from_config(
            pipe.scheduler.config, beta_schedule="linear"
        )
        try:
            pipe.load_lora_weights(
                "wangfuyun/AnimateLCM",
                weight_name="AnimateLCM_sd15_t2v_lora.safetensors",
                adapter_name="lcm-lora",
            )
            pipe.set_adapters(["lcm-lora"], [0.8])
            print("[animation] LCM LoRA fused at weight 0.8")
        except Exception as e:
            print(
                f"[animation] WARNING: LCM LoRA load failed ({e}); "
                "continuing without — quality may degrade. "
                "Make sure the `peft` package is installed."
            )
    else:
        # Default schedulers other than DDIM give noisier motion on AnimateDiff.
        pipe.scheduler = DDIMScheduler.from_config(
            pipe.scheduler.config,
            beta_schedule="linear",
            clip_sample=False,
            timestep_spacing="linspace",
            steps_offset=1,
        )

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    pipe = pipe.to(device)
    pipe.set_progress_bar_config(disable=True)
    # New API replaces the deprecated pipe.enable_vae_slicing.
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_slicing"):
        pipe.vae.enable_slicing()
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        # Tiling cuts peak VAE memory ~2x at small encode/decode overhead.
        pipe.vae.enable_tiling()

    _PIPE_CACHE[cache_key] = pipe
    return pipe


def free_pipe() -> None:
    """Drop any loaded AnimateDiff pipelines + flush MPS cache.

    Call between Shorts (or just before you switch channels) to release
    the multi-GB pipeline graph from unified memory. Cheap no-op if no
    pipeline was loaded.
    """
    import gc

    _PIPE_CACHE.clear()
    gc.collect()
    try:
        import torch  # type: ignore

        if torch.backends.mps.is_available() and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
    except Exception:
        pass


def _frames_for_duration(duration_s: float) -> int:
    return max(MIN_FRAMES, min(MAX_FRAMES, int(math.ceil(duration_s * FPS))))


def _frames_to_mp4(frames: list, out_path: Path, fps: int = FPS) -> Path:
    """Write a list of PIL images to an mp4 via ffmpeg.

    Avoids relying on ``diffusers.utils.export_to_video`` (which pulls
    imageio + imageio-ffmpeg). We already have ffmpeg as a hard dep.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, im in enumerate(frames):
            im.save(Path(tmpdir) / f"f_{i:04d}.png")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", f"{tmpdir}/f_%04d.png",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg frames->mp4 failed (exit {result.returncode})\n"
                f"{result.stderr[-1500:]}"
            )
    return out_path


def generate_clip(
    prompt: str,
    style_prefix: str,
    seed: int,
    duration_s: float,
    out_path: Path,
    provider: str = "animatediff_toonyou",
    base_model: str | None = None,
    motion_model: str | None = None,
    num_inference_steps: int = 25,
    guidance_scale: float = 7.5,
    negative_prompt: str = "blurry, low quality, distorted, deformed, watermark, text, signature",
) -> Path:
    """Generate one animated clip for one beat. Writes mp4 at FPS fps."""
    if provider not in PROVIDER_DEFAULTS:
        raise ValueError(
            f"unknown motion_provider {provider!r}. "
            f"choices: {list(PROVIDER_DEFAULTS)}"
        )
    defaults = PROVIDER_DEFAULTS[provider]
    base_model = base_model or defaults["base"]
    motion_model = motion_model or defaults["motion_adapter"]

    import torch  # type: ignore

    full_prompt = f"{style_prefix.strip()}, {prompt.strip()}"
    n_frames = _frames_for_duration(duration_s)

    if provider == "animatediff_lcm":
        # AnimateLCM is trained for 4-step inference. Higher steps actually
        # *degrade* quality. CFG must be low (~1-2) for LCM to converge.
        num_inference_steps = 4
        guidance_scale = 1.5

    pipe = _build_pipe(provider, base_model, motion_model)
    print(
        f"[animation] {n_frames}f @ {FPS}fps ({n_frames / FPS:.2f}s) "
        f"{WIDTH}x{HEIGHT} seed={seed}"
    )

    output = pipe(
        prompt=full_prompt,
        negative_prompt=negative_prompt,
        num_frames=n_frames,
        width=WIDTH,
        height=HEIGHT,
        guidance_scale=guidance_scale,
        num_inference_steps=num_inference_steps,
        generator=torch.Generator(device="cpu").manual_seed(seed),
    )
    frames = output.frames[0]  # list[PIL.Image]
    return _frames_to_mp4(frames, out_path, fps=FPS)


def beat_to_prompt(beat_text: str) -> str:
    """Same heuristic fallback as images.beat_to_prompt — used only when no
    per-beat ``prompts.json`` is provided. The real prompts come from the
    rewriter (Claude) at script-creation time."""
    return f"a single character {beat_text.strip().rstrip('.,!?;:').lower()}"


# ---------- v1 hook: ToonCrafter chaining ---------------------------------


def extend_with_tooncrafter(
    *,
    start_clip: Path,
    end_frame_prompt: str,
    seed: int,
    out_path: Path,
    style_prefix: str,
) -> Path:
    """v1 stub: chain a long beat by interpolating between AnimateDiff
    chunks with ``Doubiiu/ToonCrafter``.

    Not wired into the v0.5 path because beats are clamped to <=3s,
    which fits a single 24-frame AnimateDiff window. Documented here as
    the next quality jump.
    """
    raise NotImplementedError(
        "ToonCrafter chaining is the v1 path — not needed while beats are "
        "clamped to beat_max_s <= 3.0. Plumb it in once you start running "
        "longer beats. Model: Doubiiu/ToonCrafter (~10 GB, Apache-2.0)."
    )
