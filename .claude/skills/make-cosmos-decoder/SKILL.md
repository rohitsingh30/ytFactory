---
name: make-cosmos-decoder
description: Author a "How We Knew" physics+space deep-dive for Cosmos Decoded — pairs a physics prediction with the experiment/mission/observation that confirmed it. Three-act (prediction → experiment → consequence) 25-40 min long-form (16:9) PLUS a paired 50-60s Short (9:16) isolating the act-2 measurement. Footage-only (NASA / NTRS / ESA / CERN / LIGO / Wikimedia / arXiv / LoC / Smithsonian / stock — NO AI image gen, NEVER YouTube re-uploads). Produces narrations/<slug>.json + shotlist/<slug>.json AND narrations/<slug>-short.json + shotlist/<slug>-short.json, then hands off to historyrecapped/scripts/render_long_form.py and render_footage_only.py — both --channel cosmosdecoded. Use when the user says "make me a cosmos decoder", "how we knew <X>", "physics decoder on <Y>", "long-form physics", "Eddington 1919 video", "LIGO video", "JWST decoder". For 50-60s science-news Shorts use /make-script. For Top-10 science countdowns use /make-top10. For sports docs use /make-sports-doc.
learnings_consulted:
  - cosmosdecoded/learnings/channel.md
  - cosmosdecoded/config.yaml
  - .claude/skills/make-skill/learnings/heuristics.md
  - historyrecapped/learnings/long_form_hook_template.md
  - historyrecapped/learnings/long_form_support_asks.md
  - historyrecapped/learnings/long_form_captions.md
  - historyrecapped/learnings/long_form_sources.md
  - feedback_critique_audio_before_image_gen
  - feedback_diffusion_negation_trap (irrelevant — no image gen here)
  - feedback_pronunciation_pretts
---

# /make-cosmos-decoder — "How We Knew" physics+space paired-output skill

This skill authors a **paired output** for the Cosmos Decoded channel:
one **25-40 min long-form deep-dive** (16:9) and one **50-60s Short**
(9:16) cut from the same source material. Both are **footage-only** —
real photographs of real experiments, NASA / ESA / CERN / LIGO direct
releases, paper page screenshots, lab notebook scans, telescope plates,
mission control footage. NO AI image gen. NO diffusion queue. No GPU
imagegen burn.

The wedge is **document-led storytelling**: every claim cites the
specific paper, mission report, or press release on screen. The
three-act structure is non-negotiable:

- **Act 1 — Prediction** (3-5 min long / 8-10s Shorts): what the
  theory said before the test. Show the paper page or chalkboard
  equation. Name the predicting physicist.
- **Act 2 — Experiment** (15-20 min long / 30-35s Shorts): the
  actual test. Photographs of apparatus + team + location. Telegram
  exchanges or lab notebooks if surviving. **The single moment of
  measurement** is this skill's payoff — it becomes the Short.
- **Act 3 — Consequence** (5-10 min long / 10-15s Shorts): what
  changed in physics or engineering after. The follow-up paper or
  mission. The Nobel (if any). The technology it enabled.

You wear three hats: **Document Researcher → Footage Curator →
"How We Knew" Writer**.

The output is **four** JSON files — long-form narration + shotlist,
Short narration + shotlist. Stages 4-7 (TTS, ffmpeg, mux, upload) live
in the renderer, not in this skill.

## How to run it

### 1. Confirm subject + slug + which act becomes the Short

If the user's prompt is partial ("make me a Cosmos Decoder on Eddington"),
state your reading back in one sentence and let them redirect. Lock the
**5-line spec** before writing anything:

