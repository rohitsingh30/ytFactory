"""Long engine — chunked TTS, section-driven visualize.

Same shape as :mod:`pipeline.render.short_engine` but defaults the
``audio`` plugin to ``tts_chunked`` (cloud TTS chunked + concat with
silence joiners), the ``timeline`` plugin to ``asr_anchors`` (cloud
whisper anchored to authored section titles), and the ``compose``
plugin to ``section_video``.

The engine has ZERO ``if visual_mode == ...`` or ``if music_policy
== ...`` branches — same dispatch pattern as the short engine. Every
plugin choice comes from spec → registry.
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
    Section,
    Timeline,
    TimelineBuilder,
    VisualProducer,
    VisualTrack,
    get_plugin,
)
from pipeline.render.short_engine import (
    ProgressCallback,
    _captions_plugin_for_layout,
    _collect_overlays,
    _compose_plugin_name,
    _emit,
    _music_plugin_name,
    _timeline_plugin_name,
    _visualize_plugin_name,
)
from pipeline.render.spec import (
    AudioMode,
    RenderSpec,
)

# Eager-import — same reason as short_engine.
import pipeline.render.audio  # noqa: F401
import pipeline.render.timeline  # noqa: F401
import pipeline.render.visualize  # noqa: F401
import pipeline.render.overlays  # noqa: F401
import pipeline.render.music  # noqa: F401
import pipeline.render.compose  # noqa: F401


_logger = logging.getLogger(__name__)


def render_long(
    spec: RenderSpec,
    script: dict[str, Any],
    work_dir: Path,
    out_path: Path,
    *,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Render one long-form to ``out_path``. Returns the final mp4 path.

    ``progress_cb(stage, msg)`` is fired at each substage boundary
    (``tts`` / ``asr`` / ``images`` / ``compose``) so the cloud
    worker's dashboard timeline can show live transitions. Per-substep
    granularity (TTS chunk N/M, panel N/M) flows through the worker's
    stdout-classifier path which catches the legacy ``print()``
    statements still emitted by the delegating plugin shims (e.g.,
    ``tts_chunked`` → ``synth_long_narration``).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _logger.info(
        "render_long: channel=%s aspect=%s res=%s duration_target=%ss",
        spec.channel, spec.aspect_ratio, spec.output_resolution,
        spec.duration_target_s,
    )

    audio_name = _long_audio_plugin_name(spec)
    timeline_name = _timeline_plugin_name(spec, default="asr_anchors")
    visualize_name = _visualize_plugin_name(spec)
    music_name = _music_plugin_name(spec)
    compose_name = _compose_plugin_name(spec, default="section_video")

    # 1. Audio (chunked by default)
    audio_plugin: AudioSynthesizer = get_plugin("audio", audio_name)
    _emit(progress_cb, "tts",
          f"Synthesizing chunked narration ({audio_name})")
    audio: AudioResult = audio_plugin.synth(spec, script, work_dir)
    _drop_f5_after_audio(spec, label="long stage-1 TTS")
    _logger.info("render_long: audio=%s duration=%.2fs chunks=%s",
                 audio.narration_path.name, audio.duration_s,
                 len(audio.chunk_timings) if audio.chunk_timings else 0)
    n_chunks = len(audio.chunk_timings) if audio.chunk_timings else 0
    _emit(progress_cb, "tts",
          f"Narration ready: {audio.duration_s:.1f}s "
          f"({n_chunks} chunks)" if n_chunks
          else f"Narration ready: {audio.duration_s:.1f}s")

    # 2. Timeline (asr_anchors by default — anchors authored sections)
    timeline_plugin: TimelineBuilder = get_plugin("timeline", timeline_name)
    _emit(progress_cb, "asr", f"Aligning sections ({timeline_name})")
    timeline: Timeline = timeline_plugin.build(spec, script, audio)
    _logger.info("render_long: timeline=%d segments", len(timeline))
    _emit(progress_cb, "asr", f"Aligned {len(timeline)} segments")

    # 3. Visuals
    visualize_plugin: VisualProducer = get_plugin("visualize", visualize_name)
    _emit(progress_cb, "images",
          f"Generating {len(timeline)} visuals via {visualize_name}")
    visuals: VisualTrack = visualize_plugin.produce(spec, timeline, work_dir)
    _logger.info("render_long: visuals=%s duration=%.2fs",
                 visuals.video_path.name, visuals.duration_s)
    _emit(progress_cb, "images",
          f"Visuals ready: {visuals.video_path.name} ({visuals.duration_s:.1f}s)")

    # 4. Music — section_mood needs sections; build from script
    music_plugin: MusicComposer = get_plugin("music", music_name)
    sections = _extract_sections(script, timeline)
    _emit(progress_cb, "compose",
          f"Composing music ({music_name}, {len(sections)} sections)")
    music_path = music_plugin.compose(
        spec, audio.duration_s, sections=sections,
    )
    _logger.info("render_long: music=%s sections=%d",
                 music_path.name, len(sections))

    # 5. Overlays
    overlays = _collect_overlays(spec, timeline, audio)
    _logger.info("render_long: overlays=%d elements", len(overlays))

    # 6. Compose (section_video by default)
    compose_plugin: FinalMux = get_plugin("compose", compose_name)
    _emit(progress_cb, "compose", "Stitching video with ffmpeg")
    mp4_path = compose_plugin.mux(visuals, audio, overlays, music_path, spec, out_path)
    _logger.info("render_long: mux done → %s", mp4_path)
    _emit(progress_cb, "compose", f"Wrote {mp4_path.name}")

    if spec.critic_loop is True:
        _logger.info("render_long: critic_loop opt-in (stub)")

    return mp4_path


def _long_audio_plugin_name(spec: RenderSpec) -> str:
    override = (spec.extra or {}).get("audio_plugin")
    if override:
        return override
    if spec.audio_mode == AudioMode.SONG:
        return "song_suno"
    return "tts_chunked"


def _extract_sections(script: dict[str, Any], timeline: Timeline) -> list[Section]:
    """Build :class:`Section` list from script + timeline.

    The MusicComposer's ``section_mood`` impl consumes sections to pick
    a different mood bed per section. Falls back to one Section
    spanning the whole timeline if the script has no sections[].
    """
    raw = script.get("sections") or script.get("chapters") or []
    if not raw:
        if timeline:
            return [Section(
                id="full",
                title="",
                start_s=timeline[0].start_s,
                end_s=timeline[-1].end_s,
                body=script.get("narration", ""),
            )]
        return []

    # Match raw[i] → timeline[i] if lengths align, else equal-share.
    out: list[Section] = []
    for i, sec in enumerate(raw):
        if i < len(timeline):
            seg = timeline[i]
            start_s, end_s = seg.start_s, seg.end_s
        else:
            start_s, end_s = 0.0, 0.0
        out.append(Section(
            id=str(sec.get("id") or f"sec_{i:03d}"),
            title=sec.get("title", ""),
            start_s=start_s,
            end_s=end_s,
            body=sec.get("body") or sec.get("narration") or "",
            extras={k: v for k, v in sec.items()
                    if k not in {"id", "title", "body", "narration"}},
        ))
    return out


__all__ = ["render_long"]


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
