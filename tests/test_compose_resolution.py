"""Regression tests for ``pipeline.compose`` aspect parameterisation
(Slice 3 of the unified-renderer rollout, plan.md / 2026-05-12).

Pre-Slice-3 ``pipeline.compose`` had module-level ``WIDTH=1080``,
``HEIGHT=1920``, ``FPS=30`` constants that were threaded into every
ffmpeg filter chain. That made it impossible for the unified renderer
to ask compose for a 16:9 1920×1080 long-form mp4 — even after the
spec said so, compose would emit ``s=1080x1920`` and ``-r 30``
regardless.

These tests pin the new contract: every public compose function takes
a ``Resolution`` kwarg and bakes its width/height/fps into the
emitted ffmpeg command. Tested via the pure-string filter helpers
(``_punch_drift_zoom_filter``, ``_clip_filter``) so we don't have to
invoke ffmpeg in a unit test.
"""
from __future__ import annotations

import pytest

from pipeline.compose import (
    Resolution,
    _DEFAULT_RES,
    _clip_filter,
    _punch_drift_zoom_filter,
)


# ---------------------------------------------------------------------------
# Resolution dataclass
# ---------------------------------------------------------------------------


def test_resolution_default_is_shorts():
    res = Resolution()
    assert res.width == 1080
    assert res.height == 1920
    assert res.fps == 30


def test_resolution_class_methods():
    assert Resolution.shorts() == Resolution(1080, 1920, 30)
    assert Resolution.long_form() == Resolution(1920, 1080, 30)


def test_resolution_from_cfg_empty():
    """No cfg entry → Shorts default. Channel YAMLs that don't set
    output_resolution don't get accidentally promoted to long-form."""
    assert Resolution.from_cfg(None) == Resolution.shorts()
    assert Resolution.from_cfg({}) == Resolution.shorts()


def test_resolution_from_cfg_long_form():
    cfg = {"output_resolution": [1920, 1080], "output_fps": 30}
    assert Resolution.from_cfg(cfg) == Resolution(1920, 1080, 30)


def test_resolution_from_cfg_custom_fps():
    cfg = {"output_resolution": [1080, 1920], "output_fps": 24}
    assert Resolution.from_cfg(cfg).fps == 24


def test_resolution_from_cfg_malformed_falls_back():
    """A garbage output_resolution should NOT crash the render — fall
    back to the Shorts default with the cfg silently corrected. The
    unified renderer's ``RenderSpec.notes`` would surface the issue
    upstream; compose itself is forgiving."""
    assert Resolution.from_cfg({"output_resolution": "not-a-list"}) == Resolution.shorts()
    assert Resolution.from_cfg({"output_resolution": [1080]}) == Resolution.shorts()


# ---------------------------------------------------------------------------
# _punch_drift_zoom_filter — the central place WIDTH/HEIGHT/FPS leaked
# ---------------------------------------------------------------------------


def test_zoom_default_emits_shorts_dimensions():
    f = _punch_drift_zoom_filter(2.0, beat_index=0)
    # Pre-Slice-3 hardcoded "s=1080x1920".
    assert "s=1080x1920" in f
    assert "fps=30" in f


def test_zoom_long_form_emits_landscape_dimensions():
    f = _punch_drift_zoom_filter(2.0, beat_index=0, resolution=Resolution.long_form())
    assert "s=1920x1080" in f
    assert "fps=30" in f


def test_zoom_custom_fps_threaded_through():
    f = _punch_drift_zoom_filter(
        2.0, beat_index=0, resolution=Resolution(1920, 1080, 24),
    )
    assert "fps=24" in f


def test_zoom_pre_scale_doubles_long_dimension():
    """The pre-scale before the zoom needs to be ~2× the larger output
    dim. Pre-Slice-3 it was hardcoded scale=2400:-1 (assumes
    portrait). Now it picks max(w, h) × 2 so landscape gets a 3840-wide
    intermediate instead of being height-truncated."""
    short_filter = _punch_drift_zoom_filter(2.0, 0, resolution=Resolution.shorts())
    long_filter = _punch_drift_zoom_filter(2.0, 0, resolution=Resolution.long_form())
    # 9:16 → max(1080,1920)*2 = 3840
    assert "scale=3840:-1" in short_filter
    # 16:9 → max(1920,1080)*2 = 3840 (same since long_form is just rotated)
    assert "scale=3840:-1" in long_filter


# ---------------------------------------------------------------------------
# _clip_filter (used by compose_clips + compose_hybrid)
# ---------------------------------------------------------------------------


def test_clip_filter_default_shorts():
    f = _clip_filter(2.0)
    assert "scale=1080:1920" in f
    assert "crop=1080:1920" in f
    assert "fps=30" in f


def test_clip_filter_long_form():
    f = _clip_filter(2.0, resolution=Resolution.long_form())
    assert "scale=1920:1080" in f
    assert "crop=1920:1080" in f
    assert "fps=30" in f


# ---------------------------------------------------------------------------
# Module-level legacy constants are still importable (back-compat) and
# match the Shorts default.
# ---------------------------------------------------------------------------


def test_legacy_module_constants_match_shorts_default():
    """Tests + tools that import WIDTH/HEIGHT/FPS directly should
    keep working. They've been demoted to "legacy aliases of
    Resolution.shorts()" but still exist."""
    from pipeline import compose
    assert compose.WIDTH == _DEFAULT_RES.width == 1080
    assert compose.HEIGHT == _DEFAULT_RES.height == 1920
    assert compose.FPS == _DEFAULT_RES.fps == 30


def test_word_caption_y_frac_is_lower_third():
    """Per the 2026-05-13 audit, the previous default of 0.45
    (vertical-centre) put captions on the character's belt buckle
    — invisible on the cake-AITA / Ronaldinho / Baghdad-Mongols
    Shorts. Pin the new lower-third standard so the same regression
    doesn't get reintroduced. See
    docs/pipeline_bug_catalogue_v2_2026-05-14.html.
    """
    from pipeline import compose
    # Lower-third = roughly y ∈ [0.66, 0.85]. Strict bounds so a
    # casual edit doesn't slip the value back to the broken centre.
    assert 0.66 <= compose._WORD_CAPTION_Y_FRAC <= 0.85, (
        f"_WORD_CAPTION_Y_FRAC={compose._WORD_CAPTION_Y_FRAC} "
        f"is not in the lower-third band [0.66, 0.85]; if you intend "
        f"to revert to the legacy 0.45 vertical-centre, justify in "
        f"the commit message + update this test"
    )
    # And specifically NOT the legacy value.
    assert compose._WORD_CAPTION_Y_FRAC != 0.45, (
        "0.45 was the broken default that put captions on the "
        "character's mid-section / belt buckle"
    )


def test_word_caption_y_pixel_position_matches_lower_third():
    """End-to-end pin: at the standard 1920-tall 9:16 frame, the
    caption overlay y-coordinate must land in the lower-third pixel
    band [1267, 1632]. Catches off-by-one math errors when the
    constant is read at compose time."""
    from pipeline import compose
    res = Resolution()
    word_y = int(res.height * compose._WORD_CAPTION_Y_FRAC)
    assert 1267 <= word_y <= 1632, (
        f"caption y={word_y}px on a 1920-tall frame is not "
        f"lower-third (expected [1267, 1632])"
    )
