---
name: make-sleep-history
description: Author a 60-120 min calming history-for-sleep video for the History Recapped channel — Sleepy Time History parity (`@SleepyTimeHistory`, 148K subs, 14.8M views). Soothing F5-TTS Sarah voice over warm-firelight-graded archival footage (or Z-Image-Turbo graphic-novel panels with a small yellow campfire visible in every frame), 16:9, sentence-level yellow italic captions, faint top-right watermark, exactly TWO inline support asks. Produces `historyrecapped/narrations/<slug>-sleep.json` + `historyrecapped/shotlist/<slug>-sleep.json`, then hands off to `scripts/historyrecapped/render_long_form.py`. Use when the user says "make me a sleep history", "long-form history for sleep", "narrate <era> for sleep", "Sleepy Time History style on <X>", "calming history narration on <Y>", "bedtime <war/era> video". For 50-60s history Shorts use `/make-script`. For Top-10 long-form use `/make-top10`. For Hindu kathaa use `/make-katha`. For physics how-we-knew use `/make-cosmos-decoder`.
learnings_consulted:
  - historyrecapped/learnings/channel.md
  - historyrecapped/learnings/long_form_channel.md
  - historyrecapped/learnings/long_form_hook_template.md
  - historyrecapped/learnings/long_form_prose_style.md
  - historyrecapped/learnings/long_form_support_asks.md
  - historyrecapped/learnings/long_form_visual_signature.md
  - historyrecapped/learnings/long_form_sources.md
  - historyrecapped/learnings/long_form_captions.md
  - historyrecapped/learnings/long_form_trim_aspect_short_circuit.md
  - historyrecapped/config.yaml (long_form: block)
  - .claude/skills/make-skill/learnings/heuristics.md
  - feedback_long_form_strict_f5
  - feedback_long_form_image_panels_capped
  - feedback_long_form_render_caffeinate
  - feedback_long_form_power_preflight
  - feedback_critique_audio_before_image_gen
  - feedback_pronunciation_pretts
  - feedback_engineer_class_of_bug
  - feedback_skills_kick_render_directly
  - project_long_form_sleep_reference_2026_05_04
---

# /make-sleep-history — Sleepy Time History parity authoring skill

This skill authors a **60-120 min calming history-for-sleep** narration
plus visual plan for the History Recapped channel, matching the
reverse-engineered parity target `@SleepyTimeHistory` (148K subs,
14.8M views — see memory `project_long_form_sleep_reference_2026_05_04`
+ `/tmp/ref_AHTEnwz0bL4/`).

You become **the narrator-novelist**: a researcher who tells one war,
one century, one geography, slowly, in the voice of an evening, with
the lights low. The output is two JSON files. Stages 4-7 (TTS,
ffmpeg, captions, mux, watermark, upload) live in
`scripts/historyrecapped/render_long_form.py` — not in this skill.

The five locked specs (reverse-engineered 2026-05-04, all already in
project docs — DO NOT paraphrase):

1. **6-beat novelistic hook** in the first 90s (`Imagine...` → sensory
   transport with era unnamed → era anchor → "you ARE this person" →
   central question → myth-bust setup). Source:
   `historyrecapped/learnings/long_form_hook_template.md`.
2. **Second-person present-tense prose** throughout, one mythbust per
   chapter, sensory-led sentences, anchor-phrase repetition. Source:
   `historyrecapped/learnings/long_form_prose_style.md`.
3. **Exactly TWO inline support asks** — one soft ask at ~3-4 min in,
   one closer. Five-asks-every-18-min cadence is RETIRED. Source:
   `historyrecapped/learnings/long_form_support_asks.md`.
