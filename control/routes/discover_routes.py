"""Auto-generate a topic for a channel — drives the create-flow
"Auto-generate topic" button.

POST /api/discover/{channel}      → one suggested topic with full source attribution
GET  /api/discover/{channel}/feed → up to 10 candidates the user can pick from

Both endpoints accept an optional :class:`DiscoverRequest` body / query
string carrying the user's currently selected form context — variant,
length_kind, language, niche_key, free-form values, and an avoid list of
topics already shown.

Routing model (2026-05-12 refactor — engineering-level fix for "topic
unrelated to selected niche"):

* **Niche-driven native routing.** When ``niche_key`` is provided AND
  resolves to a persisted :class:`pipeline.niche_specs.NicheDoc`, the
  niche's own ``source: {kind, ref}`` field is the single source of
  truth. Dispatch is uniform across every channel:

  | source.kind | source.ref                   | adapter          |
  |-------------|------------------------------|------------------|
  | reddit      | subreddit name               | reddit           |
  | wikipedia   | ``On_this_day``              | today_in_history |
  | wikipedia   | ``List_of_*`` / page title   | wikipedia list   |
  | wikipedia   | ``None``                     | LLM only         |
  | rss         | HN feed URL                  | HN top-AI        |
  | rss         | other / ``None``             | LLM only         |
  | manual / x_twitter / unknown / missing  | n/a | LLM only |

* **No fallback to channel defaults when a niche is selected.** If the
  niche resolves but has no actionable native source (manual, unknown,
  ``wikipedia`` with no ref, …), the native side returns ``[]`` and
  the LLM brainstorm carries — we deliberately do NOT fall back to a
  channel-default Wikipedia "On this day" call, which was the original
  bug source.

* **Channel-default routing only when no niche is selected.** Older
  callers that hit the GET feed endpoint with no query string fall
  through to the legacy per-channel branches in
  :func:`_native_items_for`. Every modern create-flow path passes a
  niche, so this is a back-compat safety net only.

* **LLM brainstorm is always mixed in** (per user decision 2026-05-11)
  so the picked topic respects the selected variant / niche / language
  even on Reddit-backed channels. For channels with no native source
  (``hindutavaanimated``, ``sportsrecapped``, ``rhymetimejunction``)
  LLM is the sole adapter.

* The endpoint **never returns 422 for unknown channels** — the LLM
  brainstorm path is universally available so the UI always gets a
  usable suggestion.

The endpoint is read-only — pulling never enqueues a job. The user
clicks "Use this" in the UI, which fills the Topic field.
"""
from __future__ import annotations

import json
import logging
import random
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discover")


# Per-channel discovery config
SCROLLPULSE_SUBREDDITS = [
    "AmItheAsshole",
    "AskReddit",
    "tifu",
    "relationship_advice",
    "MaliciousCompliance",
    "Showerthoughts",
    "TrueOffMyChest",
]


# Variant key (in pipeline/niches.py) → reddit subreddit. Variants not
# listed here have no native Reddit source and flow to LLM brainstorm.
VARIANT_SUBREDDIT: dict[str, str] = {
    "aita":              "AmItheAsshole",
    "aita_cliffhanger":  "AmItheAsshole",
    "tifu":              "tifu",
    "malicious":         "MaliciousCompliance",
    "prorevenge":        "ProRevenge",
}

# Variant key → wikipedia keyword (filters today_in_history by topic).
VARIANT_WIKI_KEYWORD: dict[str, str | None] = {
    "oddities":           None,  # wiki_oddities — full feed, no filter
    "tih":                None,
    "wiki_misconceptions": "misconception",
}

# Default channel-level adapter when no variant context is provided.
CHANNEL_DEFAULT_SUBREDDIT: dict[str, str] = {
    "mystoriesanimated": "AmItheAsshole",
}

# Channels the LLM brainstorm is gated *off* of via the YTFACTORY_LLM_*
# env (so unit tests / CI don't hit Azure unintentionally). Set
# ``YTFACTORY_DISCOVER_LLM_DISABLE=1`` to disable LLM augmentation
# globally (callers fall back to native-only).
LLM_BRAINSTORM_COUNT = 4


