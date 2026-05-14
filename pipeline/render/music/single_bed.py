"""Single-bed MusicComposer — picked by ``spec.music_policy = SINGLE_BED``.

Loops one music bed under the full narration duration with no
ducking — the long-form sleep history pattern. Bed file is read from
``spec.music.default_bed`` under ``<channel>/music/<bed>.mp3``.

Delegates to ``pipeline.render.long_form.build_music_bed`` for the
ffmpeg chain — that helper has the channel YAML lookup + loudness
normalisation already wired up. The bigbang PR moves the body inline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    MusicComposer,
    Section,
    register_plugin,
)


class SingleBed:
    """Loop one ambient bed for the full duration, no ducking.

    Defaults match long_form.py's historical behavior — same bed
    file lookup, same loudness target. Channels that want a different
    bed override ``spec.music.default_bed`` in their long defaults.
    """

    def compose(
        self,
        spec: Any,  # RenderSpec
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path:
        # Unused-but-required-by-Protocol arg.
        _ = sections
        from pipeline.render.long_form import build_music_bed  # noqa: PLC0415

        out_path = Path("/tmp") / f"music_bed_{int(narration_duration_s * 1000)}.wav"
        # build_music_bed signature today: (out_path, duration_s) — it
        # reads the bed file from a channel-cfg lookup that we don't
        # have here. The bigbang PR generalises this to take spec.music
        # so the function is fully channel-agnostic; for now we shim.
        build_music_bed(out_path, narration_duration_s)
        return out_path


register_plugin("music", "single_bed", SingleBed())
assert isinstance(SingleBed(), MusicComposer)


__all__ = ["SingleBed"]
