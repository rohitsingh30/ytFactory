"""Publish stage — auto-generate YouTube metadata + drive the upload.

The one-click publish flow needs the user to only pick visibility; every
other knob (title, description, hashtags, tags, thumbnail, category) is
auto-generated from the rendered script before the modal even opens.

See :mod:`pipeline.upload.metadata_generator` for the generator itself,
and ``control/routes/render_routes.py``'s ``POST /api/jobs/{id}/publish``
+ ``GET /api/jobs/{id}/publish/preview`` for the control-plane surface.

The actual YouTube API call still goes through
:func:`pipeline.upload.upload.youtube_upload` — auth, resumable upload,
quota handling, and idempotency records are unchanged.
"""
from __future__ import annotations

from pipeline.upload.metadata_generator import (
    PublishMetadata,
    generate_publish_metadata,
)

__all__ = ["PublishMetadata", "generate_publish_metadata"]