class DiscoverContextValues(BaseModel):
    """Subset of user-selected create-form fields the LLM uses for context.

    ``model_config`` allows arbitrary keys so the FE can keep evolving
    the form schema without a backend roundtrip.
    """

    model_config = {"extra": "allow"}


class DiscoverRequest(BaseModel):
    """Optional body / query carrying the create-form context.

    Validators are intentionally lenient: the FE form values dict is a
    moving target (new fields land all the time, and React state flips
    can briefly serialise undefined/null where a string is expected).
    Hard 422s here are user-visible noise that the operator can't fix —
    we'd rather coerce-or-drop than reject the whole request.
    """

    variant: str | None = None
    length_kind: str | None = None  # "short" | "long"
    language: str | None = None
    niche_key: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    avoid: list[str] = Field(default_factory=list)  # already-shown topics

    @field_validator("variant", "length_kind", "language", "niche_key", mode="before")
    @classmethod
    def _coerce_optional_str(cls, v):
        # Treat empty / missing / non-stringy as None rather than 422-ing.
        if v is None:
            return None
        if isinstance(v, bool):
            # bool subclasses int — but a bool here is meaningless;
            # silently drop rather than persist "True" as a variant key.
            return None
        if isinstance(v, str):
            s = v.strip()
            return s or None
        if isinstance(v, (int, float)):
            return str(v)
        return None

    @field_validator("values", mode="before")
    @classmethod
    def _coerce_values(cls, v):
        # The FE may send `null` / `undefined` for the form values dict
        # before any field has been touched. Treat both as empty dict.
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        # Last-ditch: ignore rather than reject.
        logger.debug("discover: dropping non-dict values payload (%s)", type(v).__name__)
        return {}

    @field_validator("avoid", mode="before")
    @classmethod
    def _coerce_avoid(cls, v):
        # Drop non-string entries instead of 422-ing on a single bad item.
        if v is None:
            return []
        if isinstance(v, str):
            # Single string instead of a list — wrap.
            return [v]
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            if isinstance(item, bool):
                # bool subclasses int — explicitly skip; "True"/"False"
                # is meaningless as an avoid-topic.
                continue
            if isinstance(item, str) and item.strip():
                out.append(item)
            elif isinstance(item, (int, float)):
                out.append(str(item))
        return out


class DiscoverItem(BaseModel):
    topic: str
    source_kind: str  # reddit_url | wikipedia_topic | user_text | youtube_video | auto | llm
    source_ref: str | None = None  # canonical URL or None for LLM picks
    source_label: str  # "r/AmItheAsshole · top of day" etc — display string
    source_excerpt: str | None = None  # short preview of the body
    metadata: dict[str, Any] = {}


class DiscoverFeed(BaseModel):
    channel: str
    adapter: str
    items: list[DiscoverItem]


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------


def _excerpt(text: str, n: int = 280) -> str:
    text = (text or "").strip().replace("\r", "").replace("\n\n", " ¶ ")
    return text[: n - 1] + "…" if len(text) > n else text


def _reddit_items(subreddit: str, listing: str = "top",
                  timeframe: str = "day", limit: int = 8) -> list[DiscoverItem]:
    from pipeline.sources import reddit_api  # noqa: PLC0415

    stories = reddit_api.fetch(
        subreddit=subreddit,
        listing=listing,
        timeframe=timeframe,
        limit=limit,
        min_chars=300,
    )
    out: list[DiscoverItem] = []
    for s in stories:
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="reddit_url",
            source_ref=s.url,
            source_label=f"r/{subreddit} · {listing} of {timeframe}",
            source_excerpt=_excerpt(s.body),
            metadata=s.metadata,
        ))
    return out


def _today_in_history_items(limit: int = 8, keyword: str | None = None) -> list[DiscoverItem]:
    from pipeline.sources import today_in_history  # noqa: PLC0415

    stories = today_in_history.fetch(limit=20)
    out: list[DiscoverItem] = []
    for s in stories:
        if keyword and keyword.lower() not in (s.title + " " + s.body).lower():
            continue
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="wikipedia_topic",
            source_ref=s.url,
            source_label=f"Wikipedia · on this day{' · ' + keyword if keyword else ''}",
            source_excerpt=_excerpt(s.body),
            metadata={"year": s.metadata.get("year"), "license": "CC-BY-SA"},
        ))
        if len(out) >= limit:
            break
    return out


