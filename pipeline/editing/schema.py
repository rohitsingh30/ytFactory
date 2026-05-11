"""Closed-form EDL schema for the cinematic editor.

The schema is deliberately tight — every field has a fixed type and
every filter / transition / LUT comes from a whitelist. The LLM
planner is constrained to emit only this shape; the executor REJECTS
any EDL that violates the whitelist. This keeps the LLM out of the
ffmpeg command line entirely.

Whitelists live here (single source of truth) and are imported by
both planner.py (system prompt + post-validate) and executor.py
(pre-flight check + filter compile). Adding a new filter/transition
requires:

1. Add to the appropriate ``*_WHITELIST`` set below.
2. Add the compile branch in ``executor._compile_filter`` /
   ``_compile_transition``.
3. Add a paragraph to the planner system prompt so the LLM knows when
   to use it.

If you change the schema, bump :data:`EDL_VERSION` and add a migration
note in ``docs/editing_agent.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional


EDL_VERSION = 1


class EditMode(str, Enum):
    POLISH = "polish"
    ASSEMBLE_CLIPS = "assemble-clips"
    ASSEMBLE_STILLS = "assemble-stills"
    ASSEMBLE_MIXED = "assemble-mixed"


# Filter whitelist — every entry must have a compile branch in
# executor._compile_filter. Args follow ffmpeg filter syntax (post-name,
# pre-comma).
FILTER_WHITELIST: set[str] = {
    "crop",       # crop=iw:iw/2.39
    "scale",      # scale=1080:608
    "eq",         # eq=contrast=1.1:saturation=1.1:gamma=1.05
    "lut3d",      # lut3d=file=luts/cinematic.cube
    "fade",       # fade=in:0:30
    "zoompan",    # zoompan=z='zoom+0.0008':d=125:s=1080x1920
    "subtitles",  # subtitles=path/to/srt.srt
    "boxblur",    # for region scrub (logos, watermarks)
    "format",     # format=yuv420p (kept for compatibility w/ filter_complex)
    "setsar",     # setsar=1
}

# Transition whitelist (xfade modes from ffmpeg's xfade filter).
TRANSITION_WHITELIST: set[str] = {
    "fade",          # plain fade in/out (the `fade` filter, not xfade)
    "xfade-fade",
    "xfade-fadeblack",
    "xfade-fadewhite",
    "xfade-wipeleft",
    "xfade-wiperight",
    "xfade-slideleft",
    "xfade-slideright",
    "xfade-dissolve",
    "xfade-pixelize",
    "xfade-radial",
    "cut",           # hard cut, no transition
}

# Audio filter whitelist.
AUDIO_FILTER_WHITELIST: set[str] = {
    "volume",
    "afade",
    "loudnorm",
    "highpass",
    "lowpass",
    "dynaudnorm",
    "amix",          # mix narration + music
    "sidechaincompress",  # ducking music under speech
}

# LUT whitelist — every cube file lives in pipeline/editing/luts/ and
# ships in the cloud container image.
LUT_WHITELIST: set[str] = {
    "cinematic.cube",
    "teal-orange.cube",
    "noir.cube",
    "warm-doc.cube",
}


# --- dataclasses ---


@dataclass
class Filter:
    type: str  # one of FILTER_WHITELIST
    args: str = ""  # raw filter arg string (validated against shlex-safe chars)


@dataclass
class AudioFilter:
    type: str  # one of AUDIO_FILTER_WHITELIST
    args: str = ""


@dataclass
class Transition:
    type: str  # one of TRANSITION_WHITELIST
    duration_s: float = 0.3
    to_idx: Optional[int] = None  # required for xfade-*; ignored for cut/fade


@dataclass
class Shot:
    idx: int
    input_ref: str  # e.g. "clip_03.mp4#scene_2" or "image_07.png" — resolved by executor
    in_s: float = 0.0
    out_s: float = 0.0  # for stills, defines the on-screen duration; for clips, the trim out
    filters: list[Filter] = field(default_factory=list)
    transition_in: Optional[Transition] = None
    transition_out: Optional[Transition] = None
    notes: str = ""  # director's intent, kept for the post-mortem; ignored by executor


@dataclass
class AudioConfig:
    duck_speech_db: float = -3.0
    loudnorm_lufs: float = -14.0
    music: Optional["MusicConfig"] = None


@dataclass
class MusicConfig:
    source: str = ""  # "archive_pd" / "youtube_audio_library" / "path"
    path: str = ""  # file path or GCS URI; validated against allowed roots
    fade_in_s: float = 1.0
    fade_out_s: float = 2.0
    duck_under_speech: bool = True


@dataclass
class LetterboxConfig:
    enabled: bool = False
    ratio: float = 2.39  # 2.39:1 cinematic scope


@dataclass
class CaptionsConfig:
    preserve_burned: bool = True
    add_overlay: bool = False
    srt_path: Optional[str] = None


@dataclass
class Edl:
    version: int = EDL_VERSION
    mode: str = EditMode.POLISH.value
    channel: Optional[str] = None  # slug, used by executor for path resolution
    aspect: str = "9:16"
    fps: int = 30
    target_duration_s: float = 60.0
    lut: Optional[str] = "cinematic.cube"
    letterbox: LetterboxConfig = field(default_factory=LetterboxConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    shots: list[Shot] = field(default_factory=list)
    captions: CaptionsConfig = field(default_factory=CaptionsConfig)
    director_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# --- parsing + validation ---


class EdlValidationError(ValueError):
    """Raised when a planner-emitted JSON violates the schema or
    whitelists. The executor uses this to refuse compilation rather
    than silently passing through unsafe ffmpeg fragments."""


def _validate_filter(f: dict, where: str) -> Filter:
    t = (f.get("type") or "").strip()
    if t not in FILTER_WHITELIST:
        raise EdlValidationError(
            f"{where}: filter type {t!r} not in whitelist "
            f"({sorted(FILTER_WHITELIST)})"
        )
    args = str(f.get("args") or "")
    # Forbid backticks, semicolons, $(), |, > to prevent shell injection
    # if any caller foolishly templates this through a shell. ffmpeg
    # itself never sees a shell — but defense in depth.
    bad_chars = set("`;$|><\n")
    if any(c in args for c in bad_chars):
        raise EdlValidationError(
            f"{where}: filter args {args!r} contain forbidden char "
            f"(one of `;$|><\\n)"
        )
    return Filter(type=t, args=args)


def _validate_transition(t: dict | None, where: str) -> Optional[Transition]:
    if not t:
        return None
    typ = (t.get("type") or "").strip()
    if typ not in TRANSITION_WHITELIST:
        raise EdlValidationError(
            f"{where}: transition type {typ!r} not in whitelist "
            f"({sorted(TRANSITION_WHITELIST)})"
        )
    return Transition(
        type=typ,
        duration_s=float(t.get("duration_s") or 0.3),
        to_idx=t.get("to_idx"),
    )


def _validate_lut(lut: str | None) -> Optional[str]:
    if not lut:
        return None
    if lut not in LUT_WHITELIST:
        raise EdlValidationError(
            f"lut {lut!r} not in whitelist ({sorted(LUT_WHITELIST)})"
        )
    return lut


def _validate_aspect(aspect: str) -> str:
    aspect = (aspect or "").strip() or "9:16"
    if aspect not in ("9:16", "16:9", "1:1", "4:5", "2.39:1"):
        raise EdlValidationError(
            f"aspect {aspect!r} not supported (9:16/16:9/1:1/4:5/2.39:1)"
        )
    return aspect


def _validate_music(m: dict | None) -> Optional[MusicConfig]:
    if not m:
        return None
    source = (m.get("source") or "").strip()
    if source and source not in ("archive_pd", "youtube_audio_library", "path"):
        raise EdlValidationError(
            f"music.source {source!r} not allowed "
            "(archive_pd / youtube_audio_library / path)"
        )
    path = (m.get("path") or "").strip()
    if path:
        # Defensive: if source=path, the executor still re-validates that
        # `path` lives under <channel>/music/ or pipeline/editing/music/.
        # Anything else gets rejected at execute time.
        if any(c in path for c in "`;$|><\n"):
            raise EdlValidationError(f"music.path {path!r} contains forbidden char")
    return MusicConfig(
        source=source,
        path=path,
        fade_in_s=float(m.get("fade_in_s") or 1.0),
        fade_out_s=float(m.get("fade_out_s") or 2.0),
        duck_under_speech=bool(m.get("duck_under_speech", True)),
    )


def build_edl_from_planner_json(payload: dict) -> Edl:
    """Validate a planner-emitted JSON dict and return a typed :class:`Edl`.

    Raises :class:`EdlValidationError` on any whitelist violation. The
    executor calls this before compiling so an LLM hallucination
    (e.g. ``filters: [{"type": "drawtext", "args": "..."}]``) gets
    caught before ffmpeg runs.
    """
    if not isinstance(payload, dict):
        raise EdlValidationError("EDL must be a JSON object at top level")

    version = int(payload.get("version") or EDL_VERSION)
    if version != EDL_VERSION:
        raise EdlValidationError(
            f"unsupported EDL version {version} (expected {EDL_VERSION}); "
            f"see docs/editing_agent.md migration notes"
        )

    mode_raw = (payload.get("mode") or EditMode.POLISH.value).strip()
    valid_modes = {m.value for m in EditMode}
    if mode_raw not in valid_modes:
        raise EdlValidationError(
            f"mode {mode_raw!r} not in {sorted(valid_modes)}"
        )

    audio_raw = payload.get("audio") or {}
    audio = AudioConfig(
        duck_speech_db=float(audio_raw.get("duck_speech_db") or -3.0),
        loudnorm_lufs=float(audio_raw.get("loudnorm_lufs") or -14.0),
        music=_validate_music(audio_raw.get("music") or payload.get("music")),
    )

    letterbox_raw = payload.get("letterbox") or {}
    letterbox = LetterboxConfig(
        enabled=bool(letterbox_raw.get("enabled", False)),
        ratio=float(letterbox_raw.get("ratio") or 2.39),
    )

    captions_raw = payload.get("captions") or {}
    captions = CaptionsConfig(
        preserve_burned=bool(captions_raw.get("preserve_burned", True)),
        add_overlay=bool(captions_raw.get("add_overlay", False)),
        srt_path=captions_raw.get("srt_path"),
    )

    shots: list[Shot] = []
    for i, s in enumerate(payload.get("shots") or []):
        if not isinstance(s, dict):
            raise EdlValidationError(f"shots[{i}] must be a JSON object")
        filters = [
            _validate_filter(f, f"shots[{i}].filters[{j}]")
            for j, f in enumerate(s.get("filters") or [])
        ]
        shots.append(
            Shot(
                idx=int(s.get("idx", i)),
                input_ref=str(s.get("input_ref") or ""),
                in_s=float(s.get("in_s") or 0.0),
                out_s=float(s.get("out_s") or 0.0),
                filters=filters,
                transition_in=_validate_transition(
                    s.get("transition_in"), f"shots[{i}].transition_in",
                ),
                transition_out=_validate_transition(
                    s.get("transition_out"), f"shots[{i}].transition_out",
                ),
                notes=str(s.get("notes") or ""),
            )
        )
    if not shots:
        raise EdlValidationError("EDL must contain at least one shot")

    return Edl(
        version=version,
        mode=mode_raw,
        channel=payload.get("channel"),
        aspect=_validate_aspect(payload.get("aspect") or "9:16"),
        fps=int(payload.get("fps") or 30),
        target_duration_s=float(payload.get("target_duration_s") or 60.0),
        lut=_validate_lut(payload.get("lut")),
        letterbox=letterbox,
        audio=audio,
        shots=shots,
        captions=captions,
        director_notes=str(payload.get("director_notes") or ""),
    )


def lut_path_for(name: str) -> Path:
    """Resolve a whitelisted LUT name to its on-disk path."""
    if name not in LUT_WHITELIST:
        raise EdlValidationError(f"lut {name!r} not in whitelist")
    return Path(__file__).parent / "luts" / name
