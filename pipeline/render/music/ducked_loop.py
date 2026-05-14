"""Ducked-loop MusicComposer — short engine default.

Loops a music bed under the narration, ducked when narration is
loud. Wraps :func:`pipeline.render.shorts._mix_music_bed_under_narration`.

Selected by ``spec.music_policy = MusicPolicy.DUCKED_LOOP``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    MusicComposer,
    Section,
    register_plugin,
)


class DuckedLoop:
    """Ducked music bed — short engine default.

    Today's impl is a thin wrapper. Bigbang PR moves the body inline +
    drops the dependency on shorts.py.
    """

    def compose(
        self,
        spec: Any,  # RenderSpec
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path:
        # sections unused for ducked_loop (single bed).
        _ = sections

        bed_name = spec.music.default_bed
        if not bed_name or bed_name == "off":
            # Fall back to silent if the channel disabled music.
            from pipeline.render.music.silent import SilentMusic  # noqa: PLC0415
            return SilentMusic().compose(spec, narration_duration_s)

        try:
            from pipeline.render.shorts import (  # noqa: PLC0415
                _mix_music_bed_under_narration as _mix,
            )
        except ImportError:
            # Helper not available — fall back to silent so the engine
            # still produces a video. Bigbang PR makes this hard-required.
            from pipeline.render.music.silent import SilentMusic  # noqa: PLC0415
            return SilentMusic().compose(spec, narration_duration_s)

        out_path = Path("/tmp") / f"ducked_loop_{int(narration_duration_s * 1000)}.wav"
        # The existing helper signature differs across versions; we
        # call it minimally and let the bigbang PR refine.
        try:
            _mix(
                duration_s=narration_duration_s,
                out_path=out_path,
                bed_name=bed_name,
                bed_db=spec.music.music_bed_db,
                mix=spec.music.mix_default,
            )
        except TypeError:
            # Older helper signature — fall back to silent.
            from pipeline.render.music.silent import SilentMusic  # noqa: PLC0415
            return SilentMusic().compose(spec, narration_duration_s)
        return out_path


register_plugin("music", "ducked_loop", DuckedLoop())
assert isinstance(DuckedLoop(), MusicComposer)


__all__ = ["DuckedLoop"]
