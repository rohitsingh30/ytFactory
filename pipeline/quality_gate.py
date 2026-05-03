"""Render-time image quality gate.

Catches obviously-broken Flux/SDXL outputs (all-black, all-blank,
ghost-doubled bodies that left the image with low edge density) before
they ship through to compose. The orchestrator retries with a bumped
seed up to a configurable max.

The checks are deliberately cheap and conservative — we want to reject
*obvious* failures (model crashed, blank canvas, total mush) without
false-positiving on legitimate flat-style cartoon art.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


# OCR is optional — pytesseract requires brew tesseract, easyocr requires
# torch + a 200MB model bundle. The text-artefact gate is feature-flagged
# off when neither is available; channels can opt-in via
# `quality_gate_ocr: true` once an OCR backend is on disk.
#
# Critic 2026-05-02: the v0 ranked sports Short rendered:
#   - "Aguero / Asurmu / (0)" garbled letters on a soccer ball
#   - "ADRDES" on a jersey back
#   - "1.4!" in a closer-character speech bubble
# These all pass the existing stddev / edge-density / luminance gates
# because they're high-detail. OCR-based rejection catches the class.
try:
    import pytesseract  # type: ignore
    _OCR_BACKEND = "tesseract"
except ImportError:
    pytesseract = None  # type: ignore[assignment]
    _OCR_BACKEND = None


# Conservative thresholds. The crayon-style channel intentionally has
# big flat color regions, so brightness variance can be lower than for
# photorealistic art — these are tuned for the AITA aesthetic.
MIN_FILE_SIZE_BYTES = 30 * 1024  # 30 KB — anything smaller is suspect
MIN_PIXEL_STDDEV = 12.0          # average channel std-dev across the image
MIN_EDGE_DENSITY = 0.005         # share of pixels above edge threshold

# Mean and median luminance floors (0–255). Critic finding 2026-05:
# 5 of 23 beats in one Short rendered near-black even though edge
# density and stddev passed — they had a thin outline character on a
# dim background, so the original two checks didn't catch them. Mean
# luminance + a P75 percentile check force "the image is mostly dark"
# regressions to fail the gate. Tuned for pastel-bright channels; a
# legit-night channel can lower these via channel YAML
# (`image_min_mean_luminance`, `image_min_p75_luminance`).
MIN_MEAN_LUMINANCE = 80.0   # mean over all pixels
MIN_P75_LUMINANCE = 130.0   # 75th-percentile pixel — tolerates a small
                            # dim character on a bright bg, but rejects
                            # a bright character on a dim/black bg.


def _has_text_artefact(img: Image.Image, max_chars: int = 4) -> tuple[bool, str]:
    """Use Tesseract OCR to detect rendered text on the image.

    Returns ``(found, snippet)``. Skips silently if no OCR backend is
    available (returns ``(False, "")``). Threshold ``max_chars`` is
    permissive — diffusion models occasionally render 1-2 stray
    glyph-like marks that don't read as text to humans, but a confident
    word-length string on a prop is an artefact.
    """
    if not _OCR_BACKEND or pytesseract is None:
        return False, ""
    try:
        # config: PSM 11 = sparse text, no orientation detection.
        # Strip non-printable + collapse whitespace before measuring.
        raw = pytesseract.image_to_string(img, config="--psm 11")
    except Exception:
        return False, ""
    cleaned = "".join(c for c in raw if c.isalnum() or c.isspace()).strip()
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_chars:
        return True, cleaned[:60]
    return False, ""


def check_image(
    path: Path,
    *,
    expected_w: int | None = None,
    expected_h: int | None = None,
    min_file_size: int = MIN_FILE_SIZE_BYTES,
    min_stddev: float = MIN_PIXEL_STDDEV,
    min_edge_density: float = MIN_EDGE_DENSITY,
    min_mean_luminance: float = MIN_MEAN_LUMINANCE,
    min_p75_luminance: float = MIN_P75_LUMINANCE,
    reject_text_artefacts: bool = False,
    max_text_chars: int = 4,
) -> tuple[bool, str]:
    """Return (ok, reason). reason is empty on pass, a short string on fail.

    ``reject_text_artefacts`` (default False): when True AND an OCR
    backend is installed, rejects images with detected text > max_text_chars.
    Channel YAML opts in via ``quality_gate_ocr: true``. No-op on
    backends that are guidance-distilled and frequently emit gibberish
    text (rejection rate becomes too high) — start with sports-ranked.
    """
    if not path.exists():
        return False, "file does not exist"

    size = path.stat().st_size
    if size < min_file_size:
        return False, f"file too small ({size} < {min_file_size} bytes)"

    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        return False, f"could not open image: {e}"

    if expected_w and img.width != expected_w:
        return False, f"width {img.width} != expected {expected_w}"
    if expected_h and img.height != expected_h:
        return False, f"height {img.height} != expected {expected_h}"

    # Drop alpha if present, so std-dev / edge stats are comparable.
    if img.mode != "RGB":
        img = img.convert("RGB")

    stat = ImageStat.Stat(img)
    avg_stddev = sum(stat.stddev) / len(stat.stddev)
    if avg_stddev < min_stddev:
        return False, (
            f"image is nearly flat (avg channel stddev {avg_stddev:.1f} "
            f"< {min_stddev}) — likely all-black/all-blank Flux output"
        )

    # Luminance gate. A "dark frame" regression (where the model emits a
    # near-black scene with only character outlines visible) passes the
    # stddev + edge checks because the outlines provide both — but it
    # still reads as "the video glitched" to viewers. Mean luminance
    # catches uniformly-dark frames; the P75 catches "bright character
    # on a black bg" (which has high stddev but low overall brightness).
    luma = img.convert("L")
    luma_pixels = list(luma.getdata())
    mean_luminance = sum(luma_pixels) / max(1, len(luma_pixels))
    if mean_luminance < min_mean_luminance:
        return False, (
            f"mean luminance {mean_luminance:.1f} < {min_mean_luminance} "
            f"— image is too dark (likely the dark-frame regression)"
        )
    sorted_luma = sorted(luma_pixels)
    p75 = sorted_luma[int(0.75 * (len(sorted_luma) - 1))]
    if p75 < min_p75_luminance:
        return False, (
            f"P75 luminance {p75} < {min_p75_luminance} — image is "
            f"predominantly dim (>25% of pixels darker than expected)"
        )

    # Edge density via Pillow's built-in FIND_EDGES (Sobel-ish).
    edges = luma.filter(ImageFilter.FIND_EDGES)
    edge_pixels = sum(1 for p in edges.getdata() if p > 30)
    edge_density = edge_pixels / (img.width * img.height)
    if edge_density < min_edge_density:
        return False, (
            f"edge density {edge_density:.4f} < {min_edge_density} — "
            f"image looks like total mush"
        )

    if reject_text_artefacts:
        found, snippet = _has_text_artefact(img, max_chars=max_text_chars)
        if found:
            return False, (
                f"text artefact detected on image (OCR read {snippet!r}) "
                f"— likely garbled text on a prop / jersey / speech bubble"
            )

    return True, ""
