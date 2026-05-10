---
name: make-cosmos-short
description: Author a 50-60s "How We Knew" physics+space Short for Cosmos Decoded — isolates the single moment of measurement (the strain plot lighting up, the photographic plate developing, the four blind imaging teams revealing the same ring) into a 9:16 vertical Short. Footage-only (NASA / NTRS / ESA / CERN / LIGO / Wikimedia / arXiv / LoC / Smithsonian / stock — NO AI image gen, NEVER YouTube re-uploads). Produces cosmosdecoded/raw/<slug>-short.json (slim dossier) + cosmosdecoded/narrations/<slug>-short.json + cosmosdecoded/shotlist/<slug>-short.json + cosmosdecoded/narrations/<slug>-short.pronounce.json, then hands off to historyrecapped/scripts/render_footage_only.py --channel cosmosdecoded. Use when the user says "make me a cosmos short", "physics short on <X>", "50-second decoder on <Y>", "short version of <slug>", "Short of the EHT measurement", "GW150914 Short". For the 25-40 min long-form deep-dive use /make-cosmos-long. For Top-10 science countdowns use /make-top10. For sports head-to-head Shorts use /make-last5. For MyStoriesAnimated single-story Shorts use /make-mystories-short.
learnings_consulted:
  - cosmosdecoded/learnings/channel.md
  - cosmosdecoded/learnings/auto_source_prep.md
  - cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md
  - cosmosdecoded/config.yaml
  - .claude/skills/make-skill/learnings/heuristics.md
  - feedback_pronunciation_pretts
  - feedback_critique_audio_before_image_gen
---

# /make-cosmos-short — 50-60s "How We Knew" physics+space Short skill

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

This skill authors a **50-60 second Short** (9:16) for the Cosmos
Decoded channel. The wedge is the **single moment of measurement** —
the strain plot lighting up, the photographic plate developing, the
four blind imaging teams revealing the same ring, the gamma-ray
detector clicking, the telegram going out. **Footage-only** — real
photographs of real experiments, NASA / ESA / CERN / LIGO direct
releases, paper page screenshots, lab notebook scans, telescope plates,
mission control footage. NO AI image gen. NO diffusion queue.

Unlike /make-cosmos-long (which spends 25-40 minutes on prediction →
experiment → consequence), this skill **collapses the three-act
structure into 50-60 seconds**: ~10s setup of the prediction, ~30s on
the moment of measurement, ~10s on the consequence, ~5s closer. The
moment of measurement IS the hook.

You wear three hats: **Document Researcher → Footage Curator → Short-
form Writer**.

The output is **four** JSON files — `raw/<slug>-short.json` (slim
dossier, just enough to ground every claim), `narrations/<slug>-short.json`
(7-beat narration), `shotlist/<slug>-short.json` (5-7 windows in 9:16),
and `narrations/<slug>-short.pronounce.json` (TTS sidecar). Stages
4-7 (TTS, ffmpeg, mux, upload) live in the renderer.

**Pairing note (decoupled from /make-cosmos-long):** the user has
chosen that each skill writes its own dossier independently. This
skill does NOT require `cosmosdecoded/raw/<slug>.json` to exist before
running. If the user wants consistency between long-form and Short,
they should run /make-cosmos-long first and explicitly instruct
/make-cosmos-short to "reuse the dossier" — there is no automatic
linkage at the skill level beyond filename convention.

## How to run it

> **CLOUD-TTS RULE (2026-05-07):** This skill's render path reads
> `tts_provider:` from the channel YAML. That field MUST be a
> `cloudrun_*` engine (chatterbox / f5 / higgs / cosyvoice /
> indicparler / indicf5). Local providers (`kokoro` / `f5_tts` /
> local `chatterbox`) are automatic-fallback paths only, NEVER the
> default. The renderer hard-fails at preflight on any local
> provider unless `LOCAL_TTS_OVERRIDE=1`. If a render comes back
> with audibly degraded voice, FIRST check `<channel>/config.yaml`
> for a misconfigured `tts_provider:` block (Shorts top-level vs
> long-form sub-block) and fix it before re-rendering. Source of
> truth: [`docs/cloudrun_tts.md`](/Users/rohit/ytFactory/docs/cloudrun_tts.md)
> + [`docs/tts_stack.md`](/Users/rohit/ytFactory/docs/tts_stack.md).
> Voice refs deployed at `pipeline/voice_refs/`: `sarah.wav` (female,
> default English), `michael.wav` (male, sports-doc default).

