"""No-op OverlayProducer.

Always returns an empty list. Useful as the engine's fallback when a
spec field's value doesn't map to any registered overlay plugin
(``spec.captions_enabled = False`` selects this for the captions
slot, for example), and as a Protocol-conformance smoke test so
``import pipeline.render.overlays`` always succeeds.

Bigbang PR adds the real overlay impls (``word_caption_pngs``,
``sentence_caption_ass``, ``lower_third``, ``chapter_card``,
``anchored_footage``). Each is a single-file edit + a spec-flag
check in the engine.
"""
from __future__ import annotations

from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)


class NoopOverlay:
    """Always returns ``[]`` — no overlay elements contributed.

    This isn't just a placeholder: it's the plugin the engine
    selects when a spec flag (e.g. ``captions_enabled = False``)
    means "skip this overlay slot". Keeps the engine's dispatch
    loop uniform — every spec flag → some plugin name → some impl,
    even when the answer is "nothing to overlay".
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        return []


register_plugin("overlays", "noop", NoopOverlay())
assert isinstance(NoopOverlay(), OverlayProducer)


__all__ = ["NoopOverlay"]