4. **Visual signature** — a literal small bright yellow campfire (or
   analogous warm-light source: lantern, hearth, lit torch, oil lamp,
   brazier) visible in every frame as the warm-light anchor; yellow
   italic sentence captions `#FFD93D`; faint white top-right
   "HISTORY RECAPPED" watermark. Two render paths (selectable):
     - **Path A — `archival_footage` (current default):** warm-cool
       ffmpeg grade on archival footage from archive.org PD or
       DroneScapes-class HD-with-dispute-fallback (`long_form.visual_grade.filter`).
     - **Path B — `image_panels` (gated):** Z-Image-Turbo graphic-novel
       panels with locked "small warm-yellow light source" prompt
       rule. **Hard-capped at 24 panels** in the renderer
       (`feedback_long_form_image_panels_capped.md`); above that the
       Metal GPU command buffer times out. Author hybrid plans
       (image_panels for hook + closer, archival_footage for the
       bulk) where you want Path B's look without paying its cost.
   Source: `historyrecapped/learnings/long_form_visual_signature.md`.
5. **Sleep-niche upload metadata** — uses the existing
   `long_form.upload` block in `historyrecapped/config.yaml` (sleep
   tags + chapter-stamped description + AI-disclosure paragraph).
   The skill emits `chapters: []` and `sources: []` so the renderer
   can resolve `{chapters_block}` and `{script.sources}` tokens at
   upload time.

## How to run it

### 1. Confirm subject + slug + render path + duration target

If the user's prompt is partial ("make me a sleep history on Verdun"),
state your reading back in one sentence and let them redirect. Lock
the **5-line spec** before writing anything:

- **Subject** — one war, one campaign, one era, one biography. e.g.
  `verdun-1916`, `pacific-war-1941-1942`, `eastern-front-1941-1945`,
  `napoleon-russia-1812`, `byzantium-1204`, `hanseatic-league`.
  Sleep-history works with any era as long as the geography is
  specific enough to anchor the sensory beats — "World War 2" is too
  big; "the Italian Campaign 1943-45" is right.
- **Slug** — kebab-case, **must end in `-sleep`** (the renderer +
  upload split key `*-sleep` slugs from Shorts in the shared
  `historyrecapped/narrations/` dir). Pattern:
  `<topic>-<year-or-range>-sleep`. e.g.
  `verdun-1916-sleep`, `eastern-front-1941-1945-sleep`.
- **Render path** — `archival_footage` (default; reuses existing
  source mp4s + warm-firelight grade) or `image_panels` (gated at 24
  panels — only viable for short ~30-40 min videos OR as a hybrid
  hook/closer overlay on top of an archival_footage spine). When in
  doubt, default to `archival_footage` — it's the parity target's
  fallback path AND the only path the renderer is hardened on.
- **Duration target** — `[3600, 4500]` (60-75 min, default for new
  topics), `[5400, 6300]` (90-105 min, deep-dive), `[7000, 7200]`
  (120 min cap, marquee episodes). The config caps at `[3600, 7200]`;
  exceeding it will be rejected by the length-budget gate.
- **Channel** — `historyrecapped` (this skill is single-channel).
  Refuse if `historyrecapped/config.yaml` is missing.

### 2. Load channel context (heuristics #8 + #46)

Before drafting, run these reads. Bake the literals into the
narration; never paraphrase the closer-ask wording:

```bash
cat historyrecapped/learnings/channel.md
cat historyrecapped/learnings/long_form_channel.md
cat historyrecapped/learnings/long_form_hook_template.md
cat historyrecapped/learnings/long_form_prose_style.md
cat historyrecapped/learnings/long_form_support_asks.md
cat historyrecapped/learnings/long_form_visual_signature.md
cat historyrecapped/learnings/long_form_sources.md
cat historyrecapped/config.yaml
ls historyrecapped/narrations/                 # check for slug collision
ls historyrecapped/footage/long_sources/       # what HD source mp4s already cached
```

**Inheritance contract — these literals are LOCKED, do not paraphrase:**

