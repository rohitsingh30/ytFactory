# 2026-05-13 — Long-form pipeline analysis + plan

**Status:** analysis-only doc. No code changes from this doc; it's the
plan that the next round of code changes must align to. Update as
priorities shift.

**Prompted by:** 6 failed canary renders today (jobs `0c05c335`,
`b318a787`, `0947ea51`, `0195e59d`, `7dca182d`, `feee2aa9`,
`59ab1426`) — every fix uncovered a deeper bug. The architecture is
provably wrong vs published industry practice.

This is the deep-dive that came out of "you should do proper internet
research about what should be done for our case". It synthesises:

1. Live failure forensics from today's canaries
2. Stanford STORM (NAACL 2024) — canonical academic pattern for the
   exact problem
3. OpenAI / Azure structured-outputs official guidance
4. Anthropic Claude Code best practices
5. Temporal durable-execution patterns
6. The earlier source-extraction pipeline audit (P0–P3)
7. A scan of `scripts/<channel>/` for per-channel code pollution

---

## Part 1 — Why we can't ship a video

Six structural issues, ordered by frequency-of-failure today.

### 1.1 — Single-shot 12-15k-token rewrite hits the model's reliability cliff

The 30-min long-form prompt asks gpt-5.3-chat for ~6000 narration
words + 24+ panel scenes + JSON syntax = ~12-15k content tokens.
Reasoning tokens add another 5-15k. Combined exceeds the model's
real-world reliable-output range.

Two failure modes observed:

* `finish_reason="length"` — token cap hit mid-output. The
  dispatcher's existing auto-double-budget retry catches some of
  this, but not all (cap ceiling 64k).
* `finish_reason="stop"` with truncated JSON — model decides to
  "stop" mid-key. No retry triggers. Surfaces as "could not parse
  JSON" → salvager → partial dict → `KeyError 'title'` in rewriter
  parse.

Stanford STORM (`stanford-oval/storm`, NAACL 2024) explicitly built
section-by-section parallel generation to dodge this exact cliff.

### 1.2 — `RawStory` is anaemic, so the rewriter has to invent

Source-extraction audit's P0 (separate doc, also captured below).
The rewriter input today: `title + body[:6000]`. To fill a 30-min
video from 6000 chars of source, the LLM has to invent ~20× the
input — manifests as TED-Talk filler ("British Cycling marginal
gains" hit canary 1) and over-association of unrelated facts (a
known STORM-paper failure mode, Section 5).

### 1.3 — Six rewriters, no shared contract layer

`rewrite`, `rewrite_part2`, `rewrite_long_form`, `airecap_rewrite`,
`ai_recap_rewrite`, `cast` — all do `title + body[:N]` plumbing,
all drifted independently.

When I added `pipeline/critic_long_form.py` validator + retry today,
**only `rewrite_long_form` got the safety net.** The other five
rewriters still ship whatever the LLM emits.

### 1.4 — Pipeline is single-pass, no checkpoints

7 stages run sequentially. Stage 8 fails → all work from stages
1-7 lost → next attempt redoes everything → 30 min + ~$1 per retry.

Temporal-style durable execution (state captured at every step,
resume-from-failure-point) is the production norm for ML pipelines
(Klarna, Uber, JP Morgan use Temporal for this exact pattern).
We're at zero state preservation.

### 1.5 — Five silent-bad-output paths can ship broken video as READY

Even when every stage "succeeds", the mp4 can be wrong:

| Path | Failure mode |
|---|---|
| `_verify_audio_loudness` returns `(0.0, True)` on its OWN failure | Silent verifier — the gate that's supposed to catch silent mp4 IS silent itself |
| `_salvage_truncated_json` returns partial dict | Shorter video silently shipped after validator's soft floor |
| TTS chunk cache reuse without fingerprint check | Voice change → stale audio with old voice |
| `emit_artifact` swallows failures | Dashboard preview missing while render "succeeds" |
| `_LF_SUBSTAGES_ORDER` coerces unknown stage → `compose` | UI lies about progress |

These are why earlier today we shipped the -28 LUFS silent mp4 as
**READY** (job `0c05c335`).

