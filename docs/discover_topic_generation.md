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

## Adapter routing

`_native_items_for(channel, req)` returns `(adapter_label, items)`:

| channel             | variant                   | adapter                                    |
| ------------------- | ------------------------- | ------------------------------------------ |
| `mystoriesanimated` | `aita` (default)          | `reddit:AmItheAsshole`                     |
| `mystoriesanimated` | `aita_cliffhanger`        | `reddit:AmItheAsshole`                     |
| `mystoriesanimated` | `tifu`                    | `reddit:tifu`                              |
| `mystoriesanimated` | `malicious`               | `reddit:MaliciousCompliance`               |
| `mystoriesanimated` | `prorevenge`              | `reddit:ProRevenge`                        |
| `mystoriesanimated` | `oddities`                | `wikipedia:onthisday`                      |
| `mystoriesanimated` | `tih`                     | `wikipedia:onthisday`                      |
| `mystoriesanimated` | `wiki_misconceptions`     | `wikipedia:onthisday · misconception`      |
| `mystoriesanimated` | `aita_cooking`            | `(no native — LLM only)`                   |
| `scrollpulse`       | any                       | `reddit:<random> (round-robin)`            |
| `historyrecapped`   | any                       | `wikipedia:onthisday`                      |
| `cosmosdecoded`     | any                       | `wikipedia:onthisday (physics)`            |
| `hindutavaanimated` | any                       | `(no native — LLM only)`                   |
| `sportsrecapped`    | any                       | `(no native — LLM only)`                   |
| `rhymetimejunction` | any                       | `(no native — LLM only)`                   |

Variants → subreddits are mapped via `VARIANT_SUBREDDIT` /
`VARIANT_WIKI_KEYWORD` in `discover_routes.py`. Adding a new variant is a
one-line entry there.

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
  cosmos physics filter, no-native channels).
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
