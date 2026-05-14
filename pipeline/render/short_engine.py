"""Short engine — single-pass TTS, beat-driven visualize.

Orchestrates the seven plugin slots
(:mod:`pipeline.render.contracts`) for a single short render. Pure
dispatch — has ZERO ``if visual_mode == ...`` or ``if audio_mode ==
...`` branches. Every plugin choice comes from the spec via
``get_plugin(slot, name)``.

Stage order
-----------

1. ``audio.synth(spec, script, work_dir)`` → ``AudioResult``
2. ``timeline.build(spec, script, audio)`` → ``Timeline``
3. ``visualize.produce(spec, timeline, work_dir)`` → ``VisualTrack``
4. ``music.compose(spec, audio.duration_s, sections=None)`` → ``Path``
5. (parallel) every active overlay producer → concatenated
   ``list[OverlayElement]``
6. ``compose.mux(visuals, audio, overlays, music, spec, out_path)`` → mp4
7. (optional) critic loop if ``spec.critic_loop is True``

Plugin selection (driven entirely by spec fields, no engine knowledge)
----------------------------------------------------------------------

* audio plugin    = ``"song_suno"`` if ``spec.audio_mode == SONG`` else
                    ``"tts_single"``
* timeline plugin = ``"asr_beats"`` (always; tests can override via
                    ``spec.extra["timeline_plugin"]``)
* visualize plugin = ``spec.visual_mode.value``
* music plugin    = ``spec.music_policy.value``
* compose plugin  = ``"beat_slideshow"`` (the canonical short mux)
* overlays        = collected from spec flags (see
                    :func:`_collect_overlay_plugins` below)

Tests can override any plugin-name pick via
``spec.extra["<slot>_plugin"]`` — used by engine goldens to swap in
the fixture-loading variants without changing channel YAML.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    AudioSynthesizer,
    FinalMux,
    MusicComposer,
    OverlayElement,
    OverlayProducer,
    Timeline,
    TimelineBuilder,
    VisualProducer,
    VisualTrack,
    get_plugin,
)
from pipeline.render.spec import (
    AudioMode,
    CaptionsLayout,
    RenderSpec,
)

# Eager-import the plugin packages so their register_plugin calls run.
# Without this the engine's first get_plugin call would raise
# PluginNotFound — a hidden first-render-of-the-process bug.
import pipeline.render.audio  # noqa: F401
import pipeline.render.timeline  # noqa: F401
import pipeline.render.visualize  # noqa: F401
import pipeline.render.overlays  # noqa: F401
import pipeline.render.music  # noqa: F401
import pipeline.render.compose  # noqa: F401


_logger = logging.getLogger(__name__)


def render_short(
    spec: RenderSpec,
    script: dict[str, Any],
    work_dir: Path,
    out_path: Path,
) -> Path:
    """Render one short to ``out_path``. Returns the final mp4 path.

    Stages run sequentially today; the bigbang PR may parallelise the
    audio + timeline stages with the visualize stage when the
    visualize plugin doesn't depend on timeline (the existing
    ``stage_overlap`` policy already handles this for legacy renderers).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _logger.info(
        "render_short: channel=%s slug-source=%s aspect=%s res=%s",
        spec.channel,
        spec.extra.get("topic", "")[:40],
        spec.aspect_ratio,
        spec.output_resolution,
    )

    # 1. Audio
    audio_plugin: AudioSynthesizer = get_plugin("audio", _audio_plugin_name(spec))
    audio: AudioResult = audio_plugin.synth(spec, script, work_dir)
    _logger.info("render_short: audio=%s duration=%.2fs",
                 audio.narration_path.name, audio.duration_s)

    # 2. Timeline
    timeline_plugin: TimelineBuilder = get_plugin(
        "timeline", _timeline_plugin_name(spec, default="asr_beats"),
    )
    timeline: Timeline = timeline_plugin.build(spec, script, audio)
    _logger.info("render_short: timeline=%d segments", len(timeline))

    # 3. Visuals
    visualize_plugin: VisualProducer = get_plugin(
        "visualize", _visualize_plugin_name(spec),
    )
    visuals: VisualTrack = visualize_plugin.produce(spec, timeline, work_dir)
    _logger.info("render_short: visuals=%s duration=%.2fs",
                 visuals.video_path.name, visuals.duration_s)

    # 4. Music
    music_plugin: MusicComposer = get_plugin("music", _music_plugin_name(spec))
    music_path = music_plugin.compose(spec, audio.duration_s, sections=None)
    _logger.info("render_short: music=%s", music_path.name)

    # 5. Overlays — collect from every active producer.
    overlays = _collect_overlays(spec, timeline, audio)
    _logger.info("render_short: overlays=%d elements", len(overlays))

    # 6. Compose
    compose_plugin: FinalMux = get_plugin(
        "compose", _compose_plugin_name(spec, default="beat_slideshow"),
    )
    mp4_path = compose_plugin.mux(visuals, audio, overlays, music_path, spec, out_path)
    _logger.info("render_short: mux done → %s", mp4_path)

    # 7. Critic loop — opt-in only (per user direction 2026-05-14).
    if spec.critic_loop is True:
        _run_critic_loop(spec, mp4_path, work_dir)

    return mp4_path