def _wikipedia_list_items(page: str, limit: int = 8) -> list[DiscoverItem]:
    """Scrape a Wikipedia list page (``List_of_*`` / ``Lists_of_*``).

    Wraps :func:`pipeline.sources.wikipedia.fetch`, which HTML-scrapes
    bullet-list and wikitable entries from one page and returns a
    :class:`pipeline.sources.base.RawStory` per entry. Each emitted
    :class:`DiscoverItem` gets a UNIQUE ``source_ref`` (page URL + an
    entry-slug fragment) so :func:`_filter_avoid` can blacklist a single
    picked entry without nuking the whole page.

    Returns ``[]`` when the page yields no usable entries (e.g.,
    Wikipedia hub pages that just link to per-era leaf lists). Callers
    use this signal to fall through to the LLM brainstorm.
    """
    from pipeline.sources import wikipedia  # noqa: PLC0415

    stories = wikipedia.fetch(page=page, limit=limit, min_chars=80)
    out: list[DiscoverItem] = []
    pretty_page = page.replace("_", " ")
    for s in stories:
        # Per-entry source_ref so avoid-list filtering is item-scoped,
        # not page-scoped. Wikipedia list entries don't have their own
        # canonical URL (the parser only knows the host page), so we
        # synthesise a fragment id from the entry slug.
        entry_ref = f"{s.url}#{s.slug}" if s.url else s.slug
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="wikipedia_topic",
            source_ref=entry_ref,
            source_label=f"Wikipedia · {pretty_page}",
            source_excerpt=_excerpt(s.body),
            metadata={
                "page": s.metadata.get("page", page),
                "page_url": s.url,
                "license": s.metadata.get("license", "CC-BY-SA"),
            },
        ))
    return out


def _ai_news_items(limit: int = 8) -> list[DiscoverItem]:
    from pipeline.sources import ai_news  # noqa: PLC0415

    stories = ai_news.fetch(limit=limit)
    out: list[DiscoverItem] = []
    for s in stories:
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="user_text",  # external article but we render as topic
            source_ref=s.url,
            source_label="HN · top AI today",
            source_excerpt=_excerpt(s.body),
            metadata=s.metadata,
        ))
    return out


# ---------------------------------------------------------------------------
# LLM brainstorm
# ---------------------------------------------------------------------------


_LLM_BRAINSTORM_SYSTEM = """You are a Shorts producer brainstorming
fresh, hook-first topic ideas for a specific YouTube channel. The
operator will render the chosen topic with the channel's existing
narrator voice, image style and length budget — your only job is to
return short, scroll-stopping topic seeds (NOT full scripts).

Hard rules:
* Match the channel language exactly (English / Hindi / Hinglish).
* Honour the operator's selected variant + niche style guide when given.
* Each topic must be self-contained (a complete idea, not a teaser).
* Keep each topic under 110 characters.
* No topic that overlaps semantically with the operator's "avoid" list.

Return ONLY a JSON object with the shape:

  {"items": [{"topic": "...", "hook": "..."}]}

The "hook" is one short sentence (≤25 words) the narrator could open with.
Output the JSON object only — no markdown, no commentary."""


def _resolve_niche_doc(channel: str, niche_key: str | None):
    if not niche_key:
        return None
    try:
        from pipeline.niche_specs import get_niche  # noqa: PLC0415
        return get_niche(channel, niche_key)
    except Exception:  # noqa: BLE001
        logger.debug("niche lookup failed for %s/%s", channel, niche_key, exc_info=True)
        return None


def _channel_summary(channel: str):
    try:
        from pipeline.schemas import customization  # noqa: PLC0415
        return customization.get_channel(channel)
    except Exception:  # noqa: BLE001
        return None


def _llm_disabled() -> bool:
    import os  # noqa: PLC0415
    return os.environ.get("YTFACTORY_DISCOVER_LLM_DISABLE", "").strip().lower() in {"1", "true", "yes"}


