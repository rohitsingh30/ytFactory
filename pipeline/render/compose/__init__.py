"""Compose plugin family — :class:`pipeline.render.contracts.FinalMux` impls.

Layout-agnostic — a FinalMux stacks the visual track + every overlay
element + audio + music, regardless of which producers contributed.

* :mod:`.beat_slideshow_mux` — short engine final mux
* :mod:`.section_video_mux`  — long engine final mux (handles overlay
                                 foreground footage as a special case via
                                 the OverlayElement layer convention)
"""
from __future__ import annotations

from . import beat_slideshow_mux  # noqa: F401
from . import section_video_mux  # noqa: F401
