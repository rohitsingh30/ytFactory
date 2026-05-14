"""Anchored foreground footage OverlayProducer (sports_doc shape).

Each Segment in the timeline that has an ``extras['footage_url']``
field becomes a layer-10 OverlayElement carrying the trimmed clip.
The visualize plugin (typically ``footage_filler``) renders the
b-roll background; this overlay producer composites the foreground
match clips on top at their authored anchors.

Plugin activation: ``spec.overlay_timeline = True`` in the wizard.
Pairs with ``spec.visual_mode = footage_filler``.

Today's impl is a delegating wrapper around
``pipeline.render.sports_doc._prep_footage_clip``. Bigbang PR moves
the body inline.
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


class AnchoredFootage:
    """Foreground match-clips composited at authored anchors.

    Reads ``footage_plan`` entries from ``spec.extra['footage_plan_path']``
    OR walks ``timeline`` for Segments with
    ``extras['footage_url']``. Trims each clip via the existing
    sports_doc helper, returns one OverlayElement per clip.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        footage_plan_path = (spec.extra or {}).get("footage_plan_path")
        if not footage_plan_path or not Path(footage_plan_path).exists():
            _logger.info("anchored_footage: no footage_plan_path — no overlays")
            return []

        try:
            import json  # noqa: PLC0415
            from pipeline.render.sports_doc import _prep_footage_clip  # noqa: PLC0415
        except ImportError:
            _logger.warning("anchored_footage: sports_doc helpers unavailable")
            return []

        try:
            plan = json.loads(Path(footage_plan_path).read_text())
        except Exception as exc:  # noqa: BLE001
            _logger.warning("anchored_footage: failed to parse footage_plan: %s", exc)
            return []

        out_dir = audio.narration_path.parent / "anchored_footage"
        out_dir.mkdir(parents=True, exist_ok=True)
        sources_dir = out_dir / "sources"

        elements: list[OverlayElement] = []
        # Walk match_footage + talking_heads + archival_footage blocks.
        for arr_name in ("match_footage", "talking_heads", "archival_footage"):
            for entry in plan.get(arr_name, []):
                start_s = float(entry.get("at_s", 0))
                end_s = start_s + float(entry.get("duration_s", 5))
                cid = entry.get("id", "anchored")
                try:
                    clip_path = _prep_footage_clip(
                        entry, sources_dir, out_dir,
                        spec.output_resolution[0],
                        spec.output_resolution[1],
                        spec.output_fps,
                        None,  # no grade filter — bigbang plumbs from spec
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("anchored_footage: skip %s (%s)", cid, exc)
                    continue
                elements.append(OverlayElement(
                    start_s=start_s,
                    end_s=end_s,
                    layer=10,
                    asset_path=clip_path,
                    extras={"id": cid, "kind": arr_name},
                ))
        return elements


register_plugin("overlays", "anchored_footage", AnchoredFootage())
assert isinstance(AnchoredFootage(), OverlayProducer)


__all__ = ["AnchoredFootage"]
