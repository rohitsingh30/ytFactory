"""Per-image deterministic validators.

Cheap gates that catch common image-gen failures BEFORE the vision LLM
judge spends a token: blank/black frames, all-white frames (NSFW
filter blanks), file-too-tiny (broken stream), aspect-ratio mismatch.

Mirrors :mod:`pipeline.llm.script_check`, :mod:`pipeline.llm.cast_lint`,
and :mod:`pipeline.llm.prompt_lint`. Issues this validator emits are
stage-routed by :mod:`pipeline.llm.fix_router` to the ``images`` stage
with a per-beat ``sub_index`` so the orchestrator's cache invalidates
ONLY that beat's image.

Heavier checks (cast-lock drift, beat-text mismatch, anatomy
gibberish) live in :mod:`pipeline.llm.judges.image_judge` because
they need vision.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pipeline import observability as _obs


# Pixel sentinels — black/white frames usually mean the GPU service
# failed silently or the safety filter blanked the output.
_BLACK_LUMA_THRESHOLD = 8.0     # 0–255; <8 == effectively black
_WHITE_LUMA_THRESHOLD = 247.0   # >247 == effectively white
_MIN_FILE_BYTES = 5_000          # PNGs smaller than this are corrupt /
                                  # generation-failed sentinels


@dataclass(frozen=True)
class ImageIssue:
    severity: str          # "error" | "warning"
    code: str
    message: str
    target_path: str       # 'beats[N].image'


@_obs.traced("llm.image_lint.check_image", category="image",
             capture=["beat_index", "expected_aspect"])
def check_image(
    image_path: Path,
    *,
    beat_index: int,
    expected_aspect: str = "9:16",
) -> list[ImageIssue]:
    """Run every deterministic gate. Returns ALL issues.

    Args:
        image_path: PNG on disk.
        beat_index: For ``target_path`` so the orchestrator's cache
            invalidates only this beat.
        expected_aspect: ``"9:16"`` for Shorts, ``"16:9"`` for long-form.
    """
    issues: list[ImageIssue] = []
    target_path = f"beats[{beat_index}].image"

    if not image_path.exists():
        issues.append(ImageIssue(
            severity="error", code="image_missing",
            message=f"image file does not exist: {image_path}",
            target_path=target_path,
        ))
        return issues

    size = image_path.stat().st_size
    if size < _MIN_FILE_BYTES:
        issues.append(ImageIssue(
            severity="error", code="image_too_small",
            message=(f"image is {size} bytes (<{_MIN_FILE_BYTES}) — "
                     f"likely a corrupt response or generation failure"),
            target_path=target_path,
        ))
        return issues   # pixel checks pointless on a broken file

    pixel_issues = _pixel_gates(image_path, beat_index, target_path,
                                 expected_aspect=expected_aspect)
    issues.extend(pixel_issues)
    return issues


def _pixel_gates(
    image_path: Path,
    beat_index: int,
    target_path: str,
    *,
    expected_aspect: str,
) -> list[ImageIssue]:
    """Open the image and run pixel-level checks. Lazy-imports PIL so
    this module is cheap to import on hot paths that don't actually
    open files.
    """
    issues: list[ImageIssue] = []
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        # Without PIL we can't do pixel checks; that's fine — file-size
        # gate already caught the gross failures.
        return issues

    try:
        with Image.open(image_path) as im:
            im.load()
            w, h = im.size
            # Aspect check.
            actual = f"{w}:{h}"
            if expected_aspect == "9:16" and w >= h:
                issues.append(ImageIssue(
                    severity="warning", code="image_aspect_wrong",
                    message=(f"expected 9:16 (portrait) but image is "
                             f"{w}x{h} ({actual}) — landscape Short "
                             f"will be letterboxed"),
                    target_path=target_path,
                ))
            elif expected_aspect == "16:9" and h > w:
                issues.append(ImageIssue(
                    severity="warning", code="image_aspect_wrong",
                    message=(f"expected 16:9 (landscape) but image is "
                             f"{w}x{h} ({actual})"),
                    target_path=target_path,
                ))
            # Luminance check (downsample for speed).
            mini = im.convert("L").resize((32, 32))
            pixels = list(mini.getdata())
            avg = sum(pixels) / len(pixels)
            if avg < _BLACK_LUMA_THRESHOLD:
                issues.append(ImageIssue(
                    severity="error", code="image_blank_black",
                    message=(f"image is effectively black (avg luma "
                             f"{avg:.1f}/255) — generation likely failed "
                             f"silently or returned a sentinel"),
                    target_path=target_path,
                ))
            elif avg > _WHITE_LUMA_THRESHOLD:
                issues.append(ImageIssue(
                    severity="error", code="image_blank_white",
                    message=(f"image is effectively white (avg luma "
                             f"{avg:.1f}/255) — likely the safety "
                             f"filter blanked the output, or the model "
                             f"emitted an empty canvas"),
                    target_path=target_path,
                ))
    except Exception as e:  # noqa: BLE001
        issues.append(ImageIssue(
            severity="error", code="image_open_failed",
            message=f"failed to open image with PIL: {e}",
            target_path=target_path,
        ))
    return issues


# ---------------------------------------------------------------------------
# Convenience for contracts
# ---------------------------------------------------------------------------

ERROR_CODES: frozenset[str] = frozenset({
    "image_missing", "image_too_small", "image_blank_black",
    "image_blank_white", "image_open_failed",
})


def errors(issues: Iterable[ImageIssue]) -> list[ImageIssue]:
    return [i for i in issues if i.severity == "error"]


def warnings(issues: Iterable[ImageIssue]) -> list[ImageIssue]:
    return [i for i in issues if i.severity == "warning"]
