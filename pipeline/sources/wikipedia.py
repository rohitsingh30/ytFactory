"""Wikipedia oddities source adapter (use case 2.4 in DESIGN.md).

Pulls list-style "oddity" pages and returns one ``RawStory`` per entry.
Wikipedia's list pages come in two shapes — bullet lists and tables —
and the parser handles both.

  - **Bullet lists** (e.g. ``List_of_common_misconceptions``): each
    top-level ``<li>`` inside ``<div class="mw-parser-output">`` is one
    entry.

  - **Wikitables** (e.g. ``List_of_unusual_deaths_in_the_21st_century``):
    each ``<tr>`` is one entry; the cell texts are joined.

Citations, footnotes, and reference lists are filtered out.

No extra dependencies — stdlib ``html.parser`` only. Wikipedia content
is CC-BY-SA so attribution is part of every record.
"""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
from pathlib import Path

import requests

from pipeline.observability.event_helpers import safe_track as _track

from .base import RawStory, save_raw, slugify


USER_AGENT = "ytFactory/0.1 (https://github.com/local; story aggregator)"
TIMEOUT = 25


def _source_attempt(kind: str, ref: str, backend: str) -> None:
    _track(
        "source.fetch_attempt",
        category="http",
        metadata={"kind": kind, "ref": ref, "backend": backend},
    )


def _source_ok(*, status_code: int, body_chars: int) -> None:
    _track(
        "source.fetch_ok",
        category="http",
        success=True,
        metadata={"status_code": status_code, "body_chars": body_chars},
    )


def _source_fallback(*, reason: str, original_status: int | None = None) -> None:
    _track(
        "source.fetch_fallback",
        category="http",
        success=False,
        metadata={"fallback_reason": reason, "original_status": original_status},
    )


# Curated set of pages that produce good 10-20s shorts material.
# Hub pages (e.g. plain "List_of_unusual_deaths") are useless for entries
# — they only link to per-era sub-pages — so the presets target the
# leaf pages directly.
PRESET_PAGES = {
    "unusual_deaths_21c": "List_of_unusual_deaths_in_the_21st_century",
    "unusual_deaths_20c": "List_of_unusual_deaths_in_the_20th_century",
    "unusual_deaths_19c": "List_of_unusual_deaths_in_the_19th_century",
    "unusual_deaths_premodern": "List_of_unusual_deaths_in_the_early_modern_period",
    "unusual_animal_deaths": "List_of_unusual_animal_deaths",
    # The "List of common misconceptions" was split in 2024 into per-topic
    # leaf pages. Each leaf is its own preset.
    "misconceptions_history": "List_of_common_misconceptions_about_history",
    "misconceptions_science": "List_of_common_misconceptions_about_science,_technology,_and_mathematics",
    "misconceptions_arts": "List_of_common_misconceptions_about_arts_and_culture",
    "misconceptions_middle_ages": "List_of_common_misconceptions_about_the_Middle_Ages",
    "unusual_articles": "Wikipedia:Unusual_articles",
}

# Default — points at the most consistently-shaped, modern page.
DEFAULT_PAGE = "unusual_deaths_21c"


_CITATION_PATTERNS = [
    re.compile(r"\bdoi:\s*\d", re.I),
    re.compile(r"\bISSN\b", re.I),
    re.compile(r"\bJSTOR\b", re.I),
    re.compile(r"\bRetrieved\b\s+\w+\s+\d+,\s*\d{4}", re.I),
    re.compile(r"\bArchived from the original\b", re.I),
    re.compile(r"^\^\s"),                 # leading "^ " backref
    re.compile(r"^[a-z]\s*\^", re.I),     # "a ^" backref
]


def _looks_like_citation(text: str) -> bool:
    return any(p.search(text) for p in _CITATION_PATTERNS)