def _llm_topic_items(channel: str, req: DiscoverRequest, *, count: int = LLM_BRAINSTORM_COUNT) -> list[DiscoverItem]:
    """Brainstorm topic ideas with the configured LLM backend.

    Resilient: any failure (no backend, network error, malformed JSON,
    schema mismatch) returns ``[]`` and the caller falls back to native
    sources / a friendlier 502.
    """
    if _llm_disabled() or count <= 0:
        return []

    summary = _channel_summary(channel)
    niche_doc = _resolve_niche_doc(channel, req.niche_key)

    bits: list[str] = []
    if summary:
        bits.append(f"Channel: {summary.label} ({channel}) — {summary.tagline}.")
        bits.append(f"Language: {summary.language}. Default format: {summary.default_format}.")
    else:
        bits.append(f"Channel: {channel}.")
    if req.variant:
        bits.append(f"Variant: {req.variant}.")
    if req.length_kind:
        bits.append(f"Length kind: {req.length_kind}.")
    if req.language:
        bits.append(f"User-selected language: {req.language}.")
    if niche_doc:
        bits.append(f"Niche label: {niche_doc.label}. Description: {niche_doc.description}")
        if niche_doc.prompt_style_guide:
            bits.append(f"Style guide: {niche_doc.prompt_style_guide}")
        if niche_doc.hook_template:
            bits.append(f"Preferred hook template: {niche_doc.hook_template}")
    notes = (req.values.get("notes") or "").strip() if req.values else ""
    if notes:
        bits.append(f"Operator notes for this batch: {notes}")
    if req.avoid:
        # Cap to keep prompt short.
        avoid = req.avoid[:20]
        bits.append("Avoid topics that overlap with: " + " | ".join(avoid))
    bits.append(f"Return at least {count} topic ideas.")

    user_msg = "\n".join(bits)

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "minLength": 4, "maxLength": 200},
                        "hook":  {"type": "string", "maxLength": 240},
                    },
                    "required": ["topic"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    try:
        from pipeline.llm.cli import call_llm  # noqa: PLC0415
        prompt = f"{_LLM_BRAINSTORM_SYSTEM}\n\n{user_msg}"
        result = call_llm(
            prompt,
            output_json=True,
            json_schema=schema,
            model="haiku",
            stage="discover_brainstorm",
        )
    except Exception as e:  # noqa: BLE001
        # Bumped from INFO → WARNING (2026-05-12). When this is the
        # only reason discover 502s (Reddit native also failed), we
        # need to see it in the cloud log scan, not just in INFO-level
        # debug streams.
        logger.warning(
            "LLM brainstorm raised for %s (variant=%s niche=%s): %s",
            channel, req.variant, req.niche_key, e,
        )
        return []

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "LLM brainstorm returned non-JSON string for %s: %s "
                "(first 200 chars: %r)",
                channel, e, result[:200],
            )
            return []
    if not isinstance(result, dict):
        logger.warning(
            "LLM brainstorm returned non-dict result for %s: type=%s",
            channel, type(result).__name__,
        )
        return []
    raw_items = result.get("items") or []
    if not isinstance(raw_items, list):
        logger.warning(
            "LLM brainstorm 'items' field is not a list for %s: type=%s",
            channel, type(raw_items).__name__,
        )
        return []
    if not raw_items:
        # Common silent failure mode: Azure honours response_format and
        # returns ``{"items": []}`` rather than raising. Without this
        # log line the only symptom is a 502 with no upstream cause.
        logger.warning(
            "LLM brainstorm returned empty 'items' for %s (variant=%s niche=%s)",
            channel, req.variant, req.niche_key,
        )
        return []

    label_bits = [f"LLM · {summary.label}"] if summary else ["LLM"]
    if req.variant:
        label_bits.append(req.variant)
    label = " · ".join(label_bits)

    out: list[DiscoverItem] = []
    seen: set[str] = set()
    for r in raw_items:
        if not isinstance(r, dict):
            continue
        topic = (r.get("topic") or "").strip()
        if not topic or topic.lower() in seen:
            continue
        seen.add(topic.lower())
        hook = (r.get("hook") or "").strip()
        out.append(DiscoverItem(
            topic=topic,
            source_kind="llm",
            source_ref=None,
            source_label=label,
            source_excerpt=hook or None,
            metadata={
                "variant": req.variant,
                "niche_key": req.niche_key,
                "language": req.language or (summary.language if summary else None),
                "model_tier": "haiku",
                "hook": hook or None,
            },
        ))
        if len(out) >= count:
            break
    if raw_items and not out:
        # Items existed but every one was malformed / duplicate / empty.
        # Surfaces typo-class bugs (e.g. schema field renamed) loudly.
        logger.warning(
            "LLM brainstorm produced %d raw items for %s but all were "
            "discarded as malformed/duplicate (sample: %r)",
            len(raw_items), channel, raw_items[0] if raw_items else None,
        )
    return out


