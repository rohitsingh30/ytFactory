"""Stage 3.4 (sports) — Wikipedia event dossier.

Runs BEFORE cast.py for the SportsStoriesAnimated channel (and any
other channel that benefits from event-grounded characters). Output
is a per-story dossier at
``data/intermediate/<channel>/dossier/<slug>.json`` that captures the
real-world event:

- ``people[]`` — every named person who matters to the story, with
  era-specific kit / physical descriptors and a phonetic pronunciation
  hint (so Kokoro doesn't mangle "Aguero" / "Dzeko" / "Mbappé").
- ``match{}`` / ``event{}`` — date, venue, result, competition.
- ``key_moments[]`` — narrated beats grounded in the article's
  timeline so script-writing has facts to lean on.
- ``pronunciation_dict{}`` — flat name → respelling map, consumed by
  pipeline/audio.py before TTS.

The LLM (opus via claude CLI) is given the Wikipedia article plaintext
and asked for structured output. Wikipedia is fetched with stdlib
``requests`` — no scraping, just the public ``/w/api.php`` endpoint.

Per-character SEEDS are filled by code (deterministic hash of name)
*after* the LLM returns, so the LLM can't introduce non-determinism.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

import requests

from pipeline.llm import cli as llm


WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST = "https://en.wikipedia.org/api/rest_v1"
USER_AGENT = "ytFactory/0.1 (https://github.com/local; sports event research)"
TIMEOUT = 30

# Cap article body sent to LLM. Wikipedia match articles can run 50k+
# chars; opus handles it but we don't need the full thing — first ~15k
# covers lead + match summary + goalscorers, which is what we want.
MAX_ARTICLE_CHARS = 15_000


def _slug_to_title(slug: str) -> str | None:
    """Best-effort slug → human title. Returns None if no good guess.

    Caller can override by passing ``--title`` to the CLI. The mapping
    is intentionally light — most of the work happens via Wikipedia's
    search API, which is forgiving.
    """
    cleaned = slug.replace("-", " ").replace("_", " ").strip()
    return cleaned or None


def _wiki_search(query: str) -> str | None:
    """Search Wikipedia and return the canonical title of the top hit."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 1,
        "format": "json",
    }
    try:
        r = requests.get(
            WIKI_API,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json().get("query", {}).get("search", [])
    except (requests.RequestException, ValueError) as e:
        print(f"[wiki] search failed for {query!r}: {e}")
        return None
    if not hits:
        return None
    return hits[0].get("title")


def _wiki_extract(title: str) -> str | None:
    """Fetch the plaintext extract of a Wikipedia article by title."""
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "titles": title,
        "format": "json",
        "redirects": 1,
    }
    try:
        r = requests.get(
            WIKI_API,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
    except (requests.RequestException, ValueError) as e:
        print(f"[wiki] extract failed for {title!r}: {e}")
        return None
    for page in pages.values():
        text = page.get("extract")
        if text:
            return text
    return None


def fetch_article(query: str) -> tuple[str, str] | None:
    """Search Wikipedia and return ``(title, plaintext)`` or None.

    ``query`` is a free-text search string (e.g. the story title or
    a hand-curated event name).
    """
    title = _wiki_search(query)
    if not title:
        return None
    text = _wiki_extract(title)
    if not text:
        return None
    return title, text


# Schema for the LLM's structured output. Validated by claude CLI's
# --json-schema flag; pipeline/llm.py wrapper handles the response shape.
DOSSIER_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["match", "people", "key_moments", "pronunciation_dict"],
    "properties": {
        "match": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "date", "summary"],
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string"},  # YYYY-MM-DD or "" if unclear
                "venue": {"type": "string"},
                "competition": {"type": "string"},
                "result": {"type": "string"},
                "summary": {"type": "string"},
            },
        },
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "role", "team", "visual",
                             "pronunciation_phonetic"],
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "role": {"type": "string"},
                    "team": {"type": "string"},
                    "visual": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["body", "hair", "kit"],
                        "properties": {
                            "body": {"type": "string"},
                            "hair": {"type": "string"},
                            "facial_hair": {"type": "string"},
                            "skin": {"type": "string"},
                            "kit": {"type": "string"},
                            "shirt_number": {"type": "string"},
                            "era_notes": {"type": "string"},
                        },
                    },
                    "pronunciation_phonetic": {"type": "string"},
                    "pronunciation_ipa": {"type": "string"},
                },
            },
        },
        "key_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["time", "description"],
                "properties": {
                    "time": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "pronunciation_dict": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}