> **SCOPING RULE (2026-05-07):** Stage-1 / scope-confirmation MUST use
> `AskUserQuestion` with **2-4 prefilled options** grounded in
> channel exemplars (`cosmosdecoded/raw/`, `cosmosdecoded/narrations/`)
> + canonical not-yet-covered measurement moments (LIGO strain plot,
> EHT 4-team reveal, Eddington plate, COBE blackbody, Higgs di-photon
> bump, JWST first deep field, etc.). Auto-detect long-form dossier
> at `cosmosdecoded/raw/<slug>.json` and offer "reuse this dossier"
> as the first option when present. Never ask "type your subject" —
> propose options. Source of truth:
> [`docs/skill_prefilled_options.md`](/Users/rohit/ytFactory/docs/skill_prefilled_options.md).

### 1. Confirm subject + slug + the measurement-moment anchor

If the user's prompt is partial ("make me a Short on EHT"), state your
reading back in one sentence and let them redirect. Lock the **4-line
spec** before writing anything:

- **Subject** — the physics prediction + its proving event. e.g.
  `eddington-1919-eclipse`, `pound-rebka-1959`, `ligo-2015-gw150914`,
  `eht-2019-m87`, `super-kamiokande-1998`.
- **Slug** — kebab-case + `-short` suffix. The output filename
  contract is `<slug>-short.json` for narration/shotlist/dossier/
  pronounce.json. Long-form parent slug (if known) is recorded inside
  the narration as `long_form_parent`.
- **Channel** — `cosmosdecoded` (this skill is single-channel).
  Refuse with a `/create-youtube-channel cosmosdecoded` suggestion if
  `cosmosdecoded/config.yaml` is missing.
- **Measurement-moment anchor** — which exact beat from the
  experiment becomes the Short's centerpiece. Default = "the single
  moment of measurement" (the glance through the telescope, the
  strain plot lighting up, the four imaging teams revealing the same
  image, the telegram going out). Override only if the user names a
  different beat.

### 2. Load channel context (heuristics #8 + #46)

Before any drafting, run these reads. Bake the literals into the
narration file; never paraphrase:

```bash
cat cosmosdecoded/learnings/channel.md
cat cosmosdecoded/config.yaml
ls cosmosdecoded/learnings/
ls cosmosdecoded/narrations/      # check for slug collision; check if <slug>.json exists (long-form parent)
```

If `cosmosdecoded/raw/<slug>.json` already exists from a previous
/make-cosmos-long run, **read it** and pull the act-2 measurement
moment + key physicist names + result value directly from there.
Do NOT re-research; the long-form dossier is more thorough than what
a Short would mint cold. If no long-form dossier exists, build a slim
one (Section 3).

Inheritance contract (literal — do not paraphrase):

| Field                  | Short                                              |
|------------------------|----------------------------------------------------|
| TTS provider           | `cloudrun_chatterbox` + `pipeline/voice_refs/sarah.wav` at speed 1.0 |
| Aspect                 | 9:16                                                |
| Duration band          | 50-60s                                              |
| Captions               | word-by-word PNGs; **numerical form for dates / years / numbers** (narration text stays word-spelled for TTS, but captions auto-convert "two thousand and fifteen" → "2015", "September fourteenth" → "Sept 14", "thirty-six" → "36") via `pipeline.captions.numericalize_caption` |
| Closer                 | `SUBSCRIBE for more decoders\nLIKE if this changed how you see physics` (audio); **end-screen LIKE button at top + SUBSCRIBE button at bottom** during the closer beat — overlaid onto the BBH closer footage; word captions suppressed in the closer window per the rivalry-recap end-card pattern |
| Category               | 28 (Sci/Tech)                                       |
| Footage policy         | PD or CC only; NEVER YouTube news re-uploads        |
| Visual signature       | cool dawn + observatory blue grade                  |

