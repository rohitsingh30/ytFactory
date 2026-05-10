"""Pull the last N head-to-head fixtures between two entities.

Companion to ``.claude/skills/make-rivalry-recap`` — does the
researcher-hat work so the skill author can jump straight to writing
narration + picking moments + nailing footage windows.

What it does:

1. Resolves the rivalry's Wikipedia article (either a free-text search
   like ``"Liverpool–Manchester United rivalry"`` or a URL the user
   passes via ``--wiki-list-url``).
2. Pulls the article plaintext via the existing
   ``pipeline.wiki_research._wiki_extract`` helper.
3. Asks claude CLI (haiku, structured output) to return the last N
   competitive fixtures as a strict-schema list of
   ``{date, competition, venue, score, key_moment}``.
4. Writes a raw JSON to
   ``<out>/raw/<slug>.json`` shaped for the rivalry-recap variant —
   matches the SKILL.md schema, with placeholder footage URLs and
   per-fixture YouTube search queries the skill author then resolves.

Doesn't fetch broadcast clips. Picking the actual YouTube URL,
in_s, and out_s is curatorial work and stays with the skill author —
the helper produces the search query they'll paste, plus the
authoritative fixture metadata so prose-writing has facts to lean on.

Usage:

    .venv/bin/python sportstoriesanimated/scripts/research_rivalry.py \
        --entity-a "Manchester United" --entity-b "Liverpool" --last 5 \
        --slug "mufc-vs-lfc-last5" \
        --out sportstoriesanimated/ranked

    .venv/bin/python sportstoriesanimated/scripts/research_rivalry.py \
        --entity-a "Real Madrid" --entity-b "FC Barcelona" --last 5 \
        --wiki-list-url "https://en.wikipedia.org/wiki/List_of_El_Cl%C3%A1sico_matches" \
        --slug "madrid-vs-barca-last5" \
        --out sportstoriesanimated/ranked

The output JSON is a starting point — review the fixtures the LLM
extracted and correct any hallucinations against the source article
before authoring the narration. Wikipedia "list of matches" pages
are the authoritative source.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

# Allow running as a standalone script from repo root.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from pipeline import llm, wiki_research  # noqa: E402


_FIXTURES_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fixtures"],
    "properties": {
        "fixtures": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "date", "competition", "venue", "score", "key_moment",
                ],
                "properties": {
                    "date": {"type": "string"},
                    "competition": {"type": "string"},
                    "venue": {"type": "string"},
                    "score": {"type": "string"},
                    "home_team": {"type": "string"},
                    "away_team": {"type": "string"},
                    "key_moment": {"type": "string"},
                    "key_player": {"type": "string"},
                },
            },
        },
    },
}


_PROMPT_TMPL = """You are an English football historian. From the Wikipedia article below,
extract the **last {N} competitive fixtures** between {ENTITY_A} and {ENTITY_B},
ordered chronologically (oldest first, most recent last).

Rules:
- Competitive only — Premier League, FA Cup, League Cup, Champions League, Europa
  League, Community Shield. NO friendlies / pre-season / testimonials.
- "Last" = most recent. Treat the article as definitive; if a fixture isn't in
  the article, don't invent one.
- For each fixture, summarise the **single most replayed moment** in
  ``key_moment`` (winning goal, comeback equaliser, red card, controversial VAR
  decision, missed penalty, brawl). One sentence, ≤25 words. If the fixture was
  a dull 0-0 with no incident, write "no replayable moment".
- ``key_player`` = the player whose action defines the moment (the scorer / the
  sent-off / the keeper who saved). Empty if no individual stands out.
- ``date`` = ISO YYYY-MM-DD when known; just YYYY-MM if the article lacks the day.
- ``score`` = "<home> N-N <away>" with the actual numbers.
- ``home_team`` / ``away_team`` = full club names.