_PROMPT = """\
You are researching a real-world sports moment so a YouTube Shorts
pipeline can render it accurately.

The story is being made into a 15-25s animated Short in the style of
Tifo Football: minimalist line-art cartoon characters identified by
KIT + JERSEY NUMBER + body type (NOT facial likeness — we are NOT
trying to recreate real faces). At the climactic beat the animation
cuts to real broadcast footage of the moment.

Your job: read the Wikipedia article below and extract the dossier
the pipeline needs to (a) draw the right people in cartoon form,
(b) pronounce their names correctly via TTS, and (c) write narration
grounded in real facts.

STORY (as authored by the user):
\"\"\"
{story}
\"\"\"

WIKIPEDIA ARTICLE — "{wiki_title}":
\"\"\"
{wiki_text}
\"\"\"

Return ONLY a JSON object matching the schema. Specific guidance:

- ``match`` — date in YYYY-MM-DD if known; otherwise empty string.
  ``summary`` is 2-3 sentences capturing what made this moment
  significant, written so a script-writer can lean on it.

- ``people`` — every named person who appears or is referenced in the
  story or is critical to the moment (goal-scorer, assister, manager,
  notable opponent). DO NOT include every player on the team sheet —
  only people the narration is likely to mention. Each entry's
  ``visual`` must be ERA-SPECIFIC: what they looked like AT THIS
  EVENT, not their current appearance. Kit colors, kit sponsor
  details, era-specific haircut/beard. Be concise — diffusion attention
  drops past ~50 tokens per character.

- ``pronunciation_phonetic`` — respelled in plain English so a TTS
  reader will produce a sensible reading. Use SMALL CAPS on the
  stressed syllable: e.g. "Aguero" → "ah-GWAIR-oh", "Dzeko" →
  "JEK-oh", "Iniesta" → "in-YES-tah", "Mbappé" → "em-bap-PAY".
  Even for English names, give a respelling if the surname is
  commonly mispronounced (e.g. "Pochettino" → "po-cheh-TEE-no").
  ``pronunciation_ipa`` is optional and only included if confidently
  known.

- ``pronunciation_dict`` — flat ``{{name_or_alias: phonetic}}`` map
  covering every form the narration is likely to use. Include short
  aliases ("Aguero", "Kun", "Aguerooooo") all pointing to the same
  respelling. The pipeline does case-preserving regex substitution
  on this dict before TTS, so include EVERY commonly-used form.

- ``key_moments`` — 3-6 timestamped beats from the match (or event)
  that the narration could lean on. ``time`` is in the form the
  narration would say it ("eighty-eighth minute", "stoppage time",
  "after halftime") — not "minute 88".

If the Wikipedia article is about a broader topic (e.g. a season
rather than a single match) and the story is one specific moment
within it, focus the dossier on THAT moment.
"""


def _seed_for(name: str) -> int:
    """Deterministic per-character seed from the canonical name."""
    h = hashlib.sha256(name.lower().encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % (2**31)


def _attach_seeds(dossier: dict) -> dict:
    """Fill ``people[i].seed`` with a stable hash of ``name``."""
    for p in dossier.get("people") or []:
        if isinstance(p, dict) and p.get("name"):
            p["seed"] = _seed_for(p["name"])
    return dossier


def author_dossier(
    *,
    raw_story: dict,
    channel_cfg: dict,
    out_path: Path,
    wiki_query: str | None = None,
) -> dict:
    """Author the dossier for one story. Caches to ``out_path``.

    ``raw_story`` schema mirrors cast.author_cast: ``{slug, title,
    body, source, url, metadata}``. ``wiki_query`` overrides the
    auto-derived search string (use it when slug-based search misses).
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    story_text = f"{title}\n\n{body}" if title else body
    if not story_text:
        raise ValueError("raw_story has no title or body")

    query = wiki_query or title or _slug_to_title(raw_story.get("slug", ""))
    if not query:
        raise ValueError("could not derive a Wikipedia query from raw_story")

    print(f"[wiki] searching for {query!r}…")
    article = fetch_article(query)
    if article is None:
        raise RuntimeError(
            f"no Wikipedia article found for {query!r}. "
            "Pass --wiki-query with a more specific search string, or "
            "stage the article text manually at "
            f"data/intermediate/.../dossier/{raw_story.get('slug')}.wiki.txt"
        )
    wiki_title, wiki_text = article
    print(f"[wiki] fetched '{wiki_title}' ({len(wiki_text)} chars)")

    prompt = _PROMPT.format(
        story=story_text[:3000],
        wiki_title=wiki_title,
        wiki_text=wiki_text[:MAX_ARTICLE_CHARS],
    )

    print(f"[wiki] authoring dossier via claude CLI for {raw_story.get('slug')!r}…")
    raw = llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=DOSSIER_SCHEMA,
        model=llm.model_for("cast"),  # same tier as cast — judgment-heavy
        timeout_s=300,
    )

    if not isinstance(raw, dict) or "people" not in raw:
        raise ValueError(f"wiki_research returned unexpected shape: {raw!r}")

    raw["wiki"] = {"title": wiki_title, "fetched_chars": len(wiki_text)}
    _attach_seeds(raw)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(raw, indent=2))

    n_people = len(raw.get("people") or [])
    n_pron = len(raw.get("pronunciation_dict") or {})
    print(
        f"[wiki] wrote {out_path} — match={raw.get('match', {}).get('title','?')!r}, "
        f"{n_people} people, {n_pron} pronunciations"
    )
    return raw


def load_dossier(path: Path) -> dict | None:
    """Read a dossier.json. Returns None if missing or malformed."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and "people" in raw:
            return raw
    except (json.JSONDecodeError, AttributeError):
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Author a Wikipedia event dossier for a sports story."
    )
    ap.add_argument("--raw", required=True,
                    help="Path to data/intermediate/<channel>/raw/<slug>.json")
    ap.add_argument("--out", required=True,
                    help="Destination dossier JSON path")
    ap.add_argument("--wiki-query",
                    help="Override the auto-derived Wikipedia search string")
    ap.add_argument("--channel-yaml",
                    help="Optional channel YAML; only its image_style_prefix is used")
    args = ap.parse_args()

    raw_story = json.loads(Path(args.raw).read_text())
    channel_cfg: dict = {}
    if args.channel_yaml:
        import yaml
        channel_cfg = yaml.safe_load(Path(args.channel_yaml).read_text()) or {}

    author_dossier(
        raw_story=raw_story,
        channel_cfg=channel_cfg,
        out_path=Path(args.out),
        wiki_query=args.wiki_query,
    )


if __name__ == "__main__":
    main()
