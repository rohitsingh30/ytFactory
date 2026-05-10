"""Public package shim — re-exports everything from ``pipeline.images.images``.

Pre-fix (2026-05-10): this ``__init__.py`` was empty. ``pipeline.images``
existed as both a flat module (``pipeline/images.py`` — 1339 lines,
legacy local providers) AND a package (``pipeline/images/`` — newer,
cloud-only). Python's import machinery prefers the package over a
same-name flat module, so ``from pipeline import images`` resolved to
this empty ``__init__.py`` — every ``images.generate()`` /
``images.warmup()`` / ``images.load_prompts()`` call in the renderer
would have raised ``AttributeError`` on the laptop path, and the
cloud render-worker only worked because it shells out to a fresh
subprocess that re-imports things differently. **Symmetric bug to
``pipeline.images_cloudrun``** — see ``pipeline/images_cloudrun.py``
for the matching shim that redirects to ``pipeline.images.images_cloudrun``.

The resolution: re-export the package's ``images`` submodule's full
public API at package level via ``__getattr__`` so each access
re-resolves against the live submodule. Static ``from`` imports would
capture the function object at import time and ``patch.object(
pipeline.images.images, 'generate', ...)`` in tests would never take
effect via the package binding.

The flat ``pipeline/images.py`` (legacy 1339-line local-provider
version) stays on disk for now as a redirect shim — same content as
this ``__init__.py`` — to keep historical bytecode caches and any
out-of-tree importers from breaking. Both files import the SAME
canonical implementation; there is no longer a split-brain.
"""

from pipeline.images import images as _impl_mod


_PUBLIC = (
    "beat_to_prompt",
    "build_full_prompt",
    "generate",
    "lint_prompt",
    "load_prompts",
    "reset_image_state",
    "strip_text_bait",
    "validate_provider_config",
    "warmup",
)


def __getattr__(name: str):
    """Lazy re-export. Each access re-resolves against the live
    ``pipeline.images.images`` submodule so ``unittest.mock.patch.object``
    targeting that submodule is honoured by callers that imported
    ``from pipeline import images``.
    """
    if name in _PUBLIC:
        return getattr(_impl_mod, name)
    raise AttributeError(f"module 'pipeline.images' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_PUBLIC))

