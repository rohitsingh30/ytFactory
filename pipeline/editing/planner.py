"""LLM planner for the cinematic editor.

Goes from director_notes + input manifest → an :class:`Edl` JSON
matching :mod:`pipeline.editing.schema`. The LLM call uses the
existing dispatcher in ``pipeline.llm.cli.call_llm`` so:

* On the laptop  → Claude CLI (free, OAuth-billed)
* On render-worker-v2 → Azure OpenAI (reuses chat-assistant secrets)
* Optional      → Anthropic SDK (pay-per-token)

The planner emits ONLY the closed-form JSON; the LLM never produces
ffmpeg commands. Every field is re-validated against the whitelist by
:func:`pipeline.editing.schema.build_edl_from_planner_json` before the
executor sees it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from pipeline.llm.cli import call_llm, ClaudeCLIError

from .schema import (
    Edl,
    EditMode,
    EdlValidationError,
    EDL_VERSION,
    FILTER_WHITELIST,
    LUT_WHITELIST,
    TRANSITION_WHITELIST,
    build_edl_from_planner_json,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """\
You are the EDITOR + DIRECTOR + COLORIST for ytFactory, a YouTube
content factory. You build cinematic edit decisions for ffmpeg.

Your job: given (a) a list of input artifacts (mp4 clips, image
stills, or both) and (b) the director's intent notes, emit a single
JSON document conforming to the EDL v{EDL_VERSION} schema. NOTHING
ELSE — no prose, no markdown, no ffmpeg commands. The downstream
executor enforces a strict whitelist.

ALLOWED filters (use only these):
{filter_whitelist}

ALLOWED transitions:
{transition_whitelist}

ALLOWED LUTs (3D color grades) — pick exactly ONE per EDL:
{lut_whitelist}

LUT picking guide:
- cinematic.cube  : default. Slight contrast lift + film roll-off.
- teal-orange.cube: action / drama / sports. Strong color contrast.
- noir.cube       : crime / mystery / dramatic war footage. Desaturated.
- warm-doc.cube   : documentary, history, devotional. Warm shadows.

Rules:
- mode MUST match the input shape: 1 mp4 → "polish"; folder of mp4s →
  "assemble-clips"; folder of images → "assemble-stills"; mixed →
  "assemble-mixed".
- aspect MUST match {aspect_constraint} (channel constraint).
- target_duration_s MUST land within {duration_band}.
- For polish: keep the original shot order; cuts only trim dead air.
- For assemble-stills: hold each image >=1.8s, <=4.0s. Use zoompan for
  Ken Burns motion (slow zoom-in OR slow zoom-out, never both).
- For assemble-clips/mixed: pick the strongest 4-12 shots, hold each
  1.5-3.5s. Tag transition_out for every shot except the last.
- xfade transitions REQUIRE transition_out.to_idx = next shot's idx.
- Shots reference inputs by exact basename (or basename#scene_N for
  PySceneDetect-split clips). The executor resolves to disk paths.
- audio.loudnorm_lufs = -14.0 always (YouTube standard).
- Music is OPTIONAL. If you set audio.music, source MUST be one of
  "archive_pd" / "youtube_audio_library" / "path", and path MUST live
  under <channel>/music/ or pipeline/editing/music/.

