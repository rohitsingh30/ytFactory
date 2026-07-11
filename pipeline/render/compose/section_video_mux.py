"""Long engine FinalMux — section-video shape.

Same logic as :mod:`pipeline.render.compose.beat_slideshow_mux` but
defaults to 16:9 (1920×1080). Both engines emit through the same
underlying ffmpeg chain — the difference is just the spec defaults
that flow through, plus the long engine's chunked-TTS audio path
(handled upstream by :mod:`pipeline.render.audio.tts_chunked`).

Why a separate plugin instead of one shared mux:

The long engine carries section_mood music (multi-bed crossfade) +
chapter card overlays + lower-thirds — all of which look identical
to the FinalMux but the ffmpeg invocation differs in the duration
ceiling. Long renders are 5–120 minutes and the ``-t`` cap on the
final pass is what prevents a runaway. Short renders cap at 120 s
naturally so the same logic with a different spec works fine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.compose.beat_slideshow_mux import BeatSlideshowMux
from pipeline.render.contracts import (
    AudioResult,
    FinalMux,
    OverlayElement,
    VisualTrack,
    register_plugin,
)


class SectionVideoMux:
    """Long engine final mux. Inherits the beat_slideshow_mux impl
    wholesale."""

    def __init__(self) -> None:
        self._inner = BeatSlideshowMux()
        self._inner._telemetry_purpose = "section_video_mux"

    def mux(
        self,
        visuals: VisualTrack,
        audio: AudioResult,
        overlays: list[OverlayElement],
        music: Path,
        spec: Any,
        out_path: Path,
    ) -> Path:
        return self._inner.mux(visuals, audio, overlays, music, spec, out_path)


register_plugin("compose", "section_video", SectionVideoMux())
assert isinstance(SectionVideoMux(), FinalMux)


__all__ = ["SectionVideoMux"]
