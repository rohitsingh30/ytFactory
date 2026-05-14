"""Timeline plugin family — :class:`pipeline.render.contracts.TimelineBuilder` impls.

Each module here implements the TimelineBuilder Protocol. The engine
picks based on engine kind (and per-render TimelineBuilder config):

* short engine → :mod:`.asr_beats` (cloud whisper, group words into beats)
* long engine  → :mod:`.asr_anchors` (cloud whisper, anchor authored
  section/chapter titles to the actual narration)

For tests, :mod:`.timeline_from_fixture` loads a pre-recorded JSON
from disk so engine goldens don't depend on the cloud whisper service.
"""
from __future__ import annotations

from . import asr_beats  # noqa: F401
from . import asr_anchors  # noqa: F401
from . import timeline_from_fixture  # noqa: F401
