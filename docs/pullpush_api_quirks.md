# Pullpush API quirks

> **Established 2026-05-12** while shipping the Pullpush dispatcher in
> `pipeline/sources/reddit_api.py` (commit `e600e22`). Every quirk
> below was discovered from a live `curl` against `api.pullpush.io`,
> NOT from their docs — so future contributors don't repeat the
> discovery. Keep this doc concrete (paste the actual `HTTP 400`
> bodies, the actual response shapes) rather than generic.

Pullpush (`https://api.pullpush.io`) is a Pushshift-compatible Reddit
archive — open-source, no auth, the cloud-side fallback when Reddit's
`www.reddit.com` JSON API IP-blocks Cloud Run egress
(`docs/cloud_egress_blocked_apis.md`). It works, but its API has four
non-obvious gotchas that all surface as `HTTP 400` or empty results
rather than helpful errors.

## Quirks (verified live 2026-05-12)

### 1. `after` / `before` MUST be Unix-timestamp INTEGERS

Reddit's own search API accepts `after=24h` / `7d` shortcuts. **Pullpush
does NOT** — it returns:

```
GET /reddit/search/submission/?subreddit=AmItheAsshole&after=24h
HTTP/1.1 400 Bad Request
{"error":"Invalid integer - 24h"}
```

Use `int(time.time()) - <seconds_offset>`. Reference impl:
`pipeline/sources/reddit_api.py::_pullpush_submission_search`.

### 2. `over_18` query param is REJECTED

Reddit accepts `over_18=true|false` for NSFW filtering. **Pullpush
does NOT** — `?over_18=false` returns `HTTP 400` (no helpful error
body). NSFW filtering MUST be done client-side, by checking the
`over_18` field on each returned item dict and dropping those that
match the caller's `skip_nsfw` flag. Reference impl:
`_build_raw_story_from_anon_dict` in the same module.

### 3. Pullpush is days-delayed, not "hours-delayed"

The original `docs/cloud_egress_blocked_apis.md` (2026-05-11) said
Pullpush is "hours-delayed". **Verified 2026-05-12: it's days-delayed.**
Concrete reproduction:

```
GET /reddit/search/submission/?subreddit=AmItheAsshole&size=10&sort=desc&sort_type=score&after=<NOW-7d>
→ count: 0   # no posts in the last 7 days

GET /reddit/search/submission/?subreddit=AmItheAsshole&size=10&sort=desc&sort_type=score&after=<NOW-30d>
→ count: 0   # no posts in the last 30 days

GET /reddit/search/submission/?subreddit=AmItheAsshole&size=10&sort=desc&sort_type=score&after=<NOW-365d>
→ count: 28  # archive starts ~2025-05-19 (about 1 year back from 2026-05-12)
```

The current freshness gap fluctuates — check it again before planning
a feature that needs ≤ N-day-old data.

**Mitigation:** widening cascade in
`pipeline/sources/reddit_api.py::_fetch_via_pullpush`:

```
requested timeframe (e.g. "day" → 86_400s)
  → "7d"   (604_800s)
    → "30d"  (2_592_000s)
      → "365d" (31_536_000s)
        → "all"  (no after param)
```

It stops as soon as `limit` items are collected, dedupes by post_id
across windows, and records the actual window used in
`metadata["pullpush_window"]` so the UI / logs show "Pullpush · top
365d" honestly. **A widened window must be strictly broader than the
requested one** — adding a 7d cascade entry when the caller already
asked for "all" would be a regression. The dispatcher's
`_is_wider_than_requested()` enforces this.

### 4. `?ids=POST_ID` lookup is unreliable

The endpoint `GET /reddit/search/submission/?ids=<post_id>` returns 0
results even for posts that ARE in the archive (verified 2026-05-12
against post `1kpr2bx`, which the same archive returns when queried
by `?subreddit=...&q=<title-keywords>`). Root cause unknown — likely
indexing inconsistency in their fork. Other endpoints variants we
tried:

| variant | result |
|---|---|
| `?ids=<post_id>` (no prefix) | 0 results for known-archived posts |
| `?ids=t3_<post_id>` (with reddit prefix) | 0 results |
| `/reddit/submission/?ids=<post_id>` (different path) | `endpoint not found` |

**Mitigation:** for `fetch_post_by_url(url)`, try `?ids=` first; on
empty, fall back to a keyword-search using the URL slug as `q`. URL
slugs (`aita_for_telling_my_wife_the_lock`) closely mirror post
titles so the slug→`q` substitution finds the post most of the time.
Reference impl: `pipeline/sources/reddit_api.py::_fetch_post_via_pullpush`
+ `_pullpush_lookup_by_slug`. When multiple posts come back from the
slug search (same title can be reposted), prefer the one with the
exact original `post_id`; falling back to the first result is the
last resort.

## Response shape

Submission search returns:

```json
{
  "data": [
    {
      "title": "AITA for ...",
      "selftext": "<long story body>" | "[removed]" | "[deleted]" | "",
      "permalink": "/r/AmItheAsshole/comments/<post_id>/<slug>/",
      "is_self": true,
      "stickied": false,
      "over_18": false,
      "score": 42,
      "num_comments": 17,
      "author": "<username>" | "[deleted]",
      "id": "<post_id>",
      "created_utc": 1747659430.0,
      "subreddit": "AmItheAsshole"
    }
  ],
  "metadata": {...},
  "error": null
}
```

Many archived posts have `selftext == "[removed]"` or `"[deleted]"`
because Reddit moderates after the archive snapshot — the marker is
preserved. Filter at the adapter layer rather than asking every
caller to remember.

## See also

- `docs/cloud_egress_blocked_apis.md` — the parent doc covering why
  we use Pullpush at all (Reddit IP-blocks Cloud Run egress).
- `pipeline/sources/reddit_api.py` — the dispatcher + adapters
  implementing the workarounds above.
- `tests/test_sources_reddit_api.py::FetchViaPullpushTest` +
  `::FetchPostByUrlTest` — pin every quirk above with a regression test.
- `feedback_pullpush_api_quirks.md` — terse memory pointer.
