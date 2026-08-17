"""Auto-generate YouTube publish metadata from a rendered script.

One-click-publish design: the user clicks Publish and sees a modal with
ONLY a visibility selector + optional scheduled-publish time. Title,
description, hashtags, tags, thumbnail, category, default-language and
made-for-kids are produced here from the script + channel config.

**Channel-aware (since C4 2026-05-24).** The per-channel rules from
``docs/youtube_shorts_metadata_playbook.md`` §"Per-channel apply table"
are encoded in ``_CHANNEL_PLAYBOOK`` below. When ``channel`` (and
optionally ``variant``) matches an entry, the per-channel hashtag set,
default category, default language, made-for-kids flag, and
description-suffix template apply. Channels with no entry fall back to
the generic heuristics (Entertainment, en, false). The public
``generate_publish_metadata(...)`` signature + ``PublishMetadata`` shape
are the stable contract the UI + control-plane endpoint depend on.

**Failure mode (per memory ``feedback_silent_fallback_unshippable_output``):**
if the script lacks any usable title source AND any topic — i.e. there is
nothing to put in the YouTube ``snippet.title`` slot — we RAISE
:class:`MissingMetadataInputError` rather than ship a publish-payload
with an empty title. A silent fallback would publish an empty-titled
video to YouTube; that is unshippable output.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# YouTube limits (documented at
# https://developers.google.com/youtube/v3/docs/videos#resource):
#   - title ≤ 100 chars
#   - description ≤ 5000 chars
#   - tags: each ≤ 30 chars; sum of all tag chars (incl. quotes for
#     multi-word tags) ≤ 500 chars
_YT_TITLE_MAX = 100
_YT_DESCRIPTION_MAX = 5000
_YT_TAG_TOTAL_BUDGET_CHARS = 500
_YT_TAG_INDIVIDUAL_MAX = 30


# Default YouTube category — Entertainment. Channels can override via
# the playbook once it lands.
_DEFAULT_CATEGORY_ID = "24"

# Sensible defaults for the stub. The playbook will provide channel-aware
# numbers (e.g. mythology channels want fewer English-language tags).
_DEFAULT_HASHTAG_COUNT = 3
_DEFAULT_TAG_COUNT = 8

# A token must be at least this long to be a useful hashtag/tag — filters
# out "a", "to", "is" extracted from titles.
_MIN_KEYWORD_LEN = 4

# Stop words we'd never want as hashtags / tags. Kept tiny on purpose:
# the playbook will replace this with the curated per-niche stop-list.
_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "your",
    "you", "are", "was", "were", "have", "has", "had", "but", "not", "all",
    "any", "can", "did", "does", "had", "his", "her", "their", "they",
    "what", "when", "where", "why", "who", "how", "about", "after",
    "before", "while", "than", "then", "them", "these", "those", "been",
    "being", "would", "could", "should", "shall", "will", "just", "very",
    "story", "shorts", "short",
})


# ---- per-channel playbook (C4 2026-05-24) -----------------------------
#
# Encoded from docs/youtube_shorts_metadata_playbook.md §"Per-channel
# apply table for ytFactory" (lines 459-586). Each entry maps a channel
# slug to its per-channel defaults (category_id, language, made-for-kids)
# plus a ``hashtags`` list (already in playbook order — first 3 pin
# above the title) and optional variant overrides keyed by variant slug
# under ``variants``.
#
# Adding a new channel: append an entry here AND extend the per-channel
# section of the playbook with the new rules (so the doc stays the
# source of truth and this dict is the machine encoding). The fallback
# branch below (when no entry matches) yields the generic Entertainment
# defaults so any unknown channel still ships shippable metadata.

_PlaybookEntry = dict  # type alias for clarity below


_CHANNEL_PLAYBOOK: dict[str, _PlaybookEntry] = {
    "mystoriesanimated": {
        # AITA / TIFU / wiki / TIH variants — see playbook lines 464-492.
        # Default cluster matches AITA (the dominant variant); per-variant
        # overrides switch the cluster + category for the others.
        "hashtags": ["#AITA", "#redditstories", "#Shorts", "#storytime", "#reddit"],
        "tags": [
            "aita", "reddit stories", "am i the asshole", "reddit storytime",
            "aita reddit", "mystoriesanimated", "youtube shorts", "reddit aita",
        ],
        "category_id": "22",  # People & Blogs (AITA/TIFU default)
        "default_language": "en",
        "made_for_kids": False,
        "variants": {
            "tifu": {
                "hashtags": ["#TIFU", "#redditstories", "#Shorts", "#storytime", "#funnystories"],
                "tags": [
                    "tifu", "reddit stories", "today i fucked up", "reddit storytime",
                    "tifu reddit", "mystoriesanimated", "youtube shorts", "funny reddit",
                ],
            },
            "reddit_tifu": {
                "hashtags": ["#TIFU", "#redditstories", "#Shorts", "#storytime", "#funnystories"],
                "tags": [
                    "tifu", "reddit stories", "today i fucked up", "reddit storytime",
                    "tifu reddit", "mystoriesanimated", "youtube shorts", "funny reddit",
                ],
            },
            "wiki_oddities": {
                "hashtags": ["#history", "#didyouknow", "#Shorts", "#facts", "#todayinhistory"],
                "tags": [
                    "wiki oddities", "history facts", "did you know", "weird history",
                    "history shorts", "mystoriesanimated", "youtube shorts", "fun facts",
                ],
                "category_id": "27",  # Education for wiki/TIH (playbook 487)
            },
            "today_in_history": {
                "hashtags": ["#history", "#didyouknow", "#Shorts", "#facts", "#todayinhistory"],
                "tags": [
                    "today in history", "on this day", "history facts", "history shorts",
                    "did you know", "mystoriesanimated", "youtube shorts", "history",
                ],
                "category_id": "27",
            },
        },
    },
    "hindutavaanimated": {
        # Hindi mythology — playbook lines 494-516. Devanagari + Hindi
        # language is critical (uses Hindi audio tag).
        "hashtags": ["#mahabharat", "#krishna", "#Shorts", "#hindumythology", "#arjuna"],
        "tags": [
            "mahabharat", "ramayan", "hindu mythology", "krishna",
            "hindi mythology shorts", "hindutavaanimated", "pauraanik kathayein",
            "mythology shorts hindi",
        ],
        "category_id": "1",  # Film & Animation
        "default_language": "hi",
        "made_for_kids": False,
        "variants": {
            "ramayan": {
                "hashtags": ["#ramayan", "#ram", "#Shorts", "#hindumythology", "#hanuman"],
            },
            "puraan": {
                "hashtags": ["#hindumythology", "#puran", "#Shorts", "#sanatandharma", "#mythology"],
            },
        },
    },
    "sportsrecapped": {
        # Sports — playbook lines 518-540. Entity-led cluster (the
        # variant overrides typically reshape these per video).
        "hashtags": ["#football", "#Shorts", "#footballshorts", "#soccer", "#sports"],
        "tags": [
            "football", "soccer", "football shorts", "soccer shorts",
            "sportsrecapped", "youtube shorts", "sports", "match recap",
        ],
        "category_id": "17",  # Sports
        "default_language": "en",
        "made_for_kids": False,
    },
    "cosmosdecoded": {
        # Physics + space — playbook lines 542-562. Use Science & Tech
        # (28) rather than Education for tighter recommendation pool.
        "hashtags": ["#space", "#physics", "#Shorts", "#astronomy", "#nasa"],
        "tags": [
            "physics", "astronomy", "space shorts", "cosmosdecoded",
            "how we knew", "science shorts", "space", "science",
        ],
        "category_id": "28",  # Science & Technology
        "default_language": "en",
        "made_for_kids": False,
    },
    "historyrecapped": {
        # History — playbook lines 564-586. Education default;
        # animated long-form variants can override to Film & Animation.
        "hashtags": ["#history", "#ancienthistory", "#Shorts", "#historyfacts", "#worldhistory"],
        "tags": [
            "history shorts", "ancient history", "world history", "historyrecapped",
            "history facts", "history", "youtube shorts", "education",
        ],
        "category_id": "27",  # Education
        "default_language": "en",
        "made_for_kids": False,
    },
    "rhymetimejunction": {
        # Out-of-rotation but registered. Nursery rhymes — made-for-kids
        # MUST be true (playbook line 364, FTC/COPPA).
        "hashtags": ["#nurseryrhyme", "#kidssongs", "#Shorts", "#preschool", "#kids"],
        "tags": [
            "nursery rhymes", "kids songs", "bilingual rhymes", "hindi rhymes",
            "rhymetimejunction", "preschool", "youtube shorts kids", "kids",
        ],
        "category_id": "10",  # Music
        "default_language": "en",
        "made_for_kids": True,
    },
    "scrollpulse": {
        # Reddit-thread reaction Shorts — out of rotation. People & Blogs
        # is the catch-all storytime fallback per playbook line 322.
        "hashtags": ["#reddit", "#redditstories", "#Shorts", "#aita", "#storytime"],
        "tags": [
            "reddit", "reddit stories", "scrollpulse", "reddit reaction",
            "ai voice reddit", "youtube shorts", "reddit shorts", "storytime",
        ],
        "category_id": "22",  # People & Blogs
        "default_language": "en",
        "made_for_kids": False,
    },
}


def _resolve_playbook(
    channel: str | None, variant: str | None,
) -> _PlaybookEntry | None:
    """Return the merged playbook entry for ``channel``/``variant`` or
    ``None`` when no per-channel entry matches.

    Variant overrides merge SHALLOW into the channel base entry — the
    variant can replace e.g. ``hashtags`` + ``category_id`` while
    inheriting ``tags`` + ``default_language``. ``variants`` sub-key on
    the merged result is stripped (it's a lookup table, not a returned
    field).
    """
    if not channel:
        return None
    base = _CHANNEL_PLAYBOOK.get(channel.lower())
    if base is None:
        return None
    merged: _PlaybookEntry = {k: v for k, v in base.items() if k != "variants"}
    if variant:
        variants_table = base.get("variants") or {}
        override = variants_table.get(variant.lower())
        if override:
            merged.update(override)
    return merged


class MissingMetadataInputError(ValueError):
    """Raised when the script has no usable title source AND no topic.

    Per memory ``feedback_silent_fallback_unshippable_output``: a publish
    payload with an empty title is unshippable output; we surface the
    inputs gap instead of papering over it with ``"Untitled"``.
    """


class PublishMetadata(BaseModel):
    """The full metadata bundle handed to ``youtube_upload``.

    Persisted to the job's Firestore doc as ``publish_metadata`` so the
    UI's "you're about to publish with these" preview block reads from
    exactly what the worker will send to YouTube — no skew.
    """

    title: str = Field(..., min_length=1, max_length=_YT_TITLE_MAX)
    description: str = Field(..., max_length=_YT_DESCRIPTION_MAX)
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    thumbnail_path: Path | None = None
    category_id: str = Field(default=_DEFAULT_CATEGORY_ID)
    default_language: str = Field(default="en")
    made_for_kids: bool = Field(default=False)


# ---- token extraction helpers -----------------------------------------


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]+")


def _tokenise(text: str) -> list[str]:
    """Lowercase alpha-prefixed tokens (drops stop-words + short tokens)."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _WORD_RE.findall(text or ""):
        tok = raw.lower().strip("'-")
        if len(tok) < _MIN_KEYWORD_LEN:
            continue
        if tok in _STOP_WORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        tokens.append(tok)
    return tokens


