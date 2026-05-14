"""Short engine — single-pass TTS, beat-driven visualize.

Orchestrates the seven plugin slots
(:mod:`pipeline.render.contracts`) for a single short render. Pure
dispatch — has ZERO ``if visual_mode == ...`` or ``if audio_mode ==
...`` branches. Every plugin choice comes from the spec via
``get_plugin(slot, name)``.

Stage order
-----------

Sequential stages:

1. ``audio.synth(spec, script, work_dir)`` → ``AudioResult``
2. ``timeline.build(spec, script, audio)`` → ``Timeline``
3. ``music.compose(spec, audio.duration_s)`` → ``Path``
4. (active overlay producers, collected from spec flags) →
   concatenated ``list[OverlayElement]``
5. ``compose.mux(visuals, audio, overlays, music, spec, out_path)`` → mp4
6. (optional) critic loop if ``spec.critic_loop is True``

PARALLEL stage (when ``stage_overlap.gpu_safe_to_overlap`` returns
True — both providers must be cloud-bound):

* ``visualize.produce(spec, preliminary_timeline, work_dir)`` runs on
  a worker thread BEFORE the real timeline lands. The visualize
  producer only reads ``Segment.text`` (not ``start_s`` / ``end_s``)
  for image-gen prompts, so we can build a SYNTHETIC timeline from
  the authored script texts upfront. The real per-segment timestamps
  from ``asr_beats`` arrive later — compose re-times visuals to the
  real boundaries at mux time.

  Same pattern as legacy ``pipeline.render.shorts``'s TTS ⫽ image-
  gen overlap (see ``docs/parallel_stage_overlap.md``).

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
    Segment,
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
from pipeline.stage_overlap import StageOverlap, gpu_safe_to_overlap

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

    Overlaps the slow image-gen stage with the slow TTS + ASR stages
    when both providers are cloud-bound (per
    :func:`pipeline.stage_overlap.gpu_safe_to_overlap`). On laptop
    fallback (local TTS or local image gen) falls back to strict
    sequential to avoid Metal/MPS contention.
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

    # Resolve plugin names once so the same picks apply on both the
    # main thread and the visualize worker thread.
    audio_name = _audio_plugin_name(spec)
    timeline_name = _timeline_plugin_name(spec, default="asr_beats")
    visualize_name = _visualize_plugin_name(spec)
    music_name = _music_plugin_name(spec)
    compose_name = _compose_plugin_name(spec, default="beat_slideshow")

    # ----- Stage overlap gate ---------------------------------------------
    # The visualize plugin can run on a worker thread BEFORE TTS + ASR
    # finish IF both providers are cloud-bound. Same policy as legacy
    # shorts.py — see docs/parallel_stage_overlap.md + the comment in
    # pipeline/stage_overlap.py for the GPU-contention reasoning.
    overlap_safe, overlap_reason = gpu_safe_to_overlap(
        tts_provider=spec.voice_provider,
        image_provider=spec.extra.get("image_provider"),
    )

    # ----- Build the preliminary timeline (text-only, synthetic timing) ---
    # The visualize plugin reads Segment.text for image-gen prompts;
    # it does NOT need real ASR-derived start_s / end_s. Building a
    # synthetic timeline from the authored script lets us kick off
    # image-gen BEFORE TTS completes — the legacy pattern from
    # pipeline.render.preliminary_beats applied here as the universal
    # short-engine fast path.
    preliminary_timeline = _build_preliminary_timeline(script)

    audio_plugin: AudioSynthesizer = get_plugin("audio", audio_name)
    timeline_plugin: TimelineBuilder = get_plugin("timeline", timeline_name)
    visualize_plugin: VisualProducer = get_plugin("visualize", visualize_name)
    music_plugin: MusicComposer = get_plugin("music", music_name)
    compose_plugin: FinalMux = get_plugin("compose", compose_name)

    if overlap_safe and preliminary_timeline:
        _logger.info(
            "render_short: [overlap] %s — visualize on worker thread "
            "in parallel with TTS + ASR (%d preliminary beats)",
            overlap_reason, len(preliminary_timeline),
        )
        audio, timeline, visuals = _run_overlapped(
            spec=spec,
            script=script,
            work_dir=work_dir,
            audio_plugin=audio_plugin,
            timeline_plugin=timeline_plugin,
            visualize_plugin=visualize_plugin,
            preliminary_timeline=preliminary_timeline,
        )
    else:
        if not preliminary_timeline:
            reason = "no preliminary beats authored (script has no text)"
        else:
            reason = overlap_reason
        _logger.info("render_short: [overlap] disabled (%s) — sequential stages", reason)
        audio, timeline, visuals = _run_sequential(
            spec=spec,
            script=script,
            work_dir=work_dir,
            audio_plugin=audio_plugin,
            timeline_plugin=timeline_plugin,
            visualize_plugin=visualize_plugin,
        )

    _logger.info(
        "render_short: audio=%s duration=%.2fs timeline=%d visuals=%s",
        audio.narration_path.name, audio.duration_s,
        len(timeline), visuals.video_path.name,
    )

    # 4. Music
    music_path = music_plugin.compose(spec, audio.duration_s, sections=None)
    _logger.info("render_short: music=%s", music_path.name)

    # 5. Overlays — collect from every active producer.
    overlays = _collect_overlays(spec, timeline, audio)
    _logger.info("render_short: overlays=%d elements", len(overlays))

    # 6. Compose
    mp4_path = compose_plugin.mux(visuals, audio, overlays, music_path, spec, out_path)
    _logger.info("render_short: mux done → %s", mp4_path)

    # 7. Critic loop — opt-in only (per user direction 2026-05-14).
    if spec.critic_loop is True:
        _run_critic_loop(spec, mp4_path, work_dir)

    return mp4_path


# ---------------------------------------------------------------------------
# Stage overlap helpers
# ---------------------------------------------------------------------------


def _build_preliminary_timeline(script: dict[str, Any]) -> Timeline:
    """Build a synthetic-timing :class:`Timeline` from the authored
    script texts so the visualize stage can run BEFORE TTS+ASR.

    Each authored line becomes one :class:`Segment` with synthetic
    equal-spaced timing (start_s / end_s are placeholders — the
    visualize plugin reads ``Segment.text`` for prompt-authoring
    and ignores timestamps).

    Returns ``[]`` when the script has no usable text. Callers MUST
    treat that as "overlap not eligible — fall back to strict
    sequential".

    Same pattern as legacy ``pipeline.render.preliminary_beats``
    applied to the new contracts.Timeline shape.
    """
    DEFAULT_BEAT_S = 2.0

    # Short scripts authored by /make-script have one of these shapes:
    # 1. shots[] with narration_line per shot + optional closer
    # 2. beats[] with text per beat
    # 3. plain ``narration`` string
    shots = script.get("shots") or []
    if shots:
        lines: list[str] = []
        for s in shots:
            line = (s.get("narration_line") or s.get("text") or "").strip()
            if line:
                lines.append(line)
        closer = (script.get("closer") or {}).get("narration_line")
        if closer:
            lines.append(closer.strip())
    elif script.get("beats"):
        lines = [b.get("text", "").strip() for b in script["beats"]
                 if b.get("text", "").strip()]
    else:
        narration = (script.get("narration") or "").strip()
        if not narration:
            return []
        # Split on sentence boundaries when no beats are authored.
        # The visualize plugin will get one Segment per sentence.
        import re
        lines = [s.strip() for s in re.split(r"(?<=[.!?])\s+", narration)
                 if s.strip()]

    if not lines:
        return []

    return [
        Segment(
            start_s=i * DEFAULT_BEAT_S,
            end_s=(i + 1) * DEFAULT_BEAT_S,
            text=line,
            anchor_id=f"beat_{i:03d}",
            kind="beat",
        )
        for i, line in enumerate(lines)
    ]


def _run_overlapped(
    *,
    spec: RenderSpec,
    script: dict[str, Any],
    work_dir: Path,
    audio_plugin: AudioSynthesizer,
    timeline_plugin: TimelineBuilder,
    visualize_plugin: VisualProducer,
    preliminary_timeline: Timeline,
) -> tuple[AudioResult, Timeline, VisualTrack]:
    """Parallel path: visualize runs on a worker thread while
    audio + timeline run on the main thread. Re-joins before
    returning.

    The visualize plugin sees the PRELIMINARY timeline (text-only,
    synthetic timing). The real per-segment timestamps from
    ``timeline_plugin.build(spec, script, audio)`` are computed on
    the main thread post-TTS. Compose re-times visuals against the
    real timeline at mux time.
    """
    with StageOverlap(label=f"short-engine-{spec.channel[:16]}",
                      max_workers=1, log=True) as overlap:
        # Branch: visualize (heaviest stage — Flux cloud per beat).
        # ``run_ms`` would be ideal here but we just want the result.
        visuals_fut = overlap.submit(
            "visualize",
            visualize_plugin.produce,
            spec, preliminary_timeline, work_dir,
        )

        # Main thread: audio + timeline (sequential — timeline needs audio).
        audio = audio_plugin.synth(spec, script, work_dir)
        _drop_f5_after_audio(spec, label="short stage-1 TTS (post-overlap)")
        timeline = timeline_plugin.build(spec, script, audio)

        # Re-join.
        visuals = visuals_fut.result()

    return audio, timeline, visuals


def _drop_f5_after_audio(spec: RenderSpec, *, label: str) -> None:
    """Free F5-TTS-MLX weights after the audio stage when the renderer
    used local F5. Same guard the legacy renderers wired into stage
    boundaries — keeps 1.35 GB from leaking into the visualize/mux
    Metal context. No-op on cloud TTS providers."""
    provider = (spec.voice_provider or "").lower()
    if "f5" not in provider:
        return
    try:
        from pipeline.preflight import reset_mlx_state  # noqa: PLC0415
        reset_mlx_state(drop_f5=True, label=label)
    except Exception:
        pass


def _run_sequential(
    *,
    spec: RenderSpec,
    script: dict[str, Any],
    work_dir: Path,
    audio_plugin: AudioSynthesizer,
    timeline_plugin: TimelineBuilder,
    visualize_plugin: VisualProducer,
) -> tuple[AudioResult, Timeline, VisualTrack]:
    """Strict-sequential path: audio → timeline → visualize. Picked
    when the overlap gate refuses (local TTS, local image-gen, or
    YTFACTORY_DISABLE_STAGE_OVERLAP=1)."""
    audio = audio_plugin.synth(spec, script, work_dir)
    _drop_f5_after_audio(spec, label="short stage-1 TTS")
    timeline = timeline_plugin.build(spec, script, audio)
    visuals = visualize_plugin.produce(spec, timeline, work_dir)
    return audio, timeline, visuals


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
