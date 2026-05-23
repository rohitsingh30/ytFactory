"""Plugin contracts for the unified render engines.

Single source of truth for the seven plugin slot Protocols that the
``short_engine`` and ``long_engine`` dispatch by spec field name lookup.
Engines have ZERO ``if visual_mode == ...`` branches — they consult
the registry, get back a Protocol-conforming impl, call it.

History
-------

This module landed 2026-05-14 as part of the 4-renderer-to-2-engine
consolidation (see plan.md in the session workspace). Before this
landing:

- ``shorts.py`` (3344 LoC) hardwired the per-beat AI-image-slideshow
  pipeline; sports_doc.py (1306 LoC) hardwired the overlay-timeline
  pipeline; long_form.py (2734 LoC) hardwired the panel/archival
  pipeline; footage_only.py (946 LoC) hardwired the shotlist pipeline.
  Combined ~8.3k LoC of orchestration with high duplication.
- The 4 modules each shipped their own subset of: TTS, ASR alignment,
  visualize, captions, music bed, watermark, mux. The same loudnorm
  bug had to be fixed in two places (commit ``ac4b396``, 2026-05-12).

Post-landing:

- Two engines (``short`` / ``long``) call the seven Protocols below.
- Each Protocol has multiple impls under ``pipeline/render/<slot>/``
  (e.g. ``visualize/ai_beat_slideshow.py``,
  ``overlays/word_caption_pngs.py``).
- Engines never know which impl is selected — they pass the spec to
  ``get_<slot>(spec)`` and call the result.
- Adding a new visual_mode = add a module + register it. No engine,
  no contracts, no spec change required.

Plugin slot summary
-------------------

1. ``AudioSynthesizer``   — picked by ``spec.audio_mode`` + ``spec.voice_provider``
2. ``TimelineBuilder``    — picked by engine (short → asr_beats; long → asr_anchors)
3. ``VisualProducer``     — picked by ``spec.visual_mode``. ALWAYS-ON background.
4. ``OverlayProducer``    — LIST-VALUED. Captions, lower-thirds, chapter cards,
                            anchored foreground footage are ALL impls. Engine
                            collects every active producer and concatenates.
5. ``MusicComposer``      — picked by ``spec.music_policy``
6. ``FinalMux``           — picked by engine. Layout-agnostic — stacks visuals +
                            every overlay element + audio + music.
7. (registry helpers)     — see ``register_plugin``, ``get_<slot>``.

Why Protocols not ABCs
----------------------

``typing.Protocol`` (PEP 544) gives us structural typing without
inheritance — plugin authors don't have to import the Protocol; they
just need to expose a method with the right signature. This makes
adding a new plugin a single-file edit AND lets type checkers
(mypy / pyright) catch contract drift at the call site.

Why list-valued OverlayProducer
-------------------------------

Captions, lower-thirds, chapter cards, and "foreground match-footage
clip at this anchor" (the sports_doc case) are all the same shape:
``a list of time-windowed elements that composite on top of the
visual track``. Folding them into ONE Protocol with N impls means:

- Adding a new overlay kind (say, "watermark badge for sponsored
  videos") = add ``overlays/sponsored_badge.py``, register it. Engine
  doesn't change.
- Compose plugins don't need separate code paths for caption layer
  vs lower-third layer vs chapter card layer — they just ``stack by
  (layer, start_s)``.
- The user can mix and match: turn captions OFF + lower-thirds ON +
  chapter cards ON = three booleans on the spec, three list-extend
  calls in the engine, no orchestration churn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

# We import RenderSpec lazily inside Protocol method signatures to avoid
# a circular import (spec.py is the public spec definition; contracts.py
# is consumed by spec.py callers). The ``"RenderSpec"`` string forward
# references work the same way as a real type for type checkers.

__all__ = [
    # Dataclasses
    "AudioResult",
    "Segment",
    "Timeline",
    "VisualTrack",
    "OverlayElement",
    "Section",
    # Protocols
    "AudioSynthesizer",
    "TimelineBuilder",
    "VisualProducer",
    "OverlayProducer",
    "MusicComposer",
    "FinalMux",
    # Registry
    "register_plugin",
    "get_plugin",
    "list_plugins",
    "PluginNotFound",
    # Exceptions (2026-05-15 fail-loud audit)
    "RenderFailedError",
    # Convenience
    "PluginSlot",
]


# ---------------------------------------------------------------------------
# Fail-loud exception (2026-05-15 silent-fallback audit)
# ---------------------------------------------------------------------------


class RenderFailedError(RuntimeError):
    """Raised when a plugin would otherwise silently degrade to a
    visibly-broken artifact (solid-color visual, captions-less mp4,
    >10% per-beat image-gen failures, …).

    Established 2026-05-15 after the silent-fallback audit found:

    - cosmosdecoded shipped 26-min black mp4s because
      ``archival_shotlist`` → ``longform_panels`` → ``_solid_color``
      ran silently when no shotlist was authored;
    - 8/10 mystoriesanimated shorts shipped with ZERO captions because
      a ``from pipeline.captions import render_word_caption`` ImportError
      was caught + logged at WARNING level + returned ``[]``;
    - sportsrecapped renders shipped with frozen-frame tails because
      ``ai_beat_slideshow`` per-beat image-gen failures (40–60% of beats
      failing during cloud incidents) were swallowed with ``continue``
      and ``compose`` padded the missing frames with the last image.

    The fix: each of those sites now raises ``RenderFailedError``
    (subclass of ``RuntimeError``) instead of returning a broken
    artifact. The single env override
    ``YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1`` re-enables the legacy
    silent-degrade path for emergency renders only.

    Error messages MUST include:

    - the originating file:line (or function name) so the operator can
      locate the failing site without grepping;
    - the original cause's ``repr()`` chained via ``raise … from exc``
      so the traceback shows the underlying ImportError /
      ``CloudRunUnavailable`` / etc.

    See ``docs/post-audit-2026-05-15.md`` (companion doc to the
    2026-05-14 audit) for the full list of touched sites + the test
    matrix in ``tests/render/test_fail_loud_fallbacks.py``.
    """


# ---------------------------------------------------------------------------
# Data shapes the Protocols speak
# ---------------------------------------------------------------------------


@dataclass
class AudioResult:
    """The output of any AudioSynthesizer.

    Carries enough information for downstream stages (TimelineBuilder,
    OverlayProducer, FinalMux) to align visuals + captions + overlays
    against the actual narrated audio without re-probing it.
    """

    narration_path: Path
    """The narrated wav file on disk. Sample rate / channels / codec
    are set by the synthesizer impl; downstream stages must NOT assume
    any specific format — they probe via ``ffprobe`` (or read
    ``duration_s`` here)."""

    duration_s: float
    """Measured duration in seconds. Set by the synthesizer impl after
    write. Must match what ``ffprobe`` would report on
    ``narration_path`` — the field is here so callers don't need to
    re-probe in the hot path."""

    voice_fingerprint: dict[str, Any] = field(default_factory=dict)
    """Hashable summary of the cfg fields that drove this synthesis
    (provider, voice_id, speed, post_atempo, chunk_target_chars, ...).
    Same shape as
    :func:`pipeline.render.shared.voice_fingerprint.compute_fingerprint`.
    Used by the engine's cache layer to invalidate stale narration.wav
    when the user picks a different voice."""

    chunk_timings: list[tuple[float, float]] | None = None
    """Optional per-chunk (start_s, end_s) for chunked TTS. ``None``
    when the synthesizer is single-pass. The long engine consumes this
    to align ``Section.start_s`` to chunk boundaries when the
    TimelineBuilder is ``authored_sections`` (no ASR call needed)."""


@dataclass
class Segment:
    """One time-windowed unit of narrated audio.

    The unit varies by engine:

    - ``short`` engine produces ``kind="beat"`` segments via
      ASR-derived word grouping (typically 0.5–4 s each).
    - ``long`` engine produces ``kind="section"`` segments either from
      the authored envelope (no ASR) or by ASR-anchoring authored
      titles to the narrated audio (~30–600 s each).
    - ``sports_doc``-style overlay timelines also produce
      ``kind="chapter"`` segments aligned to chapter title text.

    Visualize / overlay / compose plugins consume Segments without
    caring HOW they were built — they just see ``(start_s, end_s,
    text, anchor_id)``.
    """

    start_s: float
    end_s: float
    text: str
    """The narrated text in this window. Used by visualize plugins
    that prompt-author from the text (ai_beat_slideshow) or by
    overlay plugins that burn captions."""

    anchor_id: str
    """Stable identifier that visualize / overlay producers use to
    correlate a Segment with their own per-segment outputs.

    Format conventions:

    - For ASR-derived beats: ``beat_<idx>`` (zero-padded to 3 digits).
    - For authored sections: the section ``id`` from the envelope.
    - For chapters in overlay-timeline mode: ``chapter_<id>``.

    Plugins should treat this as an opaque key; they should NOT parse
    the format. New segment kinds may pick their own scheme."""

    kind: str = "beat"
    """Discriminator: ``beat`` | ``section`` | ``chapter`` | ``custom``."""

    words: list[Any] | None = None
    """Optional word-level timings inside this segment, in narration
    order. Each entry is a ``pipeline.beats.Word`` (or a duck-typed
    object with ``.text``, ``.start``, ``.end``).

    Populated by ASR-derived timeline builders (short engine's
    ``asr_beats``) so word-level overlay producers (TikTok-style
    one-word-at-a-time captions) can iterate words with their own
    enable windows instead of squashing the whole beat text into a
    single PNG.

    None for non-ASR segments (authored sections, chapter cards,
    fixture-driven timelines) where word-level timing isn't
    available. Word-level caption plugins fall back to single PNG
    per beat when ``words is None`` — pre-2026-05-17 behaviour."""


# Type alias for clarity at call sites.
Timeline = list[Segment]


@dataclass
class VisualTrack:
    """The always-on background video produced by a VisualProducer.

    One continuous video file covering ``[0, audio.duration_s]``.
    Overlay elements composite on top of this; the FinalMux stacks
    them.

    Why a single track and not per-segment clips:

    - Avoids costly concat-mux passes in the engine (a single track
      means the FinalMux just ``-i visuals -i audio -i overlays...``,
      not ``concat 30 clips together first``).
    - Lets a producer emit a single zoom-and-pan over one image that
      spans 5 segments, or an entirely-different visual that ignores
      segment boundaries (e.g., footage_filler cycling b-roll
      regardless of beats).
    """

    video_path: Path
    duration_s: float

    extras: dict[str, Any] = field(default_factory=dict)
    """Producer-specific metadata that downstream stages may want
    (e.g., ``{"images": [Path, ...]}`` for the AI slideshow producer
    so artifacts.py can emit per-image previews to the dashboard)."""


@dataclass
class OverlayElement:
    """One time-windowed element to composite on top of the visual track.

    Captions, lower-thirds, chapter cards, and anchored foreground
    footage are ALL OverlayElements (just emitted by different
    OverlayProducer impls). The FinalMux stacks them by ``(layer,
    start_s)`` and doesn't care which producer contributed which.

    Layer convention (lowest = furthest back):

    - 10 — anchored foreground footage clips (sports_doc match clips)
    - 20 — lower-thirds (speaker name + handle)
    - 30 — chapter cards (full-frame slates)
    - 40 — captions (word PNGs / sentence ASS / etc)
    - 50 — watermark (always on top — but typically rendered as part
            of the visual track, not as an OverlayElement)

    Producers SHOULD pick from this list; new layers may be added by
    pull request once the convention is documented.
    """

    start_s: float
    end_s: float
    layer: int
    asset_path: Path
    """Path to the asset to overlay. Either a PNG (for static overlays
    like captions, chapter cards) or an mp4 (for moving overlays like
    foreground footage clips)."""

    region: tuple[int, int, int, int] | None = None
    """(x, y, w, h) of the overlay's position on the canvas. ``None``
    = the producer wants the FinalMux to position it (e.g., captions
    are typically bottom-centered with margin-from-bottom that the
    FinalMux knows from spec.captions_layout)."""

    blend: str = "over"
    """ffmpeg blend mode (``over`` is the default alpha-compositing
    mode). Other values: ``screen``, ``multiply``, ``addition`` for
    special-effect overlays."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Producer-specific metadata (e.g., ``{"text": "...", "lang":
    "en"}`` for caption overlays so the FinalMux can emit them as a
    sidecar SRT alongside the burned-in video)."""


@dataclass
class Section:
    """One authored section in a long-form script.

    Used by ``long_engine`` when the TimelineBuilder is
    ``authored_sections`` (or post-ASR-anchoring). The MusicComposer's
    ``section_mood`` impl consumes this list to pick a different mood
    bed per section.
    """

    id: str
    title: str
    start_s: float
    end_s: float
    body: str
    """The full narrated text of this section. The visualize plugin
    may prompt-author from this if it produces per-section panels;
    the music plugin may inspect for tone hints."""

    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The seven Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class AudioSynthesizer(Protocol):
    """Slot picked by ``spec.audio_mode`` + ``spec.voice_provider``.

    Examples of impls:

    - ``audio.tts_single`` — single-pass cloud TTS for short engine
    - ``audio.tts_chunked`` — chunked cloud TTS for long engine
    - ``audio.song_suno`` — Suno-API sung song
    - ``audio.audio_from_fixture`` — test-only: load wav from disk
    """

    def synth(
        self,
        spec: "RenderSpec",
        script: dict[str, Any],
        work_dir: Path,
    ) -> AudioResult: ...


@runtime_checkable
class TimelineBuilder(Protocol):
    """Slot picked by engine.

    - ``short`` engine → ``timeline.asr_beats`` (cloud whisper, group
      words into beats).
    - ``long`` engine → ``timeline.asr_anchors`` (cloud whisper, match
      authored anchors to actual narration).
    - Tests → ``timeline.timeline_from_fixture`` (load JSON from disk).
    """

    def build(
        self,
        spec: "RenderSpec",
        script: dict[str, Any],
        audio: AudioResult,
    ) -> Timeline: ...


@runtime_checkable
class VisualProducer(Protocol):
    """Slot picked by ``spec.visual_mode``.

    Produces the ALWAYS-ON background video covering ``[0,
    audio.duration_s]``. Overlay producers composite on top.

    Examples of impls:

    - ``visualize.ai_beat_slideshow`` — Flux cloud, one image per beat
    - ``visualize.motion_clips`` — AnimateDiff/SVD per beat
    - ``visualize.hybrid_beat_footage`` — footage match + AI fallback
    - ``visualize.footage_windows`` — shotlist-driven (was footage_only)
    - ``visualize.longform_panels`` — Flux cloud, panels per section
    - ``visualize.archival_shotlist`` — archive.org/Wikimedia shotlist
    - ``visualize.footage_filler`` — b-roll cycled (paired with
      ``overlays.anchored_footage`` for the sports_doc shape)
    """

    def produce(
        self,
        spec: "RenderSpec",
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack: ...


@runtime_checkable
class OverlayProducer(Protocol):
    """Slot is LIST-VALUED — zero or more impls run, each contributing
    time-windowed elements that composite on top of the visual track.

    Producers are activated by spec flags:

    - ``spec.captions_layout`` selects ONE caption producer
      (``overlays.word_caption_pngs`` /
      ``overlays.sentence_caption_ass`` / no-op).
    - ``spec.lower_thirds`` (bool) toggles ``overlays.lower_third``.
    - ``spec.chapter_cards`` (bool) toggles ``overlays.chapter_card``.
    - ``spec.visual_mode == OVERLAY_TIMELINE`` toggles
      ``overlays.anchored_footage`` (the sports_doc foreground clips).

    Engine collects all active producers, runs each, concatenates the
    returned lists, hands the merged ``list[OverlayElement]`` to the
    FinalMux. Compose stacks by ``(layer, start_s)`` — order
    independence is a Protocol guarantee.
    """

    def produce(
        self,
        spec: "RenderSpec",
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]: ...


@runtime_checkable
class MusicComposer(Protocol):
    """Slot picked by ``spec.music_policy``.

    Examples of impls:

    - ``music.ducked_loop`` — short-style ducked bed under narration
    - ``music.single_bed`` — long-form single ambient loop
    - ``music.section_mood`` — per-section mood crossfade (consumes
      the optional ``sections`` arg)
    - ``music.none`` — explicit no-op (returns a silent wav of
      ``narration_duration_s`` seconds)
    """

    def compose(
        self,
        spec: "RenderSpec",
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path: ...


@runtime_checkable
class FinalMux(Protocol):
    """Slot picked by engine.

    Layout-agnostic — stacks the visual track + every overlay element
    + audio + music. Doesn't care how many overlay producers
    contributed.

    Examples of impls:

    - ``compose.beat_slideshow_mux`` — short engine final mux
    - ``compose.section_video_mux`` — long engine final mux (handles
      overlay foreground footage as a special case via the
      ``OverlayElement`` layer convention)
    """

    def mux(
        self,
        visuals: VisualTrack,
        audio: AudioResult,
        overlays: list[OverlayElement],
        music: Path,
        spec: "RenderSpec",
        out_path: Path,
    ) -> Path: ...


# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------


class PluginNotFound(LookupError):
    """Raised by :func:`get_plugin` when no impl is registered for the
    requested ``(slot, name)``.

    The error message lists the registered names so the operator can
    spot a typo immediately (``"foo" not found in slot=visualize;
    available: ai_beat_slideshow / motion_clips / ..."``).
    """


# Slot is just a string for now — we deliberately don't make it an
# enum so plugin authors can add new slots without an enum bump.
PluginSlot = str

_REGISTRY: dict[PluginSlot, dict[str, Callable[..., Any]]] = {}


def register_plugin(slot: PluginSlot, name: str, impl: Any) -> None:
    """Register ``impl`` as the plugin for ``(slot, name)``.

    Plugin modules call this at import time:

    .. code-block:: python

        # pipeline/render/visualize/ai_beat_slideshow.py
        from pipeline.render.contracts import register_plugin

        class AiBeatSlideshow:
            def produce(self, spec, timeline, work_dir): ...

        register_plugin("visualize", "ai_beat_slideshow", AiBeatSlideshow())

    Re-registering with the same ``(slot, name)`` REPLACES the old
    impl (useful for tests that swap in a stub).

    The Protocol contract for the slot is documented on the matching
    Protocol class above; ``register_plugin`` does NOT enforce it at
    registration time (Protocols are structural — duck typing wins).
    Type checkers + per-plugin unit tests catch contract drift.
    """
    _REGISTRY.setdefault(slot, {})[name] = impl


def get_plugin(slot: PluginSlot, name: str) -> Any:
    """Look up the registered impl for ``(slot, name)`` or raise
    :class:`PluginNotFound`.

    The engine calls this every time it dispatches a slot. ``slot``
    is one of {audio, timeline, visualize, overlays, music, compose};
    ``name`` is a spec field (``spec.visual_mode.value``,
    ``spec.captions_layout.value``, etc).
    """
    impls = _REGISTRY.get(slot)
    if impls is None or name not in impls:
        available = sorted(impls.keys()) if impls else []
        raise PluginNotFound(
            f"plugin {name!r} not found in slot={slot!r}; "
            f"available: {' / '.join(available) if available else '(none registered)'}"
        )
    return impls[name]


def list_plugins(slot: PluginSlot) -> list[str]:
    """Return the registered plugin names for ``slot``, sorted.

    Used by the dashboard / wizard to populate dropdowns of available
    visual_modes / caption styles / music policies / etc without the
    UI having to maintain a hardcoded mirror.
    """
    impls = _REGISTRY.get(slot, {})
    return sorted(impls.keys())
