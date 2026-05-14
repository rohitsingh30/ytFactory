"""Silent MusicComposer — picked by ``spec.music_policy = NONE``.

Generates a silent wav of the requested duration via ``ffmpeg``'s
``anullsrc`` lavfi source. Same sample rate / channel layout as the
narration so the FinalMux can stream-copy the mix without resampling.

Useful for channels that don't ship music (footage_only kathaa,
voice-only podcasts) and for engine goldens that want to isolate
music-bus interactions from compose behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    MusicComposer,
    Section,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg


class SilentMusic:
    """Emit a silent wav of ``narration_duration_s`` seconds.

    Sample rate / channel layout match the standard cloud TTS output
    (24000 Hz mono) so the FinalMux can mix without an extra resample
    pass. If the channel uses a different TTS sample rate the FinalMux
    handles the conversion at the mix stage.
    """

    def compose(
        self,
        spec: Any,  # RenderSpec
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path:
        # Unused-but-required-by-Protocol kwargs.
        _ = spec, sections
        out_path = Path("/tmp") / f"silent_music_{int(narration_duration_s * 1000)}.wav"
        run_ffmpeg([
            "-f", "lavfi",
            "-t", f"{narration_duration_s:.3f}",
            "-i", "anullsrc=r=24000:cl=mono",
            "-c:a", "pcm_s16le",
            str(out_path),
        ])
        return out_path


register_plugin("music", "none", SilentMusic())
assert isinstance(SilentMusic(), MusicComposer)


__all__ = ["SilentMusic"]