| Field                  | Long-form sleep value                                                                              |
|------------------------|----------------------------------------------------------------------------------------------------|
| TTS provider           | F5-TTS-MLX (`pipeline/voice_refs/sarah.wav`, speed=0.95, atempo=0.85 → ~135 wpm)                  |
| Aspect / resolution    | 16:9 / 1920×1080 / 30 fps                                                                          |
| Captions               | sentence-level yellow italic `#FFD93D`, sans-serif, drop-shadow, bottom-center                    |
| Watermark              | "HISTORY RECAPPED" top-right, ~55% opacity white, 28pt, 32px margin                                |
| Audio mix              | narration -6 dB / music bed -28 dB / source 0.0 dB (muted for ContentID safety)                    |
| Music bed              | `historyrecapped/music/aether-loop.wav` (loop + crossfade)                                          |
| Visual grade (Path A)  | `eq=gamma=1.04:saturation=0.95,colorbalance=...` (warm-cool firelight; locked in YAML)             |
| Visual style (Path B)  | "hand-drawn graphic-novel illustrated panel … small bright yellow campfire … cool blue ambient …" |
| Asks (count + wording) | exactly 2 inline; soft early ~3-4 min, soft closer at end (see literal below)                       |
| Closer literal         | `"We put a lot of effort into bringing you this. If it helped you rest, a quiet like helps the channel, and subscribing tells us to make more like this. Sleep well."` |
| Early-ask literal      | `"We put a lot of effort into bringing you these stories. If you've enjoyed listening, a quiet like helps the channel, and subscribing tells us to make more. Thank you for staying with us."` (vary wording slightly between videos but stay in the same gentle register) |
| Upload category        | 27 (Education) — NOT a Short                                                                        |
| Privacy default        | private (promote to public after format sign-off + ContentID dispute window)                        |

**The Shorts closer (`SUBSCRIBE for more deep dives \n LIKE if you learned something`) DOES NOT carry over.** It's punchy/documentary cadence and breaks the sleep register.

### 3. Hat 1 — Researcher (build the chapter outline)

