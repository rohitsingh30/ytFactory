"""Reddit story-mining adapter with a backend dispatcher.

Drives:

* `/api/discover/{channel}` (control plane) — `fetch()` for top-of-day
  candidate topics from a subreddit.
* `cloud/render-worker-v2/entrypoint.py::_fetch_source` — `fetch_post_by_url()`
  for paste-a-Reddit-URL renders that need the full submission body.

## Why a backend dispatcher

Reddit blocks unauthenticated traffic from Cloud Run egress IP ranges
(`asia-southeast1` confirmed 2026-05-11) — the same call that works
from a laptop returns ``403 Client Error: Blocked`` from cloud. See
``docs/cloud_egress_blocked_apis.md`` for the full post-mortem.

``_pick_backend()`` selects between three implementations so callers
don't have to care:

| backend  | host                  | auth          | freshness         | scope                   |
|----------|-----------------------|---------------|-------------------|-------------------------|
| anon     | www.reddit.com        | none          | live              | laptop only (403 cloud) |
| pullpush | api.pullpush.io       | none          | hours-delayed     | cloud-safe              |
| oauth    | oauth.reddit.com      | client-creds  | live              | NOT YET IMPLEMENTED     |

### Selection precedence

1. ``REDDIT_FETCH_BACKEND`` env var, if set to a valid value (``anon``,
   ``pullpush``, or ``oauth``) — wins, even on Cloud Run.
2. ``K_SERVICE`` env present (Cloud Run runtime marker) → ``pullpush``.
3. Otherwise → ``anon``.

OAuth is **not** auto-selected from ``REDDIT_CLIENT_ID`` /
``REDDIT_CLIENT_SECRET`` being set, because the OAuth fetcher is a
``NotImplementedError`` stub. Auto-picking it from secret presence
would mean a future operator who provisions Reddit secrets to "make
prod faster" would actually regress prod from "Pullpush works" to
"NotImplementedError 502". When OAuth is implemented and smoke-tested,
flip the precedence — until then, only the explicit env var picks it.

## Why Pullpush returns no comment tree (and what we do instead)

Pullpush mirrors the public Pushshift API — it indexes submissions and
comments separately and is hours-delayed. For ``fetch_post_by_url`` we
only need the submission body; comments are fetched on-render via a
different code path. If a freshly-posted URL isn't yet in the
Pullpush index, ``fetch_post_by_url`` raises ``RedditFetchError`` and
the caller (cloud render worker) degrades to user-typed ``topic`` /
``notes``.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from pipeline.observability.event_helpers import safe_track as _track

from .base import RawStory, save_raw, slugify


# ---------------------------------------------------------------------------
# Module-level config
# ---------------------------------------------------------------------------

USER_AGENT = "ytFactory/0.1 (+https://github.com/local; story aggregator)"
TIMEOUT = 20

ANON_BASE_URL = "https://www.reddit.com"
PULLPUSH_BASE_URL = "https://api.pullpush.io"

# Backend identifier constants — keep in sync with REDDIT_FETCH_BACKEND env.
BACKEND_ANON = "anon"
BACKEND_PULLPUSH = "pullpush"
BACKEND_OAUTH = "oauth"
_VALID_BACKENDS = frozenset({BACKEND_ANON, BACKEND_PULLPUSH, BACKEND_OAUTH})


class RedditFetchError(RuntimeError):
    """Raised when a Reddit fetch fails after dispatcher + retries.

    Distinct from ``requests.HTTPError`` so callers can ``except
    RedditFetchError`` without also catching unrelated ``requests``
    failures elsewhere in their stack.
    """


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


def _decision_source(*, chosen: str, alternatives: list[str], reason: str) -> None:
    _track(
        "decision.source",
        category="decision",
        metadata={
            "scope": "source",
            "chosen": chosen,
            "alternatives": alternatives,
            "reason": reason,
        },
    )


# Reddit ``timeframe`` → human label + seconds offset for the
# Pullpush ``after`` filter (Pullpush wants Unix-timestamp INTEGERS,
# not the ``24h``/``7d`` shortcuts Reddit's own API supports — verified
# live 2026-05-12, the shortcut form returns ``HTTP 400 Invalid integer``).
# A ``None`` seconds offset means "no after filter" (top of all time).
_PULLPUSH_OFFSET_FOR_TIMEFRAME: dict[str, tuple[str, int | None]] = {
    "hour":  ("1h",   3_600),
    "day":   ("24h",  86_400),
    "week":  ("7d",   604_800),
    "month": ("30d",  2_592_000),
    "year":  ("365d", 31_536_000),
    "all":   ("all",  None),
}

# Pullpush widening cascade. The strict "top of day" window is almost
# always empty on Pullpush — its archive is days-to-weeks behind live
# Reddit (verified 2026-05-12: ``after=now-7d`` returned 0 results;
# ``after=now-365d`` returned full results; no-filter returns the
# all-time top). Each widening step is tried until ``limit`` usable
# items are collected. Tuple is (window_label, seconds_offset). The
# caller's requested timeframe is prepended at runtime.
_PULLPUSH_WIDEN_CASCADE: tuple[tuple[str, int | None], ...] = (
    ("7d",   604_800),
    ("30d",  2_592_000),
    ("365d", 31_536_000),
    ("all",  None),
)


# Compiled once — Reddit post URLs / permalinks both look like
# ``/r/<subreddit>/comments/<post_id>/<slug>/[?...]``. We capture the
# subreddit, post id, AND the title slug so :func:`fetch_post_by_url`
# can do a keyword-search fallback when Pullpush's ``?ids=`` lookup
# returns empty (which it does inconsistently — verified 2026-05-12).
_POST_URL_RE = re.compile(
    r"/r/(?P<subreddit>[^/]+)/comments/(?P<post_id>[a-z0-9]+)(?:/(?P<slug>[^/?#]*))?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def _pick_backend() -> str:
    """Return the Reddit backend name to use for *this* call.

    Re-evaluated on every call (not module-cached) so tests and
    operational toggles take effect without a process restart.

    Order:

    1. ``REDDIT_FETCH_BACKEND`` env (case-insensitive) if it parses to
       one of the known backends — wins.
    2. ``K_SERVICE`` env present (Cloud Run sets this on every
       container) → ``pullpush``.
    3. Default → ``anon`` (laptop dev, CI).
    """
    explicit = (os.environ.get("REDDIT_FETCH_BACKEND") or "").strip().lower()
    if explicit in _VALID_BACKENDS:
        return explicit
    if os.environ.get("K_SERVICE"):
        return BACKEND_PULLPUSH
    return BACKEND_ANON


# ---------------------------------------------------------------------------
# Adapter: anonymous Reddit JSON (laptop only)
# ---------------------------------------------------------------------------

def _fetch_via_anon(
    subreddit: str,
    listing: str,
    timeframe: str,
    limit: int,
    min_chars: int,
    max_chars: int,
    skip_nsfw: bool,
) -> list[RawStory]:
    """Original ``www.reddit.com/r/X/<listing>.json`` flow.

    Kept identical (modulo factoring) to the pre-dispatcher behaviour
    so the existing ``tests/test_sources_reddit_api.py`` regression
    suite for anon parsing still passes.
    """
    url = f"{ANON_BASE_URL}/r/{subreddit}/{listing}.json"
    params: dict[str, str] = {"limit": str(min(limit * 3, 100))}
    if listing == "top":
        params["t"] = timeframe

    print(f"[reddit_api anon] GET {url}  ({listing}/{timeframe}, limit={limit})")
    _source_attempt("reddit_listing", f"{subreddit}/{listing}/{timeframe}", BACKEND_ANON)
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    payload = r.json()

    out: list[RawStory] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        story = _build_raw_story_from_anon_dict(d, subreddit,
                                                min_chars=min_chars,
                                                max_chars=max_chars,
                                                skip_nsfw=skip_nsfw)
        if story is None:
            continue
        out.append(story)
        if len(out) >= limit:
            break

    print(f"[reddit_api anon] kept {len(out)} stories after filtering")
    return out


def _build_raw_story_from_anon_dict(
    d: dict[str, Any],
    subreddit: str,
    *,
    min_chars: int,
    max_chars: int,
    skip_nsfw: bool,
) -> RawStory | None:
    """Apply the AITA-shaped filters to a single Reddit anon JSON
    submission dict and return a :class:`RawStory` or ``None``.

    Factored out of :func:`_fetch_via_anon` so :func:`_fetch_via_pullpush`
    can re-use the same filtering policy on the Pullpush response shape
    (which carries the same fields, just at the top level rather than
    nested under ``children[].data``).
    """
    if d.get("stickied") or not d.get("is_self"):
        return None
    if skip_nsfw and d.get("over_18"):
        return None

    body = (d.get("selftext") or "").strip()
    # Pullpush returns ``"[removed]"`` / ``"[deleted]"`` for posts that
    # got moderated post-archive — useless for narration. Anon Reddit
    # rarely returns these (they get filtered server-side) but it's
    # cheap to belt-and-braces.
    if body.lower() in {"[removed]", "[deleted]"}:
        return None
    if not (min_chars <= len(body) <= max_chars):
        return None

    title = (d.get("title") or "").strip()
    permalink = d.get("permalink") or ""
    return RawStory(
        slug=slugify(f"{subreddit}-{title}"),
        title=title,
        body=body,
        source=f"reddit:{subreddit}",
        url=f"https://reddit.com{permalink}",
        metadata={
            "score": d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
            "author": d.get("author"),
            "created_utc": d.get("created_utc"),
            "post_id": d.get("id"),
        },
    )


# ---------------------------------------------------------------------------
# Adapter: Pullpush archive (cloud-safe)
# ---------------------------------------------------------------------------

def _fetch_via_pullpush(
    subreddit: str,
    listing: str,
    timeframe: str,
    limit: int,
    min_chars: int,
    max_chars: int,
    skip_nsfw: bool,
) -> list[RawStory]:
    """Fetch via Pullpush submission search.

    Pullpush is a Pushshift-compatible Reddit archive (see
    https://pullpush.io). It's hours-delayed but does not IP-block
    Cloud Run.

    Differences from the anon path the caller should be aware of:

    * Listings other than ``top`` are emulated: ``hot``/``new``/``rising``
      all map to ``sort_type=created_utc`` (most recent first).
    * ``top`` maps to ``sort_type=score``.
    * Many archived posts have ``selftext == "[removed]"`` — we filter
      those out at adapter level so callers see a clean stream.
    * If the requested ``timeframe`` window yields fewer than ``limit``
      usable items, we **widen** the window (24h → 7d → 30d) before
      giving up, recording the actual window used in
      ``story.metadata["pullpush_window"]``.
    """
    sort_type = "score" if listing == "top" else "created_utc"
    requested_label, requested_offset = _PULLPUSH_OFFSET_FOR_TIMEFRAME.get(
        timeframe, ("24h", 86_400),
    )

    # Build the cascade: caller's requested window first, then any
    # cascade entry that's strictly WIDER than the requested window
    # (a narrower entry would just return a subset we already have).
    # ``None`` offset means "no after filter" — the broadest possible
    # window — so it's wider than every concrete offset.
    seen_offsets: set[int | None] = set()
    cascade: list[tuple[str, int | None]] = []

    def _is_wider_than_requested(offset: int | None) -> bool:
        if requested_offset is None:
            # Caller asked for the broadest window already.
            return False
        if offset is None:
            return True
        return offset > requested_offset

    def _add(label: str, offset: int | None) -> None:
        if offset in seen_offsets:
            return  # coverage: defensive guard against duplicate window registration
        seen_offsets.add(offset)
        cascade.append((label, offset))

    _add(requested_label, requested_offset)
    for label, offset in _PULLPUSH_WIDEN_CASCADE:
        if _is_wider_than_requested(offset):
            _add(label, offset)

    out: list[RawStory] = []
    seen_post_ids: set[str] = set()
    for idx, (window_label, offset) in enumerate(cascade):
        if len(out) >= limit:
            break
        if idx > 0:
            _decision_source(
                chosen=f"pullpush_window:{window_label}",
                alternatives=[f"pullpush_window:{cascade[idx - 1][0]}"],
                reason=f"prior_window_insufficient kept={len(out)} limit={limit}",
            )
        items = _pullpush_submission_search(
            subreddit=subreddit,
            sort_type=sort_type,
            after_seconds_offset=offset,
            size=100,  # overfetch hard — the [removed] filter is brutal
        )
        print(f"[reddit_api pullpush] window={window_label} (offset={offset}s) "
              f"raw={len(items)} kept_so_far={len(out)}")
        for d in items:
            if len(out) >= limit:
                break
            # A widened window naturally re-includes the narrower
            # window's posts — dedupe by post id so the cascade doesn't
            # return the same story twice.
            post_id = d.get("id")
            if post_id and post_id in seen_post_ids:
                continue
            story = _build_raw_story_from_anon_dict(
                d, subreddit,
                min_chars=min_chars, max_chars=max_chars, skip_nsfw=skip_nsfw,
            )
            if story is None:
                continue
            if post_id:
                seen_post_ids.add(post_id)
            # Track which window this came from + that we used Pullpush
            # — handy for the discover UI to show "Pullpush · top week"
            # when the strict day window came up empty.
            story.metadata["pullpush_window"] = window_label
            story.metadata["backend"] = BACKEND_PULLPUSH
            out.append(story)

    print(f"[reddit_api pullpush] kept {len(out)} stories after filtering")
    return out


def _pullpush_submission_search(
    *,
    subreddit: str,
    sort_type: str,
    after_seconds_offset: int | None,
    size: int,
) -> list[dict[str, Any]]:
    """Hit Pullpush ``/reddit/search/submission/`` and return the raw
    item list (no client-side filtering — caller decides).

    ``after_seconds_offset`` is converted to a Unix timestamp at call
    time (``int(time.time()) - offset``). ``None`` omits the ``after``
    filter entirely (top of all time).

    NSFW filtering is *not* delegated to Pullpush — its ``over_18=false``
    query param returns ``HTTP 400`` (verified live 2026-05-12). NSFW
    filter is applied client-side in :func:`_build_raw_story_from_anon_dict`.
    """
    url = f"{PULLPUSH_BASE_URL}/reddit/search/submission/"
    params: dict[str, str] = {
        "subreddit": subreddit,
        "size": str(size),
        "sort": "desc",
        "sort_type": sort_type,
    }
    if after_seconds_offset is not None:
        params["after"] = str(int(time.time()) - after_seconds_offset)

    ref = f"{subreddit}/{sort_type}/{after_seconds_offset if after_seconds_offset is not None else 'all'}"
    _source_attempt("reddit_listing", ref, BACKEND_PULLPUSH)
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    payload = r.json()
    if not isinstance(payload, dict):
        return []  # coverage: defensive guard against malformed pullpush payload
    data = payload.get("data")
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Adapter: OAuth (NOT IMPLEMENTED yet)
# ---------------------------------------------------------------------------

_OAUTH_NOT_IMPL_MSG = (
    "REDDIT_FETCH_BACKEND=oauth is documented in "
    "docs/cloud_egress_blocked_apis.md but not yet implemented. "
    "Use REDDIT_FETCH_BACKEND=pullpush (or unset) for cloud, or "
    "REDDIT_FETCH_BACKEND=anon for laptop dev."
)


def _fetch_via_oauth(*_args: Any, **_kwargs: Any) -> list[RawStory]:
    raise NotImplementedError(_OAUTH_NOT_IMPL_MSG)


def _fetch_post_via_oauth(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError(_OAUTH_NOT_IMPL_MSG)


# ---------------------------------------------------------------------------
# Public: subreddit listing fetch (top of day, etc)
# ---------------------------------------------------------------------------

def fetch(
    subreddit: str,
    listing: str = "top",
    timeframe: str = "day",
    limit: int = 25,
    min_chars: int = 400,
    max_chars: int = 6000,
    skip_nsfw: bool = True,
    *,
    backend: str | None = None,
) -> list[RawStory]:
    """Pull stories from one subreddit, dispatching to a backend.

    Backend is chosen per-call by :func:`_pick_backend` unless the
    caller forces one via the ``backend`` kwarg (used in tests).

    Args mirror the original anon implementation for back-compat — every
    pre-dispatcher caller works unchanged.

    Raises:
        RedditFetchError: anything the underlying adapter raises gets
            wrapped so callers (e.g. ``discover_routes._safe_native_items_for``)
            can ``except RedditFetchError`` cleanly. The original
            exception is preserved as ``__cause__``.
    """
    chosen = (backend or _pick_backend()).lower()

    try:
        if chosen == BACKEND_PULLPUSH:
            return _fetch_via_pullpush(
                subreddit, listing, timeframe, limit,
                min_chars, max_chars, skip_nsfw,
            )
        if chosen == BACKEND_OAUTH:
            return _fetch_via_oauth(
                subreddit, listing, timeframe, limit,
                min_chars, max_chars, skip_nsfw,
            )
        # Default: anonymous reddit JSON.
        return _fetch_via_anon(
            subreddit, listing, timeframe, limit,
            min_chars, max_chars, skip_nsfw,
        )
    except NotImplementedError:
        # Surface the un-implemented oauth path verbatim — the message
        # tells the caller exactly what to set instead.
        raise
    except requests.HTTPError as exc:
        raise RedditFetchError(
            f"reddit fetch failed (backend={chosen}, sub={subreddit}): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Public: single-post fetch by URL (for paste-a-Reddit-URL render flow)
# ---------------------------------------------------------------------------

def fetch_post_by_url(
    url_or_permalink: str,
    *,
    backend: str | None = None,
) -> dict[str, Any]:
    """Fetch a single Reddit submission by its URL or permalink.

    Used by ``cloud/render-worker-v2/entrypoint.py::_fetch_source`` when
    the user picks a ``source_kind="reddit_url"`` topic in the discover
    UI and triggers a render. Returns the same shape the worker
    expects:

    .. code:: python

        {"title": str, "body": str, "source": "reddit:<sub>", "url": str}

    Backend selection mirrors :func:`fetch`. On Pullpush the post may
    not yet be archived (Pullpush is hours-to-days delayed) — the
    ``?ids=`` lookup is also flaky (returns empty even for posts
    that do exist in the archive, verified 2026-05-12). We work
    around that with a title-slug keyword-search fallback. If both
    fail, raise :class:`RedditFetchError` and the caller degrades
    to user-typed ``topic`` / ``notes``.
    """
    chosen = (backend or _pick_backend()).lower()
    parts = _parse_post_url(url_or_permalink)
    if parts is None:
        raise RedditFetchError(f"could not parse Reddit post url: {url_or_permalink!r}")
    subreddit, post_id, slug = parts

    try:
        if chosen == BACKEND_PULLPUSH:
            return _fetch_post_via_pullpush(post_id, url_or_permalink,
                                             subreddit=subreddit, slug=slug)
        if chosen == BACKEND_OAUTH:
            return _fetch_post_via_oauth(post_id, url_or_permalink)
        return _fetch_post_via_anon(post_id, url_or_permalink)
    except NotImplementedError:
        raise
    except requests.HTTPError as exc:
        raise RedditFetchError(
            f"reddit post fetch failed (backend={chosen}, post_id={post_id}): {exc}"
        ) from exc


def _parse_post_url(
    url_or_permalink: str,
) -> tuple[str, str, str] | None:
    """Return ``(subreddit, post_id, title_slug)`` parsed from a Reddit
    URL or permalink, or ``None`` if it isn't a Reddit post URL.

    Handles every shape we've seen:

    * ``https://www.reddit.com/r/AmItheAsshole/comments/1kqb0if/title_slug/``
    * ``https://reddit.com/r/X/comments/1kqb0if`` (no slug)
    * ``/r/X/comments/1kqb0if/title_slug/``       (bare permalink)
    * ``https://old.reddit.com/r/X/comments/1kqb0if/.../``

    The slug may be empty if the URL doesn't include one — callers
    (specifically the Pullpush keyword-search fallback) should treat
    an empty slug as "no fallback available".
    """
    if not url_or_permalink:
        return None
    m = _POST_URL_RE.search(url_or_permalink)
    if not m:
        return None
    return (m.group("subreddit"), m.group("post_id"), (m.group("slug") or ""))


def _extract_post_id(url_or_permalink: str) -> str | None:
    """Back-compat shim: just the post id, for callers that don't need
    subreddit / slug. New code should call :func:`_parse_post_url`.
    """
    parts = _parse_post_url(url_or_permalink)
    return parts[1] if parts else None


def _fetch_post_via_anon(post_id: str, original_url: str) -> dict[str, Any]:
    """Anon path: ``https://www.reddit.com/comments/<id>.json`` returns
    a 2-element list — the first item is the submission listing.
    """
    url = f"{ANON_BASE_URL}/comments/{post_id}.json"
    print(f"[reddit_api anon] GET {url}  (single post)")
    _source_attempt("reddit_url", post_id, BACKEND_ANON)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    payload = r.json()
    if not isinstance(payload, list) or not payload:
        raise RedditFetchError(f"unexpected anon post payload shape for {post_id}")  # coverage: defensive guard against malformed reddit payload

    children = (payload[0] or {}).get("data", {}).get("children", [])
    if not children:
        raise RedditFetchError(f"anon post {post_id} returned no submission")  # coverage: defensive guard against empty reddit children
    d = (children[0] or {}).get("data", {})
    return _post_dict_to_render_payload(d, fallback_url=original_url)


def _fetch_post_via_pullpush(
    post_id: str,
    original_url: str,
    *,
    subreddit: str = "",
    slug: str = "",
) -> dict[str, Any]:
    """Pullpush path: try ``/reddit/search/submission/?ids=<post_id>``
    first; on empty (Pullpush ``ids`` lookup is flaky) fall back to
    ``?subreddit=<sub>&q=<slug-as-keywords>&size=3`` and pick the
    closest match by post_id, then by title-similarity to the slug.
    """
    # Primary lookup by id.
    items = _pullpush_lookup_by_id(post_id)
    if not items and slug and subreddit:
        # Fallback: keyword-search using the URL slug. Slugs are
        # underscore- or hyphen-separated lowercase tokens that closely
        # mirror the post title.
        _decision_source(
            chosen="pullpush_slug_search",
            alternatives=["pullpush_id_lookup"],
            reason="pullpush_id_lookup_empty",
        )
        _source_fallback(reason="pullpush_id_lookup_empty", original_status=200)
        items = _pullpush_lookup_by_slug(subreddit=subreddit, slug=slug)

    if not items:
        # Common case: the post was made too recently and isn't in the
        # archive yet, OR the slug fallback didn't find anything close.
        # Caller (render worker) catches this and degrades to user-typed
        # topic/notes.
        _source_fallback(reason="pullpush_no_archived_submission", original_status=200)
        raise RedditFetchError(
            f"pullpush has no submission archived for post id {post_id} "
            "(post may be < a few hours old or missing from the archive; "
            "try again later or fall back to user topic/notes)"
        )

    # Prefer an exact post_id match if the slug fallback returned a
    # batch (Pullpush sometimes archives the same title multiple times).
    chosen = next((it for it in items if it.get("id") == post_id), items[0])
    return _post_dict_to_render_payload(chosen, fallback_url=original_url)


def _pullpush_lookup_by_id(post_id: str) -> list[dict[str, Any]]:
    """Direct ``?ids=<post_id>`` lookup. Returns empty list when
    Pullpush has no record (or returns the unreliable empty response).
    """
    url = f"{PULLPUSH_BASE_URL}/reddit/search/submission/"
    params = {"ids": post_id}
    print(f"[reddit_api pullpush] GET {url}?ids={post_id}")
    _source_attempt("reddit_url", post_id, BACKEND_PULLPUSH)
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    payload = r.json()
    if not isinstance(payload, dict):
        return []  # coverage: defensive guard against malformed pullpush ids payload
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def _pullpush_lookup_by_slug(*, subreddit: str, slug: str) -> list[dict[str, Any]]:
    """Keyword-search fallback. Reddit URL slugs look like
    ``aita_for_telling_my_wife_the_lock`` — convert underscores /
    hyphens to spaces and use as the ``q`` param.
    """
    keywords = re.sub(r"[-_]+", " ", slug).strip()
    if not keywords:
        return []  # coverage: defensive guard against empty slug keyword string
    url = f"{PULLPUSH_BASE_URL}/reddit/search/submission/"
    params = {"subreddit": subreddit, "q": keywords, "size": "5"}
    print(f"[reddit_api pullpush] GET {url}?subreddit={subreddit}&q={keywords[:60]}…")
    _source_attempt("reddit_url", f"{subreddit}/{keywords[:80]}", BACKEND_PULLPUSH)
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    payload = r.json()
    if not isinstance(payload, dict):
        return []  # coverage: defensive guard against malformed pullpush slug payload
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def _post_dict_to_render_payload(
    d: dict[str, Any], *, fallback_url: str,
) -> dict[str, Any]:
    """Project a Reddit submission dict (anon shape == pullpush shape
    at the top level, both flat) into the ``{title, body, source, url}``
    contract the render worker expects.
    """
    title = (d.get("title") or "").strip()
    body = (d.get("selftext") or "").strip()
    if body.lower() in {"[removed]", "[deleted]"}:
        body = ""
    if not title and not body:
        raise RedditFetchError("reddit post has no title and no selftext")  # coverage: defensive guard against fully-empty reddit submission

    subreddit = d.get("subreddit") or ""
    permalink = d.get("permalink") or ""
    url = f"https://reddit.com{permalink}" if permalink else fallback_url
    return {
        "title": title,
        "body": body,
        "source": f"reddit:{subreddit}" if subreddit else "reddit",
        "url": url,
    }


# ---------------------------------------------------------------------------
# CLI (unchanged from pre-dispatcher version)
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subreddit", required=True, help="e.g. AmItheAsshole, tifu, MaliciousCompliance")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "rising"])
    ap.add_argument("--timeframe", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--min-chars", type=int, default=400)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--out", default="data/intermediate")
    ap.add_argument("--channel", default="aita_text")
    ap.add_argument("--backend", default=None,
                    choices=[BACKEND_ANON, BACKEND_PULLPUSH, BACKEND_OAUTH],
                    help="Force backend (default: auto-pick from env / K_SERVICE)")
    args = ap.parse_args()

    stories = fetch(
        subreddit=args.subreddit,
        listing=args.listing,
        timeframe=args.timeframe,
        limit=args.limit,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
        backend=args.backend,
    )
    dest = Path(args.out) / args.channel / "raw"
    for s in stories:
        path = save_raw(s, dest)
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
