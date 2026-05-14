"""Overlays plugin family — :class:`pipeline.render.contracts.OverlayProducer` impls.

OverlayProducer is the LIST-VALUED slot — the engine collects every
active producer (driven by spec flags) and concatenates the returned
``list[OverlayElement]`` lists. Compose plugins stack them by
``(layer, start_s)`` regardless of which producer contributed which
element.

Active producer set = function of spec:

* :mod:`.word_caption_pngs` — when
  ``spec.captions_enabled and spec.captions_layout == CENTER_WORD_BY_WORD``
* :mod:`.sentence_caption_ass` — when
  ``spec.captions_enabled and spec.captions_layout in {BOTTOM_ONE_LINE, BOTTOM_TWO_LINE}``
* :mod:`.lower_third` — when ``spec.lower_thirds == True``
* :mod:`.chapter_card` — when ``spec.chapter_cards == True``
* :mod:`.anchored_footage` — when ``spec.overlay_timeline == True``

Adding a new overlay kind = add a module here that registers a
Protocol-conforming impl AND add a spec flag (in
``pipeline/render/spec.py``) that the engine consults to decide
whether to activate it. Engines themselves never grow new code paths.
"""
from __future__ import annotations

from . import noop  # noqa: F401
from . import sentence_caption_ass  # noqa: F401
from . import word_caption_pngs  # noqa: F401