# ---------------------------------------------------------------------------
# Native adapter routing
# ---------------------------------------------------------------------------


# Hosts whose RSS feed maps to the existing :func:`_ai_news_items`
# (HN top-AI). Any other ``rss`` niche degrades to LLM-only — adding
# new RSS sources is a feature, not a bug-fix.
_HN_RSS_HOSTS: frozenset[str] = frozenset({
    "news.ycombinator.com",
})


def _is_hn_rss(ref: str | None) -> bool:
    if not ref:
        return False
    try:
        from urllib.parse import urlparse  # noqa: PLC0415
        host = (urlparse(ref).hostname or "").lower()
    except Exception:  # noqa: BLE001  # coverage: defensive guard around urlparse, hard to trigger from string input
        return False  # coverage: defensive guard around urlparse, hard to trigger from string input
    return host in _HN_RSS_HOSTS


def _niche_native_items_for(
    niche, req: DiscoverRequest, *, limit: int,
) -> tuple[str, list[DiscoverItem]]:
    """Route to a native source based on the *selected niche's* declared
    ``source: {kind, ref}`` field.

    This is the niche-driven branch — it does NOT consult the channel
    name. The dispatch table mirrors the one documented in the
    discover-routes module docstring:

    | kind        | ref                          | adapter |
    |-------------|------------------------------|---------|
    | reddit      | subreddit name               | reddit  |
    | wikipedia   | ``On_this_day``              | today_in_history |
    | wikipedia   | other page title             | wikipedia list  |
    | wikipedia   | ``None``                     | none (LLM only) |
    | rss         | HN feed URL                  | _ai_news_items |
    | rss         | other URL / ``None``         | none (LLM only) |
    | manual / x_twitter / unknown / missing | ``*`` | none (LLM only) |

    Returns ``("", [])`` for any niche with no actionable native
    source. The caller MUST honour that signal — i.e. NOT fall back to
    a channel-default adapter — otherwise we silently reintroduce the
    "user picked X niche but got an unrelated topic" bug. See
    :func:`_native_items_for` for the precedence wiring.
    """
    src = niche.source
    if src is None or not src.kind:
        return ("", [])

    kind = src.kind
    ref = src.ref

    if kind == "reddit":
        if not ref:
            return ("", [])
        # Strip a leading "r/" / "/r/" if a hand-edited niche uses one.
        sub = ref.lstrip("/")
        if sub.lower().startswith("r/"):
            sub = sub[2:]
        if not sub:
            return ("", [])  # ref was just "r/" or "/r/" — treat as null
        return (f"reddit:{sub}", _reddit_items(sub, "top", "day", limit))

    if kind == "wikipedia":
        if not ref:
            return ("", [])
        if ref == "On_this_day":
            return ("wikipedia:onthisday", _today_in_history_items(limit))
        # Any other wiki ref is a list-page title to scrape.
        return (
            f"wikipedia:{ref}",
            _wikipedia_list_items(page=ref, limit=limit),
        )

    if kind == "rss":
        if _is_hn_rss(ref):
            return ("rss:news.ycombinator.com", _ai_news_items(limit))
        return ("", [])

    # manual / x_twitter / youtube / unknown — LLM brainstorm carries.
    return ("", [])


