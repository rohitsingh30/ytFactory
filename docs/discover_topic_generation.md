# Auto-generate topic — discover routes

The `/api/discover/{channel}` surface drives the **"Auto-generate topic"**
button in the create wizard. Earlier this only worked for four channels
(`mystoriesanimated`, `scrollpulse`, `historyrecapped`, `cosmosdecoded`)
and ignored the user's selected variant / niche / language; the other
channels surfaced a 422 toast. As of 2026-05-11 the dispatcher always
returns a usable suggestion, blends LLM brainstorming with native
sources, and respects the form context.

## Endpoints

* `GET  /api/discover/{channel}/feed?variant=…&length_kind=…&language=…&niche_key=…`
  — up to 10 candidate topics. Query-string context for cacheability.
* `POST /api/discover/{channel}` — single picked topic. Body is an
  optional `DiscoverRequest`:

  ```jsonc
  {
    "variant": "tifu",            // create-form variant key
    "length_kind": "short",        // "short" | "long"
    "language": "hi",              // optional override
    "niche_key": "tifu",           // matches <channel>/niches/<key>.json when present
    "values": { "notes": "...", "music_bed": "ambient_low" },
    "avoid": ["topic A", "https://r/x/123"]   // already-shown topics/URLs
  }
  ```

Both endpoints return either a `DiscoverItem` or `DiscoverFeed` —
schemas live in `control/routes/discover_routes.py`.

## Adapter routing — niche-source-driven (2026-05-12)

> **Channel-wide engineering fix (commit `c4bf4fc`, 2026-05-12).**
> Pre-fix, `_native_items_for(channel, req)` hard-coded per-channel
> routing (`historyrecapped` → always today_in_history, etc.) and
> ignored `req.niche_key` entirely. A user picking the
> `ancient_civilizations` niche on `historyrecapped` got a Wikipedia
> "On this day in 1982: Pope assassination attempt" — utterly
> unrelated. Post-fix, dispatch is driven by the **selected niche's
> persisted `source: {kind, ref}` field** uniformly across every
> channel.

### How dispatch works now

`_native_items_for(channel, req, limit)`:

1. **Niche-driven** (when `req.niche_key` resolves to a NicheDoc):
   call `_niche_native_items_for(niche, req, limit)`. This is the
   ONLY native source; the function MUST NOT fall back to a
   channel-default adapter.
2. **Niche-key set but unresolvable** (deleted niche, FE cache stale,
   typo): return `("", [])` and let the LLM brainstorm carry.
3. **No niche selected** (`req.niche_key` absent — older clients,
   GET feed without query string): legacy channel-default branches
   kick in (the table below). Back-compat safety net only.

### Niche-source dispatch table

`_niche_native_items_for(niche, req, limit)` switches on
`niche.source.{kind, ref}`. The same table applies regardless of
which channel hosts the niche:

| `source.kind` | `source.ref`                              | adapter                                   |
| ------------- | ----------------------------------------- | ----------------------------------------- |
| `reddit`      | subreddit name (or `r/<name>`)            | `_reddit_items(sub, "top", "day")`        |
| `reddit`      | `null` / `r/` (no actual sub)             | `("", [])` — LLM-only                     |
| `wikipedia`   | `On_this_day`                             | `_today_in_history_items`                  |
| `wikipedia`   | `List_of_*` / `Lists_of_*` / page title   | `_wikipedia_list_items(page=ref)`         |
| `wikipedia`   | `null`                                    | `("", [])` — LLM-only                     |
| `rss`         | HN feed URL (`news.ycombinator.com`)      | `_ai_news_items`                           |
| `rss`         | other URL / `null`                        | `("", [])` — LLM-only                     |
| `manual` / `x_twitter` / `youtube` / unknown / missing | `*`              | `("", [])` — LLM-only                     |

**No fallback to channel defaults when a niche is selected.** This is
a deliberate precedence rule — falling back to (e.g.)
`historyrecapped → today_in_history` for a niche with `source.kind =
manual` would silently reintroduce the original "wrong topic" bug.
LLM brainstorm is the always-available second leg.

### `_wikipedia_list_items(page, limit)`

Thin wrapper around `pipeline.sources.wikipedia.fetch(page=...)`. Each
DiscoverItem gets a UNIQUE `source_ref` of the form
`https://en.wikipedia.org/wiki/<page>#<entry-slug>` so the
`avoid` filter blacklists individual picks, NOT the whole page after
the first "Generate another" click.

### Legacy channel-default branches (no-niche fallback only)

These run only when `req.niche_key` is absent. Every modern create-
flow path passes a niche, so these are effectively a back-compat
shim for older clients and ad-hoc `GET /feed` calls without a query
string.

| channel             | variant                   | adapter                                    |
| ------------------- | ------------------------- | ------------------------------------------ |
| `mystoriesanimated` | `aita` (default)          | `reddit:AmItheAsshole`                     |
| `mystoriesanimated` | `tifu` / `malicious` / `prorevenge` | `reddit:<sub>` per `VARIANT_SUBREDDIT` |
| `mystoriesanimated` | `oddities` / `tih` / `wiki_misconceptions` | `wikipedia:onthisday[ · keyword]` |
| `scrollpulse`       | any                       | `reddit:<random> (round-robin)`            |
| `historyrecapped`   | any                       | `wikipedia:onthisday`                      |
| `cosmosdecoded`     | any                       | `wikipedia:onthisday (physics)`            |
| `hindutavaanimated` / `sportsrecapped` / `rhymetimejunction` | any | `("", [])` — LLM only |

