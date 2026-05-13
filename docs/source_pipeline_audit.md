# Source-extraction pipeline audit (2026-05-13)

**Status:** opinion / proposal — not yet acted on.
**Scope:** every channel that mines text from an external source
(Reddit / Wikipedia / arxiv / YouTube transcript / today-in-history /
news article).
**Triggered by:** "are we just downloading the reddit title for stories?"
The answer turned out to be no — but the architectural pattern that
made the question reasonable to ask is real and worth fixing.

This doc ranks pipeline-level fixes **P0 → P3**. P0 is the data-shape
choke-point that everything else chains off; touching it is the
single highest-leverage change.

---

## TL;DR

* `RawStory` (the only handoff between fetch and rewrite) is anaemic —
  one flat `body: str` plus an untyped `metadata: dict`. Rich source
  signal (Reddit comments, Wikipedia infoboxes, OP edits, related
  papers) gets dropped at fetch because there is no slot for it.
* Six rewriters (`rewrite`, `rewrite_part2`, `rewrite_long_form`,
  `airecap_rewrite`, `ai_recap_rewrite`, `cast`) all do the same
  `title + body[:N]` plumbing and have drifted independently.
* Per-channel choices (closer wording, char caps, character defaults)
  are baked into adapters and YAML when they should be derived from
  source signal we already throw away.
* "Primary sources" cited in cosmos / katha / top-10 skill prompts
  are not actually fetched by the pipeline — the LLM cites them from
  training data and the renderer trusts it.
* No render persists a `source_trace.json` that maps narration spans
  back to source spans, so source-fidelity QC is structurally hollow.
* `pipeline/llm/cache.py` exists but the dispatcher (`pipeline/llm/cli.py`)
  doesn't use it — every rewrite re-pays for identical context.

---

## Architectural smell: the `RawStory` choke point

`pipeline/sources/base.py`:

```python
@dataclass
class RawStory:
    slug: str
    title: str
    body: str
    source: str
    url: str
    metadata: dict = field(default_factory=dict)
```

Every adapter MUST flatten its source into this shape. Every consumer
only reads two fields:

```bash
$ grep -nE "raw_story\.get" pipeline/llm/*.py | grep -v test
pipeline/llm/rewrite.py:558:    title = (raw_story.get("title") or "").strip()
pipeline/llm/rewrite.py:559:    body  = (raw_story.get("body")  or "").strip()
pipeline/llm/rewrite_long_form.py:440-441:                  # same two lines
pipeline/llm/airecap_rewrite.py:131-132:                    # same two lines
pipeline/llm/ai_recap_rewrite.py:131-132:                   # same two lines
pipeline/llm/cast.py:167-168:                              # same two lines
```

**Not one rewriter ever reads `metadata`.** Even if a fetcher stuffed
comments into `metadata.top_comments`, no consumer would notice.

So the fetch-time char cap is always "what the first consumer's
prompt happened to need" — never "what the source can give us":

| Adapter | Cap | Where the cap actually lives |
|---|---|---|
| `reddit_api` | 6000 chars | `rewrite.py` slices `story_text[:6000]` |
| `wikipedia` (oddities) | 1500 chars | the 110-160 word Short prompt |
| `today_in_history` | extract only | same Short prompt |
| `ai_news` | 4000 chars | `airecap_rewrite.py` ~6000 minus prompt overhead |
| `research/wiki` | 15000 chars | sportsrecapped dossier extraction prompt |
| `youtube_video` | uncapped | long-form has no cap either |

When ScrollPulse needed Reddit comments, **someone forked the fetcher**
(`pipeline/social/reddit_scrape.py`) instead of extending `RawStory`.
We now maintain two Reddit clients with different output shapes.

---

## P0 — Replace `RawStory` with a richer typed `RawDoc`

**Why P0.** This single change unblocks every other item in this doc.
While the data shape is anaemic, every quality fix gets squeezed
through `metadata: dict` and silently dropped.

**Proposal:**

