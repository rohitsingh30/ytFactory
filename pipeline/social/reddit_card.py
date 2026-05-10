"""Render Reddit post + comment cards as PNGs for the ScrollPulse Short.

Output format: each "card" is a 1080×1152 PNG (the top region of the
1080×1920 Short frame, leaving 768 px below for gameplay).

Card kinds:
  * post_card    — header (sub pill + author + age) + title + (truncated) selftext + engagement
  * comment_card — single top-rated comment with author / age / body / score
  * verdict_card — final verdict beat (NTA / YTA / etc., big text on dark bg)

Long selftext or comments are PAGINATED into multiple cards.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import textwrap
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CARD_W, CARD_H = 1080, 1152

# URLs in selftext / comments overflow the card right edge — PIL/textwrap can't
# break long unbroken tokens — and the narrator never reads them anyway.
# See scrollpulse/learnings/url_strip_in_card_bodies.md.
_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def _strip_urls(text: str) -> str:
    return _URL_RE.sub("", text or "").strip()


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Trim ``text`` to ≤``max_chars`` characters, backing up to the nearest
    sentence boundary (``.!?``) and appending a horizontal ellipsis. Falls back
    to a hard char cut + ellipsis if no boundary is found within the window.

    See scrollpulse/learnings/split_screen_polish.md § 1.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
    if cut < max_chars * 0.4:
        cut = window.rfind(" ")
    if cut <= 0:
        return window.rstrip() + "…"
    return text[: cut + 1].rstrip() + " …"
BG = (26, 26, 27)             # #1A1A1B Reddit dark theme
CARD_BG = (39, 39, 41)         # #272729 inset card
ACCENT = (255, 69, 0)          # #FF4500 Reddit orange
WHITE = (255, 255, 255)
SUBTLE = (180, 180, 180)
META = (140, 140, 140)
PILL_GREY = (60, 60, 62)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ["/System/Library/Fonts/HelveticaNeue.ttc",
         "/System/Library/Fonts/Avenir Next.ttc"]
        if not bold else
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/System/Library/Fonts/Avenir Next.ttc"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap text to lines that fit ``max_width``."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        candidate = " ".join(cur + [w])
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] > max_width and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def _humanize_score(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0", "")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0", "")
    return str(n)


def _humanize_age(unix_ts: float | None) -> str:
    if not unix_ts:
        return ""
    age = time.time() - unix_ts
    if age < 3600:
        return f"{int(age / 60)}m"
    if age < 86_400:
        return f"{int(age / 3600)}h"
    if age < 86_400 * 30:
        return f"{int(age / 86_400)}d"
    return f"{int(age / (86_400 * 30))}mo"


def _draw_subreddit_pill(img: Image.Image, draw: ImageDraw.ImageDraw, sub: str, x: int, y: int) -> int:
    """Orange circular ``r/`` mark + ``r/<sub>`` text. Returns the right edge x."""
    icon_d = 60
    draw.ellipse((x, y, x + icon_d, y + icon_d), fill=ACCENT)
    icon_font = _font(34, bold=True)
    draw.text((x + 13, y + 4), "r/", font=icon_font, fill=WHITE)
    name_font = _font(34, bold=True)
    name_x = x + icon_d + 16
    name_y = y + 12
    draw.text((name_x, name_y), f"r/{sub}", font=name_font, fill=WHITE)
    bbox = draw.textbbox((0, 0), f"r/{sub}", font=name_font)
    return name_x + (bbox[2] - bbox[0])


def _draw_meta_line(
    draw: ImageDraw.ImageDraw, x: int, y: int, *, author: str, age: str
) -> None:
    txt = f"u/{author}  ·  {age}" if age else f"u/{author}"
    draw.text((x, y), txt, font=_font(28), fill=META)


def _draw_engagement(
    draw: ImageDraw.ImageDraw, x: int, y: int, score: int, comments: int
) -> None:
    f = _font(34, bold=True)
    fm = _font(30)
    # Up-arrow
    arrow = "▲"
    draw.text((x, y), arrow, font=f, fill=ACCENT)
    draw.text((x + 38, y + 2), _humanize_score(score), font=f, fill=WHITE)
    cx = x + 38 + 130
    draw.text((cx, y + 4), "💬", font=fm, fill=SUBTLE)
    draw.text((cx + 44, y + 2), _humanize_score(comments), font=f, fill=WHITE)


def render_post_card(
    *,
    subreddit: str,
    title: str,
    author: str,
    score: int,
    num_comments: int,
    age: str,
    body_excerpt: str = "",
    out_path: pathlib.Path,
    max_body_lines: int = 4,
) -> pathlib.Path:
    """Dynamic-height post card. Same auto-shrink pattern as
    render_comment_card — sized to title (+ optional body excerpt) +
    engagement footer. Eliminates the dead grey band on AskReddit-style
    no-selftext posts AND on long-selftext posts."""
    pad = 60
    title_font = _font(60, bold=True)
    body_font = _font(38)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1), BG))

    # Layout pass — measure title wrap + body excerpt wrap.
    title_lines = _wrap(title, title_font, CARD_W - 2 * pad, measure)
    body_excerpt = _strip_urls(body_excerpt)
    body_lines: list[str] = []
    has_body_ellipsis = False
    if body_excerpt:
        wrapped = _wrap(body_excerpt, body_font, CARD_W - 2 * pad, measure)
        body_lines = wrapped[:max_body_lines]
        has_body_ellipsis = len(wrapped) > max_body_lines

    # y math: pad (60) + subreddit pill (60+40 below) + title lines (70 each)
    # + 20 gap + body lines (50 each, optional) + bottom pad. No engagement
    # row per user ask 2026-05-08 ("remove the upvote thing completely").
    title_h = len(title_lines) * 70
    body_h = len(body_lines) * 50 + (4 if has_body_ellipsis else 0)
    content_end_y = pad + 100 + title_h + 20 + (body_h if body_lines else 0)
    card_h = content_end_y + pad

    img = Image.new("RGB", (CARD_W, card_h), BG)
    draw = ImageDraw.Draw(img)
    y = pad

    _draw_subreddit_pill(img, draw, subreddit, pad, y)
    _draw_meta_line(draw, pad + 80 + len(subreddit) * 24, y + 18, author=author, age=age)
    y += 100

    for line in title_lines:
        draw.text((pad, y), line, font=title_font, fill=WHITE)
        y += 70
    y += 20

    if body_lines:
        for line in body_lines:
            draw.text((pad, y), line, font=body_font, fill=(220, 220, 220))
            y += 50
        if has_body_ellipsis:
            draw.text((pad, y - 4), "...", font=body_font, fill=SUBTLE)

    # `score` and `num_comments` are intentionally unused — the engagement
    # row was removed 2026-05-08. Keep the params for caller compat.
    _ = (score, num_comments)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_body_card(
    *,
    subreddit: str,
    title: str,
    body_chunk: str,
    page_num: int,
    out_path: pathlib.Path,
) -> pathlib.Path:
    """Selftext continuation card — small subreddit pill + excerpted title +
    body chunk. Used when selftext is long enough to need multiple beats."""
    img = Image.new("RGB", (CARD_W, CARD_H), BG)
    draw = ImageDraw.Draw(img)
    pad = 60
    y = pad

    _draw_subreddit_pill(img, draw, subreddit, pad, y)
    y += 100

    title_font = _font(46, bold=True)
    title_lines = _wrap(title, title_font, CARD_W - 2 * pad, draw)
    for line in title_lines[:2]:  # truncate title to 2 lines on continuation
        draw.text((pad, y), line, font=title_font, fill=SUBTLE)
        y += 56
    if len(title_lines) > 2:
        draw.text((pad, y - 8), "...", font=title_font, fill=SUBTLE)
    y += 24

    body_font = _font(42)
    max_lines = (CARD_H - y - pad) // 56
    body_chunk = _strip_urls(body_chunk)
    for line in _wrap(body_chunk, body_font, CARD_W - 2 * pad, draw)[:max_lines]:
        draw.text((pad, y), line, font=body_font, fill=WHITE)
        y += 56

    # Page indicator bottom-right
    if page_num > 0:
        page_font = _font(28)
        page_text = f"({page_num + 1})"
        bbox = draw.textbbox((0, 0), page_text, font=page_font)
        pw = bbox[2] - bbox[0]
        draw.text((CARD_W - pad - pw, CARD_H - pad - 30), page_text, font=page_font, fill=META)

    img.save(out_path)
    return out_path


def render_comment_card(
    *,
    author: str,
    body: str,
    score: int,
    age: str,
    out_path: pathlib.Path,
    max_body_lines: int = 3,
) -> pathlib.Path:
    """Dynamic-height comment card. Layout pass first to compute card height
    from actual content (author + N body lines + score footer), then render
    to a 1080×<card_h> PNG. The split-screen renderer reads the PNG height
    and overlays gameplay starting at y=card_h. Eliminates the dead grey
    band that made every card look half-loaded — see
    scrollpulse/learnings/split_screen_polish.md § "auto-shrink"."""
    pad = 60
    inner_pad = 50
    body_font = _font(44)
    box_x0 = pad
    box_x1 = CARD_W - pad
    x_text = box_x0 + inner_pad
    max_w = box_x1 - inner_pad - x_text

    # Layout pass — compute body line count using a throwaway draw context.
    body = _strip_urls(body)
    body = _truncate_at_sentence(body, max_chars=52 * max_body_lines)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1), BG))
    all_lines = _wrap(body, body_font, max_w, measure)
    visible = all_lines[:max_body_lines]
    has_ellipsis = len(all_lines) > max_body_lines

    # Heights — author meta (50) + body (58 per line). No score footer per
    # user ask 2026-05-08 ("remove the upvote thing from reddit thing
    # completely") — comment cards now show only author + body.
    body_h = len(visible) * 58
    if has_ellipsis:
        body_h += 8
    box_inner_h = inner_pad + 50 + body_h + inner_pad
    box_y0 = pad
    box_y1 = box_y0 + box_inner_h
    card_h = box_y1 + pad

    img = Image.new("RGB", (CARD_W, card_h), BG)
    draw = ImageDraw.Draw(img)

    # Inset rounded comment box + left orange accent bar.
    draw.rounded_rectangle((box_x0, box_y0, box_x1, box_y1), radius=24, fill=CARD_BG)
    draw.rectangle((box_x0, box_y0, box_x0 + 8, box_y1), fill=ACCENT)

    y = box_y0 + inner_pad
    meta = f"u/{author}  ·  {age}" if age else f"u/{author}"
    draw.text((x_text, y), meta, font=_font(30, bold=True), fill=ACCENT)
    y += 50

    for line in visible:
        draw.text((x_text, y), line, font=body_font, fill=WHITE)
        y += 58
    if has_ellipsis:
        draw.text((x_text, y - 8), "…", font=body_font, fill=SUBTLE)

    # `score` is intentionally unused — the upvote footer was removed
    # 2026-05-08. Keep the parameter in the signature for callers.
    _ = score

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_verdict_card(
    *, verdict: str, caption: str, out_path: pathlib.Path
) -> pathlib.Path:
    img = Image.new("RGB", (CARD_W, CARD_H), BG)
    draw = ImageDraw.Draw(img)
    # Centered big verdict + smaller caption below.
    big_font = _font(220, bold=True)
    bbox = draw.textbbox((0, 0), verdict, font=big_font)
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    bx = (CARD_W - bw) // 2 - bbox[0]
    by = (CARD_H - bh) // 2 - bbox[1] - 60
    draw.text((bx, by), verdict, font=big_font, fill=ACCENT)

    cap_font = _font(56)
    cap_bbox = draw.textbbox((0, 0), caption, font=cap_font)
    cw = cap_bbox[2] - cap_bbox[0]
    cx = (CARD_W - cw) // 2 - cap_bbox[0]
    draw.text((cx, by + bh + 60), caption, font=cap_font, fill=WHITE)
    img.save(out_path)
    return out_path


def paginate_text(text: str, *, chars_per_page: int = 380) -> list[str]:
    """Split selftext into ~equal chunks at sentence boundaries."""
    if not text:
        return []
    pages: list[str] = []
    cur = ""
    for sentence in text.replace("\n", " ").split(". "):
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = (cur + ". " + sentence).strip(". ") if cur else sentence
        if len(candidate) > chars_per_page and cur:
            pages.append(cur.strip())
            cur = sentence
        else:
            cur = candidate
    if cur:
        pages.append(cur.strip())
    return pages


def main() -> None:
    """CLI smoke test — render every card kind from a scraped reddit JSON."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, type=pathlib.Path,
                    help="Output JSON from pipeline.social.reddit_scrape")
    ap.add_argument("--out-dir", required=True, type=pathlib.Path)
    # Verdict card text — defaults to AITA-style "NTA"; override for non-AITA
    # subs (e.g. AskReddit → "PEAK." / "WINNER" / a one-word callout).
    ap.add_argument("--verdict", default="NTA",
                    help="Big text on the verdict card (default: NTA)")
    ap.add_argument("--verdict-caption", default="The verdict is in",
                    help="Smaller caption under the verdict (default: 'The verdict is in')")
    args = ap.parse_args()

    b = json.loads(args.bundle.read_text())
    p = b["post"]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    age = _humanize_age(p.get("created_utc"))

    # Hero / post card
    render_post_card(
        subreddit=p["subreddit"], title=p["title"], author=p["author"],
        score=p["score"], num_comments=p["num_comments"], age=age,
        body_excerpt=p.get("selftext", "")[:280],
        out_path=args.out_dir / "00_post.png",
    )

    # Body continuation cards
    for i, chunk in enumerate(paginate_text(p.get("selftext", ""))):
        render_body_card(
            subreddit=p["subreddit"], title=p["title"],
            body_chunk=chunk, page_num=i,
            out_path=args.out_dir / f"01_body_{i + 1}.png",
        )

    # Top comment cards
    for i, c in enumerate(b["top_comments"][:6]):
        render_comment_card(
            author=c["author"], body=c["body"], score=c["score"],
            age="",  # comments don't carry created_utc here
            out_path=args.out_dir / f"02_comment_{i + 1}.png",
        )

    # Verdict card (AITA-style by default; override via --verdict / --verdict-caption)
    render_verdict_card(
        verdict=args.verdict, caption=args.verdict_caption,
        out_path=args.out_dir / "03_verdict.png",
    )

    print(f"rendered {len(list(args.out_dir.iterdir()))} cards in {args.out_dir}")


if __name__ == "__main__":
    main()
