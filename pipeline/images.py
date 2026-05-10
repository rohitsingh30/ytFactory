"""Compatibility shim — re-exports from ``pipeline.images.images``.

This file existed historically as the flat 1339-line module containing
the legacy local image providers (sdxl_lightning, mflux Flux Schnell,
mflux Z-Image-Turbo). After the 2026-05-09 nuclear cleanup the
canonical implementation moved to ``pipeline/images/images.py``
(cloud-only, 798 lines), but the flat copy was left on disk diverged.

Python's import machinery prefers the package over a same-name flat
module, so ``from pipeline import images`` now resolves to
``pipeline/images/__init__.py`` (which itself re-exports from
``pipeline.images.images``). This file is kept only as a redirect for
out-of-tree code that imported the flat path directly via
``importlib`` or sys.path tricks.

Symmetric to ``pipeline/images_cloudrun.py`` — see that module for the
matching shim that redirects to ``pipeline.images.images_cloudrun``.
"""

from pipeline.images.images import *  # noqa: F401, F403
from pipeline.images.images import (  # noqa: F401
    beat_to_prompt,
    build_full_prompt,
    generate,
    lint_prompt,
    load_prompts,
    reset_image_state,
    strip_text_bait,
    validate_provider_config,
    warmup,
)
