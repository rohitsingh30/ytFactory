"""Single source of truth for what every render IS — the RenderSpec.

Every render in ytFactory is fully described by one RenderSpec. The
spec is built once, at the top of every render, by merging four
sources in precedence order:

    1. Base channel YAML defaults                  (lowest priority)
    2. Optional variant/niche YAML overlay
    3. Form overrides (proposal.channel_overrides)
    4. Inferred defaults (length_s → kind, etc)    (highest priority for
                                                    fields the others
                                                    didn't set)

The downstream renderer (``pipeline.render.video``) then dispatches
stages by ``spec.kind`` and ``spec.visual_mode``. No more 4 separate
mega-modules each hardcoding their own aspect ratio + length range.

Why this exists
---------------

Pre-2026-05-12 a render request like
``(channel=mystoriesanimated, length_s=1800)`` silently produced a
9:16 30-min Short because the cloud worker always invoked
``pipeline.render.shorts`` regardless of length, and shorts.py is
hardwired to 9:16 / ≤120s. There was no code path that COULD have
honored the user's input — the structural answer was 7,000 lines of
near-duplicate renderer code (shorts.py 3128, long_form.py 1968,
sports_doc.py 1052, footage_only.py 846) each owning a different
combination of (kind, aspect, visual_mode). The user's bug was a
symptom of that fragmentation.

The spec gives the worker a uniform target to interpret + dispatch
from, and gives users the freedom to combine inputs the original
4-renderer split wouldn't allow.

Discriminated unions, not flat optional bag
-------------------------------------------

The rubber-duck critique was sharp on this: a flat dict of optional
knobs doesn't actually unify the renderers, because their internal
artifact contracts ARE different. A sports-doc with chaptered
narration + footage_plan + overlay timeline cannot fit through the
Shorts beat→image→compose pipeline. So:

- ``kind`` is a typed discriminator (short / long_form / sports_doc /
  footage_only). It picks WHICH orchestration runs.
- ``visual_mode`` is a typed discriminator (ai_beat_slideshow /
  longform_panels / archival_shotlist / sports_overlay_timeline / …).
  It picks WHICH visualize stage runs.

Other fields (aspect_ratio, output_resolution, voice_id, music_bed,
captions_density, …) are scalars consumed by the matching orchestrator
+ visualize stage. Inferred defaults pick sane values when the form +
YAML chain leaves them unset, so any (channel, niche, form-input)
combo produces a fully-typed spec without raising.

Phantom niches are first-class
------------------------------

When the form sends ``format=<niche>`` and no
``pipeline/variants/<channel>/<niche>.yaml`` exists, the spec builder
DOES NOT silently fall back to the channel YAML. Instead it:

- proceeds from channel defaults + form overrides, AND
- appends a one-line note to ``spec.notes`` that the dashboard +
  Firestore job doc surface to the user:
  ``experimental niche '<niche>': no overlay YAML; rendering from
  channel defaults + form overrides``.

So users can experiment with novel niches via the form (or the
``POST /api/channels/<ch>/niches/draft`` AI-draft endpoint) and the
render still proceeds — but they SEE that they're on an experimental
path, not silently routed to the channel default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discriminators
# ---------------------------------------------------------------------------


class RenderKind(str, Enum):
    """Top-level orchestration choice. Determines which stage chain runs.

    - ``short``       — 9:16, ≤120 s, single-pass TTS, ASR-driven beats,
                        ai/motion/hybrid visualize. Today's
                        ``pipeline.render.shorts`` codepath.
    - ``long_form``   — 16:9, 5-120 min, chunked TTS, sectioned narration,
                        panels OR archival_shotlist visualize. Today's
                        ``pipeline.render.long_form`` codepath.
    - ``sports_doc``  — 16:9, 5-30 min, chaptered narration + separate
                        footage_plan, overlay-timeline visualize. Today's
                        ``pipeline.render.sports_doc`` codepath.
    - ``footage_only``— any aspect, any length, shotlist windows over
                        external clips, no AI image gen. Today's
                        ``pipeline.render.footage_only`` codepath.
    """
    SHORT = "short"
    LONG_FORM = "long_form"
    SPORTS_DOC = "sports_doc"
    FOOTAGE_ONLY = "footage_only"


class VisualMode(str, Enum):
    """How the visualize stage produces beat-aligned visuals.

    Each is a different artifact contract — they are NOT interchangeable
    parameter values on a single visualize function. The visualize
    stage dispatches on this enum.

    - ``ai_beat_slideshow``        — one Flux/SD image per ASR beat,
                                     cross-fades. Default for Shorts on
                                     animated channels.
    - ``motion_clips``             — one motion_provider clip per beat
                                     (AnimateDiff/SVD/etc). Default for
                                     Shorts when channel YAML sets a
                                     ``motion_provider``.
    - ``hybrid_beat_footage``      — beats matched to archival footage
                                     where available, AI fallback otherwise.
                                     ``visual_source=both`` form input.
    - ``footage_windows``          — ASR-free, shotlist-driven windows
                                     of external mp4 clips. ``visual_source=
                                     footage`` form input + shotlist.
    - ``longform_panels``          — long-form sectioned narration → 1
                                     Flux panel per ~30-60 s with
                                     Ken Burns motion. Default long-form
                                     for animated channels.
    - ``archival_shotlist``        — long-form sectioned narration matched
                                     to a curated archive.org/Wikimedia
                                     shotlist. Default long-form for
                                     historyrecapped / sportsrecapped.
    - ``sports_overlay_timeline``  — chaptered narration + separate
                                     footage_plan with talking-head /
                                     match-footage / chapter-card lanes.
                                     sports_doc kind only.
    """
    AI_BEAT_SLIDESHOW = "ai_beat_slideshow"
    MOTION_CLIPS = "motion_clips"
    HYBRID_BEAT_FOOTAGE = "hybrid_beat_footage"
    FOOTAGE_WINDOWS = "footage_windows"
    LONGFORM_PANELS = "longform_panels"
    ARCHIVAL_SHOTLIST = "archival_shotlist"
    SPORTS_OVERLAY_TIMELINE = "sports_overlay_timeline"


class AudioMode(str, Enum):
    """How the narration/audio track is produced.

    - ``voice``  — TTS via the channel's tts_provider + voice (default).
    - ``song``   — Suno-API sung song; tts_provider unused.
    """
    VOICE = "voice"
    SONG = "song"


class CaptionsDensity(str, Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    DENSE = "dense"


class MusicPolicy(str, Enum):
    """Selects the music plugin (``pipeline/render/music/<policy>.py``).

    Added 2026-05-14 as part of the 4-renderer-to-2-engine
    consolidation. Pre-fix, music behaviour was implicit per renderer:
    ``shorts.py`` always ducked a single bed, ``long_form.py`` always
    looped a single ambient track, ``sports_doc.py`` always cycled
    section moods, ``footage_only.py`` had no music at all. Now a
    typed enum with one plugin per value.

    - ``ducked_loop``  — short-style: loop ``music_bed.mp3`` under
      the narration with sidechain ducking. Default for shorts.
    - ``single_bed``   — long-form: loop one ambient track for the
      full duration. Default for long-form.
    - ``section_mood`` — per-``Section`` mood crossfade. Picks a
      different bed per ``sections[]`` entry from
      ``<channel>/music/<mood>/``. Default for overlay_timeline.
    - ``none``         — explicitly silent. Picks the no-op plugin
      that emits a silent wav of narration_duration_s.
    """
    DUCKED_LOOP = "ducked_loop"
    SINGLE_BED = "single_bed"
    SECTION_MOOD = "section_mood"
    NONE = "none"


class WatermarkPosition(str, Enum):
    """Where the channel watermark sits on each frame.

    Added 2026-05-14. Pre-fix ``render_watermark_png`` produced a PNG
    but each renderer's compose stage hardcoded the ffmpeg overlay
    position (``W-w-32:32`` for top-right in long_form,
    ``W-w-32:H-h-32`` for bottom-right in shorts, etc).
    """
    TOP_RIGHT = "tr"
    TOP_LEFT = "tl"
    BOTTOM_RIGHT = "br"
    BOTTOM_LEFT = "bl"
    NONE = "none"


class CaptionsLayout(str, Enum):
    """How captions appear on screen.

    Three options the wizard exposes (2026-05-14):

    - ``center_word_by_word`` — TikTok-style. ONE word at a time, large,
      vertically centred (slight bottom-bias for chin-tap clearance).
      The default for shorts. Drives ``caption_mode="word"`` in
      ``pipeline/compose.py`` and selects the word-PNG path in
      ``pipeline/render/long_form.py``.

    - ``bottom_one_line`` — one short line at the bottom, MarginV=80.
      Sentence-level cues, hard-truncated to a single line if too long.
      Drives ``caption_mode="beat"`` (canvas height = 1 line) for shorts
      and ``build_captions_ass(max_lines=1)`` for long-form.

    - ``bottom_two_line`` — bottom strip, wraps to at most 2 lines.
      Drives ``caption_mode="beat"`` (canvas height = 2 lines) for
      shorts and ``build_captions_ass(max_lines=2)`` (the historical
      long-form default) for long-form.
    """
    CENTER_WORD_BY_WORD = "center_word_by_word"
    BOTTOM_ONE_LINE = "bottom_one_line"
    BOTTOM_TWO_LINE = "bottom_two_line"


# ---------------------------------------------------------------------------
# Spec dataclass
# ---------------------------------------------------------------------------


def _default_resolution_for(aspect: str) -> tuple[int, int]:
    """Standard YouTube resolutions for each supported aspect.

    Channel YAML or form overrides may set output_resolution explicitly,
    in which case THAT wins. This is the inferred default when neither
    source set it.
    """
    return {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1":  (1080, 1080),
        "4:5":  (1080, 1350),
    }.get(aspect, (1080, 1920))


# ---------------------------------------------------------------------------
# Nested style/config dataclasses (added 2026-05-14)
#
# Each one bundles a family of related knobs that USED to be hardcoded
# inside one of the four renderers (see knob_inventory.txt in the
# session workspace for the full audit). RenderSpec now carries one of
# each; engines + plugins read from them; the form-input registry
# exposes every leaf as a CustomizationField for wizard / API surfacing.
#
# Defaults are conservative — they reproduce the historical behavior of
# ``long_form.py`` (the most-tested renderer) so a channel that doesn't
# override anything still renders the same way it did pre-2026-05-14.
# ---------------------------------------------------------------------------


@dataclass
class TtsConfig:
    """TTS-driven audio knobs.

    Replaces the per-renderer ``_TONE_OVERRIDES`` table + scattered
    ``tts_speed`` / ``tts_post_atempo`` literals. ``tone_overrides`` is
    the same shape ``sports_doc._TONE_OVERRIDES`` had:
    ``{tone_name: {"speed": float, "atempo": float}}``.
    """
    speed_default: float = 0.98
    post_atempo_default: float = 1.0
    chunk_target_chars: int = 380
    chunk_join_silence_s: float = 0.4
    tone_overrides: dict[str, dict[str, float]] = field(default_factory=lambda: {
        "tifo-academic":   {"speed": 0.98, "atempo": 1.00},
        "intense-podcast": {"speed": 1.02, "atempo": 0.97},
        "playful-spicy":   {"speed": 1.00, "atempo": 1.00},
        "serious-doc":     {"speed": 0.95, "atempo": 0.92},
    })


@dataclass
class CaptionStyleConfig:
    """Caption rendering knobs — colors, fonts, ASS canvas size, etc.

    Defaults match ``long_form.py``'s yellow-italic Sleepy Time History
    style. Channels that want bold-white captions (sports_doc) override
    ``text_rgba`` + ``italic`` in their ``defaults: { long: { ... } }``
    block.
    """
    font_size: int = 38
    font_size_minimal: int = 130
    font_size_standard: int = 110
    font_size_dense: int = 90
    text_rgba: tuple[int, int, int, int] = (255, 217, 61, 255)  # warm yellow
    italic: bool = True
    play_res_x: int = 1920
    play_res_y: int = 1080
    black_intro_buffer_s: float = 0.30


@dataclass
class MusicPolicyConfig:
    """Music bed knobs — default file, mix levels, duck level."""
    default_bed: str = "ambient_low"
    """The bed file (without ``.mp3`` extension) under
    ``<channel>/music/``. Empty string = no bed."""
    music_bed_db: float = -28.0
    """LUFS / dB of the bed under narration."""
    filler_level_db: float = -22.0
    """LUFS / dB of the bed under FILLER (no-narration) windows."""
    mix_default: float = 0.35
    """Linear mix factor for the bed when ducked (0.0 = silent, 1.0 = full)."""


@dataclass
class VideoGradeConfig:
    """Color-grade knobs — sigma for blurred letterbox, brightness shift."""
    blur_sigma: float = 22.0
    brightness: float = 0.0


@dataclass
class WatermarkConfig:
    """Watermark PNG knobs — font size, text color/alpha, margin from edge."""
    font_size: int = 28
    text_rgba: tuple[int, int, int, int] = (255, 255, 255, 140)
    margin: int = 32
    position: WatermarkPosition = WatermarkPosition.TOP_RIGHT


@dataclass
class ChapterCardConfig:
    """Chapter card knobs — colors, font sizes, hold duration.

    Defaults match ``sports_doc.py``'s deep-teal slab + orange chapter
    number aesthetic. Channels that want a different palette set
    ``bg_rgba`` / ``number_color`` in their long block.
    """
    bg_rgba: tuple[int, int, int, int] = (10, 22, 38, 240)
    bg_color: str = "0x0a1626"
    number_color: tuple[int, int, int, int] = (255, 168, 0, 255)
    number_font_size: int = 48
    title_font_size: int = 72
    duration_s: float = 3.0


@dataclass
class LowerThirdConfig:
    """Lower-third (speaker name + handle) knobs."""
    bg_rgba: tuple[int, int, int, int] = (10, 22, 38, 215)
    text_rgba: tuple[int, int, int, int] = (255, 255, 255, 255)
    accent_rgba: tuple[int, int, int, int] = (255, 168, 0, 255)
    font_size: int = 36
    handle_font_size: int = 24
    hold_min_s: float = 2.5


@dataclass
class FillerConfig:
    """No-narration filler video knobs (used by overlay_timeline mode)."""
    bg_color: str = "0x0a1626"


@dataclass
class NetworkConfig:
    """Network-bound knobs (e.g. ffprobe timeout)."""
    timeout_s: int = 30


@dataclass
class RenderSpec:
    """Fully-typed description of one render. Built by :func:`build_spec`."""

    # -- Identity / orchestration ------------------------------------------
    channel: str
    """Channel slug — e.g. ``mystoriesanimated``."""

    niche: str | None
    """Niche/variant slug if the form picked one — e.g. ``aita_animated``.
    None = render directly from base channel YAML."""

    kind: RenderKind
    """Top-level orchestration choice."""

    visual_mode: VisualMode
    """Per-beat visualize strategy."""

    # -- Output shape ------------------------------------------------------
    aspect_ratio: str
    """e.g. ``9:16``, ``16:9``, ``1:1``."""

    output_resolution: tuple[int, int]
    """(width, height) in px. Inferred from aspect_ratio if YAML/form
    didn't set it explicitly."""

    output_fps: int = 30

    # -- Duration ----------------------------------------------------------
    duration_target_s: int | None = None
    """Target final duration in seconds. None = renderer picks per
    channel YAML defaults."""

    duration_max_s: int | None = None
    """Hard cap on final duration. Overrides any narration over-run."""

    # -- Audio -------------------------------------------------------------
    audio_mode: AudioMode = AudioMode.VOICE
    voice_provider: str | None = None
    """e.g. ``cloudrun_chatterbox``, ``cloudrun_indicparler``,
    ``f5_tts``, ``kokoro``. None = use channel YAML default."""

    voice_id: str | None = None
    """Voice ref WAV path OR bare voice id. None = channel default."""

    music_bed: str | None = None
    """Music bed key (without .mp3 extension) or 'off'. None = channel
    default. ``off`` explicitly disables the bed."""

    song_style: str | None = None
    song_vocal_gender: str | None = None  # 'f' | 'm'
    song_model: str | None = None  # Suno model id

    # -- Captions / on-screen text ----------------------------------------
    captions_density: CaptionsDensity = CaptionsDensity.STANDARD
    captions_enabled: bool = True
    captions_layout: CaptionsLayout = CaptionsLayout.CENTER_WORD_BY_WORD
    """Wizard-selected layout (2026-05-14). Replaces the older
    captions_density field as the user-facing knob; density stays for
    back-compat but is now derivable from layout (1-line=minimal,
    2-line=standard, word=standard). Renderer dispatches on
    captions_layout — see ``CaptionsLayout`` docstring for the
    per-mode rendering path."""

    # -- Music / overlays / chapter cards / lower-thirds (added 2026-05-14)
    # These are the user-selectable knobs that USED to be hardcoded per
    # renderer. ``music_policy`` picks the music plugin; the three
    # bools toggle individual overlay producers. Same shape as every
    # other typed enum/bool on the spec — form descriptors expose them
    # all in the wizard.
    music_policy: MusicPolicy = MusicPolicy.DUCKED_LOOP
    lower_thirds: bool = False
    chapter_cards: bool = False
    overlay_timeline: bool = False
    """When True, engine activates the ``anchored_footage`` overlay
    producer (foreground match-clips + b-roll filler — the
    sports_doc shape). The visual_mode for this case is
    ``footage_filler`` so the background is b-roll cycle and the
    overlays carry the anchored cuts."""

    # -- Critic loop ------------------------------------------------------
    critic_loop: bool | None = None
    """Per user direction (2026-05-14): NO defaults-by-kind. ``None``
    means "respect whatever the channel's ``defaults:`` block says";
    explicit ``True`` / ``False`` from the form overrides everything.
    The engine treats ``None`` as ``False`` if no channel default
    applies, so renders never silently spend critic budget."""

    # -- Tone (sports_doc-style narration cadence override) --------------
    tone: str | None = None
    """When set, the AudioSynthesizer applies the ``tone_overrides[tone]``
    speed/atempo values from ``TtsConfig``. ``None`` = use
    ``speed_default`` / ``post_atempo_default``. Values today:
    ``tifo-academic`` / ``intense-podcast`` / ``playful-spicy`` /
    ``serious-doc`` — channel YAMLs can register additional tones."""

    # -- Nested typed config (defaults reproduce historical behavior) ----
    tts: TtsConfig = field(default_factory=TtsConfig)
    caption_style: CaptionStyleConfig = field(default_factory=CaptionStyleConfig)
    music: MusicPolicyConfig = field(default_factory=MusicPolicyConfig)
    video_grade: VideoGradeConfig = field(default_factory=VideoGradeConfig)
    watermark: WatermarkConfig = field(default_factory=WatermarkConfig)
    chapter_card: ChapterCardConfig = field(default_factory=ChapterCardConfig)
    lower_third: LowerThirdConfig = field(default_factory=LowerThirdConfig)
    filler: FillerConfig = field(default_factory=FillerConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)

    # -- Visual presentation ----------------------------------------------
    narrator_visual_mode: str = "on_screen"
    """``on_screen`` (default) puts the narrator character in image
    prompts. ``voice_only`` keeps the narrator off-camera (used by
    sportsrecapped + cosmosdecoded). Free-form so channels can
    experiment without an enum bump."""

    visual_source: str | None = None
    """Form override: ``ai`` / ``footage`` / ``both``. None = use
    channel default. Selects which visual_mode the spec builder
    derived."""

    # -- Upload (passes through to upload stage) ---------------------------
    visibility: str | None = None  # 'public' | 'unlisted' | 'private' | None
    schedule_at: str | None = None  # ISO-8601 UTC

    # -- Passthrough + observability ---------------------------------------
    extra: dict[str, Any] = field(default_factory=dict)
    """Form-override keys with no typed home. Downstream stages may
    consume them. Anything here is by definition NOT silently dropped
    — it's reachable from the spec."""

    notes: list[str] = field(default_factory=list)
    """Human-readable notes about how the spec was resolved. The cloud
    worker writes these into the Firestore job doc so the dashboard +
    operators can see e.g. ``experimental niche 'unresolved_mysteries':
    no overlay YAML; rendering from channel defaults + form overrides``.
    NEVER raise from the builder; surface ambiguity here instead."""

    # -- Provenance -------------------------------------------------------
    source_channel_yaml: str | None = None
    source_variant_yaml: str | None = None
    """Absolute paths the spec was built from. Empty = no YAML on disk
    (phantom niche). Useful for ops + audit."""

    def to_dict(self) -> dict[str, Any]:
        """Firestore-friendly representation. Enums → str values, tuple
        → list. The cloud worker writes this into ``job.render_spec`` so
        the dashboard can show "the system interpreted your inputs as
        kind=short, visual_mode=ai_beat_slideshow, aspect=9:16".

        Post-2026-05-14 also flattens the nested config dataclasses
        (TtsConfig, CaptionStyleConfig, MusicPolicyConfig, …) into their
        primitive shapes so Firestore + dashboard JSON deserialize them
        without a custom decoder. Tuples become lists; enums become
        bare str values.
        """
        d = asdict(self)
        # Top-level Enums → bare str values (asdict doesn't unwrap Enum).
        for k, v in list(d.items()):
            if isinstance(v, Enum):
                d[k] = v.value
        d["kind"] = self.kind.value
        d["visual_mode"] = self.visual_mode.value
        d["audio_mode"] = self.audio_mode.value
        d["captions_density"] = self.captions_density.value
        d["captions_layout"] = self.captions_layout.value
        d["music_policy"] = self.music_policy.value
        d["output_resolution"] = list(self.output_resolution)
        # Nested config Enums + tuples — recurse one level (none of the
        # nested configs themselves contain further nested configs).
        d["watermark"] = {
            **d["watermark"],
            "position": self.watermark.position.value,
            "text_rgba": list(self.watermark.text_rgba),
        }
        d["caption_style"] = {
            **d["caption_style"],
            "text_rgba": list(self.caption_style.text_rgba),
        }
        d["chapter_card"] = {
            **d["chapter_card"],
            "bg_rgba": list(self.chapter_card.bg_rgba),
            "number_color": list(self.chapter_card.number_color),
        }
        d["lower_third"] = {
            **d["lower_third"],
            "bg_rgba": list(self.lower_third.bg_rgba),
            "text_rgba": list(self.lower_third.text_rgba),
            "accent_rgba": list(self.lower_third.accent_rgba),
        }
        return d


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