**Voice cadence rule (corrected 2026-05-08 v2 after the v1 critique):**
The channel-configured TTS for Cosmos Decoded Shorts is
`cloudrun_chatterbox` + `sarah.wav` at speed 1.0. Empirical cadence
on `ligo-2015-gw150914-short v2`: **186 words → 75.5s wav = 148 wpm**
(re-measurement after v1's 231 wpm reading turned out to be derived
from a TRUNCATED audio — the v1 wav contained only 97/154 words,
and 97/40s = 145 wpm matches v2 within 2%). **Word budget for 50-60s
narration is 125-150 words.** ~135 words is the safe middle of the
band including ~0.5s pauses at each `\n\n` paragraph break. Going
under leaves trailing silent video; going over makes a 60+ second
"Short". Always re-confirm against the live `cosmosdecoded/config.yaml
shorts:` block.

**Mandatory: split narration into 4-6 paragraphs separated by `\n\n`.**
Without paragraph breaks, `pipeline/tts/cloudrun.py::_synth_cloudrun_chatterbox`
falls back to single-shot synthesis which silently truncates at ~40s
of audio (~95-100 words at this cadence). The chunking helper
`_synth_cloudrun_chunked` requires `\n\n` paragraph breaks (or, post-
2026-05-08 patch, single paragraphs >350 chars trigger a sentence-
group fallback). Belt-and-suspenders: ALWAYS author with explicit
paragraph breaks. See `cosmosdecoded/learnings/chatterbox_silent_truncation.md`.

### 3. Hat 1 — Document Researcher (slim version)

If the long-form dossier already exists at `cosmosdecoded/raw/<slug>.json`,
SKIP this step — read the existing dossier and proceed to Section 4.

If not, build a **slim** dossier sufficient to ground every claim in
the 145-165 word narration. Save as `cosmosdecoded/raw/<slug>-short.json`
with a reduced schema:

```json
{
  "slug": "<slug>-short",
  "subject_summary": "<one sentence: prediction + proving event>",
  "prediction": {
    "claim_one_line": "<the single number / law the test was checking>",
    "predicting_paper": {"author": "...", "title": "...", "year": ...}
  },
  "experiment": {
    "date": "...",
    "location": "...",
    "instrument": "...",
    "moment_of_measurement": "<the single beat that becomes the Short's centerpiece — be specific>",
    "result_value": "<the number, with uncertainty if available>"
  },
  "consequence": {
    "immediate": "<one-sentence consequence — Nobel / mission followup / technology enabled>"
  },
  "sources": ["...", "..."]
}
```

Reference exemplars: `cosmosdecoded/raw/eddington-1919-eclipse.json`
and `cosmosdecoded/raw/eht-2019-m87.json` (full long-form dossiers —
read for the level of detail; the Short's slim dossier is a
3-paragraph subset).

The dossier is the SOURCE OF TRUTH. Every claim in the narration MUST
trace to a field here.

### 4. Hat 2 — Footage Curator

Cosmos Decoded is footage-only. NO AI image gen. NO diffusion. Every
visual must trace to one of the source classes below. Cite per beat
in the shotlist's `attribution` field.

**Source classes (ranked, default to top of list):**

1. **NASA Image and Video Library** (`images.nasa.gov`) — PD, fully open.
2. **NASA Technical Reports Server (NTRS)** — PD mission reports + memos.
3. **CERN open archive** — CC-BY accelerator + detector.
4. **LIGO Open Science Center** — strain plots, site photos.
5. **ESA / Hubble / JWST releases** — CC-BY-SA-IGO.
6. **Wikimedia Commons** — physicist portraits, observatory exteriors,
   period photography. CC-BY-SA. Attribution required.
7. **arXiv / Royal Society / Phys Rev Letters paper PDFs** — page
   screenshots as fair-use editorial commentary. **Cap on-screen
   time at 8s per beat.**
8. **Library of Congress** — historical scientific photography.
9. **Smithsonian National Air & Space photos**.
10. **Stock b-roll** — Pexels / Pixabay / Storyblocks.

**FORBIDDEN sources:** YouTube re-uploads of news clips, animated-
physics channels, AI-generated stills, footage from competitor decoder
channels (Veritasium, PBS Space Time, Cool Worlds).