`VARIANT_SUBREDDIT` and `VARIANT_WIKI_KEYWORD` in `discover_routes.py`
remain for this fallback; they're NOT consulted on the niche-driven
path. Adding a new variant on a channel means seeding the niche JSON
under `<channel>/niches/<key>.json` with the right `source: {kind,
ref}` — no code change needed.

### Behaviour change for `wiki_misconceptions` / `wiki_oddities`

Pre-fix, these mystoriesanimated variants used `today_in_history`
filtered by the keyword `misconception` (essentially "today's events
that happen to mention the word misconception"). Post-fix, when the
user selects them as a niche, dispatch routes through
`niche.source = {kind: wikipedia, ref: List_of_common_misconceptions}`
(or `List_of_unusual_deaths` for oddities) and scrapes the actual
list page. This is the intended improvement — narrower, on-topic
topics.

## LLM brainstorm

`_llm_topic_items(channel, req)` always runs alongside the native
adapter (per the 2026-05-11 product decision: blend LLM into the
candidate pool even for Reddit-backed channels). It assembles a prompt
from:

* The `ChannelSummary` (`label`, `tagline`, `language`, `default_format`).
* The user-selected `variant` and `length_kind`.
* The matching `NicheDoc` when `<channel>/niches/<niche_key>.json` exists
  (gives the LLM the curated `prompt_style_guide` and `hook_template`).
* Any free-form `values.notes` from the form.
* The `avoid` list (already-shown topics — capped at 20 entries).

It dispatches via [`pipeline.llm.cli.call_llm`](./llm_backend_dispatcher.md)
with `model="haiku"`, `output_json=True`, and a strict JSON Schema
requiring `{"items": [{"topic": "...", "hook": "..."}]}`. Failures
(missing backend, network error, malformed JSON) are non-fatal —
`_llm_topic_items` returns `[]` and the caller falls back to native
items only.

### Disabling LLM augmentation

* `YTFACTORY_DISCOVER_LLM_DISABLE=1` — disables LLM brainstorming
  globally. Used in unit tests and as a kill-switch if Azure costs
  spike.
* The dispatcher inherits all backend selection from
  `YTFACTORY_LLM_BACKEND` (laptop → `cli`, cloud → `azure_openai`,
  see `docs/llm_backend_dispatcher.md`).

### Response shape

LLM-generated items are tagged `source_kind="llm"` with
`source_ref=null` and `source_label="LLM · <Channel> · <variant>"`. The
suggested hook lives at `metadata.hook`; the create wizard currently
ignores it but downstream tools (e.g. the rewrite stage) can grab it
when the user clicks "Use this".

## Failure modes

* Both native + LLM empty → `502 upstream returned no candidates`.
* Channel has no native adapter AND `YTFACTORY_DISCOVER_LLM_DISABLE=1` →
  `502 no auto-generate source produced topics for '<channel>'` with a
  hint to set `YTFACTORY_LLM_BACKEND`.
* The endpoint **never** returns 422 for "unknown channel" any more —
  every channel is handled.

## Frontend wiring

`web-next/lib/api.ts::discoverApi.{feed,pickOne}` accept an optional
`DiscoverContext`. The create page (`web-next/app/app/create/page.tsx`)
threads `variant`, `length_kind`, `language`, `niche_key`, `values`,
and a local `shownTopics` array (scoped to one mount of the topic card
so successive "Generate another" clicks don't repeat).

## Tests

`tests/test_routes_discover.py` covers:

* All adapter branches (Reddit per-variant, Wikipedia onthisday,
  cosmos physics filter, no-native channels) — via the legacy
  channel-default fallback path.
* **Niche-driven dispatch (2026-05-12)** — every branch in
  `_niche_native_items_for` (reddit / wikipedia+On_this_day /
  wikipedia+List_of_* / wikipedia+null / rss+HN / rss+other /
  manual / x_twitter / unknown / missing source / `r/`-only ref)
  pinned at unit + integration + end-to-end levels. The exact
  user-reported regression (historyrecapped + ancient_civilizations
  → must NOT call today_in_history) is asserted in
  `TestBuildFeedNicheDriven::test_history_ancient_civilizations_does_not_show_today_in_history`.
* `_wikipedia_list_items` adapter — per-entry `source_ref` is unique
  (so `_filter_avoid` doesn't nuke the whole page after one pick).
* `_filter_avoid` — case-insensitive match on topic and on `source_ref`.
* LLM brainstorm: env-disabled, parsed-list, raw-string payload,
  upstream failure, dedup.
* `_build_feed`: blended native + LLM, LLM-only fallback for no-native
  channels, 502 when both empty.
* HTTP layer: `GET /feed?variant=tifu` routes to r/tifu;
  `POST /{channel}` honours body context + `avoid`; LLM-only Hindutava
  path returns 200.

LLM tests patch `pipeline.llm.cli.call_llm` and
`control.routes.discover_routes._llm_disabled` so CI never hits a real
LLM backend.