- **Subject** — the physics prediction + its proving event. e.g.
  `eddington-1919-eclipse` (light bends near mass), `pound-rebka-1959`
  (time slows near mass), `ligo-2015-gw150914` (gravitational waves
  exist), `eht-2019-m87` (black holes have shadows),
  `super-kamiokande-1998` (neutrinos have mass),
  `mars-polar-lander-1999` (the failure decoder),
  `cassini-grand-finale-2017` (Saturn's rings are young).
- **Slug** — kebab-case, the contract for every downstream artifact.
  `eddington-1919-eclipse` is the canonical pattern: `<scientist-or-mission>-<year>-<event>`.
- **Channel** — `cosmosdecoded` (this skill is single-channel).
  Refuse with a `/create-youtube-channel cosmosdecoded` suggestion if
  `cosmosdecoded/config.yaml` is missing.
- **Long-form duration target** — `short-doc` (25-30 min, default for
  single-experiment subjects) | `extended-doc` (32-40 min, for
  multi-paper / multi-team subjects like LIGO).
- **Short hook anchor** — which exact moment from act 2 becomes the
  9:16 Short. Default = "the single moment of measurement" (the
  glance through the telescope, the strain plot lighting up, the
  telegram going out). Override only if the user names a different
  beat.

### 2. Load channel context (heuristics #8 + #46)

Before any drafting, run these reads. Bake the literals into both
narration files; never paraphrase:

```bash
cat cosmosdecoded/learnings/channel.md
cat cosmosdecoded/config.yaml
ls cosmosdecoded/learnings/
ls cosmosdecoded/narrations/      # check for slug collision
```

Inheritance contract (literal — do not paraphrase):

| Field                  | Shorts                                              | Long-form                                                   |
|------------------------|-----------------------------------------------------|-------------------------------------------------------------|
| TTS provider           | Kokoro `am_michael` at 1.0                          | F5-TTS-MLX `pipeline/voice_refs/sarah.wav` 1.0 + atempo 1.0 |
| Aspect                 | 9:16                                                | 16:9                                                        |
| Duration band          | 50-60s                                              | 1500-2400s (25-40 min)                                      |
| Captions               | word-by-word PNGs                                   | sentence-level white sans-serif (NOT yellow italic sleep)   |
| Closer                 | `SUBSCRIBE for more decoders\nLIKE if this changed how you see physics` | embedded as inline ask in last 90s of narration            |
| Category               | 28 (Sci/Tech)                                       | 28 (Sci/Tech)                                               |
| Footage policy         | PD or CC only; NEVER YouTube news re-uploads        | same                                                        |
| Visual signature       | cool dawn + observatory blue grade                  | same (config.yaml `long_form.visual_grade.filter`)          |

**Voice cadence rule (class-of-bug):** the long-form `tts_speed: 1.0`
+ `atempo: 1.0` is **calm-explainer**, not sleep. NEVER drift the
prose into bedtime cadence ("close your eyes... drift into the night
of 1919..."). Cosmos Decoded long-form is "Veritasium without the
host" — engaged, second-person rare, declarative, technical without
being lecturing. If the prose starts feeling sleepy, you have drifted
out of register; rewrite.

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

Save the dossier as `cosmosdecoded/raw/<slug>.json` with this schema:

```json
{
  "slug": "eddington-1919-eclipse",
  "subject_summary": "How Eddington's 1919 solar eclipse expedition confirmed that light bends near mass — the first empirical test of general relativity.",
  "prediction": {
    "claim": "Light bends by 1.75 arcseconds passing the Sun's limb (full GR) vs 0.87 arcseconds (Newtonian limit).",
    "predicting_paper": {"author": "Albert Einstein", "title": "Die Grundlage der allgemeinen Relativitätstheorie", "year": 1916, "doi_or_url": "..."},
    "key_physicists": [{"name": "Albert Einstein", "role": "predictor", "wikimedia_portrait": "..."}]
  },
  "experiment": {
    "date": "1919-05-29",
    "location": "Príncipe (Eddington) + Sobral (Crommelin/Davidson)",
    "instrument": "Astrographic refractor + photographic plates",
    "team": [{"name": "Arthur Eddington", "role": "lead, Príncipe", "wikimedia_portrait": "..."}, ...],
    "moment_of_measurement": "Plate comparison at Cambridge, autumn 1919 — Eddington measures the deflection on the developed plates.",
    "proving_paper": {"author": "Dyson, Eddington, Davidson", "title": "A Determination of the Deflection of Light by the Sun's Gravitational Field", "year": 1920, "doi_or_url": "..."},
    "result_value": "1.61 ± 0.30 arcseconds (Sobral) — consistent with GR within error"
  },
  "consequence": {
    "immediate": "Royal Society announcement Nov 6 1919; Einstein becomes a global figure overnight (NYT headline Nov 10).",
    "physics_followup": ["Pound-Rebka 1959", "Hafele-Keating 1971", "Gravity Probe B 2011"],
    "tech_enabled": ["GPS time-dilation correction (1989)"],
    "nobel_status": "Einstein gets Nobel 1921 (for photoelectric, NOT for relativity — committee was cautious)."
  },
  "counterfactual": "If light hadn't bent, the photographic plates from Sobral would have shown the Hyades stars at their normal angular positions; Newtonian gravity would have remained the dominant theory of gravitation.",
  "sources": ["...", "..."],
  "footage_pool_seed": ["images.nasa.gov/..", "commons.wikimedia.org/..", "..."]
}
```

This dossier is the SOURCE OF TRUTH for both the long-form and the
Short. Every claim in either narration MUST trace to a field here.

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

Build the **shotlist** as `cosmosdecoded/shotlist/<slug>.json` with
beat-aligned source URLs:

```json
{
  "slug": "eddington-1919-eclipse",
  "aspect": "16:9",
  "windows": [
    {
      "beat_index": 0,
      "in_s": 0,
      "out_s": 6,
      "source_url": "https://commons.wikimedia.org/wiki/File:1919_eclipse_negative.jpg",
      "source_class": "wikimedia",
      "attribution": "© Royal Astronomical Society / Wikimedia Commons (PD)",
      "match_text": "On the morning of May 29, 1919, on the island of Príncipe...",
      "zoom": {"start": [0.5, 0.5, 1.0], "end": [0.5, 0.5, 1.18]}
    },
    ...
  ],
  "fallback_b_roll": [
    "https://www.pexels.com/video/observatory-at-night-...",
    "https://images.nasa.gov/details/PIA12345"
  ]
}
```

The Shorts shotlist (`cosmosdecoded/shotlist/<slug>-short.json`) is a
**subset** — typically 5-7 windows from act 2 of the long-form
shotlist. Same schema, `aspect: "9:16"`. Reuse the long-form windows
where possible (the renderer's letterbox path handles 16:9 → 9:16
fitting).

### 5. Hat 3 — "How We Knew" Writer

Draft the long-form narration first. Then carve out the Short.

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

Three chapters, anchored on three acts:

```json
{
  "slug": "eddington-1919-eclipse",
  "title": "How We Knew Light Bends — Eddington's 1919 Eclipse",
  "hook": "On the morning of May 29, 1919, on a volcanic island off the coast of West Africa, Arthur Eddington pointed a camera at a total solar eclipse — and proved Albert Einstein right.",
  "chapters": [
    {"start_s": 0,    "title": "Prediction (1916): Einstein bends the geometry of space",   "body": "<450-750 words>"},
    {"start_s": 240,  "title": "Experiment (1919): the photographic plates of Príncipe",    "body": "<2250-3000 words>"},
    {"start_s": 1440, "title": "Consequence: Einstein becomes Einstein, and GPS is born",   "body": "<750-1500 words>"}
  ],
  "support_asks": [
    {"start_s": 220, "type": "soft_inline", "text": "If you're enjoying this, the SUBSCRIBE button helps the channel survive."},
    {"start_s": 2100, "type": "closer_inline", "text": "If this changed how you see physics, leave a like — it tells YouTube to show this to someone else who'd want to know."}
  ],
  "sources": [
    "Dyson, Eddington & Davidson 1920, A Determination of the Deflection of Light…",
    "Einstein 1916, Die Grundlage der allgemeinen Relativitätstheorie",
    "Royal Astronomical Society Archive, eclipse expedition plates"
  ],
  "title_options": [
    "How We Knew Light Bends — Eddington's 1919 Eclipse Expedition",
    "The Photograph That Made Einstein Famous",
    "How a Solar Eclipse in 1919 Proved General Relativity"
  ],
  "thumbnail_brief": "split-pane: left = period eclipse photo with star markers, right = Einstein 1919 portrait. Caption 'How we knew' bottom-left in serif."
}
```

**Two embedded support asks per video** (heuristic + carryover from
`historyrecapped/learnings/long_form_support_asks.md` REVISED 2026-05-04
— TWO asks, not five. The ~18 min cadence is RETIRED). One soft early
ask at ~3-4 min in, one closer near the end. INLINE in narration text
only — no panel render.

#### 5c. Short carve-out

The Short is **the act-2 measurement moment isolated**. Draft after
the long-form is done; never write it cold.

- **Hook (3-5s):** the moment of measurement, named. *"This is the
  photograph that made Einstein famous."* / *"This is the strain plot
  LIGO printed at 9:50 UTC, September 14, 2015."*
- **Setup (10-15s):** prediction + stake. *"Einstein had said light
  bends by 1.75 arcseconds. If Eddington's plates showed less,
  Newton would still rule physics."*
- **Reveal (20-25s):** the actual measurement, on screen. Plate
  scan / strain plot / decay curve. The single number.
- **Consequence (8-12s):** what changed. One Nobel, one mission,
  one technology, one piece of common-sense knowledge.
- **Closer (3-5s):** literal `cosmosdecoded` closer:
  `SUBSCRIBE for more decoders` + `LIKE if this changed how you see physics`.

Word budget at Kokoro `am_michael` 1.0 (~165 wpm): 145-165 words for
50-60s.

Save as `cosmosdecoded/narrations/<slug>-short.json`. Same schema as
`/make-script` Shorts (compatible with `render_footage_only.py` Shorts
path):

```json
{
  "slug": "eddington-1919-eclipse-short",
  "long_form_parent": "eddington-1919-eclipse",
  "hook": "This is the photograph that made Einstein famous.",
  "narration": "...",
  "beats": [
    {"index": 0, "text": "This is the photograph that made Einstein famous.", "match_text": "the photograph that made Einstein"},
    ...
  ],
  "closer": "SUBSCRIBE for more decoders\nLIKE if this changed how you see physics",
  "title_options": ["The Photograph That Made Einstein Famous", "How One Eclipse in 1919 Proved Relativity"]
}
```

### 6. Quality gates (heuristics #31-#38)

Run these IN ORDER. Block emit on any failure. Mechanical, not
advisory.

1. **Banned-phrase scan** — grep both narrations for:
   - `"Einstein was wrong"` / `"NASA covered up"` / `"the truth they don't want you to know"` (clickbait + defamation)
   - `"smash that subscribe button"` (memory: spoken_subscribe_ask)
   - `"vote in comments"` / `"comment which experiment next"` (anti-pattern carried from HistoryRecapped closer)
   - Verdict acronyms `AITA|WIBTA|YTA|NTA|NAH|ESH` (memory: no_verdict_acronyms_in_audio)
   - Sleep-mode prose ("close your eyes", "drift off", "let your mind settle") — these belong on HistoryRecapped sleep-long-form, not here. Block.
2. **Pronunciation pre-pass** — phonetic respelling for foreign /
   proper nouns BEFORE TTS. Examples that have bitten us in the
   pipeline: `Eddington` (ED-ing-tun), `Pound-Rebka` (POUND-REB-kə),
   `Príncipe` (PRIN-si-pay), `Schwarzschild` (SHVARTS-shild),
   `Einstein` (ICE-stine, NOT EEN-stine), `Joule` (JOOL),
   `Cassini` (kas-SEE-nee), `arcsecond` (ARK-seh-cund), `Mössbauer`
   (MERSS-bow-er). Build the respelling map per slug; save next to
   the narration as `cosmosdecoded/narrations/<slug>.pronounce.json`
   so future renders inherit it (memory: feedback_pronunciation_pretts).
3. **Length budget** — long-form total runtime calc (word_count /
   wpm × 60s) MUST land in `[1500, 2400]`s. Shorts MUST land in
   `[50, 60]`s. Reject and rewrite outside.
4. **Source-fidelity check** — every named claim (date, name, value,
   institution, paper title) must trace to a `raw/<slug>.json`
   dossier field. Reject ungrounded claims.
5. **/critique-audio gate** — REQUIRED before any further work.
   After narration TTS first chunk renders, invoke /critique-audio
   on the wav. TTS bugs invalidate downstream. (memory:
   feedback_critique_audio_before_image_gen — note: no image gen
   here, but the rule generalises: critique audio FIRST.)
6. **ContentID scan** — every `source_url` in the shotlist must be
   in the allowed-sources list (Section 4). Block any URL pointing
   at a YouTube reupload, news org, or competitor channel.
7. **Aspect-ratio + voice-ID match** — long-form shotlist
   `aspect: "16:9"`, Short shotlist `aspect: "9:16"`. TTS provider
   matches `cosmosdecoded/config.yaml`. Refuse drift.
8. **Sleep-mode register check** — sample 3 random sentences from
   the long-form draft, ask: "would this fit on HistoryRecapped's
   sleep channel?" If yes, the prose has drifted. Rewrite to
   engaged-explainer cadence.

### 7. Auto source-prep (MANDATORY, runs by default)

The renderer needs local mp4 files in `<channel>/footage/long_sources/`
(long-form) or `<channel>/footage/sources/` (Shorts). Authoring shotlists
only produces URLs + filenames. The class-of-bug fix (2026-05-05) is
`pipeline/cosmos_footage_prep.py`, which the skill **always** runs after
the four JSONs land on disk and before kicking off any renderer.

**Always invoke (idempotent — re-runs skip already-present files):**

```bash
.venv/bin/python -u -m pipeline.footage.cosmos_footage_prep \
    --channel cosmosdecoded --slug <long-slug>
.venv/bin/python -u -m pipeline.footage.cosmos_footage_prep \
    --channel cosmosdecoded --slug <short-slug>
```

What it does, automatically, per shotlist entry:
- Resolves `commons.wikimedia.org/wiki/File:Foo.jpg` → direct upload URL via
  Wikimedia API (downloads a 2400px-wide thumb, not the multi-MB original).
- Resolves `images.nasa.gov/details/<id>` → highest-res asset via NASA images-api.
- Resolves `archive.org/details/<id>` → mp4 via `archive.org/metadata/<id>`.
- Direct `.mp4`/`.jpg`/`.png`/`.webp` URLs → urllib download.
- For `source_type: still_image` entries: ffmpeg zoompan (1.00→1.18)
  to produce an N-second mp4 of the right aspect (1920×1080 long-form,
  1080×1920 Short).

It returns a structured report: `✓ fetched`, `✓ skipped`, `⚠ manual fallback`,
`✗ errors`. **Manual fallback is the expected outcome for paywalled hosts**
(royalsocietypublishing, nature.com, NYT TimesMachine, Pexels) — the prep
tool prints the URL + destination filename so the curator can save the
asset to disk by hand. Without `cosmos_footage_prep.py`, this used to be
49 manual fetches per video; now it's typically 2–4.

After prep, run `ls cosmosdecoded/footage/long_sources/` and confirm every
clip's `source` field has a corresponding file. **Any missing file blocks
the renderer.** Clear-state: zero manual entries before invoking the
renderer.

### 8. Renderer handoff

Both renderers exist and are channel-parametric. NO new code.

```bash
# Long-form (16:9, 25-40 min)
caffeinate -dimsu .venv/bin/python -u historyrecapped/scripts/render_long_form.py \
    --channel cosmosdecoded --slug eddington-1919-eclipse

# Shorts (9:16, 50-60s) — letterboxes still_image/16:9 windows to 9:16
.venv/bin/python -u historyrecapped/scripts/render_footage_only.py \
    --channel cosmosdecoded --slug eddington-1919-eclipse-short
```

Wrap long-form with `caffeinate -dimsu` per
`feedback_long_form_render_caffeinate.md` and
`feedback_long_form_power_preflight.md`. AC + lid open required for
F5-TTS-MLX. Low Power Mode hard-rejected by `render_long_form.py`
preflight.

### 9. Report back

After both narrations + shotlists land on disk, report:

```
✓ long-form narration: cosmosdecoded/narrations/eddington-1919-eclipse.json
✓ long-form shotlist:  cosmosdecoded/shotlist/eddington-1919-eclipse.json
✓ short narration:     cosmosdecoded/narrations/eddington-1919-eclipse-short.json
✓ short shotlist:      cosmosdecoded/shotlist/eddington-1919-eclipse-short.json
✓ dossier:             cosmosdecoded/raw/eddington-1919-eclipse.json
✓ pronunciations:      cosmosdecoded/narrations/eddington-1919-eclipse.pronounce.json

quality gates:
  ✓ banned-phrase scan
  ✓ pronunciation pre-pass
  ✓ length budget (long: 28m04s, short: 56s)
  ✓ source-fidelity (every claim traces to dossier)
  ✓ aspect/voice match
  ✓ ContentID scan (no YT reuploads)
  ✓ sleep-mode register check (engaged explainer, not sleep)
  ⏳ /critique-audio gate — run after first TTS chunk

next:
  1. .venv/bin/python -u historyrecapped/scripts/render_long_form.py --channel cosmosdecoded --slug eddington-1919-eclipse
  2. /critique-audio cosmosdecoded/cache/eddington-1919-eclipse/narration.wav
  3. .venv/bin/python -u historyrecapped/scripts/render_footage_only.py --channel cosmosdecoded --slug eddington-1919-eclipse-short
```

### 10. Self-learning hook (heuristics #39-#44)

After the user runs `/critique-audio` or `/critique-video` on either
output:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo / one bad pronunciation / one wrong date) →
     fix in the JSON output, append a 1-line note to
     `.claude/skills/make-cosmos-decoder/learnings/_index.md`.
   - **CLASS-OF-BUG** (every future run will hit it — e.g. Kokoro
     mispronounces every Latin scientific term, or every long-form
     drifts into sleep-mode at the act-3 transition) → fix in
     `pipeline/<module>.py` OR in this SKILL.md prompt, then append
     a regression note to
     `.claude/skills/make-cosmos-decoder/learnings/<topic>.md` AND
     mirror to `cosmosdecoded/learnings/<topic>.md` per CLAUDE.md
     dual-save rule.
2. Update MEMORY.md index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in Section 6 that blocks emit on detection.
4. Carry `learnings_consulted:` in this file's frontmatter (already
   present); update when a new learning lands.