**Wikimedia URL guess miss-rate (class-of-bug, 2026-05-07):** see
`cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md`. Pattern-
guessed File: URLs miss ~60% of the time on Commons. Either verify
each File: page via the Wikimedia API before emit OR carry a
`_wikimedia_search_query` sibling string the prep tool can `srsearch`
on MANUAL fallback.

Build the **shotlist** as `cosmosdecoded/shotlist/<slug>-short.json`
with 5-7 beat-aligned windows in 9:16 aspect. Schema documented in the
exemplar `cosmosdecoded/shotlist/eht-2019-m87-short.json`. The
renderer's letterbox path handles 16:9 source → 9:16 fitting.

### 5. Hat 3 — Short-form Writer

Draft the 50-60 second narration. The act-2 measurement moment IS the
hook.

#### 5a. Beat structure (mandatory)

Every Cosmos Decoded Short follows this 5-beat structure (with two
internal sub-beats), totaling 7 narration beats matching the renderer's
caption schedule:

- **Beat 0 — Hook (3-5s):** the moment of measurement, named directly.
  *"This is the photograph that made Einstein famous."* / *"This is
  the strain plot LIGO printed at 9:50 UTC, September 14, 2015."* /
  *"On July 24, 2018, four imaging teams in Cambridge opened their
  laptops at the same moment."*
- **Beat 1 — Setup expansion (8-12s):** the proper-name / date / place
  context. *"May 29, 1919. A British astronomer named Arthur Eddington
  pointed a telescope at a total solar eclipse off the coast of West
  Africa."*
- **Beat 2 — Stake / prediction (10-15s):** what was being tested and
  what was at stake. *"Einstein had said mass curves spacetime, and
  starlight grazing the Sun would bend by 1.75 arcseconds. Newton said
  0.87. A factor of two."*
- **Beat 3 — The reveal (12-18s):** the actual measurement, on screen.
  This is where you spend the most time. *"They counted down. They
  unveiled the screens at the same moment. Every screen showed the
  same thing. A bright orange ring."*
- **Beat 4 — The number (5-8s):** the single number that landed.
  *"42 microarcseconds of ring."* / *"1.98 arcseconds, plus or minus
  0.12."*
- **Beat 5 — Consequence (5-8s):** one Nobel, one mission, one
  technology, one piece of common-sense knowledge. *"That number is
  why your phone navigates."* / *"Within a week, the New York Times
  put the words 'Lights All Askew in the Heavens' on its front page."*
- **Beat 6 — Closer (3-5s):** literal `cosmosdecoded` closer:
  `SUBSCRIBE for more decoders` + `LIKE if this changed how you see physics`.
  Never paraphrase.

Word budget at cloudrun_chatterbox + sarah.wav 1.0 (148 wpm including
inter-paragraph pauses, re-measured 2026-05-08 v2 after the v1 wpm
reading turned out to be on a TRUNCATED wav): **125-150 words** total
for 50-60s. ~135 words = safe middle of band. The earlier 190-230
number was wrong — the v1 wpm calc divided 154 script-words by a
40s wav that only contained 97 of those words. Real cadence is
~148 wpm with `\n\n` paragraph pauses included.

Save as `cosmosdecoded/narrations/<slug>-short.json`:

```json
{
  "slug": "<slug>-short",
  "long_form_parent": "<slug>",          // omit if standalone Short with no long-form
  "format": "shorts",
  "channel": "cosmosdecoded",
  "duration_target_s": [50, 60],
  "wpm_target": 165,
  "hook": "<the act-2 measurement moment, named directly>",
  "narration": "<full 145-165 word narration body, all numbers + dates word-spelled>",
  "beats": [
    {"index": 0, "text": "...", "match_text": "<key phrase for caption-to-clip alignment>"},
    {"index": 1, "text": "...", "match_text": "..."},
    {"index": 2, "text": "...", "match_text": "..."},
    {"index": 3, "text": "...", "match_text": "..."},
    {"index": 4, "text": "...", "match_text": "..."},
    {"index": 5, "text": "...", "match_text": "..."},
    {"index": 6, "text": "SUBSCRIBE for more decoders. LIKE if this changed how you see physics.", "match_text": "SUBSCRIBE for more decoders"}
  ],
  "closer": "SUBSCRIBE for more decoders\nLIKE if this changed how you see physics",
  "title_options": ["...", "..."],
  "thumbnail_brief": "...",
  "source_url": "<primary source-of-truth URL for description>",
  "source": "<full source citation>",
  "_pronunciation_notes": "see <slug>-short.pronounce.json sidecar"
}
```