```python
@dataclass
class RawDoc:
    slug:         str
    title:        str
    body:         str
    source:       str
    url:          str

    # everything below is OPTIONAL and source-kind-specific. Adapters
    # populate what's natural for them; rewriters opt in to fields
    # they consume. No field is silently dropped.
    excerpts:     list[Excerpt]   = field(default_factory=list)  # quote+offset
    comments:     list[Comment]   = field(default_factory=list)  # OP-style threads
    updates:      list[Update]    = field(default_factory=list)  # OP edits, follow-ups
    related_docs: list["RawDoc"]  = field(default_factory=list)  # cited paper, linked page
    media:        list[MediaRef]  = field(default_factory=list)  # images / video
    metadata:     SourceMetadata                                 # TYPED per source-kind
```

**Hard rules that come with the new shape:**

1. **No truncation in adapters.** Cap moves to prompt-builder where
   it's an explicit channel choice.
2. **`metadata` becomes typed.** A union over `RedditMetadata`,
   `WikipediaMetadata`, `YouTubeTranscriptMetadata`, … so rewriters
   that key off it stop being string-typed dict accesses.
3. **One Reddit fetcher.** `pipeline/social/reddit_scrape.py` retires;
   its comment-fetch path lands on `pipeline/sources/reddit_api.py`
   behind a `with_comments=True` flag.
4. **Backward-compat shim.** `RawStory` aliases `RawDoc` with empty
   list defaults so pre-migration call sites keep working until each
   one is reviewed and either consumes the new fields or explicitly
   opts out.

**Concrete migration steps (in order, each safely shippable):**

1. Define `RawDoc` + per-source `*Metadata` dataclasses; alias
   `RawStory = RawDoc` for back-compat. **No behaviour change.**