Sleep-history audiences complete on prose quality, not novel facts.
Don't pile new dates onto every paragraph; pick 8-12 chapter beats
where each chapter has:
- ONE cliché the listener already carries (the mythbust target)
- ONE concrete sensory anchor (a river, a mountain, a fortified
  village, a season's mud, a cup of bitter coffee)
- 2-4 anchor names to repeat (people, places, units) — not 20

Save the chapter outline + source list as `historyrecapped/raw/<slug>-sleep.json`:

```json
{
  "slug": "verdun-1916-sleep",
  "subject_summary": "Ten months of attritional combat at Verdun in 1916, told slowly, for sleep.",
  "anchor_phrases": ["1916", "the fort", "the Voie Sacrée", "your section"],
  "chapter_outline": [
    {"index": 0,  "title": "The country before the war", "cliché_to_bust": "Verdun was just a fortress town", "sensory_anchor": "the Meuse Heights at dawn"},
    {"index": 1,  "title": "February 21st: the bombardment", "cliché_to_bust": "the Germans achieved surprise", "sensory_anchor": "the chalk dust under the trees"},
    ...
  ],
  "sources": [
    "Alistair Horne — The Price of Glory (1962)",
    "Paul Jankowski — Verdun: The Longest Battle of the Great War (2014)",
    "BDIC archive — French Army diaries, May-October 1916"
  ],
  "footage_pool_seed": [
    "archive.org/details/Verdun-1916-pathe-newsreel",
    "youtube.com/<DroneScapes-restored-WW1-compilation>"
  ]
}
```

This dossier is the SOURCE OF TRUTH for the narration JSON. Every
named claim in the narration must trace to a `sources[]` entry.

### 4. Hat 2 — Narrator-novelist (write the prose)

Author the long-form narration in this order:

1. **Hook (first 90s, ~140-180 words at ~135 wpm).** Strict 6-beat
   structure per `long_form_hook_template.md`. Open with the literal
   word *Imagine*. Strip modern context (no alarm, no phone, no
   schedule). Sensory transport in beat 2 — light, smell, sound,
   touch — with era UNNAMED. Beat 3 anchors the era in one sentence.
   Beat 4 identifies (`you are a private in the British Expeditionary
   Force`). Beat 5 poses the central question. Beat 6 names the
   cliché the audience carries and promises to subvert it.
2. **Chapter 1.** ~700-900 words. Open with the chapter's
   cliché-to-bust. Land sensory detail in the first or second
   sentence. Repeat one of the anchor phrases.
3. **Early support ask (chapter 1 close, ~3-4 min in).** Inline soft
   ask. Use the early-ask literal (with mild wording variation per
   video to avoid feeling mechanical). Footage rolls; no animated
   screen, no music dip — just narration.
4. **Chapters 2 through N-1.** Each ~700-1000 words. One mythbust per
   chapter open. Anchor phrase repeated at chapter open + 1-2 mid-chapter.
5. **Final chapter + closer ask.** The closer ask IS the closer; do
   not add a separate "thanks for watching" outro. Use the closer
   literal (with mild wording variation).
6. **Total word budget at ~135 wpm:**
   - 60 min → 8,100 words
   - 90 min → 12,150 words
   - 120 min → 16,200 words

Sentence distribution per chapter: ~15% short (≤8 words), ~60%
medium (9-22), ~25% long (≥23 words, max 4 commas, parseable on a
single breath). Run a final pass to grep for documentary connectives
(`Furthermore`, `In conclusion`, `It is important to note`, `As
mentioned earlier`, `Studies have shown`) and rewrite — these are
kill-on-sight in sleep prose.

#### 4a. Output narration JSON schema

Write to `historyrecapped/narrations/<slug>-sleep.json`:

```json
{
  "slug": "verdun-1916-sleep",
  "title": "Verdun 1916 — Ten Months in the Mud (sleep history)",
  "hook": "Tonight, we go to Verdun. Ten months of mud, fort, and shellfire on the heights of the Meuse — told slowly, for sleep.",
  "narration": "<single string, the entire script — hook + chapters + asks + closer concatenated with double-newline paragraph breaks>",
  "chapters": [
    {"start_s": 0,    "title": "The country before the war"},
    {"start_s": 280,  "title": "February 21st: the bombardment"},
    {"start_s": 1100, "title": "Fort Douaumont, lost in an hour"},
    ...
  ],
  "sources": [
    "Alistair Horne — The Price of Glory (1962)",
    "Paul Jankowski — Verdun: The Longest Battle of the Great War (2014)"
  ],
  "title_options": [
    "Verdun 1916 — Ten Months in the Mud (sleep history)",
    "The Longest Battle of the Great War — A Calm Telling",
    "Verdun, 1916 — A Quiet History for Sleep"
  ],
  "thumbnail_brief": "graphic-novel illustrated panel: small group of French soldiers around a brazier in a chalk dugout at dawn, brazier glowing yellow at the focal point, distant cool-blue ridgeline beyond. Caption 'VERDUN 1916' bottom-left in serif."
}
```

`chapters[]` powers the upload `{chapters_block}` token (renders as
`MM:SS Title` lines in the YouTube description). Estimate `start_s`
per chapter from cumulative word count × (60 / 135).

`narration` is a SINGLE STRING — the renderer's `_split_into_chunks`
re-splits at runtime. Use double-newline (`\n\n`) for paragraph
breaks. Do NOT pre-chunk in JSON.

#### 4b. Image panels (Path B only)

If render path is `image_panels` (or hybrid), additionally emit a
`panels[]` array in the narration JSON, capped at 24:

```json
{
  ...
  "panels": [
    {
      "index": 0,
      "start_s": 0,
      "end_s": 22,
      "scene": "a small group of French infantry in horizon-blue 1916 uniforms gathered around a brazier in a chalk-walled dugout at dawn, the brazier's small bright yellow fire glowing at the center of the composition, faces lit warm, distant cool-blue trench parapet silhouette beyond, mode: bold_ink",
      "mode": "bold_ink"
    },
    ...
  ]
}
```

**Per-panel scene rules** (locked — diffusion drops the campfire if
not in the per-beat scene string, even with the prefix):
- Always name a small bright yellow warm-light source explicitly
  (campfire, brazier, oil lamp, lantern, lit torch, hearth, glowing
  forge, sun on a window).
- Pin the era + period dress (`1916 horizon-blue uniforms`,
  `WW1 leather Stahlhelm`, etc.) — diffusion drifts to anachronistic
  kit otherwise.
- Alternate `mode: bold_ink` (active scenes) and `mode: soft_painted`
  (reflective scenes) per chapter to avoid same-style fatigue.
- 18-22s hold per panel; 1.5s crossfade.
- **Cap at 24 panels** — the renderer hard-rejects more.

### 5. Hat 3 — Footage curator (write the shotlist)

For `archival_footage` (default), write
`historyrecapped/shotlist/<slug>-sleep.json`:

```json
{
  "_comment": "Italian Campaign 1943-45 sleep — single 95-min source window. ContentID dispute playbook per long_form_sources.md.",
  "slug": "verdun-1916-sleep",
  "clips": [
    {"source": "<filename in historyrecapped/footage/long_sources/>.mp4", "in_s": 30.0, "out_s": 5430.0}
  ]
}
```

**Source policy (locked, ranked by risk tolerance per `long_form_sources.md`):**

1. **archive.org PD** (480p-720p, zero risk) — Capra "Why We Fight",
   Ford "Battle of Midway", US National Archives, DVIDS. Use for
   pilots and channels where any ContentID friction is unacceptable.
2. **DroneScapes / Periscope Film / Old Videos Restored** (1080p HD,
   medium-high ContentID risk, 17 USC §105 dispute fallback) —
   **production default.** Upload private/unlisted FIRST, dispute
   any claim citing PD source.
3. **Paid stock** (Pond5, Storyblocks) — only if budget allows
   zero-risk HD.

**FORBIDDEN sources:**
- Animated-map "history" channels (e.g. WPhistory) — fake archival
- Host-on-camera documentary remixes (recurring presenter shots)
- AI-generated video (channel rule)
- Any source with on-screen modern text overlays / watermarks /
  graphic packages

**Window selection MUST be verified at 1fps before commit** — sample
each candidate region with
`ffmpeg -ss N -t 60 -i src.mp4 -vf fps=1,tile=10x6 grid.png` and read
the grid before committing in_s/out_s. Pointe du Hoc v0 had 2 of 7
windows on talking heads; v1 fixed only after 1fps inspection (memory:
`historyrecapped/learnings/channel.md`).

**Single long window vs many short windows:** sleep-history prefers
ONE continuous 30-60 min source window over many 5-7s cuts (jarring
cuts wake the viewer per `long_form_channel.md`). The Italian
Campaign episode used a single `clips: [{30.0, 5730.0}]` window
against an 83-min narration. Aim for ≥30 min unbroken segments.
**Aspect-matched 1080p sources stream-copy through `_trim_clip_letterbox`** —
do not stack the gblur+overlay chain on already-16:9 1080p input
(`historyrecapped/learnings/long_form_trim_aspect_short_circuit.md`).

For `image_panels` (Path B), the shotlist `clips: []` can be empty —
the renderer reads `panels[]` from the narration JSON. For HYBRID
(panels for hook + closer, footage for bulk), emit both `clips[]`
covering the middle and `panels[]` covering the start + end with
`start_s`/`end_s` fields gating their use.

### 6. Quality gates (heuristics #31-#38)

Run these IN ORDER. Block emit on any failure. Mechanical, not
advisory.

1. **Hook structure check** — first 90s of narration must contain (in
   this order, case-insensitive): the literal word "Imagine", a
   sensory paragraph that does NOT name the year/topic, a one-line
   era anchor, the phrase "you are" or "you're", a question mark
   posing the central question, a cliché-and-promise-to-bust line.
   Reject if any beat is missing.
2. **Banned-phrase scan** — grep narration for:
   - `"Welcome back to History Recapped"` / `"Hi everyone"` / `"Today we're going to look at"` / `"Did you know that"` (Shorts/lecture intros)
   - `"smash that subscribe button"` (memory: `spoken_subscribe_ask`)
   - `"vote in comments"` / `"COMMENT which battle next"` / `"comment which X next"` (Shorts cadence)
   - Verdict acronyms `AITA|WIBTA|YTA|NTA|NAH|ESH` (memory: `no_verdict_acronyms_in_audio`)
   - Documentary connectives `Furthermore`, `In conclusion`, `It is important to note`, `As mentioned earlier`, `Studies have shown`, `To understand X, we must first` (memory: `historyrecapped/learnings/long_form_prose_style.md`)
   - The Shorts closer literal `"LIKE to honor those who served"` / `"SUBSCRIBE for more deep dives"` / `"LIKE if you learned something"` (carryover from Shorts; wrong register for sleep)
3. **Second-person prose check** — sample 30 random sentences from
   the narration body (excluding sources, asks, hook). Count
   second-person markers (`you`, `your`, `you are`, `you're`,
   imperative `imagine`, `picture`, `listen`). At least 50% of
   sampled sentences must contain at least one. If under 50%,
   block — the prose has drifted to documentary register.
4. **Two-asks check** — count occurrences of
   `"a quiet like helps the channel"` (or close variants). Must equal
   exactly 2. Reject 1 (missing early ask), 3+ (over-asking — the
   five-asks-per-video cadence is RETIRED).
5. **Pronunciation pre-pass** — phonetic respelling for foreign /
   proper nouns BEFORE TTS. Examples that have bitten the pipeline:
   `Bar-le-Duc` (BAR-luh-DOOK), `Verdun` (vair-DUHN), `Ypres`
   (EE-pruh), `Stosstruppen` (SHTOSS-troopen), `Voie Sacrée`
   (VWAH sah-CRAY), `Volturno` (vol-TOOR-no), `Kesselring`
   (KESS-el-ring), `Bruchmüller` (BROOKH-myool-er), `Goumiers`
   (GOO-mee-ay). Save respelling map next to the narration as
   `historyrecapped/narrations/<slug>-sleep.pronounce.json` so future
   re-renders inherit it (memory: `feedback_pronunciation_pretts`).
   Apply respellings IN the narration text before render — do not
   rely on a sidecar substitution layer.
6. **Length budget** — `word_count = len(narration.split())`,
   `runtime_s = word_count * 60 / 135`. Must land in
   `[duration_target_s_min, duration_target_s_max]` from spec stage 1.
   Hard cap: `[3600, 7200]` (config `long_form.duration_target_s`).
   Reject and rewrite outside.
7. **Source-fidelity** — every named claim (specific date, troop
   count, casualty figure, named operation, named person, place
   name) must trace to `raw/<slug>-sleep.json` `sources[]` or
   `chapter_outline[]`. No ungrounded speculation, no fabricated
   quotes, no invented "according to Eisenhower's diary" lines.
8. **`/critique-audio` gate (MANDATORY before image panels render)** —
   after the first TTS chunk synthesizes, run `/critique-audio` on
   the chunk wav. TTS bugs invalidate the panels render (memory:
   `feedback_critique_audio_before_image_gen`). Even on Path A
   (no image gen), run `/critique-audio` after the first 2-3 chunks
   complete to catch pronunciation regressions before paying the
   full 60-90 min TTS bill.
9. **ContentID risk classification** — for every `clips[].source` in
   the shotlist, classify as `pd` (archive.org / DVIDS / NARA), `hd-dispute` (DroneScapes / Periscope), or `paid-stock`. If `hd-dispute`, the renderer uploads private; the user files the §105 dispute per `long_form_sources.md` playbook. Block emit on unclassified or competitor-channel sources.
10. **Aspect + voice match** — shotlist `aspect: "16:9"` (implied by
    1920×1080 output); narration JSON does NOT carry a `tts_provider`
    override (config-driven only — F5 strict per
    `feedback_long_form_strict_f5`).
11. **Panel cap (Path B / hybrid only)** — `len(panels) <= 24`. The
    renderer hard-rejects above 24 (memory:
    `feedback_long_form_image_panels_capped`).
12. **Power preflight reminder** — surface in the handoff: AC + lid
    open required; Low Power Mode hard-rejected by
    `render_long_form.py` preflight (memory:
    `feedback_long_form_power_preflight`).

### 7. Renderer handoff (kick directly per feedback_skills_kick_render_directly)

Renderer exists, fully wired. NO new code. Bash-execute in the
background:

```bash
caffeinate -dimsu .venv/bin/python -u scripts/historyrecapped/render_long_form.py \
    --channel historyrecapped --slug <slug>-sleep \
    > /tmp/render_<slug>-sleep.log 2>&1 &
```

Use `--render-mode image_panels` ONLY if the spec stage-1 path is
`image_panels` (Path B). For Path A leave it unset (config default
is `archival_footage`).

`caffeinate -dimsu` is mandatory (memory:
`feedback_long_form_render_caffeinate`); sleep kills the MLX/GPU
context silently and buffered stdout hides the crash point.
`python -u` is also mandatory (unbuffered).

Echo the absolute log path and the `pgrep -fa render_long_form`
command for the user to monitor:

```
tail -f /tmp/render_<slug>-sleep.log
pgrep -fa render_long_form
```

### 8. Report back

After both JSONs land on disk, report:

```
✓ raw dossier:    historyrecapped/raw/<slug>-sleep.json
✓ narration:      historyrecapped/narrations/<slug>-sleep.json   (<word_count> words → ~<runtime_min> min)
✓ shotlist:       historyrecapped/shotlist/<slug>-sleep.json     (<n> clips, <classification>)
✓ pronunciations: historyrecapped/narrations/<slug>-sleep.pronounce.json

quality gates:
  ✓ 6-beat hook structure
  ✓ banned-phrase scan
  ✓ second-person prose ≥50% (<actual>%)
  ✓ exactly two inline asks
  ✓ pronunciation pre-pass (<n> respellings)
  ✓ length budget (<runtime_min>m, target <min>-<max>m)
  ✓ source-fidelity (every claim traces)
  ✓ aspect + voice match (16:9 + F5 sarah)
  ✓ ContentID classification (pd | hd-dispute | paid-stock)
  ✓ panel cap (Path B only, <n> ≤ 24)
  ⏳ /critique-audio gate — run after first 2-3 TTS chunks land

power preflight (manual):
  - AC + lid open required (memory: feedback_long_form_power_preflight)
  - Low Power Mode disabled
  - close other heavy MLX processes (pgrep -fa python | grep mlx)

renderer kicked (background):
  caffeinate -dimsu .venv/bin/python -u scripts/historyrecapped/render_long_form.py \
      --channel historyrecapped --slug <slug>-sleep \
      > /tmp/render_<slug>-sleep.log 2>&1 &

monitor:
  tail -f /tmp/render_<slug>-sleep.log
  pgrep -fa render_long_form
```

### 9. Self-learning hook (heuristics #39-#44)

After the user runs `/critique-audio` or `/critique-video` on the
output:

1. Classify the regression:
   - **ONE-OFF** (single mispronunciation, one bad date, one
     anachronistic kit reference) → fix in the narration JSON, append
     a 1-line note to
     `.claude/skills/make-sleep-history/learnings/_index.md`.
   - **CLASS-OF-BUG** (every sleep-history will hit it — e.g. F5
     mangles every German u-umlaut, every chapter open drifts to
     documentary voice at the 2k-word mark, every Path B render OOMs
     at panel 18) → fix in the OWNING module:
       - Pipeline bug → `pipeline/audio.py` / `pipeline/captions.py` /
         `scripts/historyrecapped/render_long_form.py`
       - Prompt bug → this SKILL.md
       - Channel rule → `historyrecapped/learnings/<topic>.md`
     Then append a regression note to
     `.claude/skills/make-sleep-history/learnings/<topic>.md` AND
     mirror to `historyrecapped/learnings/<topic>.md` per CLAUDE.md
     dual-save rule.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in Section 6 that blocks emit on detection.
4. Update this file's `learnings_consulted:` frontmatter when a new
   project doc lands.

## Important rules

- **Closer literal:** `"We put a lot of effort into bringing you this. If it helped you rest, a quiet like helps the channel, and subscribing tells us to make more like this. Sleep well."` Vary the wording slightly between videos but never paraphrase to a different register.
- **Early-ask literal:** `"We put a lot of effort into bringing you these stories. If you've enjoyed listening, a quiet like helps the channel, and subscribing tells us to make more. Thank you for staying with us."`
- **Two asks total. Not one. Not three. Not five.** The five-asks-every-18-min cadence is RETIRED 2026-05-04.
- **Six-beat hook is non-negotiable.** Reject any narration whose first 90s starts with `"Welcome back"`, `"Today we're going to"`, or jumps straight into a year/date.
- **Second-person present-tense default.** Documentary third-person voice ("Early humans woke at dawn") is kill-on-sight in sleep prose. Slip to past tense ONLY for explicit historical attribution.
- **Sensory anchor in the first or second sentence of every paragraph.** Not "it was cold" — `"the cold settles into the small bones of your hands first."`
- **Yellow campfire visible in every panel (Path B).** Diffusion drops it without explicit per-beat scene mention. The prefix alone is not enough.
- **Footage policy:** archive.org PD (zero risk) or DroneScapes-class HD (dispute fallback per `long_form_sources.md`) only. Never YouTube re-uploads of WPhistory-class animated-map "history" channels. Never host-on-camera documentary remixes.
- **Mute source audio** (`audio_source_mix: 0.0` in YAML) — their period scoring is the most ContentID-likely component AND would compete with our soft narration.
- **No image gen on Path A.** Even if `historyrecapped/config.yaml` carries `image_provider: z_image_turbo`, do not flip to `image_panels` unless the user explicitly opts in. Path B is gated at 24 panels and historically OOMs above that.
- **Stages 4-7 belong to the renderer.** This skill produces JSON; `render_long_form.py` does TTS, ffmpeg trim/letterbox, music bed, captions, watermark, mux.
- **Always use `.venv/bin/python`** for any helper commands.
- **Always wrap render with `caffeinate -dimsu`** (memory: `feedback_long_form_render_caffeinate`). Sleep mid-render will silently kill the MLX context.
- **AC + lid open required.** Low Power Mode + heavy MLX → kernel kills WindowServer (memory: `feedback_long_form_power_preflight`).
- **Single long footage window > many short cuts.** Aim for one continuous 30-60 min source window per ~80-min narration. Jarring cuts wake the viewer.
- **Aspect-matched 1080p sources stream-copy.** Don't stack the gblur+overlay chain on already-16:9 1080p input (memory: `historyrecapped/learnings/long_form_trim_aspect_short_circuit.md`).
- **Dual-save** every learning per CLAUDE.md (memory + project doc).

## Learnings from prior runs

<!-- empty on day 1; appended after each run via Section 9 -->

## Why this skill is separate from /make-top10, /make-katha, /make-cosmos-decoder

All four skills produce long-form footage-only output and ride on
`scripts/historyrecapped/render_long_form.py` (or its
`render_footage_only.py` cousin). The reason this is a separate
skill, not a `--variant` flag on an existing one:

- **`/make-top10`** is **countdown structure** (#10 → #1 with
  continuous connective tissue) and 28-32 min duration. Sleep history
  is **second-person novelistic chapter-walk** with no rank-chips,
  60-120 min, and a sleep-cadence TTS. The chapters[] schema looks
  the same on disk but the prose contract and pacing are
  incompatible.
- **`/make-katha`** is **Hindu scripture devotional** in Hindi via
  Kokoro hf_alpha for the HindutavaAnimated channel. Different
  language, different voice, different curatorial palette
  (Wikimedia temple imagery vs WW1/WW2 archival).
- **`/make-cosmos-decoder`** is **engaged-explainer "How We Knew"**
  (Veritasium-without-host) for the cosmosdecoded channel. Its
  SKILL.md explicitly bans sleep-mode prose ("close your eyes",
  "drift") as a kill-on-sight register. Sleep history requires the
  exact register Cosmos Decoded forbids.

The shared substrate (chapter outline → 8-12 chapter narration →
F5-TTS-MLX → archival footage trim/letterbox → captions → music bed
→ mux → channel-parametric upload) is real. **See engineering-efficiency
finding in stage-2 handoff: extract `pipeline/long_form_schema.py`
across all four skills as a separate refactor PR** — the chapter +
support_asks + sources + title_options shape is now common to four
skills (was three before this one). That refactor is out of scope
for this skill's first ship.
