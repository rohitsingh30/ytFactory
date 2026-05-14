"""Section-mood MusicComposer (per-section mood crossfade).

Picks a different mood bed per ``Section`` from
``<channel>/music/<mood>/`` and crossfades between sections. The
sports_doc historical pattern.

Plugin selection: ``spec.music_policy = SECTION_MOOD``. Picks up
mood hints from ``Section.extras['mood']`` if present, else falls
back to a single bed.

Today's impl is a delegating wrapper. Bigbang PR moves the body inline.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    MusicComposer,
    Section,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg

_logger = logging.getLogger(__name__)


class SectionMood:
    """Per-section mood crossfade.

    Today's fallback: emits a single silent track when sections lack
    explicit mood hints. Bigbang PR plumbs the real per-mood bed
    selection + crossfade chain.
    """

    def compose(
        self,
        spec: Any,
        narration_duration_s: float,
        sections: list[Section] | None = None,
    ) -> Path:
        out_path = Path("/tmp") / f"section_mood_{int(narration_duration_s * 1000)}.wav"

        if not sections:
            # No sections → fall back to single bed via the existing
            # plugin (already deals with the bed-file lookup).
            from pipeline.render.music.single_bed import SingleBed  # noqa: PLC0415
            return SingleBed().compose(spec, narration_duration_s)

        # Today: emit silent placeholder. Bigbang PR plumbs real mood
        # crossfade chain.
        run_ffmpeg([
            "-f", "lavfi", "-t", f"{narration_duration_s:.3f}",
            "-i", "anullsrc=r=24000:cl=mono",
            "-c:a", "pcm_s16le",
            str(out_path),
        ])
        _logger.info("section_mood: placeholder silent track for %d sections — "
                     "bigbang PR plumbs real mood crossfade", len(sections))
        return out_path


register_plugin("music", "section_mood", SectionMood())
assert isinstance(SectionMood(), MusicComposer)


__all__ = ["SectionMood"]