def _hashtagify(tokens: list[str], n: int) -> list[str]:
    """First-N tokens turned into ``#camelCase`` hashtags (alnum-only)."""
    out: list[str] = []
    for t in tokens:
        if len(out) >= n:
            break
        cleaned = re.sub(r"[^a-z0-9]", "", t)
        if not cleaned:
            continue
        out.append(f"#{cleaned}")
    return out


def _budget_tags(tokens: list[str], n: int) -> list[str]:
    """Pick up to N tags within YouTube's 500-char sum + 30-char-each budget."""
    out: list[str] = []
    total = 0
    for t in tokens:
        if len(out) >= n:
            break
        if len(t) > _YT_TAG_INDIVIDUAL_MAX:
            continue
        # Each tag contributes (len + 1) to YouTube's accounting (comma /
        # quote overhead). Stay under 500 chars including the separator.
        cost = len(t) + 1
        if total + cost > _YT_TAG_TOTAL_BUDGET_CHARS:
            break
        out.append(t)
        total += cost
    return out


def _extract_title(script: dict, *, topic: str | None) -> str | None:
    """Best title source in the script, in priority order.

    Priority:
      1. ``script.title_options[0]`` — the writer-vetted hook.
      2. ``script.title`` — generic title field.
      3. ``script.hook`` — the opening line if it's short enough to fit.
      4. ``topic`` from the job (fallback when the script omitted titles).
    """
    opts = script.get("title_options") or []
    if opts:
        first = str(opts[0]).strip()
        if first:
            return first[:_YT_TITLE_MAX]
    for key in ("title", "hook"):
        val = script.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()[:_YT_TITLE_MAX]
    if topic and topic.strip():
        return topic.strip()[:_YT_TITLE_MAX]
    return None