#### 5b. Prose discipline

- **Voice register:** declarative, third-person, active. No second-
  person ("you"). No softening modifiers. Numbers and proper nouns
  carry the gravity.
- **Document anchor — once:** at least once in the 60s, name a
  specific document or instrument on screen. *"the Sobral Cortie
  lens"*, *"the strain plot at 9:50 UTC"*, *"the four teams at the
  Black Hole Initiative"*. This is the channel's wedge in miniature.
- **No clickbait:** never "they didn't want you to know", never
  "scientists were SHOCKED", never verdict acronyms. Cosmos Decoded's
  brand on Shorts is the same as on long-form: respectful, precise,
  earned.
- **No closer-anti-patterns:** never "smash that subscribe button"
  (memory: spoken_subscribe_ask). Never "comment which experiment
  next" (carryover from HistoryRecapped closer rule). Never "Einstein
  was wrong".

### 6. Quality gates (heuristics #31-#38)

Run these IN ORDER. Block emit on any failure.

1. **Banned-phrase scan** — same list as /make-cosmos-long Section 6 +
   sleep-mode prose ("close your eyes", "drift off") which would be
   absurd in a 60s decoder Short but the gate runs anyway as
   regression-defence.
2. **Pronunciation pre-pass** — phonetic respelling for foreign /
   proper nouns BEFORE TTS. Save next to narration as
   `cosmosdecoded/narrations/<slug>-short.pronounce.json`. Reference:
   `cosmosdecoded/narrations/eht-2019-m87.pronounce.json` covers the
   entire EHT vocabulary.