# ---------------------------------------------------------------------------
# Plugin selection helpers — pure functions of the spec
# ---------------------------------------------------------------------------


def _audio_plugin_name(spec: RenderSpec) -> str:
    """Test override > audio_mode → plugin name.

    SONG mode picks ``song_suno`` (when implemented). VOICE mode picks
    ``tts_single`` (single-pass cloud TTS for short engine).
    """
    override = (spec.extra or {}).get("audio_plugin")
    if override:
        return override
    if spec.audio_mode == AudioMode.SONG:
        return "song_suno"
    return "tts_single"


def _timeline_plugin_name(spec: RenderSpec, *, default: str) -> str:
    return (spec.extra or {}).get("timeline_plugin") or default


def _visualize_plugin_name(spec: RenderSpec) -> str:
    return (spec.extra or {}).get("visualize_plugin") or spec.visual_mode.value


def _music_plugin_name(spec: RenderSpec) -> str:
    return (spec.extra or {}).get("music_plugin") or spec.music_policy.value


def _compose_plugin_name(spec: RenderSpec, *, default: str) -> str:
    return (spec.extra or {}).get("compose_plugin") or default


def _collect_overlays(
    spec: RenderSpec,
    timeline: Timeline,
    audio: AudioResult,
) -> list[OverlayElement]:
    """Run every active overlay producer, concatenate the results.

    Active set is determined by spec flags. Producers are independent
    — order doesn't matter; FinalMux sorts by ``(layer, start_s)``.
    """
    out: list[OverlayElement] = []

    # Captions — picked by spec.captions_layout when captions enabled.
    if spec.captions_enabled:
        cap_name = _captions_plugin_for_layout(spec.captions_layout)
        try:
            cap_plugin: OverlayProducer = get_plugin("overlays", cap_name)
            out.extend(cap_plugin.produce(spec, timeline, audio))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("captions overlay (%s) skipped: %s", cap_name, exc)

    # Lower-thirds — only if spec.lower_thirds is True.
    if spec.lower_thirds:
        try:
            out.extend(get_plugin("overlays", "lower_third").produce(spec, timeline, audio))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("lower_third overlay skipped: %s", exc)

    # Chapter cards — only if spec.chapter_cards is True.
    if spec.chapter_cards:
        try:
            out.extend(get_plugin("overlays", "chapter_card").produce(spec, timeline, audio))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("chapter_card overlay skipped: %s", exc)

    # Anchored foreground footage — sports_doc-style overlay timeline.
    if spec.overlay_timeline:
        try:
            out.extend(get_plugin("overlays", "anchored_footage").produce(spec, timeline, audio))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("anchored_footage overlay skipped: %s", exc)

    return out


def _captions_plugin_for_layout(layout: CaptionsLayout) -> str:
    """Map CaptionsLayout enum → overlay plugin name."""
    if layout == CaptionsLayout.CENTER_WORD_BY_WORD:
        return "word_caption_pngs"
    return "sentence_caption_ass"  # bottom_one_line + bottom_two_line


def _run_critic_loop(spec: RenderSpec, mp4_path: Path, work_dir: Path) -> None:
    """Run the critic on the rendered mp4 + log the result.

    Today this is a stub — the bigbang PR wires
    :mod:`pipeline.llm.critic` (or its successor) into the engine.
    Critic remains opt-in via ``spec.critic_loop=True``; the engine
    never silently spends critic budget on a render.
    """
    _logger.info(
        "render_short: critic_loop opt-in (mp4=%s, channel=%s) — "
        "stub for now; bigbang PR wires the real critic",
        mp4_path.name, spec.channel,
    )


__all__ = ["render_short"]