# Maps a channel slug to its character — used to infer visual_mode when
# the form doesn't pick one. NOT a hardcoded capability list — only an
# inferred-default hint. Channels can override anything via YAML.
_CHANNEL_DEFAULT_VISUAL_MODE: dict[str, dict[RenderKind, VisualMode]] = {
    "mystoriesanimated": {
        RenderKind.SHORT: VisualMode.AI_BEAT_SLIDESHOW,
        RenderKind.LONG_FORM: VisualMode.LONGFORM_PANELS,
    },
    "hindutavaanimated": {
        RenderKind.SHORT: VisualMode.AI_BEAT_SLIDESHOW,
        RenderKind.LONG_FORM: VisualMode.LONGFORM_PANELS,
    },
    "rhymetimejunction": {
        RenderKind.SHORT: VisualMode.AI_BEAT_SLIDESHOW,
        RenderKind.LONG_FORM: VisualMode.LONGFORM_PANELS,
    },
    "sportsrecapped": {
        RenderKind.SHORT: VisualMode.AI_BEAT_SLIDESHOW,
        RenderKind.LONG_FORM: VisualMode.ARCHIVAL_SHOTLIST,
        RenderKind.SPORTS_DOC: VisualMode.SPORTS_OVERLAY_TIMELINE,
    },
    "historyrecapped": {
        RenderKind.SHORT: VisualMode.HYBRID_BEAT_FOOTAGE,
        RenderKind.LONG_FORM: VisualMode.ARCHIVAL_SHOTLIST,
    },
    "cosmosdecoded": {
        RenderKind.SHORT: VisualMode.HYBRID_BEAT_FOOTAGE,
        RenderKind.LONG_FORM: VisualMode.ARCHIVAL_SHOTLIST,
    },
}

