"""Lower-third OverlayProducer (speaker name + handle).

Generates one PNG per talking-head segment (or per anchored
foreground footage clip) showing the speaker's name + social handle.
Default style: deep-teal slab + orange accent + bold white text
(matches sports_doc historical aesthetic; channels override via
``spec.lower_third`` config).

Plugin activation: ``spec.lower_thirds = True`` in the wizard.

Today's impl is a delegating wrapper around the existing
sports_doc helper. Bigbang PR moves the body inline.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)

_logger = logging.getLogger(__name__)


class LowerThird:
    """One layer-20 OverlayElement per Segment carrying speaker
    metadata.

    Pulls speaker info from ``Segment.text`` (when it parses as
    ``"Speaker Name | @handle"``) or skips the Segment otherwise.
    The bigbang PR adds a richer schema where Segments carry
    explicit speaker/handle fields.

    Style defaults read from ``spec.lower_third`` config — channels
    can override per-render.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        try:
            from pipeline.render.sports_doc import _render_lower_third  # noqa: PLC0415
        except ImportError:
            _logger.warning("lower_third: sports_doc helper unavailable — "
                            "no lower-thirds emitted")
            return []

        out_dir = audio.narration_path.parent / "lower_thirds"
        out_dir.mkdir(parents=True, exist_ok=True)

        elements: list[OverlayElement] = []
        for i, seg in enumerate(timeline):
            speaker, handle = self._parse_speaker(seg.text)
            if not speaker:
                continue
            png_path = out_dir / f"lt_{i:03d}.png"
            try:
                _render_lower_third(
                    speaker=speaker,
                    handle=handle,
                    out_path=png_path,
                    bg_rgba=spec.lower_third.bg_rgba,
                    text_rgba=spec.lower_third.text_rgba,
                    accent_rgba=spec.lower_third.accent_rgba,
                    font_size=spec.lower_third.font_size,
                    handle_font_size=spec.lower_third.handle_font_size,
                )
            except Exception:  # noqa: BLE001
                continue
            hold = max(seg.end_s - seg.start_s, spec.lower_third.hold_min_s)
            elements.append(OverlayElement(
                start_s=seg.start_s,
                end_s=seg.start_s + hold,
                layer=20,
                asset_path=png_path,
                extras={"speaker": speaker, "handle": handle},
            ))
        return elements

    def _parse_speaker(self, text: str) -> tuple[str, str]:
        """Parse ``"Name | @handle"`` from segment text. Returns
        ``("", "")`` if the format doesn't match."""
        if "|" not in text:
            return "", ""
        name, handle = text.split("|", 1)
        return name.strip(), handle.strip()


register_plugin("overlays", "lower_third", LowerThird())
assert isinstance(LowerThird(), OverlayProducer)


__all__ = ["LowerThird"]