class _EntryExtractor(HTMLParser):
    """Pull list-item and table-row text from the main parser-output div.

    Skips: ``<sup>``, ``<style>``, ``<script>``, ``<table class="navbox|infobox">``,
    ``<ol class="references">``, ``<ul class="gallery|navbox">``, and any
    ``<li>`` whose ``id`` starts with ``cite_note``.
    """

    INERT_TAGS = {"sup", "style", "script", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.in_main = False
        self.parser_output_depth = 0  # nested <div>s inside main
        self.skip_depth = 0           # >0 while inside something we ignore
        self.li_depth = 0
        self.tr_depth = 0
        self.li_buf: list[str] = []
        self.tr_buf: list[str] = []
        self.items: list[str] = []
        self._cell_buf: list[str] = []  # per-<td>/<th> while in a row
        self.in_cell = False

    # ---- helpers ---------------------------------------------------------

    def _open_skip(self) -> None:
        self.skip_depth += 1

    def _close_skip(self) -> None:
        if self.skip_depth > 0:
            self.skip_depth -= 1

    def _is_inert_table(self, attrd: dict) -> bool:
        cls = attrd.get("class", "")
        return any(c in cls for c in ("navbox", "infobox", "metadata", "ambox", "sistersitebox"))

    def _is_inert_list(self, attrd: dict) -> bool:
        cls = attrd.get("class", "")
        return any(c in cls for c in ("references", "navbox", "gallery", "mw-references"))

    # ---- handlers --------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrd = dict(attrs)
        cls = attrd.get("class", "")

        # Enter / track main content.
        if tag == "div" and "mw-parser-output" in cls:
            self.in_main = True

        if not self.in_main:
            return

        # Track nesting of inert containers we want to ignore wholesale.
        if tag in self.INERT_TAGS:
            self._open_skip()
            return
        if tag == "table" and self._is_inert_table(attrd):
            self._open_skip()
            return
        if tag in ("ol", "ul") and self._is_inert_list(attrd):
            self._open_skip()
            return
        if tag == "li" and attrd.get("id", "").startswith("cite_note"):
            self._open_skip()
            return

        if self.skip_depth > 0:
            return

        if tag == "li":
            if self.li_depth == 0:
                self.li_buf = []
            self.li_depth += 1
        elif tag == "tr":
            if self.tr_depth == 0:
                self.tr_buf = []
            self.tr_depth += 1
        elif tag in ("td", "th") and self.tr_depth > 0:
            self.in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag):
        if not self.in_main and tag != "div":
            return

        # Symmetric closes for inert wrappers. We use the same skip_depth
        # counter for all skip kinds; this is approximate but fine since we
        # only pair starts and ends within the same nesting.
        if tag in self.INERT_TAGS:
            self._close_skip()
            return
        if tag == "table" and self.skip_depth > 0:
            # Was likely opened as inert; a real wikitable table doesn't
            # increment skip_depth. Closing here is safe because non-skip
            # tables ignore this branch via skip_depth==0 at the line above.
            self._close_skip()
            return
        if tag in ("ol", "ul") and self.skip_depth > 0:
            self._close_skip()
            return
        if tag == "li" and self.skip_depth > 0:
            self._close_skip()
            return

        if self.skip_depth > 0:
            return

        if tag in ("td", "th") and self.in_cell:
            cell_text = re.sub(r"\s+", " ", "".join(self._cell_buf)).strip()
            if cell_text:
                self.tr_buf.append(cell_text)
            self.in_cell = False
            self._cell_buf = []
        elif tag == "tr" and self.tr_depth > 0:
            self.tr_depth -= 1
            if self.tr_depth == 0:
                # Drop header-only rows (single short cell) and rows with no prose.
                joined = " — ".join(c for c in self.tr_buf if c)
                if joined:
                    self.items.append(joined)
                self.tr_buf = []
        elif tag == "li" and self.li_depth > 0:
            self.li_depth -= 1
            if self.li_depth == 0:
                text = re.sub(r"\s+", " ", "".join(self.li_buf)).strip()
                if text:
                    self.items.append(text)
                self.li_buf = []
        elif tag == "div" and self.in_main:
            # We don't reliably track main-div depth with self-closing tags,
            # so treat the very last </div> as the boundary only if no other
            # signal — this is fine because we keep emitting until EOF.
            pass

    def handle_data(self, data):
        if not self.in_main or self.skip_depth > 0:
            return
        if self.in_cell:
            self._cell_buf.append(data)
        elif self.li_depth > 0:
            self.li_buf.append(data)


def _fetch_html(page: str) -> str:
    url = f"https://en.wikipedia.org/wiki/{page}"
    print(f"[wikipedia] GET {url}")
    _source_attempt("wikipedia_page", page, "wikipedia_html")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        body = getattr(r, "text", "")
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(body or ""))
        return body
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    except requests.RequestException as exc:
        _source_fallback(reason=type(exc).__name__, original_status=None)
        raise


def _strip_citations(text: str) -> str:
    """Remove [1], [12], [citation needed], etc., and known footer noise."""
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\[(citation needed|clarification needed|when\?|who\?|note \d+)\]", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def fetch(
    page: str = DEFAULT_PAGE,
    limit: int = 20,
    min_chars: int = 120,
    max_chars: int = 1500,
) -> list[RawStory]:
    """Fetch one Wikipedia page and return its entries as RawStories.

    ``page`` may be a preset key (see ``PRESET_PAGES``) or a raw page
    title (``List_of_unusual_deaths_in_the_19th_century``).
    """
    page_title = PRESET_PAGES.get(page, page)
    html = _fetch_html(page_title)

    parser = _EntryExtractor()
    parser.feed(html)

    canonical_url = f"https://en.wikipedia.org/wiki/{page_title}"
    out: list[RawStory] = []
    seen: set[str] = set()
    for raw_item in parser.items:
        clean = _strip_citations(raw_item)
        if not (min_chars <= len(clean) <= max_chars):
            continue
        if _looks_like_citation(clean):
            continue

        # Title = first sentence or first segment before " — " (table cell join).
        title = re.split(r"(?<=[.!?])\s|\s—\s", clean, maxsplit=1)[0]
        if len(title) > 140:
            title = title[:137].rstrip() + "..."
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)

        out.append(
            RawStory(
                slug=slugify(f"{page_title}-{title}"),
                title=title,
                body=clean,
                source=f"wikipedia:{page_title}",
                url=canonical_url,
                metadata={"page": page_title, "license": "CC-BY-SA"},
            )
        )
        if len(out) >= limit:
            break

    print(f"[wikipedia] kept {len(out)} entries from {page_title}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--page",
        default=DEFAULT_PAGE,
        help=f"Preset key ({', '.join(PRESET_PAGES)}) or raw page title",
    )
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--min-chars", type=int, default=120)
    ap.add_argument("--max-chars", type=int, default=1500)
    ap.add_argument("--out", default="data/intermediate")
    ap.add_argument("--channel", default="wiki_oddities")
    args = ap.parse_args()

    stories = fetch(
        page=args.page,
        limit=args.limit,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
    )
    dest = Path(args.out) / args.channel / "raw"
    for s in stories:
        path = save_raw(s, dest)
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