def _native_items_for(channel: str, req: DiscoverRequest, *, limit: int) -> tuple[str, list[DiscoverItem]]:
    """Return (adapter_label, items) for the channel + niche combo.

    Routing precedence:

    1. **Niche-driven** (when ``req.niche_key`` is set AND the niche
       resolves): dispatch on ``niche.source.{kind,ref}`` via
       :func:`_niche_native_items_for`. If the niche has no actionable
       native source (e.g. ``manual``, ``wikipedia`` with no ref,
       ``x_twitter``, unknown kind), this returns ``("", [])`` and the
       caller falls through to the LLM brainstorm. **We deliberately
       do NOT fall back to channel defaults here** — picking a niche
       and getting a channel-default Wikipedia "On this day" topic was
       the original bug.

    2. **Niche-key set but unresolvable** (typo, deleted niche, lookup
       failure): also returns ``("", [])`` so LLM brainstorm carries.
       Logged so operators can spot stale FE caches.

    3. **No niche selected** (``req.niche_key`` absent): legacy
       channel-default branches kick in. These exist only to keep
       older clients (and the GET feed endpoint when called with no
       query string) working — every modern create-flow path passes
       a niche.

    No-native channels (``hindutavaanimated``, ``sportsrecapped``,
    ``rhymetimejunction``) return ``("", [])`` so the LLM brainstorm
    is the sole adapter regardless of which path got us here.
    """
    if req.niche_key:
        niche = _resolve_niche_doc(channel, req.niche_key)
        if niche is None:
            logger.info(
                "discover: niche_key=%r on %s did not resolve; "
                "skipping native source and relying on LLM brainstorm",
                req.niche_key, channel,
            )
            return ("", [])
        return _niche_native_items_for(niche, req, limit=limit)

    # ----- No niche selected: legacy channel-default paths ---------------
    variant = req.variant or ""

    if channel == "mystoriesanimated":
        sub = VARIANT_SUBREDDIT.get(variant) or CHANNEL_DEFAULT_SUBREDDIT[channel]
        wiki_kw = VARIANT_WIKI_KEYWORD.get(variant)
        if variant in VARIANT_WIKI_KEYWORD:
            return (
                f"wikipedia:onthisday{(' · ' + wiki_kw) if wiki_kw else ''}",
                _today_in_history_items(limit=limit, keyword=wiki_kw),
            )
        return (f"reddit:{sub}", _reddit_items(sub, "top", "day", limit))

    if channel == "scrollpulse":
        # round-robin across subreddits — pick one at random per call
        sub = random.choice(SCROLLPULSE_SUBREDDITS)
        return (f"reddit:{sub} (round-robin)", _reddit_items(sub, "top", "day", limit))

    if channel == "historyrecapped":
        return ("wikipedia:onthisday", _today_in_history_items(limit))

    if channel == "cosmosdecoded":
        # Filter to topics with a physics / astronomy keyword
        items: list[DiscoverItem] = []
        for kw in ("physics", "astronomy", "telescope", "rocket", "satellite",
                   "space", "discovered", "particle", "black hole"):
            items += _today_in_history_items(limit=4, keyword=kw)
            if len(items) >= limit:
                break
        # dedupe
        seen: set[str] = set()
        deduped: list[DiscoverItem] = []
        for it in items:
            key = it.source_ref or it.topic
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)
        return ("wikipedia:onthisday (physics)", deduped[:limit])

    return ("", [])


def _filter_avoid(items: list[DiscoverItem], avoid: list[str]) -> list[DiscoverItem]:
    if not avoid:
        return items
    blocked = {a.strip().lower() for a in avoid if a and a.strip()}
    if not blocked:
        return items
    out: list[DiscoverItem] = []
    for it in items:
        if it.topic.strip().lower() in blocked:
            continue
        if it.source_ref and it.source_ref in blocked:
            continue
        out.append(it)
    return out