Output ONLY valid JSON matching the schema. Do not wrap in markdown
fences. Do not prepend or append commentary.
"""


JSON_SCHEMA_HINT = {
    "type": "object",
    "required": ["version", "mode", "aspect", "target_duration_s", "shots"],
    "properties": {
        "version": {"type": "integer"},
        "mode": {
            "type": "string",
            "enum": [m.value for m in EditMode],
        },
        "channel": {"type": ["string", "null"]},
        "aspect": {"type": "string"},
        "fps": {"type": "integer"},
        "target_duration_s": {"type": "number"},
        "lut": {
            "type": ["string", "null"],
            "enum": [None, *sorted(LUT_WHITELIST)],
        },
        "letterbox": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "ratio": {"type": "number"},
            },
        },
        "audio": {
            "type": "object",
            "properties": {
                "duck_speech_db": {"type": "number"},
                "loudnorm_lufs": {"type": "number"},
                "music": {"type": ["object", "null"]},
            },
        },
        "shots": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["idx", "input_ref"],
                "properties": {
                    "idx": {"type": "integer"},
                    "input_ref": {"type": "string"},
                    "in_s": {"type": "number"},
                    "out_s": {"type": "number"},
                    "filters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["type"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": sorted(FILTER_WHITELIST),
                                },
                                "args": {"type": "string"},
                            },
                        },
                    },
                    "transition_in":  {"type": ["object", "null"]},
                    "transition_out": {"type": ["object", "null"]},
                    "notes": {"type": "string"},
                },
            },
        },
        "captions": {"type": "object"},
        "director_notes": {"type": "string"},
    },
}


def _build_user_prompt(
    *,
    input_manifest: list[dict],
    mode: EditMode,
    director_notes: str,
    channel_profile: Optional[dict],
    target_duration_s: float,
) -> str:
    parts = [
        f"MODE: {mode.value}",
        f"TARGET_DURATION_S: {target_duration_s:.1f}",
    ]
    if channel_profile:
        parts.append(f"CHANNEL_PROFILE:\n{json.dumps(channel_profile, indent=2)}")
    parts.append(f"DIRECTOR_NOTES:\n{director_notes.strip() or '(none)'}")
    parts.append(
        "INPUT_MANIFEST (each entry has basename, kind, and per-kind metadata):"
    )
    parts.append(json.dumps(input_manifest, indent=2))
    parts.append("")
    parts.append(
        "Emit one JSON document conforming to the EDL schema. "
        "No prose. No markdown fences."
    )
    return "\n\n".join(parts)


def plan_edit(
    *,
    input_manifest: list[dict],
    mode: EditMode | str,
    director_notes: str = "",
    channel_profile: Optional[dict] = None,
    target_duration_s: float = 60.0,
    aspect: str = "9:16",
    duration_band: str = "50-60s",
    model: str = "sonnet",
    timeout_s: int = 120,
) -> Edl:
    """Call the LLM planner and return a validated :class:`Edl`.

    Args:
        input_manifest: List of dicts with at least ``basename`` and
            ``kind`` (``"mp4"`` / ``"image"``). Per-kind metadata
            (duration, dimensions, scene_list) is included so the
            planner can pace correctly.
        mode: One of :class:`EditMode`. Auto-detected from input shape
            by the skill stage; passed in here.
        director_notes: Free-form intent from the skill's stage 3.
        channel_profile: Channel constraints (closer, voice, banned
            phrasings, etc.) for context. None when running bare.
        target_duration_s: Target final runtime (planner will land
            within ±10%).
        aspect: ``"9:16"`` / ``"16:9"`` / ``"1:1"``.
        duration_band: Human-readable band shown to the LLM
            (e.g. ``"50-60s"`` for Shorts, ``"60-120m"`` for sleep).
        model: Tier alias (haiku / sonnet / opus). Sonnet is the
            default — this is a planning task, not a critique task.
        timeout_s: Per-call LLM timeout.

    Returns:
        Validated :class:`Edl`. Caller can pass it straight to
        ``pipeline.editing.executor.execute_local`` or
        ``pipeline.editing.cloudrun.execute_edit``.

    Raises:
        EdlValidationError: planner emitted JSON that violated the
            whitelist or schema.
        ClaudeCLIError: LLM backend failure (subprocess error, HTTP
            error, JSON parse error).
    """
    if isinstance(mode, str):
        mode_enum = EditMode(mode)
    else:
        mode_enum = mode

    sys_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        EDL_VERSION=EDL_VERSION,
        filter_whitelist=", ".join(sorted(FILTER_WHITELIST)),
        transition_whitelist=", ".join(sorted(TRANSITION_WHITELIST)),
        lut_whitelist=", ".join(sorted(LUT_WHITELIST)),
        aspect_constraint=aspect,
        duration_band=duration_band,
    )
    user_prompt = _build_user_prompt(
        input_manifest=input_manifest,
        mode=mode_enum,
        director_notes=director_notes,
        channel_profile=channel_profile,
        target_duration_s=target_duration_s,
    )
    full_prompt = f"{sys_prompt}\n\n---\n\n{user_prompt}"

    logger.info(
        "editing.planner: calling LLM (mode=%s, inputs=%d, target=%.1fs, model=%s)",
        mode_enum.value, len(input_manifest), target_duration_s, model,
    )
    raw = call_llm(
        full_prompt,
        output_json=True,
        json_schema=JSON_SCHEMA_HINT,
        model=model,
        timeout_s=timeout_s,
        stage="editing_plan",
    )
    if not isinstance(raw, dict):
        raise EdlValidationError(
            f"planner returned non-object JSON ({type(raw).__name__}); "
            "this is almost always a backend formatting bug — re-run with "
            "model=opus or check the LLM response in telemetry."
        )

    edl = build_edl_from_planner_json(raw)
    logger.info(
        "editing.planner: emitted EDL with %d shots, lut=%s, mode=%s",
        len(edl.shots), edl.lut, edl.mode,
    )
    return edl


def manifest_for_inputs(input_paths: list[Path]) -> list[dict]:
    """Build an ``input_manifest`` for :func:`plan_edit` from a list of
    file paths. Probes each via ffprobe (for mp4) or PIL (for images)
    so the planner has duration / dimensions / scene boundaries to
    plan against.

    Kept thin — no shelling out unless the file actually exists. Tests
    pass a hand-built manifest and skip this helper.
    """
    import subprocess

    out: list[dict] = []
    for p in input_paths:
        suf = p.suffix.lower()
        entry: dict[str, Any] = {"basename": p.name}
        if suf == ".mp4":
            entry["kind"] = "mp4"
            try:
                proc = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=duration,width,height,r_frame_rate",
                        "-of", "json", str(p),
                    ],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if proc.returncode == 0:
                    info = json.loads(proc.stdout or "{}")
                    s = (info.get("streams") or [{}])[0]
                    entry["duration_s"] = float(s.get("duration") or 0.0)
                    entry["width"] = int(s.get("width") or 0)
                    entry["height"] = int(s.get("height") or 0)
            except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as e:
                logger.debug("ffprobe failed on %s: %s", p, e)
        elif suf in (".png", ".jpg", ".jpeg", ".webp"):
            entry["kind"] = "image"
            try:
                from PIL import Image  # type: ignore  # noqa: PLC0415

                with Image.open(p) as im:
                    entry["width"], entry["height"] = im.size
            except Exception as e:  # noqa: BLE001
                logger.debug("PIL failed on %s: %s", p, e)
        else:
            entry["kind"] = "other"
        out.append(entry)
    return out
