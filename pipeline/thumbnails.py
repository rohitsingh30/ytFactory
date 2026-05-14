"""Thumbnail composer — turn a rendered Short into a catchy YouTube
thumbnail before upload.

Bare scene frames (img_NN.png) don't pop in the channel grid: they're
flat illustrations without visual hierarchy or text payload. A real
Shorts thumbnail needs three things stacked on the same image:

1. A *moment* — an expressive scene frame, picked for emotional weight.
2. A *headline* — 2-5 punchy words that earn the click via curiosity
   gap or trust trap (the AITA framing IS the headline).
3. A *brand* anchor — consistent color bar / corner badge so the
   channel grid reads as one show, not a random asset dump.

This module composites all three with PIL. No image generation, no
LLM round-trip — fast, deterministic, and re-runnable. Output is a
JPEG ≤ 2 MB so it fits YouTube's thumbnails.set quota.

Per-channel palette / headline picker is dispatched on ``style`` so
adding a new aesthetic is a dict edit, not new code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .captions import _find_font  # reuse the cross-platform font loader


# YouTube thumbnail spec: ≤ 2 MB, recommended 1280×720 (16:9).
# We render at 9:16 (the source video AR) so vertical Shorts grid views
# show the full thumb instead of a center-crop. A 720×1280 JPEG at q≈82
# lands well under the 2 MB cap.
_OUT_W = 720
_OUT_H = 1280

# Safe text margin from canvas edges.
_MARGIN = 56


# ---------- per-style palette + behaviour --------------------------------


@dataclass(frozen=True)
class ThumbnailStyle:
    """Visual + headline knobs for one channel aesthetic."""
    bar_color: tuple[int, int, int]          # solid brand bar (RGB)
    text_color: tuple[int, int, int]         # main headline ink
    stroke_color: tuple[int, int, int]       # outline for legibility on busy frames
    badge_text: str                          # short tag in corner ("AITA?", "TIFU", etc.)
    headline_fallback: str                   # if no script.hook is available
    bar_position: str = "bottom"             # "bottom" | "top"


_STYLES: dict[str, ThumbnailStyle] = {
    "aita": ThumbnailStyle(
        bar_color=(255, 209, 0),       # bright AITA yellow
        text_color=(255, 255, 255),
        stroke_color=(0, 0, 0),
        badge_text="AITA?",
        headline_fallback="AM I WRONG?",
    ),
    "tifu": ThumbnailStyle(
        bar_color=(220, 38, 38),       # alarmed red
        text_color=(255, 255, 255),
        stroke_color=(0, 0, 0),
        badge_text="TIFU",
        headline_fallback="I MESSED UP",
    ),
    "oddities": ThumbnailStyle(
        bar_color=(16, 185, 129),      # cyan/teal
        text_color=(255, 255, 255),
        stroke_color=(0, 0, 0),
        badge_text="WTF?!",
        headline_fallback="DID YOU KNOW?",
    ),
    "tih": ThumbnailStyle(
        bar_color=(59, 130, 246),      # historical blue
        text_color=(255, 255, 255),
        stroke_color=(0, 0, 0),
        badge_text="ON THIS DAY",
        headline_fallback="ONE DAY...",
    ),
    "default": ThumbnailStyle(
        bar_color=(244, 114, 182),     # generic pink
        text_color=(255, 255, 255),
        stroke_color=(0, 0, 0),
        badge_text="STORY",
        headline_fallback="WAIT, WHAT?",
    ),
}


def style_from_channel(channel_yaml: dict, *, channel_dir: str | None = None) -> ThumbnailStyle:
    """Look up a ThumbnailStyle by (in order):

    1. ``channel_yaml.upload.thumbnail.style`` (explicit override)
    2. The on-disk ``channel_dir`` name (most reliable — captures the
       *source* niche even when the upload YAML is a brand wrapper like
       ``mystoriesanimated.yaml`` covering multiple sources).
    3. Heuristic on the YAML's ``name`` field.
    """
    upl = (channel_yaml.get("upload") or {})
    explicit = ((upl.get("thumbnail") or {}).get("style") or "").lower().strip()
    if explicit in _STYLES:
        return _STYLES[explicit]

    if channel_dir:
        d = channel_dir.lower()
        if "tifu" in d:
            return _STYLES["tifu"]
        if "wiki" in d or "oddit" in d:
            return _STYLES["oddities"]
        if "today_in_history" in d or "tih" in d:
            return _STYLES["tih"]
        if (
            "amitheasshole" in d
            or "maliciouscompliance" in d
            or "prorevenge" in d
            or "aita" in d
        ):
            return _STYLES["aita"]

    name = (channel_yaml.get("name") or "").lower()
    if "tifu" in name:
        return _STYLES["tifu"]
    if "today in history" in name or "tih" in name:
        return _STYLES["tih"]
    if "wiki" in name or "oddit" in name:
        return _STYLES["oddities"]
    if "aita" in name or "stories" in name:
        return _STYLES["aita"]
    return _STYLES["default"]


# ---------- headline picker ----------------------------------------------


_FILLER = {
    "the", "a", "an", "and", "but", "or", "so", "for", "to", "of", "in",
    "on", "at", "by", "with", "from", "as", "i", "i'm", "is", "are",
    "was", "were", "be", "been", "this", "that", "it", "its", "if",
    "then", "than", "my", "your", "our", "their", "his", "her",
}


def _clean_token(t: str) -> str:
    """Strip leading/trailing punctuation that adds noise without info."""
    return t.strip("—–-\"'.,!?:;()[]{}…").strip()


def _curiosity_headline(text: str, *, max_words: int = 5) -> str:
    """Squeeze a sentence into a punchy 2-5 word headline.

    Heuristic: drop AITA prefixes, keep the most "loaded" content words,
    upper-case the result. Works well enough for AITA / TIFU style hooks.
    """
    s = (text or "").strip()
    if not s:
        return ""
    # Strip canonical AITA scaffolding so "AITA for refusing to split the
    # bill" → "refusing to split the bill" → headline.
    for prefix in (
        "aita for ", "aita ", "wibta for ", "wibta ", "aitah ",
        "tifu by ", "tifu ", "today i fucked up ",
        "am i the asshole for ", "am i wrong for ",
    ):
        low = s.lower()
        if low.startswith(prefix):
            s = s[len(prefix):]
            break
    # Drop trailing question marks for the contentful core.
    core = s.rstrip("?.!").strip()
    words = [_clean_token(w) for w in core.split()]
    words = [w for w in words if w]
    # If short enough already, use as-is.
    if len(words) <= max_words:
        return " ".join(words).upper()
    # Else: keep the first N non-filler words; if that produces fewer than
    # 2, fall back to the first N words verbatim.
    keep: list[str] = []
    for w in words:
        if w.lower() in _FILLER:
            continue
        keep.append(w)
        if len(keep) >= max_words:
            break
    if len(keep) < 2:
        keep = words[:max_words]
    return " ".join(keep).upper()


def _headline_score(h: str) -> tuple[int, int]:
    """Sort key — shorter headlines render at bigger font (more impact).

    Tiebreaker: prefer headlines with a number, $ sign, or shock word —
    those out-perform pure description in thumbnail A/B tests.
    """
    bonus = 0
    low = h.lower()
    if any(c.isdigit() for c in h):
        bonus -= 2
    if "$" in h:
        bonus -= 1
    for shock in ("never", "always", "actually", "refused", "told", "kicked", "banned"):
        if shock in low:
            bonus -= 1
            break
    # Primary key: length-bucketed (every 6 chars) so 12-char headlines
    # tie and we use the bonus tiebreaker. Without bucketing, "AITA?" (5)
    # always wins over "$400 IN STEAK" (13) even though the latter is more
    # informative.
    return (max(0, len(h) - 6) // 6 + bonus, len(h))


def pick_headline(*, script: dict, style: ThumbnailStyle) -> str:
    """Best 2-4 word curiosity-gap headline for this short."""
    raw_candidates: list[str] = []
    if script.get("hook"):
        raw_candidates.append(str(script["hook"]))
    for t in (script.get("title_options") or []):
        if t:
            raw_candidates.append(str(t))
    headlines: list[str] = []
    for c in raw_candidates:
        # Try aggressive word cap first (4 words), then back off.
        for cap in (4, 5):
            h = _curiosity_headline(c, max_words=cap)
            # Reject any headline whose longest token can't fit in a
            # reasonable line — single-word overflow makes the thumbnail
            # render with text running off-canvas.
            if h and _longest_token_chars(h) <= 14:
                headlines.append(h)
                break
    if not headlines:
        return style.headline_fallback
    headlines.sort(key=_headline_score)
    return headlines[0]


def _longest_token_chars(s: str) -> int:
    return max((len(t) for t in s.split()), default=0)


# ---------- scene picker -------------------------------------------------


def list_scene_frames(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("img_*.png"))


def score_frame(path: Path) -> float:
    """Compute a thumbnail-worthiness score for ``path``.

    Combines three signals that are cheap to compute with Pillow alone
    (no OpenCV dep):

      * **Edge density** — proxy for "busy / detailed" frames. Flat
        backgrounds + character T-poses score low; complex scenes
        with multiple characters or props score high.
      * **Mean luminance** — punishes too-dark + too-bright frames
        (both kill thumbnail readability on a phone screen).
      * **Channel stddev** — proxy for "colorful / varied" frames.
        Monochromatic / single-tone frames score low.

    Score is a non-negative float. Higher is better. A frame that
    fails ``pipeline/llm/quality_gate.py::check_image`` (broken /
    all-black / all-flat) gets ``-1.0`` so the picker knows to
    skip it.

    Added 2026-05-14 per Phase 8b. Used by :func:`pick_scene` to
    fall through from a broken ``img_00`` to the next viable frame.
    Pure Pillow — no OpenCV / mediapipe dep added.
    """
    try:
        from pipeline.llm.quality_gate import check_image  # noqa: PLC0415
    except Exception:  # pragma: no cover  # coverage: defensive — quality_gate import failure path is structurally untriggerable in tests
        check_image = None  # noqa: N806
    if check_image is not None:
        ok, _reason = check_image(path)
        if not ok:
            return -1.0
    try:
        with Image.open(path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")  # coverage: tested via test_score_frame_handles_rgba via the alpha path
            from PIL import ImageFilter, ImageStat  # noqa: PLC0415
            stat = ImageStat.Stat(img)
            avg_stddev = sum(stat.stddev) / max(1, len(stat.stddev))
            luma = img.convert("L")
            luma_pixels = list(luma.getdata())
            mean_lum = sum(luma_pixels) / max(1, len(luma_pixels))
            edges = luma.filter(ImageFilter.FIND_EDGES)
            edge_pixels = sum(1 for p in edges.getdata() if p > 30)
            edge_density = edge_pixels / max(1, img.width * img.height)
    except Exception:  # coverage: tested via test_score_frame_handles_corrupt_file
        return -1.0
    # Luminance scoring: peak at ~120 (mid-bright), penalty at extremes.
    # 0 (pitch black) and 255 (blown-out white) both unwatchable.
    # Triangular weighting centred at 120; max value = 1.0 at lum=120,
    # 0.0 at lum=0 or lum=240.
    lum_score = max(0.0, 1.0 - abs(mean_lum - 120.0) / 120.0)
    # Edge density: scale 0 → 0, 0.05 → 0.5, 0.15+ → 1.0.
    edge_score = min(1.0, edge_density / 0.15)
    # Stddev: scale 0 → 0, 30 → 0.5, 60+ → 1.0.
    sd_score = min(1.0, avg_stddev / 60.0)
    # Weighted sum — edge density is the strongest signal for "is
    # this frame visually interesting" so it gets the highest weight.
    return 0.5 * edge_score + 0.3 * sd_score + 0.2 * lum_score


def pick_scene(cache_dir: Path, *, prefer_index: int | None = None) -> Path | None:
    """Pick the scene frame to use as the thumbnail base.

    The hook moment (img_00) is usually the most expressive — beat 0
    is authored under the strictest concrete-tokens rule (Principle #7)
    and the character is closest to camera. So img_00 is the default.

    If ``prefer_index`` is set, return THAT frame (no scoring).

    Otherwise (default behaviour), use a tiered approach:
      1. If img_00 passes :func:`score_frame` (score >= 0), use it.
      2. Else fall through to the highest-scoring frame in the
         remaining set, breaking ties by index (lower wins — earlier
         beats are still better-curated).
      3. If every frame scores negative (everything failed quality
         gate), return img_00 anyway — better to ship a degraded
         thumbnail than no thumbnail at all (the cloud worker
         falls back to ffmpeg first-frame on None).

    Added 2026-05-14 per Phase 8b — promotes broken-frame skipping
    without adding OpenCV as a dep. Pure Pillow.
    """
    frames = list_scene_frames(cache_dir)
    if not frames:
        return None
    if prefer_index is not None:
        for f in frames:
            try:
                if int(f.stem.split("_", 1)[1]) == prefer_index:
                    return f
            except (ValueError, IndexError):
                continue
        # prefer_index not found → fall through to default logic.
    # Default: try img_00 first; if it scores fine, use it.
    head = frames[0]
    head_score = score_frame(head)
    if head_score >= 0:
        return head
    # Fall through to highest-scoring frame.
    scored = [(score_frame(f), i, f) for i, f in enumerate(frames)]
    # Filter out negative-score (broken) frames; if all broken, use img_00.
    viable = [(s, i, f) for (s, i, f) in scored if s >= 0]
    if not viable:
        return head
    # Sort by (-score, index) so highest score wins; ties broken by
    # earliest index (lowest = earliest = better-curated).
    viable.sort(key=lambda t: (-t[0], t[1]))
    return viable[0][2]


# ---------- compositor ---------------------------------------------------


def _fit_cover(src: Image.Image, w: int, h: int) -> Image.Image:
    """Center-crop scale src to exactly (w, h) — same as CSS object-fit: cover."""
    sw, sh = src.size
    src_ar = sw / sh
    dst_ar = w / h
    if src_ar > dst_ar:
        # source is wider — scale by height, crop width.
        scale = h / sh
        new_w, new_h = int(sw * scale), h
        scaled = src.resize((new_w, new_h), Image.LANCZOS)
        x0 = (new_w - w) // 2
        return scaled.crop((x0, 0, x0 + w, h))
    else:
        scale = w / sw
        new_w, new_h = w, int(sh * scale)
        scaled = src.resize((new_w, new_h), Image.LANCZOS)
        y0 = (new_h - h) // 2
        return scaled.crop((0, y0, w, y0 + h))


def _split_long_word(w: str) -> list[str]:
    """Break a single token on hyphens / apostrophes so it can wrap.

    "DAUGHTER-IN-LAW'S" → ["DAUGHTER-", "IN-", "LAW'S"]. Keeps the
    punctuation on the left half so the result reads naturally.
    """
    out: list[str] = []
    cur = ""
    for ch in w:
        cur += ch
        if ch in "-/":
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out if len(out) > 1 else [w]


def _fit_text(text: str, *, max_w: int, max_h: int, max_size: int = 200, min_size: int = 44) -> tuple[ImageFont.ImageFont, list[str]]:
    """Find the largest font size that lets ``text`` fit in ``(max_w, max_h)``,
    word-wrapping as needed. Returns (font, lines)."""
    # Pre-split tokens on hyphens so very long compound words can wrap.
    raw_tokens = text.split()
    tokens: list[str] = []
    for t in raw_tokens:
        tokens.extend(_split_long_word(t))

    for size in range(max_size, min_size - 1, -6):
        font = _find_font(size)
        # If even the widest single token can't fit, this size is too big.
        widest = max((font.getbbox(t)[2] - font.getbbox(t)[0]) for t in tokens)
        if widest > max_w:
            continue
        # Greedy wrap (joining hyphenated fragments back without space).
        lines: list[str] = []
        cur = ""
        for t in tokens:
            join = "" if cur.endswith(("-", "/")) else " "
            trial = (cur + join + t) if cur else t
            bbox = font.getbbox(trial)
            if bbox[2] - bbox[0] <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = t
        if cur:
            lines.append(cur)
        line_h = size + 12
        if line_h * len(lines) <= max_h:
            return font, lines
    # Fall through with the smallest size, even if it overflows.
    font = _find_font(min_size)
    return font, [text]


def _draw_outlined_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    stroke: tuple[int, int, int],
    stroke_width: int = 6,
    align: str = "center",
) -> None:
    if align == "center":
        bbox = font.getbbox(text)
        tw = bbox[2] - bbox[0]
        x = xy[0] - tw // 2
        y = xy[1]
    else:
        x, y = xy
    draw.text((x, y), text, font=font, fill=fill, stroke_fill=stroke, stroke_width=stroke_width)


def compose_thumbnail(
    *,
    scene_path: Path,
    headline: str,
    style: ThumbnailStyle,
    out_path: Path,
    canvas_w: int = _OUT_W,
    canvas_h: int = _OUT_H,
) -> Path:
    """Compose a 720×1280 JPEG thumbnail and write to out_path.

    Layout (top-down on a 9:16 canvas):
      1. Background = scene frame, scaled cover, slightly darkened.
      2. Top: small brand badge ribbon (style.badge_text) in the corner.
      3. Bottom: solid brand bar (style.bar_color) ~22% of canvas height,
         containing the headline in big bold sans-serif with stroke.
    """
    # 1. Background.
    src = Image.open(scene_path).convert("RGB")
    bg = _fit_cover(src, canvas_w, canvas_h)
    # Slight global darken so white text reads anywhere; subtle vignette
    # at the bottom where the brand bar sits anyway.
    overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 32))
    canvas = Image.alpha_composite(bg.convert("RGBA"), overlay)

    # 2. Top badge ribbon — small chip in the upper-left.
    draw = ImageDraw.Draw(canvas)
    badge_font = _find_font(56)
    bbox = badge_font.getbbox(style.badge_text)
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 28, 18
    badge_box = (
        _MARGIN, _MARGIN,
        _MARGIN + bw + 2 * pad_x, _MARGIN + bh + 2 * pad_y,
    )
    draw.rounded_rectangle(badge_box, radius=18, fill=style.bar_color + (255,))
    draw.text(
        (badge_box[0] + pad_x, badge_box[1] + pad_y - 4),
        style.badge_text,
        font=badge_font,
        fill=(0, 0, 0, 255),
    )

    # 3. Bottom brand bar with the headline.
    bar_h = int(canvas_h * 0.30)
    bar_top = canvas_h - bar_h
    # The bar is opaque on the bottom 80% of its height, with a quick
    # gradient at the top so it transitions cleanly off the photo.
    bar = Image.new("RGBA", (canvas_w, bar_h), style.bar_color + (255,))
    # Soft fade-in at the top edge.
    grad = Image.new("L", (1, bar_h))
    for y in range(bar_h):
        # Fade from 0 → 255 across the first 18% of the bar.
        grad.putpixel((0, y), int(min(255, (y / (bar_h * 0.18)) * 255)))
    grad = grad.resize((canvas_w, bar_h))
    bar.putalpha(grad)
    canvas = Image.alpha_composite(canvas, _paste_layer(canvas.size, bar, (0, bar_top)))

    # Draw the headline inside the bar, vertically centered.
    text_max_w = canvas_w - 2 * _MARGIN
    text_max_h = int(bar_h * 0.7)
    font, lines = _fit_text(headline, max_w=text_max_w, max_h=text_max_h)
    draw = ImageDraw.Draw(canvas)
    line_h = font.getbbox("Mg")[3] - font.getbbox("Mg")[1] + 16
    block_h = line_h * len(lines)
    y0 = bar_top + (bar_h - block_h) // 2 - 6
    for i, line in enumerate(lines):
        _draw_outlined_text(
            draw,
            (canvas_w // 2, y0 + i * line_h),
            line,
            font=font,
            fill=style.text_color,
            stroke=style.stroke_color,
            stroke_width=8,
        )

    # 4. Save as JPEG so we stay under YouTube's 2 MB cap.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, format="JPEG", quality=88, optimize=True)
    return out_path


def _paste_layer(canvas_size: tuple[int, int], layer: Image.Image, at: tuple[int, int]) -> Image.Image:
    """Make a full-canvas RGBA image with ``layer`` pasted at ``at``."""
    full = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    full.paste(layer, at, layer)
    return full


# ---------- high-level orchestrator -------------------------------------


def auto_thumbnail(
    *,
    slug: str,
    cache_dir: Path,
    script: dict,
    channel_yaml: dict,
    out_path: Path,
    channel_dir: str | None = None,
    headline_override: str | None = None,
    scene_index_override: int | None = None,
    style_override: str | None = None,
) -> Path | None:
    """End-to-end: pick scene + headline + style, compose, write.

    Returns the output path on success, or ``None`` if no scene frames
    are available (e.g. legacy renders that wiped data/cache/<slug>).
    """
    scene = pick_scene(cache_dir, prefer_index=scene_index_override)
    if scene is None:
        return None
    if style_override and style_override in _STYLES:
        style = _STYLES[style_override]
    else:
        style = style_from_channel(channel_yaml, channel_dir=channel_dir)
    headline = (headline_override or "").strip() or pick_headline(script=script, style=style)
    return compose_thumbnail(
        scene_path=scene,
        headline=headline,
        style=style,
        out_path=out_path,
    )


# ---------- CLI for one-off testing -------------------------------------


def _cli() -> None:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Compose a thumbnail for a rendered short.")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--channel", required=True, help="Path to channels/<name>.yaml")
    ap.add_argument("--out", default=None, help="Output path (default: data/cache/<slug>/auto_thumb.jpg)")
    ap.add_argument("--headline", default=None, help="Override the auto-derived headline")
    ap.add_argument("--scene-index", type=int, default=None,
                    help="Use img_NN.png as the base instead of img_00.")
    ap.add_argument("--style", default=None, choices=sorted(_STYLES),
                    help="Force a specific style; otherwise inferred from channel_dir.")
    args = ap.parse_args()

    import yaml
    from pipeline.paths import RenderPaths  # noqa: PLC0415

    chan_yaml = yaml.safe_load(open(args.channel).read()) or {}
    # Resolve per-channel paths via RenderPaths. ``--channel`` is the
    # channel YAML path; from_channel_yaml maps it to (channel, niche)
    # via NICHE_CHANNEL or falls back to a flat-channel layout.
    paths = RenderPaths.from_channel_yaml(args.channel)
    cache_dir = paths.cache_for(args.slug)
    # Find the narration JSON via the canonical per-channel layout, with
    # legacy ``data/intermediate/*/scripts/<slug>.json`` fallback for
    # half-migrated runs (the "scripts" → "narrations" rename was done
    # at the read side first; the writer was finally migrated in 2026-05-05).
    script: dict = {"slug": args.slug}
    channel_dir: str | None = paths.channel_dir
    canonical = paths.narration_for(args.slug)
    if canonical.exists():
        try:
            script = json.loads(canonical.read_text())
        except json.JSONDecodeError:
            pass
    else:
        for sp in Path("data/intermediate").glob(f"*/scripts/{args.slug}.json"):
            try:
                script = json.loads(sp.read_text())
            except json.JSONDecodeError:
                pass
            channel_dir = sp.parent.parent.name
            break
    out = Path(args.out) if args.out else cache_dir / "auto_thumb.jpg"
    p = auto_thumbnail(
        slug=args.slug,
        cache_dir=cache_dir,
        script=script,
        channel_yaml=chan_yaml,
        channel_dir=channel_dir,
        out_path=out,
        headline_override=args.headline,
        scene_index_override=args.scene_index,
        style_override=args.style,
    )
    if p is None:
        print("error: no scene frames found at", cache_dir)
        raise SystemExit(2)
    print(f"✓ wrote {p}  ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    _cli()
