"""Render burned-in caption PNGs with Pillow.

ffmpeg's `drawtext` and `subtitles` (libass) filters are unavailable
in the Homebrew minimal ffmpeg 8.1 build, so we render each caption
to a transparent PNG and overlay it via ffmpeg's `overlay` filter.

Two render modes:

- **per-beat** (legacy): one PNG per beat showing the full beat text
  on a translucent black bar at the bottom. Built by ``render_beat_caption``.

- **per-word karaoke** (current default): one PNG per WORD shown
  centre-vertical for the word's exact spoken interval, scaled large
  with a drop shadow and no background bar. Built by ``render_word_caption``.
  Modern Shorts/TikTok caption style. Per OpusClip + Submagic 2026 caption
  benchmarks this is the highest-retention caption style for short-form
  video. Drift becomes invisible at the word grain even when timing
  isn't perfect.

Sync architecture (DESIGN.md #40): captions are NEVER cached on disk
across runs — compose wipes any per-beat / per-word PNG before each
render and regenerates from the current beats.json. The off-by-one
between cached artefacts and beats.json that produced multi-second
caption drift in earlier renders is structurally impossible under
this contract.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .beats import Beat


_DEVANAGARI_RANGE = (0x0900, 0x097F)


def _has_devanagari(text: str) -> bool:
    if not text:
        return False
    lo, hi = _DEVANAGARI_RANGE
    return any(lo <= ord(ch) <= hi for ch in text)


def _find_font(size: int, text: str = "") -> ImageFont.ImageFont:
    """Pick a font that covers the script of ``text``.

    Class-of-bug fix per critique 2026-05-03 (abhimanyu-chakravyuh.video.md):
    the previous implementation always used Latin-only fonts (Arial Bold,
    Impact) which silently render Devanagari as empty boxes. Now we
    detect Devanagari in the text and prefer macOS' Devanagari Sangam MN
    when present, falling back to the Latin-bold candidates for English.

    Pass ``text`` whenever you have it so font selection matches the
    actual content. Callers that don't pass it default to Latin
    candidates (back-compat).

    2026-05-18 (round 6) Linux fallback fix. Pre-fix this helper ONLY
    listed macOS ``/System/Library/Fonts/...`` paths. On the Cloud Run
    Linux Docker image NONE of those paths exist, so the final return
    fell through to ``ImageFont.load_default()`` — a tiny built-in 12px
    bitmap font that IGNORES the requested ``size``. Symptom on render
    41f77152: the ASS auto-shrink loop in ``word_caption_pngs`` called
    ``_find_font(260, ...).getbbox("deglazing")`` and got back a width
    of ~80px (12px bitmap font), well under the 778px budget, so n_shrunk=0
    fired and libass rendered "deglazing" at the actual font_size=260
    with DejaVu Sans Bold (its fontconfig fallback) → clipped both
    frame edges. The Pillow measurement and libass rendering must read
    from the SAME font family to produce a consistent budget decision.
    Adding DejaVu paths (and Noto/Liberation as further fallbacks) is
    the smallest, lowest-risk fix.
    """
    devanagari_candidates = [
        "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
        "/System/Library/Fonts/Supplemental/DevanagariMT.ttc",
        "/System/Library/Fonts/Supplemental/ITFDevanagari.ttc",
        # Linux Docker fallbacks (Devanagari coverage)
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    ]
    latin_candidates = [
        # macOS preferred order — the laptop path is unchanged so any
        # author-time cached PNG widths remain reproducible.
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        # Linux Docker fallbacks — match what fontconfig picks for
        # libass at runtime so width measurement and rendering agree.
        # DejaVu Sans Bold is the de-facto default on slim Debian images
        # (also what the deployed cloud/render-worker-v2 Docker bakes in
        # via the `fonts-dejavu` apt package). Liberation Sans Bold is
        # the Red Hat / Fedora analogue. Both have ~identical metrics
        # so the auto-shrink budget translates correctly.
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    candidates = (
        devanagari_candidates + latin_candidates
        if _has_devanagari(text)
        else latin_candidates
    )
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        bbox = font.getbbox(trial)
        if bbox[2] - bbox[0] <= max_width:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def render_beat_caption(
    beat: Beat,
    out_path: Path,
    canvas_w: int = 1080,
    canvas_h: int = 320,
    font_size: int = 84,
    text_color: tuple[int, int, int, int] = (255, 240, 80, 255),  # warm yellow
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 255),
    stroke_width: int = 6,
    bg_color: tuple[int, int, int, int] = (0, 0, 0, 140),  # translucent black bar
    padding: int = 60,
    max_lines: int | None = None,
) -> Path:
    """Render this beat's text into a transparent PNG suitable for overlay.

    ``max_lines`` (2026-05-14): clamp the wrapped output to at most N
    lines. ``None`` keeps the original behaviour (canvas grows to fit
    every line). Used by the captions_layout=bottom_one_line /
    bottom_two_line modes to enforce a consistent visual style across
    sentences of varying length.
    """
    # Wrap first so we can size the canvas around the actual line count.
    # Principle #25 (NEW): caption canvas height is a function of line
    # count, never fixed — long captions used to clip ("birth?" cut off
    # the bottom of the hook on aita-birth-pool v2 because canvas_h was
    # hardcoded at 320 and 4 lines × 100px overflowed the PNG).
    font = _find_font(font_size, text=beat.text)
    lines = _wrap(beat.text.strip(), font, canvas_w - 2 * padding)
    if max_lines is not None and max_lines > 0 and len(lines) > max_lines:
        # Truncate: keep the first ``max_lines`` lines verbatim and
        # tail-truncate the last one with an ellipsis. Beats wider than
        # the canvas would otherwise stack vertically and cover the
        # frame; clamping is the user's explicit choice via
        # captions_layout=bottom_one_line / bottom_two_line.
        kept = lines[:max_lines]
        # Mark truncation only on the LAST kept line — and only if the
        # source text actually had more content beyond what we kept.
        rejoined = " ".join(kept)
        if rejoined != beat.text.strip():
            tail = kept[-1].rstrip(",.;:! ")
            kept[-1] = tail + "…"
        lines = kept
    line_h = font_size + 16
    block_h = line_h * len(lines)
    canvas_h = max(canvas_h, padding * 2 + block_h + 20)

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background bar.
    draw.rectangle([0, 0, canvas_w, canvas_h], fill=bg_color)

    y0 = (canvas_h - block_h) // 2

    for i, line in enumerate(lines):
        bbox = font.getbbox(line)
        line_w = bbox[2] - bbox[0]
        x = (canvas_w - line_w) // 2
        y = y0 + i * line_h
        draw.text(
            (x, y),
            line,
            font=font,
            fill=text_color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_word_caption(
    word_text: str,
    out_path: Path,
    canvas_w: int = 1080,
    canvas_h: int = 240,
    font_size: int = 110,
    text_color: tuple[int, int, int, int] = (255, 240, 80, 255),  # warm yellow
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 255),
    stroke_width: int = 10,
    shadow_offset: tuple[int, int] = (5, 8),
    shadow_color: tuple[int, int, int, int] = (0, 0, 0, 230),
    padding: int = 40,
    bg_pill_alpha: int = 90,
) -> Path:
    """Render ONE word as a transparent PNG sized to fit the word.

    Word-level captions are the modern Shorts/TikTok style and the
    structural fix for sync drift (see module docstring). Each word
    gets its own overlay shown for ``[word.start, word.end]`` only;
    one word at a time on screen, centred-vertical.

    Render order (bottom → top):
      1. Translucent black rounded-rect pill behind the text. Critic
         finding 2026-05: yellow text on cream/yellow pastel
         backgrounds had near-zero contrast on a phone in daylight,
         even with an 8px stroke — the stroke disappears against any
         dark pastel and the cream bg eats the yellow fill. The pill
         guarantees contrast on every image regardless of the scene's
         palette. Set ``bg_pill_alpha=0`` to disable for channels with
         dark/photographic backgrounds where the pill is unnecessary.
      2. Drop shadow.
      3. Main text with stroke.
    """
    word_text = (word_text or "").strip()
    if not word_text:
        word_text = " "

    # Auto-shrink font when the rendered word would overflow the canvas.
    # Without this, a 260pt "KETCHUP" produces a 1362×300 PNG; the
    # compose stage overlays at ``x=(W-w)/2 = (1080-1362)/2 = -141``
    # — ffmpeg accepts negative x but the visible window crops both
    # ends of the word. Worse: with the Ken Burns mp4 path (post
    # 2026-05-17) the overlay can sit ENTIRELY off-screen for
    # long words at large font sizes, which is what produced the
    # "where are my captions?" symptom on cloud renders. Scale the
    # font down so the rendered word fits inside canvas_w minus a
    # 40px safe margin on each side; this preserves the big-Shorts
    # feel for short words while keeping long words readable.
    max_text_w = max(200, canvas_w - 80)  # 40px safe margin per side
    fitted_font_size = font_size
    font = _find_font(fitted_font_size, text=word_text)
    bbox = font.getbbox(word_text)
    text_w = bbox[2] - bbox[0]
    while text_w > max_text_w and fitted_font_size > 60:
        fitted_font_size = int(fitted_font_size * 0.9)
        font = _find_font(fitted_font_size, text=word_text)
        bbox = font.getbbox(word_text)
        text_w = bbox[2] - bbox[0]
    # Track the resolved size so callers can debug-log if a word
    # had to shrink dramatically. (See word_caption_pngs.produce
    # for the first-beat debug log.)
    font_size = fitted_font_size
    text_h = bbox[3] - bbox[1]

    # Size canvas to fit text + stroke + shadow + padding so the PNG
    # is small (cheap to overlay) but the word is never clipped.
    # CRITICAL: never exceed canvas_w — overlay math depends on it.
    cw = min(
        canvas_w,
        max(text_w + stroke_width * 2 + abs(shadow_offset[0]) + padding * 2,
            font_size * 4),
    )
    ch = max(text_h + stroke_width * 2 + abs(shadow_offset[1]) + padding,
             font_size + padding)
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Centre the word in the canvas.
    x = (cw - text_w) // 2 - bbox[0]
    y = (ch - text_h) // 2 - bbox[1]

    # Translucent pill behind the text — defends against cream/pastel
    # backgrounds where stroke + shadow alone collapse. Sized to text
    # bbox + a small inset; rounded for a softer edge that doesn't
    # read as a hard "subtitle bar".
    if bg_pill_alpha > 0:
        pill_pad_x = max(stroke_width * 2, 24)
        pill_pad_y = max(stroke_width, 12)
        pill_x0 = max(0, x + bbox[0] - pill_pad_x)
        pill_y0 = max(0, y + bbox[1] - pill_pad_y)
        pill_x1 = min(cw, x + bbox[2] + pill_pad_x)
        pill_y1 = min(ch, y + bbox[3] + pill_pad_y)
        draw.rounded_rectangle(
            [pill_x0, pill_y0, pill_x1, pill_y1],
            radius=24,
            fill=(0, 0, 0, max(0, min(255, bg_pill_alpha))),
        )

    # Drop shadow first (drawn behind the main text).
    draw.text(
        (x + shadow_offset[0], y + shadow_offset[1]),
        word_text,
        font=font,
        fill=shadow_color,
        stroke_width=stroke_width,
        stroke_fill=shadow_color,
    )
    # Main text on top.
    draw.text(
        (x, y),
        word_text,
        font=font,
        fill=text_color,
        stroke_width=stroke_width,
        stroke_fill=stroke_color,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def _split_closer_format(closer_format: str) -> list[str]:
    """Split a closer_format string into 1-3 visual rows.

    Examples (input → rows):
        "LIKE if YTA, COMMENT if NTA. AITA?"
            → ["LIKE if YTA", "COMMENT if NTA"] (the closing AITA? is in
              the captions, not the panel)

        "Vote your verdict — yes or no?"
            → ["Vote your verdict", "yes or no?"]

    The split prefers commas / em-dashes / "vs" / "or" to break a
    sentence into the YTA-vs-NTA halves. The trailing AITA?/WIBTA? is
    dropped (it's burned into captions already).
    """
    s = closer_format.strip()
    # Drop the trailing AITA?/WIBTA? if present.
    s = re.sub(r"[.!]?\s*(AITA|WIBTA)\??\s*$", "", s, flags=re.IGNORECASE).strip()
    # Strip only trailing space + comma; preserve author-intended end
    # punctuation like "!" so devotional closers can read with emphasis
    # ("जय श्री कृष्णा!"). The AITA?/WIBTA? regex above already handled
    # the verdict-acronym tail.
    s = s.rstrip(", ")

    # Split on the strongest divider available.
    for sep in [",", "—", " - ", " vs ", " / "]:
        if sep in s:
            parts = [p.strip(", ").strip() for p in s.split(sep) if p.strip()]
            return [p for p in parts if p][:3]

    # Fallback — treat as a single row.
    return [s] if s else ["AITA?"]


_AITA_PALETTE = (
    (255, 90, 90, 255),   # red-ish — YTA / like
    (100, 200, 255, 255), # blue-ish — NTA / comment
    (255, 220, 90, 255),  # yellow — fallback / single-row
)


def render_rank_chip(
    rank: int,
    out_path: Path,
    *,
    canvas_w: int = 220,
    canvas_h: int = 220,
    bg_color: tuple[int, int, int, int] = (20, 20, 20, 235),
    border_color: tuple[int, int, int, int] = (255, 240, 80, 255),
    text_color: tuple[int, int, int, int] = (255, 240, 80, 255),
    border_w: int = 6,
    radius: int = 28,
    font_size: int = 110,
) -> Path:
    """Render a "#N" countdown chip — used by the tier-list channel.

    Mirrors render_closer_panel: transparent PNG, ffmpeg overlays it
    onto the final video. The chip is shown for one beat at a time
    (beat[1]→beat[2] for #5, etc.) so the countdown reads visually.
    """
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        [(border_w // 2, border_w // 2),
         (canvas_w - border_w // 2, canvas_h - border_w // 2)],
        radius=radius,
        fill=bg_color,
        outline=border_color,
        width=border_w,
    )

    label = f"#{int(rank)}"
    font = _find_font(font_size)
    bbox = font.getbbox(label)
    line_w = bbox[2] - bbox[0]
    line_h = bbox[3] - bbox[1]
    x = (canvas_w - line_w) // 2 - bbox[0]
    y = (canvas_h - line_h) // 2 - bbox[1]
    draw.text(
        (x, y),
        label,
        font=font,
        fill=text_color,
        stroke_width=4,
        stroke_fill=(0, 0, 0, 255),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_subscribe_button(
    out_path: Path,
    *,
    canvas_w: int = 720,
    canvas_h: int = 200,
    bg_color: tuple[int, int, int, int] = (204, 0, 0, 255),  # YouTube red
    text_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    radius: int = 28,
    font_size: int = 96,
) -> Path:
    """Render a centred YouTube SUBSCRIBE button — red rounded rect with
    white "SUBSCRIBE" text. Used by the sports channel as a closer
    overlay on the last 2-3 seconds of the Short.

    Per user direction (2026-05-03 Iniesta v1 critique): the YouTube CTA
    must be CENTERED on screen with the literal word "SUBSCRIBE" inside
    it — not a bare red play-icon at the bottom-left, and not relying
    on diffusion to render the word legibly inside the cartoon image.
    Programmatic PIL render guarantees legibility.
    """
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Subtle dark border for contrast over busy backgrounds.
    draw.rounded_rectangle(
        [(0, 0), (canvas_w - 1, canvas_h - 1)],
        radius=radius,
        fill=bg_color,
        outline=(0, 0, 0, 200),
        width=4,
    )

    label = "SUBSCRIBE"
    font = _find_font(font_size)
    bbox = font.getbbox(label)
    line_w = bbox[2] - bbox[0]
    line_h = bbox[3] - bbox[1]
    x = (canvas_w - line_w) // 2 - bbox[0]
    y = (canvas_h - line_h) // 2 - bbox[1]
    draw.text(
        (x, y),
        label,
        font=font,
        fill=text_color,
        stroke_width=2,
        stroke_fill=(0, 0, 0, 200),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_closer_caption_rows(
    closer_format: str,
    cache_dir: Path,
) -> list[Path]:
    """Render one center-worded caption PNG per closer-format row.

    User feedback 2026-05-02: the boxed-banner closer panel reads as a
    static slap-on overlay vs the karaoke word captions throughout the
    rest of the Short. Render each closer row using the same word-caption
    styling (warm yellow, drop shadow, stroke, translucent pill bg)
    instead, so the closer FEELS like a continuation of the captions
    rather than a separate UI surface.

    The compose stage overlays these PNGs sequentially at y=864 (the
    same y as word captions) during the closer time window, splitting
    the window evenly between rows.

    Returns one Path per row, in row order. Caller is responsible for
    timing and overlay placement.
    """
    rows = _split_closer_format(closer_format)
    paths: list[Path] = []
    for i, row_text in enumerate(rows):
        out_path = cache_dir / f"closer_row_{i:02d}.png"
        # Drive the same renderer the per-word captions use so the
        # styling is identical (font, stroke, drop shadow, pill bg).
        # Multi-word phrases work fine — render_word_caption sizes the
        # canvas to fit `font.getbbox(text)`, which is just as happy
        # with "LIKE if YTA" as with a single word.
        render_word_caption(row_text, out_path, canvas_w=1080)
        paths.append(out_path)
    return paths


def render_closer_panel(
    out_path: Path,
    *,
    closer_format: str = "LIKE if YTA, COMMENT if NTA",
    canvas_w: int = 1080,
    canvas_h: int = 360,
    title_font_size: int = 80,
    bg_color: tuple[int, int, int, int] = (0, 0, 0, 200),
    stroke_width: int = 6,
    palette: tuple[tuple[int, int, int, int], ...] = _AITA_PALETTE,
) -> Path:
    """Render a CTA panel from a channel's ``closer_format`` string.

    The panel content is fully derived from ``closer_format`` — no
    hardcoded text. The string is split into 1-3 rows by punctuation
    and each row gets a colour from ``palette`` in order.

    Per memory feedback: AITA channels use "LIKE if YTA, COMMENT if NTA"
    style — never vague "vote in comments". This is the highest-leverage
    end-of-video element.
    """
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, canvas_w, canvas_h], fill=bg_color)

    rows = _split_closer_format(closer_format)
    title_font = _find_font(title_font_size, text=closer_format)

    line_gap = 32
    total_h = len(rows) * title_font.size + max(0, len(rows) - 1) * line_gap
    y = (canvas_h - total_h) // 2

    for i, text in enumerate(rows):
        colour = palette[i % len(palette)]
        bbox = title_font.getbbox(text)
        line_w = bbox[2] - bbox[0]
        x = (canvas_w - line_w) // 2
        draw.text(
            (x, y),
            text,
            font=title_font,
            fill=colour,
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0, 255),
        )
        y += title_font.size + line_gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
