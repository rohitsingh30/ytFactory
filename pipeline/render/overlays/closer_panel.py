"""Closer-panel OverlayProducer (CTA card held at the tail of the render).

Activation: ``spec.closer_panel = True`` in the wizard / channel YAML.

The panel is a CTA card composed of the channel's ``closer_format`` string
(e.g. "LIKE if YTA, COMMENT if NTA" — read off ``spec.extra["closer_format"]``
populated by ``spec_enrich.populate_render_extras`` from the channel YAML).

P5.1 (Q72, 2026-05-23): the closer panel was dead-coded in
``pipeline/compose.py`` after the 2026-05-02 caption rework — the new
short-engine path embeds the LIKE/SUBSCRIBE iconography INTO the last
beat's diffusion prompt instead of overlaying it. Some channels still want
the explicit CTA card though (sports docs, kids' rhymes, history); this
overlay re-enables it as an opt-in plugin instead of a default-on layer.

The PNG-rendering itself is a thin wrapper around
``pipeline.captions.render_closer_panel`` so the visual stays consistent
with what every existing channel YAML's ``closer_format`` already expects.
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


class CloserPanel:
    """One full-frame closer CTA panel at the tail of the timeline.

    Reads ``closer_format`` and ``closer_hold_s`` from ``spec.extra``
    (populated by ``spec_enrich.populate_render_extras`` from the channel
    YAML). If neither is present, the producer yields no elements (still
    a no-op for safety; the engine should only invoke it when
    ``spec.closer_panel`` is True).
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        extras = getattr(spec, "extra", {}) or {}
        closer_format = (extras.get("closer_format") or "").strip()
        if not closer_format:
            return []

        try:
            hold_s = float(extras.get("closer_hold_s") or 1.0)
        except (TypeError, ValueError):
            hold_s = 1.0
        hold_s = max(0.2, hold_s)

        if not timeline:
            return []
        tail_end_s = timeline[-1].end_s
        start_s = max(0.0, tail_end_s - hold_s)

        out_dir = audio.narration_path.parent / "closer_panel"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "closer.png"

        try:
            from pipeline.captions import render_closer_panel  # noqa: PLC0415
            render_closer_panel(
                out_path,
                closer_format=closer_format,
                canvas_w=spec.output_resolution[0],
                canvas_h=max(240, spec.output_resolution[1] // 3),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "closer_panel: render_closer_panel failed (%s) — skipping",
                exc,
            )
            return []

        return [OverlayElement(
            start_s=start_s,
            end_s=tail_end_s,
            layer=35,  # above chapter_card (30), below captions (40)
            asset_path=out_path,
            extras={"closer_format": closer_format},
        )]


register_plugin("overlays", "closer_panel", CloserPanel())
