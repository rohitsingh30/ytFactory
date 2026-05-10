---
name: make-cosmos-long
description: Author a 25-40 minute long-form "How We Knew" physics+space deep-dive for Cosmos Decoded — three-act (prediction → experiment → consequence), 16:9, document-led, footage-only (NASA / NTRS / ESA / CERN / LIGO / Wikimedia / arXiv / LoC / Smithsonian / stock — NO AI image gen, NEVER YouTube re-uploads). Produces cosmosdecoded/raw/<slug>.json (dossier) + cosmosdecoded/narrations/<slug>.json + cosmosdecoded/shotlist/<slug>.json + cosmosdecoded/narrations/<slug>.pronounce.json, then hands off to historyrecapped/scripts/render_long_form.py --channel cosmosdecoded. Use when the user says "make me a long-form physics video", "long-form cosmos decoder", "how we knew <X>", "physics decoder on <Y>", "Eddington 1919 video", "LIGO video", "JWST decoder", "GW150914 long-form". For a paired 50-60s Short cut from the same act-2 measurement, also run /make-cosmos-short with the same slug after this completes — it will read this skill's dossier. For Top-10 science countdowns use /make-top10. For sports docs use /make-sports-doc. For MyStoriesAnimated single-story Shorts use /make-mystories-short.
learnings_consulted:
  - cosmosdecoded/learnings/channel.md
  - cosmosdecoded/learnings/auto_source_prep.md
  - cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md
  - cosmosdecoded/config.yaml
  - .claude/skills/make-skill/learnings/heuristics.md
  - historyrecapped/learnings/long_form_hook_template.md
  - historyrecapped/learnings/long_form_support_asks.md
  - historyrecapped/learnings/long_form_captions.md
  - historyrecapped/learnings/long_form_sources.md
  - feedback_pronunciation_pretts
  - feedback_long_form_render_caffeinate
  - feedback_long_form_power_preflight
---

# /make-cosmos-long — long-form "How We Knew" physics+space skill

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

This skill authors a **25-40 minute long-form deep-dive** (16:9) for the
Cosmos Decoded channel. **Footage-only** — real photographs of real
experiments, NASA / ESA / CERN / LIGO direct releases, paper page
screenshots, lab notebook scans, telescope plates, mission control
footage. NO AI image gen. NO diffusion queue. No GPU imagegen burn.

The wedge is **document-led storytelling**: every claim cites the
specific paper, mission report, or press release on screen. The
three-act structure is non-negotiable:

- **Act 1 — Prediction** (3-5 min): what the theory said before the
  test. Show the paper page or chalkboard equation. Name the predicting
  physicist.
- **Act 2 — Experiment** (15-20 min): the actual test. Photographs of
  apparatus + team + location. Telegram exchanges or lab notebooks if
  surviving. **The single moment of measurement** is this skill's
  payoff — and (when paired with /make-cosmos-short) the moment that
  becomes the 50-60s Short.
- **Act 3 — Consequence** (5-10 min): what changed in physics or
  engineering after. The follow-up paper or mission. The Nobel (if
  any). The technology it enabled.

You wear three hats: **Document Researcher → Footage Curator →
"How We Knew" Writer**.

The output is **four** JSON files — `raw/<slug>.json` (the dossier,
source-of-truth), `narrations/<slug>.json` (the long-form narration +
chapters), `shotlist/<slug>.json` (the 16:9 beat-aligned shotlist), and
`narrations/<slug>.pronounce.json` (TTS pronunciation sidecar). Stages
4-7 (TTS, ffmpeg, mux, upload) live in the renderer, not in this skill.

**Pairing note (decoupled from /make-cosmos-short):** the user has
chosen that each skill writes its own dossier independently. /make-cosmos-short
will write a slim Short-only dossier even when this skill's full dossier
already exists on disk. If you want consistency between long-form and
Short, run /make-cosmos-long FIRST and then ask /make-cosmos-short to
"reuse the dossier" explicitly — there is no automatic linkage at the
skill level beyond filename convention.

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
> and canonical not-yet-covered "how we knew" anchors (Eddington 1919,
> LIGO GW150914, EHT 2019 M87, JWST early-universe, Cassini Saturn,
> COBE/WMAP/Planck, Kepler exoplanets, Hubble deep field, etc.).
> Never ask "type your subject" / "type the era" — propose options.
> Source of truth: [`docs/skill_prefilled_options.md`](/Users/rohit/ytFactory/docs/skill_prefilled_options.md).