3. **Length budget** — narration MUST land in **125-150 words**
   (50-60s at cloudrun_chatterbox+sarah.wav ~148 wpm including
   `\n\n` pauses, re-measured 2026-05-08 v2 on ligo-2015-gw150914-short
   v2 after v1's reading turned out to be on a truncated wav).
   Reject and rewrite outside. Always re-confirm by reading the live
   `cosmosdecoded/config.yaml shorts.tts_provider` field.

4. **Paragraph-break gate** — narration text MUST contain at least
   2 `\n\n` paragraph breaks (3+ paragraphs total). Single-paragraph
   narrations >350 chars now trigger sentence-group chunking via
   `_synth_cloudrun_chunked`'s 2026-05-08 fallback, but defense-in-
   depth: always author with explicit breaks. Block emit if narration
   has no `\n\n` AND length > 350 chars.
4. **Source-fidelity check** — every named claim (date, name, value,
   institution, paper title) must trace to a `raw/<slug>-short.json`
   dossier field OR (if reusing) the long-form `raw/<slug>.json`
   dossier. Reject ungrounded claims.
5. **/critique-audio gate** — REQUIRED before publishing.
   After narration TTS renders, invoke /critique-audio on the wav.
   60-second narrations are unforgiving — one bad pronunciation is
   1.5% of the total runtime.
6. **ContentID scan** — every `source_url` in the shotlist must be
   in the allowed-sources list (Section 4). Block any URL pointing
   at a YouTube reupload, news org, or competitor channel.
7. **Aspect-ratio + voice-ID match** — shotlist `aspect: "9:16"`. TTS
   provider matches `cosmosdecoded/config.yaml` Shorts block. Refuse
   drift.
8. **Closer literal-match** — `closer` field MUST be exactly
   `"SUBSCRIBE for more decoders\nLIKE if this changed how you see physics"`.
   Never paraphrase. Block emit on drift (channel rule).

### 7. Auto source-prep (MANDATORY, runs by default)

The renderer needs local mp4 files in
`cosmosdecoded/footage/sources/`. Authoring shotlists only produces
URLs + filenames. Run `pipeline/cosmos_footage_prep.py` after the four
JSONs land on disk:

```bash
.venv/bin/python -u -m pipeline.cosmos_footage_prep \
    --channel cosmosdecoded --slug <slug>-short
```

What it does (same as /make-cosmos-long Section 7) — resolves Wikimedia
File: pages, NASA images.nasa.gov assets, archive.org metadata,
direct .mp4/.jpg/.png/.webp URLs; runs ffmpeg zoompan for `still_ken_burns`
entries to produce 1080x1920 9:16 mp4s. Returns structured report:
`✓ fetched`, `✓ skipped`, `⚠ manual fallback`, `✗ errors`. Manual
fallback is expected for paywalled hosts; surface the list to the
user and walk them through saving each asset.

After prep, run `ls cosmosdecoded/footage/sources/` and confirm every
window's `source` field has a corresponding file. **Any missing file
blocks the renderer.** Clear-state: zero manual entries before
invoking the renderer.

### 8. Renderer handoff

The Shorts renderer exists and is channel-parametric. NO new code.

```bash
.venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_footage_only.py -- \
    --channel cosmosdecoded --slug <slug>-short
```

The renderer's letterbox path handles 16:9 source → 9:16 fitting; the
cool-dawn observatory grade applies at compose stage from
`cosmosdecoded/config.yaml`.

### 9. Report back

After the four files land on disk, report:

```
✓ dossier:             cosmosdecoded/raw/<slug>-short.json    (or "reused: cosmosdecoded/raw/<slug>.json")
✓ short narration:     cosmosdecoded/narrations/<slug>-short.json
✓ short shotlist:      cosmosdecoded/shotlist/<slug>-short.json
✓ pronunciations:      cosmosdecoded/narrations/<slug>-short.pronounce.json

quality gates:
  ✓ banned-phrase scan
  ✓ pronunciation pre-pass
  ✓ length budget (short: 56s)
  ✓ source-fidelity (every claim traces to dossier)
  ✓ aspect/voice match (9:16 / Kokoro am_michael)
  ✓ ContentID scan (no YT reuploads)
  ✓ closer literal match
  ⏳ /critique-audio gate — run after first TTS chunk

source prep: <X fetched / Y skipped / Z manual>

next:
  1. .venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_footage_only.py -- --channel cosmosdecoded --slug <slug>-short
  2. /critique-audio cosmosdecoded/cache/<slug>-short/narration.wav
  3. (optional) /make-cosmos-long <slug>   ← if no 25-40 min deep-dive exists yet
```

### 10. Self-learning hook (heuristics #39-#44)

After the user runs `/critique-audio` or `/critique-video` on the
output:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo / one bad pronunciation / one wrong date) →
     fix in the JSON output, append a 1-line note to
     `.claude/skills/make-cosmos-short/learnings/_index.md`.
   - **CLASS-OF-BUG** → fix in `pipeline/<module>.py` OR in this
     SKILL.md prompt, then append a regression note to
     `.claude/skills/make-cosmos-short/learnings/<topic>.md` AND
     mirror to `cosmosdecoded/learnings/<topic>.md` per CLAUDE.md
     dual-save rule.
2. Update MEMORY.md index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in Section 6 that blocks emit on detection.
4. Carry `learnings_consulted:` in this file's frontmatter (already
   present); update when a new learning lands.

## Important rules

