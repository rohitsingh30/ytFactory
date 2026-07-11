"""Visualize plugin family — :class:`pipeline.render.contracts.VisualProducer` impls.

Each module implements VisualProducer. Engine picks based on
``spec.visual_mode``:

* ``ai_beat_slideshow``    → :mod:`.ai_beat_slideshow` (Flux cloud)
* ``longform_panels``      → :mod:`.longform_panels` (Flux cloud)
* ``footage_filler``       → :mod:`.footage_filler` (b-roll cycle, paired
                              with overlays.anchored_footage)

Other visual_modes (motion_clips, hybrid_beat_footage) get added as
new modules register themselves.

For tests, :mod:`.visuals_from_fixture` loads a pre-rendered mp4 from
disk so engine goldens don't depend on Flux cloud.
"""
from __future__ import annotations

from . import visuals_from_fixture  # noqa: F401
from . import longform_panels  # noqa: F401
from . import ai_beat_slideshow  # noqa: F401
from . import footage_windows  # noqa: F401
from . import footage_filler  # noqa: F401
from . import archival_shotlist  # noqa: F401