def _safe_native_items_for(
    channel: str, req: DiscoverRequest, *, limit: int,
) -> tuple[str, list[DiscoverItem]]:
    """Wrapper around :func:`_native_items_for` that swallows source
    failures (Reddit rate-limit, Wikipedia DNS, …) instead of surfacing
    them as 502s. The LLM brainstorm fallback in :func:`_build_feed`
    keeps the discover endpoint usable even when the native source is
    down — previously a single Reddit hiccup 502'd the whole call.
    """
    try:
        return _native_items_for(channel, req, limit=limit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "discover: native source failed for %s; falling back to LLM-only: %s",
            channel, exc, exc_info=True,
        )
        return ("", [])


def _build_feed(channel: str, *, limit: int = 8, req: DiscoverRequest | None = None) -> DiscoverFeed:
    """Compose the candidate feed for a channel.

    Always tries the native adapter (when one exists) AND the LLM
    brainstorm (per the 2026-05-11 product decision to always blend
    LLM-augmented suggestions). Native + LLM failures are both
    non-fatal — only "literally zero items" surfaces as 502.
    """
    req = req or DiscoverRequest()
    native_label, native_items = _safe_native_items_for(channel, req, limit=limit)
    native_items = _filter_avoid(native_items, req.avoid)

    # LLM augmentation — always attempted unless globally disabled. We
    # ask for a small batch so total feed length stays bounded.
    llm_items = _llm_topic_items(channel, req, count=min(LLM_BRAINSTORM_COUNT, limit))
    llm_items = _filter_avoid(llm_items, req.avoid)

    parts: list[str] = []
    if native_label:
        parts.append(native_label)
    if llm_items:
        parts.append("llm")
    adapter = " + ".join(parts) if parts else "llm"

    if not native_items and not llm_items:
        # Neither source produced anything — surface a clear 502 so the
        # UI can ask the user to type a topic manually.
        if not native_label:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"no auto-generate source produced topics for '{channel}' "
                    "— try writing a topic manually or set "
                    "YTFACTORY_LLM_BACKEND so the brainstorm fallback can run."
                ),
            )
        raise HTTPException(status_code=502, detail="upstream source returned no candidates")

    # Interleave native + LLM so "Generate another" returns variety
    # rather than draining one source first.
    interleaved: list[DiscoverItem] = []
    n_iter = iter(native_items)
    l_iter = iter(llm_items)
    for _ in range(max(len(native_items), len(llm_items))):
        for src in (n_iter, l_iter):
            try:
                interleaved.append(next(src))
            except StopIteration:
                continue
        if len(interleaved) >= limit:
            break

    return DiscoverFeed(channel=channel, adapter=adapter, items=interleaved[:limit])


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


@router.get("/{channel}/feed", response_model=DiscoverFeed)
async def feed(
    channel: str,
    variant: str | None = None,
    length_kind: str | None = None,
    language: str | None = None,
    niche_key: str | None = None,
) -> DiscoverFeed:
    """Up to 10 candidate topics. UI shows them as a strip.

    GET takes the optional context as query-string parameters for
    cacheability; POST takes the full body for richer values + avoid
    list (called by the "Generate another" button).
    """
    req = DiscoverRequest(
        variant=variant, length_kind=length_kind,
        language=language, niche_key=niche_key,
    )
    try:
        return _build_feed(channel, limit=10, req=req)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("discover feed failed for %s", channel, exc_info=True)
        raise HTTPException(status_code=502, detail=f"upstream source failed: {e}")


@router.post("/{channel}", response_model=DiscoverItem)
async def pick_one(channel: str, req: DiscoverRequest | None = None) -> DiscoverItem:
    """One suggested topic. Body is optional — when provided it carries
    the user's selected variant / niche / values / already-shown topics.
    """
    req = req or DiscoverRequest()
    try:
        feed_obj = _build_feed(channel, limit=10, req=req)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("discover pick_one failed for %s", channel, exc_info=True)
        raise HTTPException(status_code=502, detail=f"upstream source failed: {e}")
    if not feed_obj.items:
        raise HTTPException(status_code=502, detail="upstream returned no candidates")
    # Pick from the first 5 (the "best" of the blended pool) so the
    # signal-to-noise stays high but successive clicks don't always
    # return the same item.
    return random.choice(feed_obj.items[: min(5, len(feed_obj.items))])
