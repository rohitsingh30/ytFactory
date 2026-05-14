"""Music plugin family — :class:`pipeline.render.contracts.MusicComposer` impls.

Each module implements MusicComposer. Engine picks by
``spec.music_policy`` (a :class:`pipeline.render.spec.MusicPolicy` enum):

* ``ducked_loop``   → :mod:`.ducked_loop` (loop bed under narration with
                       sidechain duck)
* ``single_bed``    → :mod:`.single_bed` (loop one ambient track for the
                       full duration, no ducking)
* ``section_mood``  → :mod:`.section_mood` (per-section mood crossfade)
* ``none``          → :mod:`.silent` (silent wav of duration_s)
"""
from __future__ import annotations

from . import silent  # noqa: F401
from . import single_bed  # noqa: F401
from . import ducked_loop  # noqa: F401
from . import section_mood  # noqa: F401