2. Migrate `reddit_api.fetch_post_by_url()` to populate `comments[]`
   and `updates[]` (OP's reply edits).
3. AITA Short rewriter reads `comments[0]` to derive a verdict-honest
   closer. **First user-visible win.** See P1#1 below.
4. Migrate `pipeline/social/reddit_scrape.py` callers (ScrollPulse)
   to the unified fetcher. Delete the duplicate.
5. Migrate `wikipedia` / `today_in_history` / `ai_news` adapters to
   populate `media[]` + `references[]`. No consumer change yet.
6. Add `arxiv_abstract`, `nasa_ntrs`, `gita_press` adapters that
   populate `related_docs[]`. Wires P1#3 below.

---

## P0 — Collapse six near-identical rewriters into one engine

**Why P0.** Pure duplication. A craft-rule fix today touches six files;
some forget to run `script_lint`; one (`rewrite_long_form`) doesn't
go through the contract layer the orchestrator was built for.

The six modules — `rewrite.py::rewrite`, `rewrite.py::rewrite_part2`,
`rewrite_long_form.py::rewrite`, `airecap_rewrite.py::rewrite`,
`ai_recap_rewrite.py::rewrite`, `cast.py::author_cast` — all do:

```python
title = (raw_story.get("title") or "").strip()
body  = (raw_story.get("body")  or "").strip()
story_text = f"{title}\n\n{body}" if title else body
prompt = _PROMPT_TEMPLATE.format(story=story_text[:N], …format kwargs…)
out = call_claude_cli(prompt, output_json=True, model=…, stage="…")
…validate / lint / persist…
```

The contract layer at `pipeline/llm/contracts/` already encodes the
"validate, retry, persist" loop. Two of the six rewriters use it; the
other four shell out directly. **Finish wiring everything through
contracts and the rewriters become 6 short config objects.**

**Proposal:**

```python
class RewriteEngine:
    def __init__(self, channel_cfg: ChannelCfg, format_spec: FormatSpec): …
    def rewrite(self, doc: RawDoc) -> Script: …

# format_spec is data, not code:
FormatSpec(
    length=LengthBand(words=(110, 160), sentences=(10, 15), duration_s=(22, 32)),
    closer=CloserPolicy.AITA,         # or Cliffhanger, Subscribe, AIRecap, …
    schema_mode=SchemaMode.SHORT,     # or LongForm, Lyrics, …
    consume=("title", "body", "comments[0]"),  # explicit fields read from RawDoc
    pronounce_dict_source="cast",
)
```

The 6 modules become 6 `FormatSpec` instances + one engine.

---

## P1 — Closer / verdict / character are channel-hardcoded when they should be source-derived

These are the *symptom* of P0. Worth listing because each is a visible
quality bug today, not just a code-smell.

### P1#1 — AITA closer ignores the actual verdict

`pipeline/variants/mystoriesanimated/aita_*.yaml` hardcodes
`closer_format: "LIKE if YTA, COMMENT if NTA. AITA?"` regardless of
what the post's top comment actually said. Reddit's NTA/YTA verdict
is in the comment thread we already throw away.

**Fix (post-P0):** AITA rewriter reads `doc.comments[0]` for the
verdict tag. Closer becomes `"Reddit said {verdict}. You?"` or similar.
More honest, more engagement-bait, zero new prompt complexity.

### P1#2 — Per-story `cast.json` competes with YAML `character_description`

`aita_animated.yaml` carries a `character_description` "backwards-compat
fallback" for stories without `cast.json`. In practice this hides
cast-author drift — when the cast file IS authored but doesn't match
the YAML default, art looks weird in ways nobody attributes to the
right cause. Either per-story cast IS the source of truth or it isn't;
the dual path needs to die.

### P1#3 — "Primary sources" cited in skills are never fetched

`/make-cosmos-long`, `/make-katha`, `/make-top10` skill prompts say
**"cite ≥2 primary sources per case"**. The renderer never fetches
arxiv / NASA NTRS / Gita Press / sutta texts. The LLM cites them from
training data; the pipeline trusts it. For channels whose entire pitch
is "we cite primary sources", this is the credibility-gap bug.

**Fix (post-P0):** once `RawDoc.related_docs[]` exists, add fetchers:

| Channel | Fetchers to add |
|---|---|
| cosmosdecoded | `arxiv_abstract.py`, `nasa_ntrs.py`, `nasa_ads.py` |
| hindutavaanimated katha | `gita_press.py`, `valmiki_ramayana.py` |
| historyrecapped top-10 | `loc_gov.py`, `smithsonian.py`, `britannica_via_archive.py` |

The skill builds the citation list; the pipeline pulls it; the LLM is
asked to ground claims in **fetched** text. The critic's `L13 Source
fidelity` check (`pipeline/llm/critic.py:213`) finally has something
real to grade against.

### P1#4 — Six AITA YAMLs duplicate the same five lines

`min_chars: 400 / max_chars: 6000 / closer_format / closer_hold_s /
character_description` all copy-pasted across:

```
aita_animated.yaml
aita_animated_motion.yaml
aita_cliffhanger_animated.yaml
aita_cliffhanger_cooking.yaml
aita_cliffhanger_part2_animated.yaml
aita_cliffhanger_part2_cooking.yaml
aita_cliffhanger_part2_text.yaml
aita_cliffhanger_text.yaml
aita_cooking.yaml
aita_text.yaml
```

Six places to change one rule. Variants need YAML inheritance
(`extends: aita_base.yaml`) and the shared rules belong in
`pipeline/niches.py`, not the YAMLs.

---

## P2 — No `source_trace.json` per render

For any shipped video you cannot answer "which narration sentence
came from which source span?". The critic's `L13 Source fidelity`
check grades narration vs `body` — the same `body` the rewriter LLM
already saw, so the check is structurally hollow (graded by the same
context that produced the answer).

**Proposal:**

```
<channel>/<niche?>/source_trace/<slug>.json
{
  "raw_doc": { …RawDoc dump… },
  "script":  { …Script dump… },
  "spans": [
    {"narration_sentence": 0, "source_excerpts": ["raw.body[120:298]"]},
    {"narration_sentence": 1, "source_excerpts": ["raw.comments[0].body[0:90]"]},
    …
  ],
  "kept_chars":     1842,
  "source_chars":   4960,
  "compression_x":  2.7,
}
```

LLM emits the `spans[]` map as part of the rewrite output (cheap —
it already saw both sides). Operationally unblocks:

* Real source-fidelity grading (L13 with an external grader).
* Showing the user a kept-vs-dropped ratio in the dashboard.
* Catching "the LLM made up a name not in source" regressions.

---

## P2 — LLM-call dedupe across stages

`pipeline/llm/cache.py` defines `CacheBackend` + `get_default_cache()`.
`pipeline/llm/cli.py` (the dispatcher every rewriter calls) **does not
use it.** Verified 2026-05-13:

```bash
$ grep -nE "get_default_cache|prompt_hash" pipeline/llm/cli.py
# returns nothing — the "cache" hits in cli.py are about Azure
# deployment config, not prompt-result caching.
```

So a long-form render that does ~50 LLM calls (rewrite, cast, prompts,
critic, audio_critic, anatomy_check, image_lint, …) re-pays for the
identical 15k-char Wikipedia article in every stage that includes it
as context, and a re-render of the same slug re-pays for everything.

**Fix:** wire `get_default_cache()` into `call_claude_cli()` keyed on
`hash(prompt + model + temperature)`. Already-existing module, ~30
lines to wire, opt-in via `YTFACTORY_LLM_CACHE=1` to start.

---

## P3 — Source-adapter registry is hardcoded string-matching

`scripts/pull_stories.py` has separate `cmd_reddit / cmd_wiki /
cmd_tih / cmd_youtube` functions. Channel YAML says
`source_adapter: reddit_video` but no registry binds that string to
a fetcher — the dispatch is via Python `if/elif`. New source kind =
code change in three places (the YAML, `pull_stories.py`, the
relevant rewriter).

**Fix:** small registry in `pipeline/sources/__init__.py` mapping
`source_adapter` strings → `(adapter_module, fetch_callable, default_kwargs)`.
Channel YAML names the adapter; dispatcher does the lookup. New
sources become a one-file addition.

Low-pain today; every new channel/niche hits it though, so worth
fixing alongside the P0 RawDoc migration.

---

## Recommended execution order

If we touch one thing this week, it should be **P0 #1 partial** —
just enough of the `RawDoc` refactor to land the AITA verdict-derived
closer (P1 #1). That single change:

1. Proves the new shape works end-to-end against the highest-volume channel.
2. Kills the duplicate Reddit fetcher (`pipeline/social/reddit_scrape.py`).
3. Ships an immediate, visible quality win (verdict-honest closer).
4. Establishes the migration pattern for the next adapter.
5. Forces us to start `RewriteEngine` (P0 #2) because the AITA
   rewriter now has to make a real choice about which fields to consume.

Everything else (cosmos primary sources, YAML inheritance,
source_trace, cache dedupe) chains off this without re-litigating
the data shape.

---

## What this doc deliberately does NOT recommend

* **Adding more variants / channels right now.** Every new channel
  pays the per-format duplication tax. P0 #2 first.
* **A full vector-RAG layer over source.** The data isn't large enough
  to warrant it; the choke-point is shape, not scale.
* **Switching off `claude` CLI for the rewrite stage.** Backend
  dispatcher (`docs/architecture.md` §LLM) is fine; rewriter de-fork
  is independent of which backend it routes to.

---

## File map (where the work lands)

| Concern | Files |
|---|---|
| `RawDoc` shape | `pipeline/sources/base.py` (new types), every `pipeline/sources/*.py` adapter |
| Reddit unification | `pipeline/sources/reddit_api.py` (gain `with_comments`), `pipeline/social/reddit_scrape.py` (delete) |
| Rewriter de-fork | `pipeline/llm/rewrite*.py`, `pipeline/llm/cast.py`, `pipeline/llm/airecap_rewrite.py`, `pipeline/llm/ai_recap_rewrite.py`, `pipeline/llm/contracts/` |
| YAML inheritance | `pipeline/variants/mystoriesanimated/*.yaml`, new `aita_base.yaml`, `pipeline/niches.py` |
| Primary-source fetchers | new `pipeline/sources/arxiv_abstract.py`, `nasa_ntrs.py`, `gita_press.py`, `valmiki_ramayana.py` |
| `source_trace.json` | every rewriter (emit) + `pipeline/paths.py` (path) + critic (consume) |
| LLM cache wiring | `pipeline/llm/cli.py`, `pipeline/llm/cache.py` |
| Source-adapter registry | `pipeline/sources/__init__.py`, `scripts/pull_stories.py`, channel YAML loader |