_LONG_FORM_THRESHOLD_S = 120
"""Form sends length_s in seconds. ≤120 → short, >120 → long_form
(unless an explicit override says otherwise)."""


def _infer_kind(
    *,
    explicit_kind: str | None,
    length_s: int | None,
    length_kind: str | None,
) -> RenderKind:
    """Pick RenderKind from explicit form fields, else infer from length.

    Precedence:
    1. explicit_kind form override (proposal.channel_overrides.kind)
    2. length_kind form field (``short`` / ``long``)
    3. length_s threshold (>120 → long_form)
    4. Default short.
    """
    if explicit_kind:
        try:
            return RenderKind(explicit_kind)
        except ValueError:
            pass  # fall through to length-based inference
    if length_kind == "long":
        return RenderKind.LONG_FORM
    if length_kind == "short":
        return RenderKind.SHORT
    if isinstance(length_s, int) and length_s > _LONG_FORM_THRESHOLD_S:
        return RenderKind.LONG_FORM
    return RenderKind.SHORT


def _infer_aspect(kind: RenderKind, explicit: str | None) -> str:
    """Aspect ratio: explicit form/YAML override wins; else default per kind."""
    if explicit and explicit in {"9:16", "16:9", "1:1", "4:5"}:
        return explicit
    if kind in {RenderKind.LONG_FORM, RenderKind.SPORTS_DOC}:
        return "16:9"
    return "9:16"