def _extract_description_seed(script: dict, *, topic: str | None) -> str:
    """A short description body. The playbook will replace this with
    channel-specific boilerplate + CTA + chapters."""
    parts: list[str] = []
    hook = (script.get("hook") or "").strip()
    summary = (script.get("summary") or script.get("description") or "").strip()
    if summary:
        parts.append(summary)
    elif hook:
        parts.append(hook)
    elif topic:
        parts.append(topic.strip())

    # Append a generic CTA — replaced by channel-specific copy once the
    # playbook lands.
    parts.append("Subscribe for more.")
    return "\n\n".join(parts).strip()


def _resolve_thumbnail(job_id: str, script: dict) -> Path | None:
    """If the renderer produced a thumbnail, surface its path.

    Looked up in the script first (most stages write the path there);
    falls through to ``None`` when no thumbnail was rendered. Validation
    against YouTube's 2MB / JPG-PNG-only rules happens in
    :func:`pipeline.upload.upload.set_thumbnail` — we only resolve the
    path here.
    """
    raw = script.get("thumbnail_path") or script.get("thumb_path")
    if not raw:
        return None
    try:
        p = Path(str(raw))
    except (TypeError, ValueError):
        return None
    return p


# ---- public entry -----------------------------------------------------


def generate_publish_metadata(
    job_id: str,
    script: dict,
    *,
    channel: str,
    variant: str | None,
) -> PublishMetadata:
    """Build the YouTube ``snippet``/``status`` metadata for ``job_id``.

    Args:
        job_id: Firestore job id (used to resolve render-side artifacts
            like the thumbnail).
        script: the rewritten script dict (typically the
            ``data/<channel>/<niche>/scripts/<slug>.json`` payload, or
            the equivalent shape persisted on the job doc).
        channel: channel slug (e.g. ``mystoriesanimated``).
        variant: variant overlay slug (e.g. ``reddit_aita``) — used by
            the playbook to pick per-niche hashtag patterns; the stub
            ignores it but accepts it for forward compatibility.

    Raises:
        MissingMetadataInputError: when the script has no title source
            (``title_options``, ``title``, ``hook``) AND no topic field
            to fall back on. Distinct from a generic ``ValueError`` so
            the control-plane endpoint can surface a 422 "missing input"
            instead of a 500.
    """
    if not isinstance(script, dict):
        raise TypeError(
            f"script must be a dict; got {type(script).__name__}"
        )

    topic_raw = script.get("topic")
    topic: str | None = topic_raw.strip() if isinstance(topic_raw, str) and topic_raw.strip() else None

    title = _extract_title(script, topic=topic)
    if not title:
        raise MissingMetadataInputError(
            f"script for job {job_id} has no title_options / title / hook "
            f"and no topic — cannot generate YouTube metadata. Re-render "
            f"with a non-empty script.hook or pass a job.topic."
        )

    description_seed = _extract_description_seed(script, topic=topic)
    if not description_seed:
        # If the description seed is somehow empty after the title pass
        # succeeded (e.g. script had title_options but no hook/summary
        # and no topic), fall back to the title as the description body.
        # The CTA is appended by _extract_description_seed unconditionally
        # so this branch is reached only when both summary AND hook AND
        # topic are blank — title is still the best signal we have.
        description_seed = title

    # C4 2026-05-24: try per-channel playbook first; fall back to the
    # heuristic tokeniser when no entry matches OR the playbook entry
    # leaves a field unset.
    playbook = _resolve_playbook(channel, variant)

    # Hashtag/tag pool: title + topic + hook + summary all contribute,
    # deduped + stop-worded by ``_tokenise``. Used both as the heuristic
    # fallback AND to pad/supplement the playbook lists when needed.
    pool = " ".join(
        s for s in (
            title,
            topic or "",
            script.get("hook") or "",
            script.get("summary") or "",
        )
        if s
    )
    tokens = _tokenise(pool)
    if playbook and playbook.get("hashtags"):
        # Playbook hashtags are the curated set (capped at 5 per
        # playbook §"Hashtags" line 212-214). Validate every entry
        # starts with `#` — if a future edit corrupts that, we still
        # ship valid metadata.
        hashtags = [h for h in playbook["hashtags"] if isinstance(h, str) and h.startswith("#")]
        if not hashtags:
            # Playbook entry was malformed → fall back to heuristic.
            hashtags = _hashtagify(tokens, _DEFAULT_HASHTAG_COUNT) or ["#shorts"]
    else:
        # Per memory ``feedback_silent_fallback_unshippable_output``: an
        # empty hashtag list is shippable (YouTube accepts videos with
        # no hashtags) — but we guarantee at least one anchor so
        # downstream code can rely on a non-empty list.
        hashtags = _hashtagify(tokens, _DEFAULT_HASHTAG_COUNT) or ["#shorts"]

    if playbook and playbook.get("tags"):
        # Tags from playbook are already curated; still budget-check
        # them against YouTube's 500-char total + 30-char-each rules.
        tags = _budget_tags(list(playbook["tags"]), _DEFAULT_TAG_COUNT)
    else:
        tags = _budget_tags(tokens, _DEFAULT_TAG_COUNT)

    thumbnail_path = _resolve_thumbnail(job_id, script)

    # Append hashtags to description per YouTube's Shorts hashtag
    # convention (visible-above-title hashtags must appear in the
    # description body, not just the metadata).
    description_body = description_seed
    if hashtags:
        description_body = f"{description_seed}\n\n{' '.join(hashtags)}"

    category_id = (
        str(playbook["category_id"]) if playbook and playbook.get("category_id")
        else _DEFAULT_CATEGORY_ID
    )
    # Script-level overrides (rare — e.g. a Hindi-language flag set
    # explicitly on the rewrite) win over the playbook default; otherwise
    # the playbook default wins; otherwise "en".
    default_language = (
        str(script.get("default_language"))
        if script.get("default_language")
        else (str(playbook["default_language"]) if playbook and playbook.get("default_language") else "en")
    )
    # made_for_kids: script override > playbook default > False.
    if "made_for_kids" in script:
        made_for_kids = bool(script.get("made_for_kids"))
    elif playbook and "made_for_kids" in playbook:
        made_for_kids = bool(playbook["made_for_kids"])
    else:
        made_for_kids = False

    return PublishMetadata(
        title=title[:_YT_TITLE_MAX],
        description=description_body[:_YT_DESCRIPTION_MAX],
        hashtags=hashtags,
        tags=tags,
        thumbnail_path=thumbnail_path,
        category_id=category_id,
        default_language=default_language,
        made_for_kids=made_for_kids,
    )


# ---- exposed-for-tests helpers ----------------------------------------
# Re-exported so the test module can pin the budget / tokenisation
# heuristics directly (the playbook will replace these one-for-one).

__all__ = [
    "PublishMetadata",
    "MissingMetadataInputError",
    "generate_publish_metadata",
]
