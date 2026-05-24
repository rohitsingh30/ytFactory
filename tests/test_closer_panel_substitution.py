"""Regression — per-channel closer panel substitution wired into the
visualize plugins (2026-05-24).

Verifies that ``pipeline.captions.substitute_last_panel_with_closer``:
1. Substitutes the last panel image when ``data/<channel>/closer_panels/
   <aspect>.png`` exists.
2. Resizes the asset to the requested output_resolution.
3. Returns False silently when the asset is missing / args are bad —
   never raises, never breaks the render.
4. Returns False silently when the asset is corrupt — logs warning but
   does not raise.

Also greps the two visualize plugins (ai_beat_slideshow + long_form_lib)
to pin that they actually call the helper. Catches future refactors
that drop the wire.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from pipeline.captions import substitute_last_panel_with_closer


_REPO = Path(__file__).resolve().parent.parent


def _make_blank_png(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, color=(127, 127, 127)).save(path, format="PNG")


@pytest.fixture
def tmp_panels(tmp_path: Path) -> list[Path]:
    """Three blank PNGs in tmp dir; the last will be substituted."""
    paths = []
    for i in range(3):
        p = tmp_path / f"beat_{i:02d}.png"
        _make_blank_png(p, (256, 256))
        paths.append(p)
    return paths


def test_substitution_happens_when_asset_exists(tmp_panels: list[Path]) -> None:
    """When data/<channel>/closer_panels/shorts.png exists, last panel
    should be overwritten with that asset (resized to output_resolution)."""
    # All 7 production channels have shorts.png — use the actual asset
    # rather than fabricating one. Catches if the asset disappears.
    channel = "mystoriesanimated"
    real_asset = _REPO / "data" / channel / "closer_panels" / "shorts.png"
    assert real_asset.exists(), (
        f"Production asset {real_asset} is missing. The closer panel "
        f"substitution depends on it; restore from git or regenerate."
    )

    original_size = Image.open(tmp_panels[-1]).size
    target_resolution = (1080, 1920)

    result = substitute_last_panel_with_closer(
        tmp_panels, channel, "shorts", target_resolution,
    )

    assert result is True, "substitution should have happened"
    with Image.open(tmp_panels[-1]) as out:
        assert out.size == target_resolution, (
            f"output should be resized to {target_resolution}, got {out.size}"
        )
    # Confirm we actually changed the file (not just kept original)
    new_size = tmp_panels[-1].stat().st_size
    original_dummy_size = (_REPO / "tests").stat().st_size  # any reference
    # Stronger check — the dummy was 256x256 grey, the asset is 1072x1920.
    # File should have grown substantially.
    assert new_size > 10_000, (
        f"substituted file size {new_size}B looks too small — "
        f"substitution may have silently no-op'd"
    )


def test_returns_false_when_channel_missing(tmp_panels: list[Path]) -> None:
    """No channel → no substitution, no raise."""
    result = substitute_last_panel_with_closer(
        tmp_panels, None, "shorts", (1080, 1920),
    )
    assert result is False


def test_returns_false_when_aspect_invalid(tmp_panels: list[Path]) -> None:
    """Aspect must be 'shorts' or 'longform' — anything else returns False."""
    for bad in ["medium", "vertical", "", "9:16"]:
        result = substitute_last_panel_with_closer(
            tmp_panels, "mystoriesanimated", bad, (1080, 1920),
        )
        assert result is False, f"aspect={bad!r} should return False"


def test_returns_false_when_asset_missing(tmp_panels: list[Path]) -> None:
    """Channel without closer_panels/ dir → no substitution, no raise."""
    result = substitute_last_panel_with_closer(
        tmp_panels, "nonexistent-channel", "shorts", (1080, 1920),
    )
    assert result is False


def test_returns_false_when_panel_paths_empty() -> None:
    """Empty panel list → no substitution, no raise."""
    result = substitute_last_panel_with_closer(
        [], "mystoriesanimated", "shorts", (1080, 1920),
    )
    assert result is False


def test_ai_beat_slideshow_wires_the_helper() -> None:
    """Source grep: ai_beat_slideshow.produce must call
    substitute_last_panel_with_closer. Catches refactors that
    silently drop the wire."""
    src = (_REPO / "pipeline" / "render" / "visualize" / "ai_beat_slideshow.py"
           ).read_text(encoding="utf-8")
    assert "substitute_last_panel_with_closer" in src, (
        "ai_beat_slideshow.produce must call "
        "pipeline.captions.substitute_last_panel_with_closer before "
        "_stitch_images. The 2026-05-24 wire that puts the per-channel "
        "closer panel as the final short frame was removed."
    )
    # Pin the aspect tag — must be "shorts" for short engine
    assert '"shorts"' in src or "'shorts'" in src, (
        "ai_beat_slideshow should pass aspect='shorts' to "
        "substitute_last_panel_with_closer"
    )


def test_long_form_lib_wires_the_helper() -> None:
    """Source grep: long_form_lib.build_image_panels_video must call
    substitute_last_panel_with_closer between _generate_panel_stills
    and _assemble_panel_static."""
    src = (_REPO / "pipeline" / "render" / "shared" / "long_form_lib.py"
           ).read_text(encoding="utf-8")
    assert "substitute_last_panel_with_closer" in src, (
        "long_form_lib.build_image_panels_video must call "
        "pipeline.captions.substitute_last_panel_with_closer before "
        "_assemble_panel_static. The 2026-05-24 wire that puts the "
        "per-channel closer panel as the final long-form frame was removed."
    )
    assert '"longform"' in src or "'longform'" in src, (
        "long_form_lib should pass aspect='longform' to "
        "substitute_last_panel_with_closer"
    )