def _infer_visual_mode(
    *,
    channel: str,
    kind: RenderKind,
    explicit: str | None,
    visual_source: str | None,
    cfg: dict,
) -> VisualMode:
    """Pick visual_mode from explicit override → cfg hint → channel default."""
    # 1. Explicit form override wins.
    if explicit:
        try:
            return VisualMode(explicit)
        except ValueError:
            pass

    # 2. Form's ``visual_source`` translates to a mode hint.
    if visual_source == "footage":
        return VisualMode.FOOTAGE_WINDOWS
    if visual_source == "both":
        return VisualMode.HYBRID_BEAT_FOOTAGE

    # 3. Channel YAML hint: motion_provider set → motion_clips.
    if cfg.get("motion_provider"):
        return VisualMode.MOTION_CLIPS

    # 4. Per-channel default for the picked kind.
    by_kind = _CHANNEL_DEFAULT_VISUAL_MODE.get(channel, {})
    if kind in by_kind:
        return by_kind[kind]

    # 5. Last resort: short → AI slideshow; long_form → panels.
    if kind == RenderKind.LONG_FORM:
        return VisualMode.LONGFORM_PANELS
    if kind == RenderKind.SPORTS_DOC:
        return VisualMode.SPORTS_OVERLAY_TIMELINE
    return VisualMode.AI_BEAT_SLIDESHOW


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_audio_mode(v: Any) -> AudioMode:
    if v == "song":
        return AudioMode.SONG
    return AudioMode.VOICE