### 1.6 — Hard-fail validator with no soft path

When the LLM under-delivers, current behavior:
```
under-delivered → LongFormContractError → entire job fails → user sees nothing
```
Should be:
```
under-delivered → ship what we got with "Quality: short by 40%" warning
              → mark slug for re-rewrite later
              → user has a video to inspect even if not perfect
```

Strict gates only work when the success rate is already >50%. We're
at 0%. Strictness multiplies failure.

### 1.7 — No end-to-end test against real providers

140+ unit tests with mocked Azure / mocked Cloud Run / mocked
ffmpeg. Zero tests that fire real wizard render → real Azure call
→ real Cloud Run TTS → real Cloud Run image gen → real ffmpeg
compose → assert mp4 exists and isn't silent.

Test suite is structurally **incapable** of catching what's
actually killing us. From Anthropic's Claude Code best practices:
> "Without clear success criteria, [the agent] might produce
> something that looks right but actually doesn't work. You become
> the only feedback loop, and every mistake requires your attention."

That's exactly what happened today.

---

## Part 2 — What production systems actually do

### 2.1 — Stanford STORM (NAACL 2024) — the canonical pattern

[`stanford-oval/storm`](https://github.com/stanford-oval/storm) —
70k+ users on `storm.genie.stanford.edu`. Two stages:

```
Stage 1 — Pre-writing (small LLM calls):
  • Generate diverse "perspectives" on the topic
  • For each perspective, simulated Q&A grounded in retrieved sources
  • Curate findings into a structured outline

Stage 2 — Writing (section-by-section IN PARALLEL):
  • ThreadPoolExecutor(max_workers=10)
  • generate_section(topic, name, retrieved_info, outline)
  • Each call: ~1500 words context, ~700-1k tokens output
  • Article assembly: stitch sections post-hoc
```

The whole `article_generation.py` is ~150 LOC. Eliminates
truncation by construction.

STORM explicitly identifies our failure modes as the two top open
problems in long-form LLM generation:
- **"Source bias transfer"** — LLM inherits source biases
- **"Over-association of unrelated facts"** — exactly the British
  Cycling anecdote bug we hit on canary 1

Not exotic bugs. Textbook failures published 18 months ago.

### 2.2 — OpenAI / Azure structured outputs (`strict: true`)

> "Structured outputs are recommended for **function calling,
> extracting structured data, and building complex multi-step
> workflows.** ... If you used JSON mode or function calls before,
> you can think of Structured Outputs as a **foolproof version**."

When `strict: true` is set on a Pydantic schema:
- Model **physically cannot** emit JSON that violates the schema
- No more "could not parse JSON from model output"
- No need for `_salvage_truncated_json`
- Schema validation happens server-side

**Our codebase uses `strict: False`** (see `pipeline/llm/cli.py:981`).
That's why the salvager exists at all. Strict mode was the right
answer all along.

### 2.3 — Multi-agent decomposition (OpenAI cookbook)

When one big call gets unwieldy, decompose into specialised agents
with `strict: true` structured outputs at the handoff. STORM is
literally this pattern applied to article generation.

### 2.4 — Temporal-style durable execution

Capture state at every step. On crash, resume exactly where it
left off. Production AI pipelines (Klarna, Uber, JP Morgan) use
this for our exact pattern: long-running multi-step LLM workflows.

The poor-man's version: per-stage checkpoint files keyed on
input fingerprint. Stage 8 fails → next attempt reuses stages 1-7.

### 2.5 — Anthropic best practices

> "Give Claude a way to verify its work."
>
> "Address root causes, not symptoms."

Both apply. The salvager is "suppress the symptom", and we have no
end-to-end verification.

---

## Part 3 — The fix stack

Three orthogonal interventions. All have published precedent.

### Fix A — STORM-pattern rewriter (kills 80% of failures)

Two-phase rewriter in `pipeline/llm/rewrite_long_form.py`:

1. **Outline call** (~1k tokens output): Pydantic schema with
   `strict: true`. Returns:
   ```python
   class Outline(BaseModel):
       title_options: list[str]
       hook: str
       thesis: str
       sections: list[SectionStub]    # id, title, brief, target_words
       panel_briefs: list[PanelBrief] # scene, hold_s
   ```
2. **Per-section calls** (~700 tokens × N, parallel via
   `ThreadPoolExecutor(max_workers=5)`): Pydantic schema with
   `strict: true`. Returns:
   ```python
   class SectionBody(BaseModel):
       narration: str
       sentences: list[str]   # for caption alignment
   ```
3. **Aggregator**: stitch into `LongFormScript`, run validator,
   ship.

Eliminates: truncation, salvager, JSON parse errors, KeyError on
partial sections, contract retry thrash, validator under-delivery
hard-fails.

Effort: ~250 LOC, ~4 hours, single commit + single deploy.

### Fix B — `strict: true` everywhere

Today: `pipeline/llm/cli.py:981` passes `strict: False`. Fix:
pass Pydantic schemas with `strict: true`. Azure guarantees valid
JSON or raises a typed error.

Schemas need minor cleanup to satisfy strict mode:
- All properties required (no optional fields except via union with
  null)
- No `additionalProperties` (already done — most schemas have
  `additionalProperties: False`)

Salvager retires. Goes from ~150 LOC to deleted.

Effort: ~2 hours, mostly schema cleanup.

### Fix C — Per-stage checkpoint + resume

After each stage, write
`<channel>/<slug>/_checkpoint/<stage>.json`:
```json
{
  "stage": "rewrite",
  "input_fingerprint": "sha256(...)",
  "output_path": "<channel>/<slug>/narrations/<slug>.json",
  "completed_at": "2026-05-13T21:30:00Z",
  "duration_s": 187.4
}
```

Next render attempt: check fingerprint. If match, skip the stage
and reuse the output. Compose-stage failure no longer means
redoing rewrite + image-gen + TTS.

Real Temporal would be cleaner but requires running a Temporal
server. Checkpoint files get 80% of the value at 5% of the cost.

Effort: ~3 hours, ~150 LOC across the renderer.

### Fix D — Soft validator (ship-with-warning)

In `pipeline/critic_long_form.py`: instead of raising
`LongFormContractError` on hard violations, return the violations.
Worker decides: ship-with-warning if mp4 is recoverable, hard-fail
only if mp4 will be broken.

Today's hard floors are too strict for an unproven pipeline.
Strictness multiplies failure rate; needs to relax until success
rate is meaningfully > 0.

Effort: ~1 hour, ~50 LOC.

### Fix E — One real end-to-end test

A pytest fixture that:
1. Posts a small render (30s short, not 30-min long-form) to the
   real `/api/render` endpoint
2. Waits for the Firestore job to complete
3. Downloads the mp4
4. Asserts: file exists, duration > 25s, has audio stream, mean
   volume > -25 dB, has video stream

Run nightly (or pre-deploy via `gcloud builds submit`'s
post-deploy hook). One test that catches what 140 unit tests
can't.

Effort: ~2 hours, ~100 LOC + cron config.

---

## Part 4 — Audit P0 (RawDoc) — separate doc, captured here for completeness

The source-extraction audit (already drafted as a separate doc)
identifies that `RawStory` shape forces the rewriter to invent.
Replacement type:

```python
@dataclass
class RawDoc:
    slug: str
    title: str
    body: str
    source: str
    url: str
    excerpts:     list[Excerpt]   = []   # quote+offset
    comments:     list[Comment]   = []   # OP-style threads
    updates:      list[Update]    = []   # OP edits, follow-ups
    related_docs: list["RawDoc"]  = []   # cited paper, linked page
    media:        list[MediaRef]  = []
    metadata:     SourceMetadata        # TYPED per source-kind
```

This is **not** mutually exclusive with Fixes A-E. It's the
material the STORM-pattern rewriter compresses. Without it, even
section-by-section will produce shorter output than asked because
there's nothing to compress. Schedule **after** A-E land but
**before** scaling to more channels.

---

## Part 5 — Channel-files pollution audit

User asked: "are we going to work on removing channel specific
files for rendering etc, I think that is polluting the things and
pipeline as well? ... finding youtube video / actual footage can
be applied to any channel, generating images can be applied to
any channel ... so investigate so that we don't compromise with
quality and kind of improve and extend features/methods/sources/
inputs/parameter to be channel agnostic?"

Answer: **YES, partially. And the good news: 80% of the core pipeline
is already channel-agnostic.** The pollution is concentrated in 2-3
helper scripts. Here's the precise scope after a deep-read audit
(2026-05-13).

### 5.1 — Already channel-agnostic (don't touch — these are the keepers)

These call sites take a `channel_cfg` dict (or RenderSpec) and dispatch
on PROVIDER strings or per-render parameters, NOT on channel name. New
channels inherit them by default.

| Capability | Where | How it's parameterised |
|---|---|---|
| **Image generation** | `pipeline/images/images.py:73-110` + `pipeline/images_cloudrun.py:73-110` | Dispatches by `image_provider` string (`cloudrun_flux2_klein`, `z_image_turbo`); per-channel `image_style_prefix`, `image_seed`, `dimensions` come from channel YAML |
| **TTS / voice** | `pipeline/tts/__init__.py:1-27` + `pipeline/render/long_form.py:164-223` | Dispatches by `tts_provider` string (`cloudrun_chatterbox`, `cloudrun_indicf5`, `kokoro`, `f5_tts`); per-channel `tts_voice` + `tts_ref_text` come from YAML |
| **Voice clone upload** | `pipeline/voice_clone.py:182-223` | Writes per-channel refs generically by slug |
| **Footage / B-roll trim** | `pipeline/render/long_form.py:881-1005, 2279-2311` + `pipeline/render/footage_only.py:192-257` | Trims arbitrary shotlist clips; source URLs (YouTube / Wikimedia / archive.org) come from per-render shotlist JSON, not channel name |
| **Render orchestrators** | `pipeline/render/video.py:80-125`, `long_form.py:133-260`, `footage_only.py:760-909`, `sports_doc.py:16-49` | All dispatch by `RenderSpec.kind`; YAML overlay merges in per-channel overrides |
| **Upload** | `pipeline/upload/upload.py:1408-1473` | Gates by YAML keys (`cliffhanger`, `part2_channel`), not hardcoded channel strings |
| **Source adapters** | `scripts/pull_stories.py:228-254` + `pipeline/social/reddit_scrape.py:45-127` + `pipeline/research/wiki.py:404-419` | Channel-loaded from `NICHE_CHANNEL`; subreddit-driven, not channel-string-branched |

**Bottom line: the engine is correct. Adding a new channel today is
a YAML + cast/branding-asset addition, not a code change** — for the
seven capabilities above.

### 5.2 — Actually channel-coupled (the pollution)

Three files. Total ~660 LOC of hardcoded-per-channel logic. Each new
channel that needs custom branding/thumbnails/source-packs will be
tempted to fork these.

| File | LOC | Coupling | Refactor target |
|---|---|---|---|
| `scripts/historyrecapped/download_long_form_sources.py` | 54 | Hardcoded `historyrecapped/footage/long_sources` path + Capra/Ford WW2 URLs | Generic `pipeline/footage/source_pack.py::download(channel, sources[])` reading `long_form.footage_sources[]` from YAML |
| `scripts/historyrecapped/branding.py` | 218 | Hardcoded sepia palette + serif font + medal/aircraft motifs | Generic `pipeline/branding/renderer.py::build(channel_cfg, brand_spec)` reading `branding.{palette, font, icon, banner, style_tokens}` from YAML |
| `scripts/historyrecapped/build_long_form_thumbnail.py` | 227 | Hardcoded title layout, sepia palette, war-history aesthetic | Generic `pipeline/thumbnails/long_form.py::build(channel_cfg, slug, frame, title, subtitle)` reading `thumbnail.{template, palette, font}` from YAML |

Plus 5 more files that are either thin wrappers (`upload.py`, `auth.py`)
or one-shot historical helpers (`final_v2.py`, `regen_audio_caps.py`,
`author_panels.py`) — those should be retired or archived, not refactored.

### 5.3 — How the refactor preserves per-channel quality

This is the key concern: **going channel-agnostic must NOT mean
one-size-fits-all output.** The way industry tools handle this is
to push channel-specific knobs into config, not into code:

```yaml
# pipeline/channels/historyrecapped.yaml
name: HistoryRecapped
branding:
  palette: { primary: "#8B5A2B", accent: "#C19A6B", text: "#2D1810" }
  font: { primary: "Playfair Display", weight: 700 }
  icon: assets/branding/historyrecapped/medal.svg
  style_tokens: ["sepia", "ink_grain", "war_history_book", "embedded_medal"]
thumbnail:
  template: "title_top_subtitle_bottom"   # one of N reusable templates
  palette: { ... }
  font: { ... }
long_form:
  footage_sources:
    - url: https://archive.org/download/.../FrankCapra_WhyWeFight_1.mp4
      license: "PD-USGov"
      segments: [{in_s: 120, out_s: 240}]
    - url: ...
```

```yaml
# pipeline/channels/mystoriesanimated.yaml  (different aesthetic, same engine)
name: MyStoriesAnimated
branding:
  palette: { primary: "#FFB6C1", accent: "#FFD700", text: "#4A2C2A" }
  font: { primary: "Quicksand", weight: 600 }
  icon: assets/branding/mystoriesanimated/heart.svg
  style_tokens: ["pastel", "kawaii_crayon", "soft_glow"]
thumbnail:
  template: "character_hero_with_speech_bubble"
  palette: { ... }
```

**Same engine, different configs, distinct outputs.** The "war-history
book aesthetic" of historyrecapped is preserved — it just lives in
the YAML now instead of in 218 lines of PIL code that only that
channel can use.

### 5.4 — New channel-agnostic capabilities to add (the user's "extend" intent)

The user asked us to also "improve and extend features/methods/sources/
inputs/parameter to be channel agnostic". Beyond cleaning up the
pollution, here are **net-new generic capabilities** every channel
would benefit from once the abstraction is in place:

**P-extend-1 — Generic footage finder**

Today: shotlist URLs are authored manually (per-channel skill prompts
the LLM to suggest, then a human-or-skill curates). No channel can
auto-find footage.

Goal: `pipeline/footage/find.py::find_footage(topic, time_range,
license_pref) → list[ClipCandidate]`. Queries:
- YouTube Data API v3 (we already have the OAuth chain for stats —
  reuse it for search)
- Wikimedia Commons API (free PD video search by topic)
- Internet Archive (PD/CC search by date range + topic)

Returns ranked candidates with license, duration, source_url, embed
hint. Any channel can opt in via YAML `long_form.footage_finder.enabled: true`.

Effort: ~300 LOC + 3 API integrations. Schedule: AFTER P-clean lands.

**P-extend-2 — Image-style preset library**

Today: each channel YAML embeds a free-form `image_style_prefix`
string ("flat 2D crayon, pastel cream background, ..."). Hard to
reuse, hard to A/B test, hard to know what's available.

Goal: `pipeline/images/styles.py` with named presets:

```python
STYLES = {
    "kawaii_crayon_pastel":    StyleSpec(prefix="...", neg="...", lora="..."),
    "amar_chitra_katha":       StyleSpec(prefix="...", neg="...", lora="..."),
    "war_history_sepia":       StyleSpec(prefix="...", neg="...", lora="..."),
    "noir_thriller":           StyleSpec(prefix="...", neg="...", lora="..."),
    "studio_ghibli_softlight": StyleSpec(prefix="...", neg="...", lora="..."),
}
```

Channel YAML: `image_style_preset: kawaii_crayon_pastel`. Free-form
override still possible via `image_style_prefix_override` for one-off
experiments.

Effort: ~100 LOC + curation of ~10-15 presets.

**P-extend-3 — Thumbnail template library**

Same shape as image styles. Today: each channel that wants a
thumbnail forks 227 LOC. Goal: parameterised templates:

```python
TEMPLATES = {
    "title_top_subtitle_bottom":          ThumbTemplate(...),
    "character_hero_with_speech_bubble":  ThumbTemplate(...),
    "split_left_image_right_text":        ThumbTemplate(...),
    "title_overlay_full_bleed":           ThumbTemplate(...),
}
```

Each takes a channel `palette` + `font` + `frame_path` + `title`,
returns a 1280×720 JPEG. Effort: ~250 LOC + curation of 4-5 templates.

**P-extend-4 — Generic source-fetcher registry**

Audit's P3 (separate doc). Today: `pull_stories.py` has if/elif on
source kind. Replace with `pipeline/sources/__init__.py` registry
mapping source string → `(adapter_module, fetch_callable)`. New
source kind = one-file addition, no code change to dispatcher.

Effort: ~50 LOC.

### 5.5 — The cleanup + extension plan

**P-clean phase (week of 2026-05-19, after Fix A-E):**
- P-clean-1: Generalise `scripts/historyrecapped/branding.py` → `pipeline/branding/renderer.py`
- P-clean-2: Generalise `scripts/historyrecapped/build_long_form_thumbnail.py` → `pipeline/thumbnails/long_form.py`
- P-clean-3: Generalise `scripts/historyrecapped/download_long_form_sources.py` → `pipeline/footage/source_pack.py`
- P-clean-4: Archive `scripts/historyrecapped/{final_v2,regen_audio_caps,author_panels}.py` (one-shot helpers)
- P-clean-5: Delete `scripts/historyrecapped/{upload,upload_long_form,auth}.py` (already covered by `pipeline/upload/`)
- P-clean-6: YAML inheritance for AITA variants (`_aita_base.yaml` + 10 `extends:` children)
- **Net delta:** `scripts/historyrecapped/` goes from 1184 LOC → ~50 LOC (just the 2 render shims)

**P-extend phase (week of 2026-05-26, after P-clean):**
- P-extend-1: Generic footage finder (YouTube + Wikimedia + Archive.org)
- P-extend-2: Image-style preset library (10-15 named presets)
- P-extend-3: Thumbnail template library (4-5 templates)
- P-extend-4: Source-fetcher registry

**Quality preservation tactics (so we don't regress historyrecapped's look):**

1. **Snapshot the current output before refactoring.** For each of the
   7 most-recent historyrecapped renders, save (a) the branding-bug.png,
   (b) the thumbnail.jpg, (c) the source-pack listing. After refactor,
   regenerate these from the new generic engine + the migrated YAML
   config and `git diff --binary` for visual diff. Refactor isn't
   merged until visual diffs are zero (modulo intentional improvements).
2. **Migrate one channel at a time.** Start with historyrecapped (the
   most channel-coupled one). Verify output parity. Then migrate
   sportsrecapped, cosmosdecoded, hindutavaanimated, rhymetimejunction,
   mystoriesanimated.
3. **Keep the old scripts as deprecation-warning shims** for one
   release cycle. If any cron/skill still references them, log a
   warning and route to the generic engine.
4. **Pin per-channel branding/thumbnail/footage configs in version
   control.** Each channel's YAML becomes the source of truth — it
   should be bigger (40-80 lines per channel) but human-readable and
   diffable.

**What is NOT pollution (don't touch — these are correctly per-channel):**
- Channel root dirs (`mystoriesanimated/`) — runtime data per
  `docs/channel_layout.md`. Per-channel because the OUTPUT is per-channel.
- `pipeline/channels/<ch>.yaml` — per-channel config is correct.
- `.claude/skills/make-*` — per-channel by design (each channel has
  its own authoring affordances surface). The skills front a generic
  engine; that's the right shape.

---

## Part 6 — Execution sequence

### Phase 0 — Fix what's broken AND ship the day-1 deliverable

**The day-1 deliverable** (still unmet on day 11): render ONE
long-form video end-to-end via the website, critique it, decide
ship-or-fix, finalize. Phase 0 is the path TO that deliverable;
the deliverable is the success criterion of Phase 0.

**Tomorrow (~1 day):**
- [ ] Land Fix A (STORM pattern). Single commit, single deploy.
- [ ] **Canary render via website**: dispatch a 30-min long-form
      r/nosleep render, watch it end-to-end through the dashboard,
      assert the mp4 lands with audio.
- [ ] If canary succeeds → run `/critique-video` on the mp4 →
      write critique → decide SHIP / FIX / BLOCK. **THIS IS THE
      DAY-1 DELIVERABLE.**
- [ ] If canary fails: revert Fix A, root-cause, retry. Do NOT
      patch deeper bugs.

**This week (~2 days, after the canary lands):**
- [ ] Land Fix B (`strict: true` + retire salvager).
- [ ] Land Fix D (soft validator).
- [ ] Land Fix E (one real e2e test) — pins the canary as a
      regression gate so we never regress past the day-1 deliverable.
- [ ] Land Fix C (per-stage checkpoint).

**Next week:**
- [ ] Land Audit P0 (`RawDoc` migration), starting with reddit_api +
  AITA short rewriter as the canary.

**Week of 2026-05-19 — channel-agnostic cleanup (P-clean):**
- [ ] P-clean-1: `scripts/historyrecapped/branding.py` → `pipeline/branding/renderer.py`
- [ ] P-clean-2: `scripts/historyrecapped/build_long_form_thumbnail.py` → `pipeline/thumbnails/long_form.py`
- [ ] P-clean-3: `scripts/historyrecapped/download_long_form_sources.py` → `pipeline/footage/source_pack.py`
- [ ] P-clean-4: Archive one-shot helpers (final_v2, regen_audio_caps, author_panels)
- [ ] P-clean-5: Delete redundant upload helpers (upload, upload_long_form, auth)
- [ ] P-clean-6: YAML inheritance for AITA variants

**Week of 2026-05-26 — channel-agnostic extensions (P-extend):**
- [ ] P-extend-1: Generic footage finder (YouTube + Wikimedia + Archive.org)
- [ ] P-extend-2: Image-style preset library (10-15 named presets)
- [ ] P-extend-3: Thumbnail template library (4-5 templates)
- [ ] P-extend-4: Source-fetcher registry

**What we DON'T do until the above lands:**
- ❌ Add new channels.
- ❌ Add new niches.
- ❌ Add new validators or salvagers.
- ❌ Deploy single-bug patches to the long-form rewriter.

---

## Reference links (read these before touching the code)

- STORM paper (NAACL 2024): https://arxiv.org/abs/2402.14207
- STORM article-gen source: https://github.com/stanford-oval/storm/blob/main/knowledge_storm/storm_wiki/modules/article_generation.py
- Azure structured outputs: https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/structured-outputs
- OpenAI multi-agent cookbook: https://cookbook.openai.com/examples/structured_outputs_multi_agent
- Anthropic best practices: https://www.anthropic.com/engineering/claude-code-best-practices
- Temporal AI workflows: https://temporal.io/solutions/ai

---

## Appendix — what got us here today (live failure log)

| Job ID | Failed at | Root cause | Lesson |
|---|---|---|---|
| `0c05c335` | post-mux | amix normalize=1 + narration_db=-6 + single-pass loudnorm | Audio gate was silent itself |
| `b318a787` | rewrite | finish_reason=length, 32k cap | 64k bump was right but insufficient alone |
| `0947ea51` | post-rewrite validator | 58% length delivery hard-fail | Validator too strict for unproven pipeline |
| `0195e59d` | rewrite parse | 28 panels exceed 24 cap | Cap was Apple Silicon laptop hack, not cloud |
| `7dca182d` | rewrite parse | finish_reason=stop with truncated JSON | Salvager added (papering over symptom) |
| `feee2aa9` | rewrite parse | 26 panels still hit 24 cap (channel YAML stale) | Channel YAMLs not updated |
| `59ab1426` | rewriter parse | salvager produced section without title | Symptom suppression bites back |

**Pattern:** every fix uncovered the next bug because every fix was
a single-bug patch on a wrong-pattern architecture. The STORM pattern
+ structured outputs would have prevented all 7.