- **Closer literal:** `SUBSCRIBE for more decoders\nLIKE if this changed how you see physics`. Never paraphrase. Channel rule.
- **Footage policy:** PD or CC only. NEVER YouTube news re-uploads. NEVER AI-generated stills. NEVER competitor-channel rips.
- **Voice cadence:** Kokoro `am_michael` at 1.0 = ~165 wpm. Word budget 145-165 words for 50-60s narration. Out-of-band = block emit.
- **Document-led signature:** at least one specific paper / instrument / location named in the 60s. This is the channel's wedge in miniature.
- **Source-fidelity (heuristic #35):** every named claim traces to a dossier field. No ungrounded speculation.
- **Measurement-moment hook:** beat 0 names the moment directly. The hook is NOT "did you know" or "the universe is wild" — it is the specific second of the experiment.
- **Banned phrases:** "Einstein was wrong", "NASA covered up", "the truth they don't want you to know", "smash that subscribe button", "vote in comments", verdict acronyms.
- **No image gen:** NEVER flip `render_style` to `image_gen` on this channel. Document-led signature dilutes immediately.
- **Stages 4-7 belong to the renderer.** This skill produces JSON; render_footage_only.py does TTS, ffmpeg, mux, captions.
- **Always use `.venv/bin/python`** for any helper commands.
- **Dual-save** every learning per CLAUDE.md (memory + project doc).

## Learnings from prior runs

- **2026-05-07** — `eht-2019-m87-short` (run as paired output via the
  retired /make-cosmos-decoder) — surfaced the shotlist Wikimedia URL
  guess miss-rate class-of-bug. Short shotlist had only 7 windows so
  the impact was small (2/7 MANUAL after URL patches), but the rule
  applies equally:
  `cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md`.

## Why this skill is separate from /make-cosmos-long

User decision 2026-05-08: each skill writes its own dossier
independently. Reason: simpler control flow + clearer single-purpose
invocations (`/make-cosmos-short ligo-2015-gw150914` produces a Short
without first authoring 4000 words of long-form). Trade-off: the
original "act-2-becomes-Short" extraction discipline of
/make-cosmos-decoder is not enforced — if you want consistency, run
/make-cosmos-long first. This skill auto-detects the long-form dossier
at `cosmosdecoded/raw/<slug>.json` and prefers it when present, but
will mint its own slim dossier when not.

`/make-mystories-short` is **single-story Reddit Shorts** — incompatible
prose register (animated cartoon vs. document-led photography).

`/make-last5` is **last-N head-to-head footage compilation** —
no narration, broadcast commentary IS the audio. Cosmos Decoded Shorts
have authored narration as the spine.

`/make-script` (the older multi-niche Shorts skill) covered AITA / Wiki
Oddities / TIH / YouTube-source-mining; physics-decoder Shorts have a
distinct beat structure (the measurement moment IS the hook) and a
distinct closer, which is why they live in their own skill rather than
as a /make-script niche.

## Section 11 — Post-upload analysis (cross-channel rule, 2026-05-08)

After this skill's render uploads successfully (or `/critique-video`
surfaces new issues post-upload), run the conversational debrief
defined in [`docs/post_upload_analysis.md`](/Users/rohit/ytFactory/docs/post_upload_analysis.md).
Walk back through the iterations that produced this video and
classify each surprise / fix / pivot as ONE-OFF, CLASS-OF-BUG,
PIPELINE-BUG, WORKFLOW-IMPROVEMENT, or PRONUNCIATION. Save learnings
per CLAUDE.md dual-save rule (channel project doc + skill-side mirror
+ memory entry).

**Most likely topics to surface for this skill** (extend as new
learnings land):
- TTS pronunciation gaps (extend `pipeline/tts/text_normalize.py::_ACRONYM_PHRASES`)
- Photo / footage aspect inconsistency
- Image-narration sync drift
- Cache-invalidation gaps between iterations
- Closer beat / button overlay alignment
- Numerical caption transform misses
- Wikimedia File: URL miss-rate

Default to write, not skip. Small learnings compound; missing them
causes the next render to re-discover the same bug.

---

## Cloud pre-render hook (mandatory)

Before handing off to `pipeline/render/<entrypoint>.py`, do the
**routing assertion** documented in
[`docs/cloud_prerender_hook.md`](/Users/rohit/ytFactory/docs/cloud_prerender_hook.md):
read the channel `config.yaml` (and variant YAML if applicable) and
assert `tts_provider` + `image_provider` start with `cloudrun_`
(except for documented local-only paths like
`mystoriesanimated/variants/tifu.yaml` and the Hindi `kokoro hf_alpha`
fallback).

**Pre-warm is now automatic** — the renderer entrypoints call
`pipeline.cloud.warm.warm_async(channel)` immediately after argparse,
so the 5-7 min cold-load happens in parallel with the renderer boot.
**Health is now in the admin tab** — `/app/cloud` (sidebar → Cloud)
shows green/yellow/red live; for CI use `/api/cloud/health`. The
`warm-cloud`, `cloud-health`, `cloud-cost`, and
`deploy-cloud-service` skills were retired on 2026-05-10; same code
lives in `pipeline/cloud/` + the admin tab.