def _coerce_captions_density(v: Any) -> CaptionsDensity:
    if v in ("minimal", "dense"):
        return CaptionsDensity(v)
    return CaptionsDensity.STANDARD


def _coerce_captions_layout(v: Any) -> CaptionsLayout:
    """Coerce a string to CaptionsLayout. Unknown / missing values fall
    back to the channel default (center_word_by_word)."""
    if isinstance(v, CaptionsLayout):
        return v  # coverage: enum fast-path defensive guard for already-typed input value
    if isinstance(v, str):
        try:
            return CaptionsLayout(v)
        except ValueError:
            pass
    return CaptionsLayout.CENTER_WORD_BY_WORD


def _coerce_music_policy(v: Any) -> MusicPolicy:
    """Coerce to MusicPolicy. Unknown / missing → DUCKED_LOOP (the
    historical short default; long engine overrides via channel YAML).
    """
    if isinstance(v, MusicPolicy):
        return v
    if isinstance(v, str):
        try:
            return MusicPolicy(v)
        except ValueError:
            pass
    return MusicPolicy.DUCKED_LOOP


def _coerce_optional_bool(v: Any) -> bool | None:
    """Tri-state coerce for ``critic_loop``-style fields. Empty string
    or None → None (let channel default apply); ``"true"``/``True``/1
    → True; everything else → False.
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _coerce_bool(v: Any, default: bool = False) -> bool:
    """Two-state coerce. Same parsing as
    :func:`_coerce_optional_bool` but returns ``default`` for missing
    values."""
    out = _coerce_optional_bool(v)
    return default if out is None else out


# Form-override keys that have a typed home on the spec. Anything NOT
# in this set gets dumped into spec.extra so downstream stages can
# still find it — no silent drops.
_TYPED_OVERRIDE_KEYS: set[str] = {
    "kind",
    "length_kind", "length_s",
    "aspect_ratio", "output_resolution", "output_fps",
    "audio_mode",
    "voice", "voice_provider",
    "music_bed",
    "song_style", "song_vocal_gender", "song_model",
    "captions_density", "captions_enabled", "captions_layout",
    "visual_mode", "visual_source",
    "narrator_visual_mode",
    "visibility", "schedule_at",
    "duration_target_s", "duration_max_s",
    # Added 2026-05-14 — new top-level user-selectable knobs.
    "music_policy",
    "lower_thirds", "chapter_cards", "overlay_timeline",
    "critic_loop",
    "tone",
}


def build_spec(
    proposal: dict[str, Any],
    *,
    channel_yaml_path: Path | None,
    variant_yaml_path: Path | None,
) -> RenderSpec:
    """Build a fully-typed RenderSpec from proposal + YAML chain.

    Source precedence (highest priority last):
    1. Inferred defaults
    2. Base channel YAML
    3. Variant/niche YAML overlay
    4. Form overrides (proposal.channel_overrides)

    Phantom niches: when ``variant_yaml_path`` is set but the file
    doesn't exist, we DON'T silently treat it as if no variant was
    requested — we add a note to ``spec.notes`` so the dashboard
    surfaces "experimental niche, no overlay YAML" to the user.

    NEVER raises. Ambiguity (missing YAML, unknown enum value, …) goes
    into ``spec.notes``. The render either proceeds with sane defaults
    or surfaces a friendly error downstream when it actually needs the
    missing thing.
    """
    notes: list[str] = []

    channel = (proposal.get("channel") or "").strip()
    niche = (proposal.get("format") or "").strip() or None

    # Load base channel YAML.
    base_cfg: dict = {}
    if channel_yaml_path and Path(channel_yaml_path).exists():
        try:
            base_cfg = yaml.safe_load(Path(channel_yaml_path).read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            notes.append(f"failed to parse channel YAML {channel_yaml_path}: {exc}")
    elif channel_yaml_path:
        notes.append(
            f"channel YAML not found at {channel_yaml_path}; "
            f"rendering from form overrides + inferred defaults only"
        )

    # Layer variant overlay if provided AND present.
    variant_cfg: dict = {}
    if variant_yaml_path:
        if Path(variant_yaml_path).exists():
            try:
                variant_cfg = yaml.safe_load(Path(variant_yaml_path).read_text()) or {}
            except Exception as exc:  # noqa: BLE001
                notes.append(
                    f"failed to parse variant YAML {variant_yaml_path}: {exc}"
                )
        else:
            # PHANTOM NICHE — the form picked a niche the repo doesn't have a
            # backing YAML for. NOT a silent fallback: surface to the user.
            notes.append(
                f"experimental niche '{niche}': no overlay YAML at "
                f"{variant_yaml_path}; rendering from channel defaults + "
                f"form overrides"
            )

    # Merged YAML cfg — variant overrides base.
    cfg: dict = dict(base_cfg)
    cfg.update(variant_cfg)

    # Form overrides — flat dict on the proposal.
    overrides = dict(proposal.get("channel_overrides") or {})

    # Apply registered handlers (notably ``apply_voice_with_cloud_carveout``)
    # so form overrides MUTATE cfg through the same code path the
    # long-form `long_form_overlay_from_spec` uses. Pre-2026-05-15 this
    # apply step was wired ONLY into the long-form overlay path; the
    # short-form spec build silently bypassed it. Surfaced by job
    # 12275f0f (HindutavaAnimated Krishna leela) where the wizard sent
    # ``voice="ref"`` (the bare-stem of the channel YAML's old bench
    # path) — the apply_handler would have detected the unresolvable
    # bare id on a cloud channel and dropped it, but never ran. So
    # ``overrides["voice"]="ref"`` reached ``synthesize`` and IndicF5
    # raised ``requires ref_audio_text``.
    #
    # Wiring: apply_overrides runs ON `cfg` (not on overrides), so
    # invalid values get swallowed into ``cfg["_dropped_inputs"]`` and
    # the channel YAML's ``tts_voice`` survives. The voice_id
    # resolution below now reads from cfg first when overrides looks
    # invalid (see _resolve_short_form_voice_id).
    try:
        from pipeline.render.input_registry import apply_overrides as _apply_overrides  # noqa: PLC0415
        _apply_overrides(cfg, overrides, channel_key=channel)
    except Exception as exc:  # noqa: BLE001  # coverage: defensive — apply_overrides itself never raises (best-effort), this branch only fires on import-time error which can't happen in unit tests
        notes.append(
            f"input_registry.apply_overrides failed: {exc} — "
            f"proceeding with raw overrides"
        )

    # ----- length / kind inference ----------------------------------------
    length_s = _coerce_int(proposal.get("length_s")) \
        or _coerce_int(overrides.get("length_s"))
    length_kind = (overrides.get("length_kind") or "").strip() or None
    explicit_kind = (overrides.get("kind") or "").strip() or None

    kind = _infer_kind(
        explicit_kind=explicit_kind,
        length_s=length_s,
        length_kind=length_kind,
    )

    # ----- aspect + resolution --------------------------------------------
    # Pick the YAML block matching the resolved kind. For long_form/sports_doc,
    # only the kind-specific block contributes — the channel-level
    # ``output_resolution`` is the Shorts default and would otherwise pull
    # a long-form render back to 9:16.
    kind_cfg_block: dict = {}
    if kind == RenderKind.LONG_FORM:
        kind_cfg_block = cfg.get("long_form") or {}
    elif kind == RenderKind.SPORTS_DOC:
        kind_cfg_block = cfg.get("sports_doc") or {}
    elif kind == RenderKind.FOOTAGE_ONLY:
        kind_cfg_block = cfg.get("footage_only") or {}
    # SHORT uses the channel-level keys (cfg directly).

    explicit_aspect = (
        overrides.get("aspect_ratio")
        or kind_cfg_block.get("aspect_ratio")
        or _aspect_from_resolution(kind_cfg_block.get("output_resolution"))
        # Channel-level fallbacks ONLY for SHORT — long-form should never
        # inherit the Shorts aspect.
        or (cfg.get("aspect_ratio") if kind == RenderKind.SHORT else None)
        or (_aspect_from_resolution(cfg.get("output_resolution"))
            if kind == RenderKind.SHORT else None)
    )
    aspect = _infer_aspect(kind, explicit_aspect)

    explicit_res = (
        overrides.get("output_resolution")
        or kind_cfg_block.get("output_resolution")
        or (cfg.get("output_resolution") if kind == RenderKind.SHORT else None)
    )
    if isinstance(explicit_res, (list, tuple)) and len(explicit_res) == 2:
        try:
            output_resolution = (int(explicit_res[0]), int(explicit_res[1]))
        except (TypeError, ValueError):
            output_resolution = _default_resolution_for(aspect)
    else:
        output_resolution = _default_resolution_for(aspect)

    output_fps = _coerce_int(
        overrides.get("output_fps")
        or kind_cfg_block.get("output_fps")
        or cfg.get("output_fps")
    ) or 30

    # ----- visual_mode ----------------------------------------------------
    visual_mode = _infer_visual_mode(
        channel=channel,
        kind=kind,
        explicit=overrides.get("visual_mode") or cfg.get("visual_mode"),
        visual_source=overrides.get("visual_source"),
        cfg=cfg,
    )

    # ----- duration -------------------------------------------------------
    duration_target_s = length_s
    if duration_target_s is None:
        # Fall back to channel YAML's duration_target_s (a [min, max] pair
        # in some channel YAMLs; take max as the target if so).
        d_target = (cfg.get("long_form") or {}).get("duration_target_s") \
            or cfg.get("duration_target_s")
        if isinstance(d_target, (list, tuple)) and d_target:
            duration_target_s = _coerce_int(d_target[-1])
        else:
            duration_target_s = _coerce_int(d_target)

    duration_max_s = _coerce_int(
        overrides.get("duration_max_s")
        # Kind-specific block first (long_form: / sports_doc: / footage_only:).
        # Channel-level fallback ONLY for SHORT to prevent long-form's
        # multi-thousand-second cap from leaking into Shorts and tripping
        # the worker's length gate.
        or kind_cfg_block.get("duration_max_s")
        or (cfg.get("duration_max_s") if kind == RenderKind.SHORT else None)
    ) or duration_target_s

    # ----- audio ----------------------------------------------------------
    audio_mode = _coerce_audio_mode(overrides.get("audio_mode") or cfg.get("audio_mode"))

    # Voice — long_form block's tts_voice / tts_provider takes precedence
    # over the channel-level Shorts defaults when kind=long_form.
    if kind == RenderKind.LONG_FORM:
        lf = cfg.get("long_form") or {}
        cfg_voice_provider = lf.get("tts_provider") or cfg.get("tts_provider")
        cfg_voice_id = lf.get("tts_voice") or cfg.get("tts_voice")
    else:
        cfg_voice_provider = cfg.get("tts_provider")
        cfg_voice_id = cfg.get("tts_voice")

    voice_provider = overrides.get("voice_provider") or cfg_voice_provider
    # Voice fallback: when ``apply_voice_with_cloud_carveout`` dropped
    # the override (unresolvable bare id on a cloud channel — see
    # input_registry._apply_voice), the dropped value is recorded in
    # ``cfg["_dropped_inputs"]``. Don't replay the bad override into
    # spec.voice_id — fall back to the channel YAML's tts_voice, which
    # the apply handler intentionally preserved.
    dropped = {d.get("field") for d in (cfg.get("_dropped_inputs") or [])}
    if "voice" in dropped:
        voice_id = cfg_voice_id  # cfg's tts_voice survives the drop
    else:
        voice_id = overrides.get("voice") or cfg_voice_id

    # Music
    music_bed = overrides.get("music_bed") \
        or (cfg.get("long_form") or {}).get("music_bed_default") \
        or cfg.get("music_bed_default")
    if music_bed and music_bed.endswith(".mp3"):
        music_bed = music_bed[:-4]

    # ----- captions -------------------------------------------------------
    captions_density = _coerce_captions_density(overrides.get("captions_density"))
    # captions_layout is the new (2026-05-14) wizard-driven enum that
    # replaces captions_density as the user-facing knob. Channel YAML
    # may set a default; form override wins.
    captions_layout = _coerce_captions_layout(
        overrides.get("captions_layout")
        or cfg.get("captions_layout")
        or (cfg.get("long_form") or {}).get("captions_layout")
    )
    captions_enabled = bool(
        cfg.get("captions_enabled", True)
        if "captions_enabled" not in overrides
        else overrides.get("captions_enabled")
    )

    # ----- narrator visual mode ------------------------------------------
    narrator_visual_mode = (
        overrides.get("narrator_visual_mode")
        or cfg.get("narrator_visual_mode")
        or "on_screen"
    )

    # ----- new typed overrides (added 2026-05-14) ------------------------
    music_policy = _coerce_music_policy(
        overrides.get("music_policy") or cfg.get("music_policy")
    )
    lower_thirds = _coerce_bool(
        overrides.get("lower_thirds")
        if "lower_thirds" in overrides
        else cfg.get("lower_thirds"),
        default=False,
    )
    chapter_cards = _coerce_bool(
        overrides.get("chapter_cards")
        if "chapter_cards" in overrides
        else cfg.get("chapter_cards"),
        default=False,
    )
    overlay_timeline = _coerce_bool(
        overrides.get("overlay_timeline")
        if "overlay_timeline" in overrides
        else cfg.get("overlay_timeline"),
        default=False,
    )
    critic_loop = _coerce_optional_bool(
        overrides.get("critic_loop")
        if "critic_loop" in overrides
        else cfg.get("critic_loop")
    )
    tone = (overrides.get("tone") or cfg.get("tone") or "").strip() or None

    # ----- extra (non-typed pass-through) --------------------------------
    extra = {
        k: v for k, v in overrides.items()
        if k not in _TYPED_OVERRIDE_KEYS and v not in (None, "")
    }

    spec = RenderSpec(
        channel=channel,
        niche=niche,
        kind=kind,
        visual_mode=visual_mode,
        aspect_ratio=aspect,
        output_resolution=output_resolution,
        output_fps=output_fps,
        duration_target_s=duration_target_s,
        duration_max_s=duration_max_s,
        audio_mode=audio_mode,
        voice_provider=voice_provider,
        voice_id=voice_id,
        music_bed=music_bed,
        song_style=overrides.get("song_style"),
        song_vocal_gender=overrides.get("song_vocal_gender"),
        song_model=overrides.get("song_model"),
        captions_density=captions_density,
        captions_enabled=captions_enabled,
        captions_layout=captions_layout,
        music_policy=music_policy,
        lower_thirds=lower_thirds,
        chapter_cards=chapter_cards,
        overlay_timeline=overlay_timeline,
        critic_loop=critic_loop,
        tone=tone,
        narrator_visual_mode=narrator_visual_mode,
        visual_source=overrides.get("visual_source"),
        visibility=overrides.get("visibility"),
        schedule_at=overrides.get("schedule_at"),
        extra=extra,
        notes=notes,
        source_channel_yaml=str(channel_yaml_path) if channel_yaml_path else None,
        source_variant_yaml=(
            str(variant_yaml_path)
            if variant_yaml_path and Path(variant_yaml_path).exists()
            else None
        ),
    )

    _logger.info(
        "build_spec: channel=%s niche=%s → kind=%s visual_mode=%s "
        "aspect=%s res=%dx%d duration_target_s=%s notes=%d",
        channel, niche, kind.value, visual_mode.value, aspect,
        output_resolution[0], output_resolution[1],
        duration_target_s, len(notes),
    )
    for n in notes:
        _logger.info("build_spec note: %s", n)

    return spec


def _aspect_from_resolution(res: Any) -> str | None:
    """Reverse-engineer aspect from an [W, H] pair. Returns None if not
    a recognised standard."""
    if not isinstance(res, (list, tuple)) or len(res) != 2:
        return None
    try:
        w, h = int(res[0]), int(res[1])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    ratio = w / h
    # 0.05 tolerance to absorb minor rounding (e.g. 1080/1920=0.5625).
    for label, target in [("9:16", 9/16), ("16:9", 16/9), ("1:1", 1.0), ("4:5", 4/5)]:
        if abs(ratio - target) / target < 0.05:
            return label
    return None


__all__ = [
    # Top-level types
    "RenderSpec",
    "RenderKind",
    "VisualMode",
    "AudioMode",
    "CaptionsDensity",
    "CaptionsLayout",
    "MusicPolicy",
    "WatermarkPosition",
    # Nested config dataclasses (added 2026-05-14)
    "TtsConfig",
    "CaptionStyleConfig",
    "MusicPolicyConfig",
    "VideoGradeConfig",
    "WatermarkConfig",
    "ChapterCardConfig",
    "LowerThirdConfig",
    "FillerConfig",
    "NetworkConfig",
    # Builder
    "build_spec",
]