Return strictly: {{"fixtures": [...]}} with exactly {N} entries (or fewer if the
article doesn't cover that many).

Article title: {TITLE}

Article body (plaintext):
---
{BODY}
---
"""


def _resolve_article(
    *, entity_a: str, entity_b: str, wiki_list_url: str | None,
) -> tuple[str, str] | None:
    """Return (title, plaintext) for the rivalry's Wikipedia article."""
    if wiki_list_url:
        parsed = urlparse(wiki_list_url)
        if "wikipedia.org" not in parsed.netloc:
            print(f"[research] {wiki_list_url!r} is not a wikipedia.org URL")
            return None
        # /wiki/<Title> → percent-decoded plain title for the API.
        path = parsed.path or ""
        if not path.startswith("/wiki/"):
            print(f"[research] expected /wiki/<Title> path, got {path!r}")
            return None
        title = unquote(path[len("/wiki/"):]).replace("_", " ")
        text = wiki_research._wiki_extract(title)
        if text:
            return title, text
        print(f"[research] no extract for {title!r} — falling back to search")

    queries = [
        f"{entity_a}–{entity_b} rivalry",
        f"{entity_a} {entity_b} rivalry",
        f"List of {entity_a}–{entity_b} matches",
        f"{entity_a} {entity_b} matches",
    ]
    for q in queries:
        hit = wiki_research.fetch_article(q)
        if hit:
            return hit
    return None


def _shorten(text: str, *, limit: int = 18000) -> str:
    """Trim article body to fit a tractable prompt context."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return head + "\n\n[...trimmed...]\n\n" + tail


def _write_raw(
    *,
    out_dir: Path,
    slug: str,
    entity_a: str,
    entity_b: str,
    n: int,
    title: str,
    fixtures: list[dict],
) -> Path:
    """Persist the raw rivalry payload in the schema /make-rivalry-recap reads."""
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_path = raw_dir / f"{slug}.json"
    payload = {
        "slug": slug,
        "title": (
            f"{entity_a} vs {entity_b} — last {n} head-to-head moments"
        ),
        "body": (
            f"Last {n} competitive {entity_a} vs {entity_b} fixtures, ordered "
            f"chronologically (oldest = #{n}, most recent = #1). Each fixture's "
            f"most-replayed moment becomes one rank in the rivalry recap, with "
            f"a real broadcast cut-in pinned to its buildup line."
        ),
        "source": "research_rivalry:wikipedia",
        "url": (
            f"https://en.wikipedia.org/wiki/"
            f"{title.replace(' ', '_')}"
        ),
        "metadata": {
            "kind": "rivalry_recap",
            "rank_count": min(n, len(fixtures)),
            "researched_at": datetime.now(timezone.utc).isoformat(),
            "rivalry": {
                "entity_a": entity_a,
                "entity_b": entity_b,
                "fixture_filter": "competitive (PL + cups + Europe), exclude friendlies",
                "wiki_title": title,
            },
            "fixtures": [
                {
                    "rank": min(n, len(fixtures)) - i,  # most recent = rank 1
                    "date": f.get("date", ""),
                    "competition": f.get("competition", ""),
                    "venue": f.get("venue", ""),
                    "score": f.get("score", ""),
                    "home_team": f.get("home_team", ""),
                    "away_team": f.get("away_team", ""),
                    "key_moment": f.get("key_moment", ""),
                    "key_player": f.get("key_player", ""),
                    "broadcast_research_query": _research_query(
                        entity_a, entity_b, f
                    ),
                }
                for i, f in enumerate(fixtures)
            ],
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out_path


def _research_query(entity_a: str, entity_b: str, fixture: dict) -> str:
    """YouTube search query string the skill author runs to find the broadcast clip."""
    year = (fixture.get("date") or "")[:4]
    competition = fixture.get("competition") or ""
    return (
        f"{entity_a} vs {entity_b} {year} {competition} highlights official"
    ).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entity-a", required=True, help="First side of the rivalry")
    ap.add_argument("--entity-b", required=True, help="Second side of the rivalry")
    ap.add_argument("--last", type=int, default=5, help="Number of fixtures (default 5)")
    ap.add_argument(
        "--slug",
        required=True,
        help="Slug for the output raw JSON (e.g. mufc-vs-lfc-last5)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("sportstoriesanimated/ranked"),
        help="Output directory (raw/<slug>.json is written underneath)",
    )
    ap.add_argument(
        "--wiki-list-url",
        default=None,
        help=(
            "Specific Wikipedia URL to use instead of search "
            "(e.g. /wiki/List_of_El_Clasico_matches)"
        ),
    )
    ap.add_argument(
        "--model",
        default="haiku",
        help="claude CLI model (haiku default — cheap; sonnet/opus for tricky pulls)",
    )
    args = ap.parse_args()

    article = _resolve_article(
        entity_a=args.entity_a,
        entity_b=args.entity_b,
        wiki_list_url=args.wiki_list_url,
    )
    if not article:
        print(
            f"[research] could not find a Wikipedia article for "
            f"{args.entity_a} vs {args.entity_b}; pass --wiki-list-url"
        )
        return 1

    title, body = article
    print(f"[research] resolved Wikipedia article: {title}")
    print(f"[research] body length: {len(body)} chars")

    prompt = _PROMPT_TMPL.format(
        N=args.last,
        ENTITY_A=args.entity_a,
        ENTITY_B=args.entity_b,
        TITLE=title,
        BODY=_shorten(body),
    )

    print(
        f"[research] asking claude {args.model} for last {args.last} "
        f"fixtures (structured-output)..."
    )
    response = llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=_FIXTURES_SCHEMA,
        model=args.model,
    )
    if not isinstance(response, dict) or "fixtures" not in response:
        print(f"[research] unexpected response shape: {response!r}")
        return 1

    fixtures = response.get("fixtures") or []
    if not fixtures:
        print("[research] LLM returned zero fixtures — try --wiki-list-url")
        return 1

    out_path = _write_raw(
        out_dir=args.out,
        slug=args.slug,
        entity_a=args.entity_a,
        entity_b=args.entity_b,
        n=args.last,
        title=title,
        fixtures=fixtures,
    )
    print(f"[research] wrote {out_path}")
    print(f"[research] {len(fixtures)} fixture(s) extracted:")
    for i, f in enumerate(fixtures):
        rank = min(args.last, len(fixtures)) - i
        print(
            f"  #{rank}  {f.get('date','?'):10s}  "
            f"{f.get('venue','?'):24s}  "
            f"{f.get('score','?'):14s}  "
            f"{(f.get('key_moment','') or '')[:60]}"
        )
    print(
        "\nnext: hand to /make-rivalry-recap to author narration + pick "
        "broadcast clips for each fixture's broadcast_research_query."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