## Important rules

- **Closer (Shorts) literal:** `SUBSCRIBE for more decoders\nLIKE if this changed how you see physics`. Never paraphrase.
- **Closer (long-form) embedded:** TWO inline asks (one ~3-4 min in, one near end). NEVER five — the ~18 min cadence is RETIRED.
- **Footage policy:** PD or CC only. NEVER YouTube news re-uploads. NEVER AI-generated stills. NEVER competitor-channel rips.
- **Voice cadence (long-form):** F5-TTS-MLX sarah.wav at speed 1.0 + atempo 1.0 = engaged explainer. **NEVER** drift to sleep cadence (0.95+0.85). The renderer doesn't enforce this; the skill is responsible.
- **Document-led signature:** every 90-120s of narration, name a specific paper / report / memo on screen. This is the channel's wedge.
- **Source-fidelity (heuristic #35):** every named claim traces to a dossier field. No ungrounded speculation.
- **Three-act structure:** prediction → experiment → consequence. Non-negotiable. The act-2 measurement moment IS the Short.
- **Banned phrases:** "Einstein was wrong", "NASA covered up", "the truth they don't want you to know", "smash that subscribe button", "vote in comments", verdict acronyms, sleep-mode prose ("close your eyes", "drift").
- **No image gen:** even if `cosmosdecoded/config.yaml` carries an inert image-gen block, NEVER flip `render_style` to `image_gen` on this channel. Document-led signature dilutes immediately.
- **Stages 4-7 belong to the renderer.** This skill produces JSON; render_long_form.py / render_footage_only.py do TTS, ffmpeg, mux, captions.
- **Always use `.venv/bin/python`** for any helper commands.
- **Dual-save** every learning per CLAUDE.md (memory + project doc).

## Learnings from prior runs

<!-- empty on day 1; appended after each run via Section 9 -->

## Why this skill is separate from /make-top10 + /make-script

`/make-top10` is **countdown structure** (#10 → #1 with continuous
connective tissue). Cosmos Decoded is **three-act structure**
(prediction → experiment → consequence). Different schema,
incompatible chapter contract.

`/make-script` is **single-format Shorts only** (50-60s). Cosmos
Decoded is **paired output** — long-form + Short cut from the same
source material in one invocation. The pairing is the
force-multiplier; splitting it across two skills would lose the
"act-2-becomes-Short" extraction discipline.

`/make-katha` is the closest sibling structurally (long-form
footage-only, channel-specific) but the prose register (devotional
calming Hindi) and source palette (Wikimedia temple imagery) are
incompatible.

`/make-sports-doc` shares the long-form footage-only renderer but
the curatorial rule is broadcast cut-ins; Cosmos Decoded has no
broadcast equivalent (papers and plates aren't "cuts").

A schema-extension would have required forking the chapters[]
contract three ways inside `/make-top10`. The clean line is one
skill per format-grammar.

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