### 1. Confirm subject + slug + duration target

If the user's prompt is partial ("make me a long-form on Eddington"),
state your reading back in one sentence and let them redirect. Lock the
**4-line spec** before writing anything:

- **Subject** — the physics prediction + its proving event. e.g.
  `eddington-1919-eclipse`, `pound-rebka-1959`, `ligo-2015-gw150914`,
  `eht-2019-m87`, `super-kamiokande-1998`, `mars-polar-lander-1999`,
  `cassini-grand-finale-2017`.
- **Slug** — kebab-case, the contract for every downstream artifact.
  `<scientist-or-mission>-<year>-<event>` is the canonical pattern.
- **Channel** — `cosmosdecoded` (this skill is single-channel).
  Refuse with a `/create-youtube-channel cosmosdecoded` suggestion if
  `cosmosdecoded/config.yaml` is missing.
- **Duration target** — `short-doc` (25-30 min, default for
  single-experiment subjects) | `extended-doc` (32-40 min, for
  multi-paper / multi-team subjects like LIGO).

### 2. Load channel context (heuristics #8 + #46)

Before any drafting, run these reads. Bake the literals into the
narration file; never paraphrase:

```bash
cat cosmosdecoded/learnings/channel.md
cat cosmosdecoded/config.yaml
ls cosmosdecoded/learnings/
ls cosmosdecoded/narrations/      # check for slug collision
```

Inheritance contract (literal — do not paraphrase):

| Field                  | Long-form                                                   |
|------------------------|-------------------------------------------------------------|
| TTS provider           | F5-TTS-MLX `pipeline/voice_refs/sarah.wav` 1.0 + atempo 1.0 |
| Aspect                 | 16:9                                                        |
| Duration band          | 1500-2400s (25-40 min)                                      |
| Captions               | sentence-level white sans-serif (NOT yellow italic sleep). **Numerical form for dates / years / numbers** in the rendered captions even though narration text stays word-spelled for TTS — same `pipeline.captions.numericalize_caption` transform as Cosmos Decoded Shorts. |
| Closer                 | embedded as inline ask in last 90s of narration            |
| Category               | 28 (Sci/Tech)                                               |
| Footage policy         | PD or CC only; NEVER YouTube news re-uploads                |
| Visual signature       | cool dawn + observatory blue grade (config.yaml `long_form.visual_grade.filter`) |

**Voice cadence rule (class-of-bug):** `tts_speed: 1.0` + `atempo: 1.0`
is **calm-explainer**, not sleep. NEVER drift the prose into bedtime
cadence ("close your eyes... drift into the night of 1919..."). Cosmos
Decoded long-form is "Veritasium without the host" — engaged, second-
person rare, declarative, technical without being lecturing. If the
prose starts feeling sleepy, you have drifted out of register; rewrite.

### 3. Hat 1 — Document Researcher

