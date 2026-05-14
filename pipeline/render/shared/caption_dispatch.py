"""Caption-layout dispatch — pure helpers lifted out of legacy shorts.py.

These map the wizard's ``captions_layout`` knob to the
``(caption_mode, caption_max_lines, label)`` tuple the compose stage
consumes. The compose plugin in
``pipeline/render/compose/beat_slideshow_mux.py`` uses this; the
older legacy renderer used to live in
``pipeline/render/_legacy/shorts.py`` but those orchestrator modules
were deleted in the 2026-05-14 bigbang cleanup (see
``docs/post-audit-2026-05-14.md`` and the session plan).

History
-------

Pre-2026-05-14 cleanup, this lived inline in shorts.py. The
extraction is mechanical — the constants + functions are unchanged
from their previous form, just relocated so the legacy orchestrator
modules could be deleted cleanly.
"""
from __future__ import annotations


# Map the form's ``captions_density`` value → caption font size in
# pixels. Word-level captions show one word at a time, so density
# translates to text *weight on screen*: minimal = bigger, dense =
# smaller. The default (when no density is requested) matches
# ``captions.render_word_caption``'s own default of 110 — so
# existing renders without the override are unchanged.
_CAPTIONS_DENSITY_FONT_SIZE: dict[str, int] = {
    "minimal":  130,
    "standard": 110,
    "dense":    90,
}


def _resolve_caption_font_size(cfg: dict) -> int | None:
    """Look up the configured caption font size, or ``None`` for default."""
    density = (cfg.get("captions_density") or "").strip().lower()
    return _CAPTIONS_DENSITY_FONT_SIZE.get(density)


# Map captions_layout (the 2026-05-14 user-facing knob) → compose
# kwargs. Three-tuple: (caption_mode, caption_max_lines,
# friendly_label). ``captions_layout`` is the canonical key; older
# ``captions_density`` entries in cfg are translated where they
# survive in pre-migration sidecars.
_CAPTIONS_LAYOUT_DISPATCH: dict[str, tuple[str, int | None, str]] = {
    "center_word_by_word": ("word", None,  "center · word by word"),
    "bottom_one_line":     ("beat", 1,     "bottom · single line"),
    "bottom_two_line":     ("beat", 2,     "bottom · two lines"),
}


def _resolve_caption_dispatch(cfg: dict) -> tuple[str, int | None, str]:
    """Pick (caption_mode, caption_max_lines, label) from cfg.

    Precedence: cfg[captions_layout] (new wizard knob) → channel YAML
    default → fallback to "center_word_by_word". Unknown values fall
    back to the same default so a stale sidecar never breaks a render.
    """
    layout = (cfg.get("captions_layout") or "").strip().lower()
    return _CAPTIONS_LAYOUT_DISPATCH.get(
        layout, _CAPTIONS_LAYOUT_DISPATCH["center_word_by_word"],
    )


__all__ = [
    "_resolve_caption_dispatch",
    "_resolve_caption_font_size",
    "_CAPTIONS_LAYOUT_DISPATCH",
    "_CAPTIONS_DENSITY_FONT_SIZE",
]
