"""Auto-generate YouTube publish metadata from a rendered script.

One-click-publish design: the user clicks Publish and sees a modal with
ONLY a visibility selector + optional scheduled-publish time. Title,
description, hashtags, tags, thumbnail, category, default-language and
made-for-kids are produced here from the script + channel config.

**This is a stub.** The actual editorial rules — what makes a hit Short
title, the correct hashtag count, the description boilerplate that
maximises CTR — are being researched in a parallel work-stream and will
land at ``docs/youtube_shorts_metadata_playbook.md``. Once the playbook
ships, this module's heuristics are replaced site-for-site; the public
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

    # Hashtag/tag pool: title + topic + hook + summary all contribute,
    # deduped + stop-worded by ``_tokenise``.
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
    # Per memory ``feedback_silent_fallback_unshippable_output``: an
    # empty hashtag list is shippable (YouTube accepts videos with no
    # hashtags) — but for the stub we guarantee at least one anchor
    # hashtag from the title so the playbook's "no empty hashtag arrays"
    # invariant is satisfied. If even tokenisation yields nothing
    # (title was punctuation-only, which the upstream rewrite gates
    # should catch), fall back to a single ``#shorts`` anchor so
    # downstream code can rely on a non-empty list.
    hashtags = _hashtagify(tokens, _DEFAULT_HASHTAG_COUNT) or ["#shorts"]
    tags = _budget_tags(tokens, _DEFAULT_TAG_COUNT)

    thumbnail_path = _resolve_thumbnail(job_id, script)

    # Append hashtags to description per YouTube's Shorts hashtag
    # convention (visible-above-title hashtags must appear in the
    # description body, not just the metadata).
    description_body = description_seed
    if hashtags:
        description_body = f"{description_seed}\n\n{' '.join(hashtags)}"

    return PublishMetadata(
        title=title[:_YT_TITLE_MAX],
        description=description_body[:_YT_DESCRIPTION_MAX],
        hashtags=hashtags,
        tags=tags,
        thumbnail_path=thumbnail_path,
        category_id=_DEFAULT_CATEGORY_ID,
        default_language=str(script.get("default_language") or "en"),
        made_for_kids=bool(script.get("made_for_kids", False)),
    )


# ---- exposed-for-tests helpers ----------------------------------------
# Re-exported so the test module can pin the budget / tokenisation
# heuristics directly (the playbook will replace these one-for-one).

__all__ = [
    "PublishMetadata",
    "MissingMetadataInputError",
    "generate_publish_metadata",
]