The cosmos audience checks claims against papers. Source-fidelity
(heuristic #35) is non-negotiable. Build a **dossier** before drafting:

- **The originating paper(s)** — link the arXiv ID or DOI; pull the
  abstract and figure 1 caption verbatim. Page-screenshot candidates.
- **The proving paper / mission report** — same. Note the date of
  measurement, the team, and the place.
- **The instrument** — name + photo source (Wikimedia / institution
  press release / NASA archive). Specs (telescope aperture, detector
  mass, accelerator energy) get a dedicated line in the dossier.
- **The named scientists** — first + last name + role + affiliation
  at time of experiment. Wikimedia portrait URL per name.
- **The followup** — what paper / mission / Nobel cited the result.
  Link.
- **A counterfactual** — what would the result have looked like if
  the prediction had failed? This becomes the act-3 hook ("if light
  hadn't bent, you'd see the star at exactly this position…").

Save the dossier as `cosmosdecoded/raw/<slug>.json` with the schema
documented in the channel learning + reference exemplars at
`cosmosdecoded/raw/eddington-1919-eclipse.json` and
`cosmosdecoded/raw/eht-2019-m87.json`. The dossier is the SOURCE OF
TRUTH for the long-form narration. Every claim in the narration MUST
trace to a field here.

### 4. Hat 2 — Footage Curator

Cosmos Decoded is footage-only. NO AI image gen. NO diffusion. Every
visual must trace to one of the source classes below. Cite per beat in
the shotlist's `attribution` field; the renderer doesn't enforce
attribution but the description_template surfaces it.

**Source classes (ranked, default to top of list):**

1. **NASA Image and Video Library** (`images.nasa.gov`) — PD, fully
   open. Mission photos, mission control, telemetry plots, animation
   stills. **First-look for any space-mission subject.**
2. **NASA Technical Reports Server (NTRS)** — PD mission reports +
   engineering memos. Page screenshots are gold for failure-decoder
   videos (Mars Polar Lander, Challenger O-ring, Hubble mirror).
3. **CERN open archive** — CC-BY accelerator + detector photos +
   official films. ATLAS / CMS / LHC.
4. **LIGO Open Science Center** — strain plots, Hanford/Livingston
   site photos, GW150914/GW170817 data.
5. **ESA / Hubble / JWST releases** — CC-BY-SA-IGO. Cite ESA/Hubble
   per beat in description_template.
6. **Wikimedia Commons** — physicist portraits, observatory exteriors,
   period photography. CC-BY-SA. **Attribution required per beat.**
7. **arXiv / Royal Society / Phys Rev Letters paper PDFs** — page
   screenshots as fair-use editorial commentary. **Cap on-screen time
   at 8s per beat.** This is the channel's signature shot.
8. **Library of Congress** — historical scientific photography
   (Eddington 1919 plates, Cavendish notebooks, Kelvin diaries).
9. **Smithsonian National Air & Space photos** — mostly PD or CC.
10. **Stock b-roll** — Pexels / Pixabay / Storyblocks for laboratory /
    night-sky / chalkboard / observatory cutaways. Use sparingly;
    real instruments beat stock.

**FORBIDDEN sources:**
- YouTube re-uploads of news clips (ContentID minefield)
- Animated-physics channels (we want real photographs)
- AI-generated stills (channel rule — checked at quality-gate stage)
- Footage from competitor decoder channels (Veritasium, PBS Space
  Time, Cool Worlds — fair-use boundary too thin for our scale)

**Wikimedia URL guess miss-rate (class-of-bug, 2026-05-07):** see
`cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md`. Pattern-
guessed File: URLs miss ~60% of the time on Commons. Either verify
each File: page via the Wikimedia API before emit OR carry a
`_wikimedia_search_query` sibling string the prep tool can `srsearch`
on MANUAL fallback. Acceptable to leave the URL as a best-guess and
list real File: candidates in `_wikimedia_search_query`; the curator
will run `pipeline/cosmos_footage_prep.py` and substitute on miss.

Build the **shotlist** as `cosmosdecoded/shotlist/<slug>.json` with
beat-aligned source URLs (16:9 aspect). Schema documented in the
exemplar `cosmosdecoded/shotlist/eddington-1919-eclipse.json`.

### 5. Hat 3 — "How We Knew" Writer

Draft the long-form narration.

#### 5a. Long-form prose style (heuristics #24 + #26)

- **Voice register:** sober scientific documentary. Veritasium without
  the host. Second-person rare ("you can imagine the cold of the
  pre-dawn observatory…"). Mostly third-person declarative.
- **Cadence target:** ~150 wpm with F5-TTS-MLX sarah.wav at speed 1.0
  + atempo 1.0. **NEVER** the 135 wpm sleep cadence (that's
  HistoryRecapped's `long_form_channel.md`, not this channel).
  Word budget per chapter:
  - Act 1 (3-5 min) → ~450-750 words
  - Act 2 (15-20 min) → ~2250-3000 words
  - Act 3 (5-10 min) → ~750-1500 words
  - Total: 3500-5500 words for a 25-40 min runtime.
- **Mythbust per chapter:** every chapter calls out a misconception
  and corrects it. ("You'll often hear that Eddington's data alone
  proved relativity — actually, the Sobral results were sharper than
  Príncipe's, and one of Eddington's plates was discarded as
  unreliable.")
- **Document beats:** every 90-120 seconds of narration, name a
  specific document on screen. "Page 12 of the 1920 paper." "Nasa
  technical memo TM-72-XXX." This is the channel's signature.
- **No clickbait closer-anti-patterns:** never say "Einstein was
  wrong" (defamation surface + clickbait demonetisation), never
  speculate beyond paper claims, never frame a Nobel committee as
  "cowards" (defamation), never use verdict acronyms (carryover
  from MyStories rules).

#### 5b. Long-form chapter structure (mandatory for runtime ≥10 min — heuristic #28)

Three chapters, anchored on three acts. Schema from exemplar
`cosmosdecoded/narrations/eddington-1919-eclipse.json`:

```json
{
  "slug": "<slug>",
  "format": "long_form",
  "channel": "cosmosdecoded",
  "title": "How We Knew <X> — <Subject>",
  "tone": "engaged scientific explainer (Veritasium without the host); third-person declarative, second-person rare; document-led",
  "duration_target_s": [1500, 1750],
  "wpm_target": 150,
  "hook": "<60-100 word opener naming the moment of measurement>",
  "narration": "<full narration body, all numbers + dates word-spelled>",
  "chapters": [
    {"start_s": 0,    "title": "Prediction (...): <descriptor>",   "body_summary": "..."},
    {"start_s": 360,  "title": "Experiment (...): <descriptor>",  "body_summary": "..."},
    {"start_s": 1310, "title": "Consequence: <descriptor>",       "body_summary": "..."}
  ],
  "support_asks": [
    {"start_s": 200, "type": "soft_inline", "text": "<early ask>"},
    {"start_s": 1530, "type": "closer_inline", "text": "<late ask>"}
  ],
  "title_options": ["...", "..."],
  "thumbnail_brief": "...",
  "sources": ["..."],
  "_pronunciation_notes": "see <slug>.pronounce.json sidecar"
}
```

**Two embedded support asks per video** (carryover from
`historyrecapped/learnings/long_form_support_asks.md` REVISED 2026-05-04
— TWO asks, not five. The ~18 min cadence is RETIRED). One soft early
ask at ~3-4 min in, one closer near the end. INLINE in narration text
only — no panel render.

### 6. Quality gates (heuristics #31-#38)

Run these IN ORDER. Block emit on any failure. Mechanical, not
advisory.

1. **Banned-phrase scan** — grep narration for:
   - `"Einstein was wrong"` / `"NASA covered up"` / `"the truth they don't want you to know"` (clickbait + defamation)
   - `"smash that subscribe button"` (memory: spoken_subscribe_ask)
   - `"vote in comments"` / `"comment which experiment next"` (anti-pattern carried from HistoryRecapped closer)
   - Verdict acronyms `AITA|WIBTA|YTA|NTA|NAH|ESH` (memory: no_verdict_acronyms_in_audio)
   - Sleep-mode prose ("close your eyes", "drift off", "let your mind settle") — these belong on HistoryRecapped sleep-long-form, not here. Block.
2. **Pronunciation pre-pass** — phonetic respelling for foreign /
   proper nouns BEFORE TTS. Build the respelling map per slug; save
   next to the narration as `cosmosdecoded/narrations/<slug>.pronounce.json`.
   Reference: `cosmosdecoded/narrations/eht-2019-m87.pronounce.json`
   for the EHT-era schema (130+ respellings including Schwarzschild,
   Doeleman, Bouman, Falcke, ALMA, IRAM, microarcsecond, M87*, Sgr A*).
3. **Length budget** — total runtime calc (word_count / wpm × 60s)
   MUST land in `[1500, 2400]`s. Reject and rewrite outside.
4. **Source-fidelity check** — every named claim (date, name, value,
   institution, paper title) must trace to a `raw/<slug>.json`
   dossier field. Reject ungrounded claims.
5. **/critique-audio gate** — REQUIRED before any further work.
   After narration TTS first chunk renders, invoke /critique-audio
   on the wav. TTS bugs invalidate downstream.
6. **ContentID scan** — every `source_url` in the shotlist must be
   in the allowed-sources list (Section 4). Block any URL pointing
   at a YouTube reupload, news org, or competitor channel.
7. **Aspect-ratio + voice-ID match** — shotlist `aspect: "16:9"`. TTS
   provider matches `cosmosdecoded/config.yaml` long_form block.
   Refuse drift.
8. **Sleep-mode register check** — sample 3 random sentences from
   the draft, ask: "would this fit on HistoryRecapped's sleep
   channel?" If yes, the prose has drifted. Rewrite to engaged-
   explainer cadence.

### 7. Auto source-prep (MANDATORY, runs by default)

The renderer needs local mp4 files in
`cosmosdecoded/footage/long_sources/`. Authoring shotlists only
produces URLs + filenames. The class-of-bug fix (2026-05-05) is
`pipeline/cosmos_footage_prep.py`, which the skill **always** runs
after the four JSONs land on disk and before kicking off the renderer.

**Always invoke (idempotent — re-runs skip already-present files):**

```bash
.venv/bin/python -u -m pipeline.cosmos_footage_prep \
    --channel cosmosdecoded --slug <slug>
```

What it does, automatically, per shotlist entry:
- Resolves `commons.wikimedia.org/wiki/File:Foo.jpg` → direct upload URL via
  Wikimedia API (downloads a 2400px-wide thumb, not the multi-MB original).
- Resolves `images.nasa.gov/details/<id>` → highest-res asset via NASA images-api.
- Resolves `archive.org/details/<id>` → mp4 via `archive.org/metadata/<id>`.
- Direct `.mp4`/`.jpg`/`.png`/`.webp` URLs → urllib download.
- For `source_type: still_ken_burns` entries: ffmpeg zoompan (1.00→1.18)
  to produce an N-second 1920x1080 mp4.

It returns a structured report: `✓ fetched`, `✓ skipped`, `⚠ manual fallback`,
`✗ errors`. **Manual fallback is the expected outcome for paywalled hosts**
(royalsocietypublishing, nature.com, NYT TimesMachine, Pexels) — the prep
tool prints the URL + destination filename so the curator can save the
asset to disk by hand. EHT 2019 M87 surfaced a higher manual rate (35/56)
because of guessed Wikimedia File: pages — see
`cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md` for the fix.

After prep, run `ls cosmosdecoded/footage/long_sources/` and confirm every
clip's `source` field has a corresponding file. **Any missing file blocks
the renderer.** Clear-state: zero manual entries before invoking the
renderer.

### 8. Renderer handoff

The long-form renderer exists and is channel-parametric. NO new code.

```bash
.venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_long_form.py -- \
    --channel cosmosdecoded --slug <slug>
```

Wrap with `caffeinate -dimsu` per
`feedback_long_form_render_caffeinate.md` and
`feedback_long_form_power_preflight.md`. AC + lid open required for
F5-TTS-MLX. Low Power Mode hard-rejected by `render_long_form.py`
preflight.

### 9. Report back

After the four files land on disk, report:

```
✓ dossier:             cosmosdecoded/raw/<slug>.json
✓ long-form narration: cosmosdecoded/narrations/<slug>.json
✓ long-form shotlist:  cosmosdecoded/shotlist/<slug>.json
✓ pronunciations:      cosmosdecoded/narrations/<slug>.pronounce.json

quality gates:
  ✓ banned-phrase scan
  ✓ pronunciation pre-pass
  ✓ length budget (long: 28m04s)
  ✓ source-fidelity (every claim traces to dossier)
  ✓ aspect/voice match (16:9 / F5-TTS-MLX sarah.wav)
  ✓ ContentID scan (no YT reuploads)
  ✓ sleep-mode register check (engaged explainer, not sleep)
  ⏳ /critique-audio gate — run after first TTS chunk

source prep: <X fetched / Y skipped / Z manual>

next:
  1. .venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_long_form.py -- --channel cosmosdecoded --slug <slug>
  2. /critique-audio cosmosdecoded/cache/<slug>/narration.wav
  3. (optional) /make-cosmos-short <slug>   ← cuts a 50-60s 9:16 Short isolating the act-2 measurement
```

### 10. Self-learning hook (heuristics #39-#44)

After the user runs `/critique-audio` or `/critique-video` on the
output:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo / one bad pronunciation / one wrong date) →
     fix in the JSON output, append a 1-line note to
     `.claude/skills/make-cosmos-long/learnings/_index.md`.
   - **CLASS-OF-BUG** (every future run will hit it — e.g. Kokoro
     mispronounces every Latin scientific term, or every long-form
     drifts into sleep-mode at the act-3 transition) → fix in
     `pipeline/<module>.py` OR in this SKILL.md prompt, then append
     a regression note to
     `.claude/skills/make-cosmos-long/learnings/<topic>.md` AND
     mirror to `cosmosdecoded/learnings/<topic>.md` per CLAUDE.md
     dual-save rule.
2. Update MEMORY.md index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in Section 6 that blocks emit on detection.
4. Carry `learnings_consulted:` in this file's frontmatter (already
   present); update when a new learning lands.

## Important rules

- **Closer (long-form) embedded:** TWO inline asks (one ~3-4 min in, one near end). NEVER five — the ~18 min cadence is RETIRED.
- **Footage policy:** PD or CC only. NEVER YouTube news re-uploads. NEVER AI-generated stills. NEVER competitor-channel rips.
- **Voice cadence:** F5-TTS-MLX sarah.wav at speed 1.0 + atempo 1.0 = engaged explainer. **NEVER** drift to sleep cadence (0.95+0.85). The renderer doesn't enforce this; the skill is responsible.
- **Document-led signature:** every 90-120s of narration, name a specific paper / report / memo on screen. This is the channel's wedge.
- **Source-fidelity (heuristic #35):** every named claim traces to a dossier field. No ungrounded speculation.
- **Three-act structure:** prediction → experiment → consequence. Non-negotiable.
- **Banned phrases:** "Einstein was wrong", "NASA covered up", "the truth they don't want you to know", "smash that subscribe button", "vote in comments", verdict acronyms, sleep-mode prose ("close your eyes", "drift").
- **No image gen:** even if `cosmosdecoded/config.yaml` carries an inert image-gen block, NEVER flip `render_style` to `image_gen` on this channel. Document-led signature dilutes immediately.
- **Stages 4-7 belong to the renderer.** This skill produces JSON; render_long_form.py does TTS, ffmpeg, mux, captions.
- **Always use `.venv/bin/python`** for any helper commands.
- **Dual-save** every learning per CLAUDE.md (memory + project doc).

## Learnings from prior runs

- **2026-05-07** — `eht-2019-m87` (run as paired output via the
  retired /make-cosmos-decoder) — CLASS-OF-BUG: shotlist Wikimedia URL
  guesses ~60% miss rate (35/56 MANUAL). Fix:
  `cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md`.
  Mitigation: shotlist authors carry `_wikimedia_search_query` per
  entry OR `pipeline/cosmos_footage_prep.py` extends with
  `--suggest-fallback`. Two strikes = mandatory automation.
- **2026-05-05** — `eddington-1919-eclipse` (paired output) — first run
  emitted four JSONs and stopped at prep gap. Class-of-bug fix:
  `pipeline/cosmos_footage_prep.py` now runs by default in Section 7.

## Why this skill is separate from /make-cosmos-short

User decision 2026-05-08: each skill writes its own dossier
independently. Reason: simpler control flow + clearer single-purpose
invocations (`/make-cosmos-long eht-2019-m87` produces the long; `/make-
cosmos-short ligo-2015-gw150914` produces a Short without first
authoring 4000 words of long-form). Trade-off: the original
"act-2-becomes-Short" extraction discipline of /make-cosmos-decoder is
not enforced — if you want consistency, run /make-cosmos-long first
and instruct /make-cosmos-short to "reuse the dossier from
raw/<slug>.json." See also /make-cosmos-short SKILL.md.

`/make-top10` is **countdown structure** (#10 → #1). Cosmos Decoded is
**three-act structure**. Different schema, incompatible chapter contract.

`/make-katha` is the closest sibling structurally (long-form footage-
only, channel-specific) but the prose register (devotional calming
Hindi) and source palette (Wikimedia temple imagery) are incompatible.

`/make-sports-doc` shares the long-form footage-only renderer but the
curatorial rule is broadcast cut-ins; Cosmos Decoded has no broadcast
equivalent (papers and plates aren't "cuts").

`/make-sleep-history` shares the renderer but the prose register is
sleep-mode (135 wpm + warm-firelight grade) — actively rejected on
this channel.

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
