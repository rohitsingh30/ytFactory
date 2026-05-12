---
name: make-history-short
description: >-
  Author a 50-60s history Short for the History Recapped channel — any era, any subject (battles, disasters, expeditions, discoveries, assassinations, plagues, treaties, scientific milestones, ancient civilizations). Bifurcates by render path: archival = real archival YouTube footage (CriticalPast / Periscope Film, best for 20th-century newsreel-era events); animated = Osprey-plate illustrated panels via Cloud Run FLUX.2 klein (best for pre-photographic history). Skill asks the user to pick. 10-beat arc (place+date → stakes → subject → plan → complication → action → twist → resolution → cost → meaning → closer). Produces narrations/<slug>.json + (archival → shotlist/<slug>.json | animated → cache/<slug>/prompts.json). Use when the user says "make me a history short", "Apollo 11 short", "Pompeii short", "Cuban Missile Crisis short", or names any historical event for HistoryRecapped. For long-form sleep use /make-sleep-history. For Top-10 long-form use /make-top10.
---

# /make-history-short — 50-60s history Short for HistoryRecapped (archival OR animated)

This skill authors **one history episode** as a 50-60s YouTube Short
for **History Recapped** — any era, any subject. The channel's brand
is "history that actually mattered, cut to 60 seconds." Battles,
disasters, expeditions, scientific breakthroughs, assassinations,
plagues, treaties, ancient civilizations — all in scope. The 12 shipped
exemplars happen to be WW1/WW2 battles (the channel's pilot run); the
skill is intentionally agnostic to subject category.

**Two render paths — the skill asks the user to pick (Stage 1.5):**

- **Archival** — 100% real archival YouTube footage (CriticalPast /
  Periscope Film / public-archive). Best for 20th-century events with
  surviving newsreel: war, political moments, disasters, expeditions,
  early aviation/space. The 12 shipped exemplars all use this path.
  Output: `narrations/<slug>.json` + `shotlist/<slug>.json` (windows
  from one or more YouTube source URLs). Renders via
  `historyrecapped/scripts/render_footage_only.py`.
- **Animated** — illustrated history-book panels via Cloud Run FLUX.2
  klein, the Osprey-plate aesthetic locked in `historyrecapped/config.yaml`
  (vintage ink + watercolor wash, muted ochre/sepia/charcoal palette,
  dramatic chiaroscuro, era-and-equipment-locked per beat). Best for
  pre-photographic / pre-newsreel history: Pompeii, Hastings 1066,
  Constantinople 1453, Plague Year 1348, Mesopotamia, Inca, Aztec,
  Marco Polo, Renaissance court politics, etc. Output:
  `narrations/<slug>.json` + `cache/<slug>/prompts.json` (per-beat
  scene prompts). Renders via
  `scripts/make_shorts.py --channel historyrecapped/config.yaml --script <path>`.

You wear three hats: **Investigative Researcher → Documentary Voice
Writer → Visual Producer (archival OR animated, depending on Stage 1.5)**.

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
> `AskUserQuestion` with **2-4 prefilled options** spanning multiple
> eras and subject categories — NOT just battles. Mix from:
> 20th-century war (Iwo Jima, Inchon, Tet Offensive); disasters
> (Pompeii 79, Great Fire of London 1666, Halifax explosion 1917,
> Hindenburg 1937); discoveries / expeditions (Magellan, Apollo 11,
> Curie's radium, Wright brothers); political moments (Cuban Missile
> Crisis, fall of Constantinople 1453, signing of Magna Carta);
> plagues (Black Death 1348, 1918 flu); ancient (Hannibal at Cannae,
> Tutankhamun's tomb, sack of Rome 410). The 12 shipped Shorts happen
> to be WW1/WW2 battles — that's the pilot run, NOT the channel's
> ceiling. Stage-1 options should reflect history's full range, with
> a leaning toward the user's stated era if they named one. Never ask
> the user to type the subject — propose 3-4. Source of truth:
> [`docs/skill_prefilled_options.md`](/Users/rohit/ytFactory/docs/skill_prefilled_options.md).

### 1. Confirm subject + 4-line spec

Lock the **4-line spec** before writing anything. Ask one consolidated
clarification only if ambiguous; otherwise state your reading back in
one sentence and let the user redirect.

- **Subject** — the specific historical event / moment. e.g.
  `apollo-11-1969`, `pompeii-79`, `cuban-missile-crisis-1962`,
  `iwo-jima-1945`, `magna-carta-1215`, `curie-radium-1898`,
  `1918-flu-philadelphia`, `hindenburg-1937`. The spec must name a
  SCENE, not a span ("WW2" / "the Roman Empire" / "the Cold War" is
  too broad — pick the moment).
- **Slug** — `<event>-<year>` lowercase-hyphen. NO month/day in
  Shorts slugs. Match the shipped pattern: `pearl-harbor-1941`,
  `pointe-du-hoc-1944`. For pre-AD events use the year alone:
  `pompeii-79`, `cannae-216bc`.
- **Render path** — `archival` or `animated`. Lock this in Stage 1.5
  (next subsection) — drives the rest of the flow. Defaults: archival
  if subject ≥1900 AD AND has surviving newsreel; animated otherwise.
- **Meaning** — the one-line "why this mattered" that lands as
  beat 9 (the meaning beat just before the closer). Write this FIRST
  — the whole arc bends toward it. Examples: `"The guns of Pointe du
  Hoc never fire on the beaches."` (war); `"The footprints are still
  there, fifty years later."` (Apollo 11); `"In one summer, a third
  of Europe is gone."` (Black Death); `"The dome holds for a
  thousand years."` (Hagia Sophia / Constantinople).

Output of stage 1:

```
slug:         apollo-11-1969
render_path:  archival   # or 'animated' — locked in Stage 1.5
meaning:      "The footprints are still there, fifty years later."
```

**Refuse** if:

- The subject can't be grounded in canonical sourcing (Wikipedia,
  Britannica, US National Archives, IWM, Smithsonian, NASA HQ
  History, Library of Congress) in 60s of research. Don't fabricate.
- The subject is a contemporary event (post-2000) — channel niche is
  pre-2000 history. Modern events fail ContentID and YouTube's
  anti-spam pass for "news".

### 1.5. Pick render path — archival OR animated

This is the bifurcation. Lock it via `AskUserQuestion` if the user
hasn't already specified. The two paths produce DIFFERENT output
files and route through DIFFERENT renderers — so picking up-front
saves a re-run.

**Archival (real footage)** — pick this when:
- Subject is 1900+ AD AND has surviving newsreel / archival film.
- Strong CriticalPast / Periscope Film / public-archive YouTube
  source exists with ≥5-7 usable 8-10s windows of period-accurate
  footage (no presenters, no animated maps).
- The 12 shipped Shorts are all on this path — it's the channel's
  validated default.

**Animated (illustrated images)** — pick this when:
- Subject is pre-photographic (pre-1880s for moving footage,
  pre-1900s in practice). Pompeii, Hastings 1066, Constantinople
  1453, Black Death 1348, Magna Carta, ancient Rome / Greece /
  Egypt / Persia, medieval / Renaissance, the Age of Sail.
- Subject is in the photographic-but-not-newsreel band (1850-1900,
  e.g. American Civil War daguerreotypes, Curie's radium 1898) AND
  the user wants a more cinematic / less monochrome look than
  surviving stills.
- Subject IS post-1900 but available archival is class-4
  (animated-map "history" channels, kill-on-sight per §4) or
  class-5 (host-on-camera remixes) — i.e. the archival path can't
  source clean material. Reframe to animated.

The animated path uses
`historyrecapped/config.yaml:image_provider: cloudrun_flux2_klein`
(already configured) with `image_style_prefix` (already in the YAML —
"vintage illustrated military history book plate, ink hatching with
watercolor wash, muted military palette of khaki olive sepia
charcoal and dust-red, dramatic chiaroscuro lighting,
period-accurate uniforms and equipment, wide compositional framing,
single subject focus, no text, sober documentary tone"). For
non-military subjects (Pompeii, Curie, Magna Carta) the era +
equipment lock comes from per-beat scene strings written by the
Visual Producer hat (§4b) — same pattern as `/make-ranking`'s
per-rank `image_prompt_hint`.

**Output of Stage 1.5:** the `render_path` line in the 4-line spec is
locked to one of `archival` / `animated`. Stage 4 splits accordingly.

### 2. Hat 1 — Investigative Researcher: build a tiny dossier

For the chosen episode, gather:

- **Canonical sourcing** — Wikipedia article URL is the default
  `source_url` in the narration (it goes in the YouTube description
  via `description_template`); supplement with one or two of: US
  National Archives, IWM (Imperial War Museum), Smithsonian, Britannica,
  unit-history monographs. Pull these into a `_research_notes` field
  if non-trivial.
- **Numbers + quantities** — every History Recapped narration has at
  least 3 specific numbers (`225 men`, `2,937 pilots`, `40 minutes
  late`, `90 of 225`). Numbers are the wedge — they make the moment
  concrete. List 5-8 candidate numbers from the dossier; the writer
  hat picks 3-5.
- **Named units / equipment / commanders** — the cast of the
  narration. e.g. `2nd Ranger Battalion`, `Spitfire Mk I`,
  `T-34/76`, `Patton's Third Army`. Pin the period-accurate kit
  literally so neither narration nor (later) any caption-emoji
  pull-in drifts to anachronistic gear.
- **Pronunciation list** — every foreign place / unit / commander
  name. e.g. `Pointe du Hoc → PWAHNT-doo-OHK`,
  `Luftwaffe → LOOFT-vah-feh`, `Wehrmacht → VEHR-mahkt`,
  `Bf 109 → Bee-Eff one-oh-nine`. These go into
  `narrations/<slug>.json:_pronunciation_notes` AND, for any
  recurrence across episodes, additions to
  `pipeline/audio.py:_TATSAMA_RESPELLINGS` (English-side equivalent).

**Refuse to fabricate.** If you can't ground a number in canonical
text, leave it out. Devotional military-history viewers fact-check
harder than any other niche.

### 3. Hat 2 — Documentary Voice Writer: 10-beat narration

**Word budget:** 130-138 English words for ~54-58s narration at
Cloud Run Chatterbox / sarah.wav cadence (**measured ~145 wpm
chunked, speed 1.0** on `doolittle-raid-1942` 2026-05-07: 255-word
narration → 5 chunks → 102.58s of audio = 149 wpm). The narrow
130-138 band targets a 1-2s safety margin under the **strict
YouTube Shorts 60s cap** — 140+ words pushes the final mp4 past 60s
and risks YouTube classifying the upload as a regular video instead
of a Short (loses the Shorts shelf + algorithm). Outside 130-138 →
hard-fail in §6 length budget.

> **Cadence diagnostic (2026-05-07).** Cloud Run Chatterbox has a
> known internal cap of ~40s of audio per `/synth` call. For inputs
> over ~350 chars (≈ 60-70 words), `pipeline/tts/cloudrun.py`
> auto-chunks via `_split_for_chunked_synth` and concatenates chunks
> via ffmpeg. When chunking fires, the effective cadence is
> ~145 wpm — close to (slightly faster than) the Kokoro am_michael
> cadence the 12 shipped Shorts (`pointe-du-hoc-1944` /
> `battle-of-britain-few`) were rendered with pre-migration. When
> chunking DOESN'T fire (e.g. single-shot path, see the `.narration.
> chunks/` directory under `<channel>/cache/<slug>/` — its presence
> means chunking ran), Chatterbox silently truncates output at ~40s
> and the 175-word narration appears as a chipmunked 263 wpm. **If
> the rendered mp4 is materially shorter than the budget predicts,
> first check whether `.narration.chunks/` exists and contains 4-5
> chunk WAVs — its absence on a >350-char input indicates chunking
> didn't fire and the output is truncated.**

**Validated 10-beat template** — the arc generalizes across war,
disaster, discovery, expedition, plague, political-moment, and
ancient-civilization episodes. The role labels stay constant; only
the content slot shifts. Anchors below show three example episodes
side by side (war / disaster / discovery) so the universality is
explicit.

| Beat | Role | War anchor (Pointe du Hoc 1944) | Disaster anchor (Pompeii 79) | Discovery anchor (Apollo 11 1969) |
|---|---|---|---|---|
| 0 | **Place + date** — staccato opener | `"Pointe du Hoc. Normandy. 6 June 1944."` | `"Mount Vesuvius. Bay of Naples. 24 August AD 79."` | `"Sea of Tranquility. The Moon. 20 July 1969."` |
| 1 | **Stakes** — what hangs in the balance | `"On top of a hundred-foot cliff sit six German guns, ranged onto Omaha and Utah beaches."` | `"At the foot of the mountain, the Roman city of Pompeii — twenty thousand citizens, a port, a forum."` | `"The whole world is watching. A quarter of humanity, live."` |
| 2 | **Subject** — the actors / what's at center | `"The 2nd Ranger Battalion. 225 men."` | `"For two centuries, the volcano has been silent. A mountain, locals say. Vines on its slopes."` | `"Two men in a four-legged lander. Forty thousand engineers behind them."` |
| 3 | **Plan / setup** — concrete steps or context | `"The plan: land at the base of the cliff, fire grappling hooks from rocket launchers, climb."` | `"At one in the afternoon, a column of ash rises seventeen miles into the sky."` | `"Twelve minutes from descent. Computer alarms keep firing. Mission Control overrides them."` |
| 4 | **Complication** — the friction | `"The landing craft arrive forty minutes late. The ropes are soaked and heavy."` | `"The wind carries the ash south. By evening it falls like snow on Pompeii's roofs."` | `"With sixty seconds of fuel left, Armstrong sees the chosen landing site is full of boulders."` |
| 5 | **Action** — they commit anyway | `"They climb anyway."` | `"Some flee. Most stay."` | `"He flies past it."` |
| 6 | **Twist** — the unexpected pivot | `"The casemates are empty. The Germans had moved the guns inland."` | `"At dawn, the mountain stops shaking. Then a cloud of superheated gas pours down its slopes at sixty miles an hour."` | `"Twenty seconds of fuel left. He picks a flat patch and sets her down."` |
| 7 | **Resolution** — what actually happens | `"Two Rangers find them in an orchard and destroy them with thermite."` | `"Pompeii is buried in fifteen feet of ash. The bodies are sealed where they fell."` | `"The Eagle has landed."` |
| 8 | **Cost** — the human bill | `"Two days later, only 90 of the 225 are still able to fight."` | `"Two thousand dead. The city is lost for seventeen hundred years."` | `"Six hundred million people watch a man step off a ladder."` |
| 9 | **Meaning** — why it mattered, ONE sentence | `"The guns of Pointe du Hoc never fire on the beaches."` | `"What buried Pompeii also preserved it — an entire Roman city, frozen in a single afternoon."` | `"The footprints are still there, fifty years later."` |
| 10 | **Closer (spoken)** — LITERAL by subject category | (military) `"LIKE to honor those who served. SUBSCRIBE for more such stories."` | (general) `"LIKE if you learned something. SUBSCRIBE for more such stories."` | (general) `"LIKE if you learned something. SUBSCRIBE for more such stories."` |

**Hook formula** — the place + date opener IS the hook. No
"What if…", no "Today's story is…", no "Have you ever wondered…".
Staccato fragments are the channel's signature; full sentences
start at beat 1. The opener works for any era — `"Pointe du Hoc.
Normandy. 6 June 1944."` and `"Sea of Tranquility. The Moon. 20 July
1969."` and `"Mount Vesuvius. Bay of Naples. 24 August AD 79."` all
hit the same staccato.

**Closer rule — bifurcates by subject category.** Pick the LITERAL
string by what the episode is about, not by what feels closest:

| Subject category | Closer (LITERAL) |
|---|---|
| **Military / war** — battles, sieges, raids, military campaigns, intelligence ops in wartime | `"LIKE to honor those who served. SUBSCRIBE for more such stories."` |
| **All other history** — disasters, expeditions, discoveries, scientific milestones, political moments, plagues, treaties, ancient civilizations, biographies of non-military figures | `"LIKE if you learned something. SUBSCRIBE for more such stories."` |

The military closer references "those who served" and only fits when
the episode is genuinely about service members (combatants,
intelligence officers, medical corps in wartime). For Pompeii or
Apollo or Curie, "those who served" misreads as awkward — the
"learned something" variant lands cleaner. The 12 shipped Shorts all
used the military closer because they were all war episodes; this
bifurcation is the 2026-05-07 generalization.

Do NOT ship the deprecated variants:

- `"COMMENT below — which battle next?"` — the sports-borrowed CTA,
  rejected 2026-05-03 (channel.md retired this).
- `"LIKE if you learned something. SUBSCRIBE for more deep dives
  like this one. Could you have done it at <X>?"` — the old hybrid-
  format closer used in `battle-of-britain-few.json`, also retired.
- Any "smash that subscribe button" / "vote in comments" / "hit the
  bell icon" robotic CTA pattern.

**Banned phrasings** (kill-on-sight):

- Verdict acronyms (AITA / WIBTA / YTA / NTA / NAH / ESH) — wrong
  channel, but cross-channel banned list still applies.
- Editorializing — `"crazy story"`, `"unbelievable bravery"`,
  `"shocking turn"`, `"insane"`, `"epic"`. Let the facts hit;
  documentary voice is calm, declarative, third-person past tense.
- First-person — `"I"`, `"we"`, `"our boys"`. The narrator is a
  documentary voice, not a participant.
- Speculation — `"may have"`, `"might have been"`, `"some say"`. If
  you can't ground it, drop it.
- Anachronistic kit / vocabulary — `"M16"` in WW2, `"Stuka"` in
  1939 as a verb, `"laptop"` in any pre-1980 episode, `"protocol"`
  for pre-modern bureaucratic process. Era voice only.
- Modern operator language — `"on target"`, `"hit the LZ"`,
  `"breaching the perimeter"` (military); `"unpack this"`,
  `"deep-dive"`, `"crystallizes"` (modern-explainer drift).

**Sentence shape** — short, declarative, present tense for the
action beats (`"They climb anyway."`), past tense for the meaning
beat (`"The guns never fire."`). Vary 4-12 words per sentence; never
write a 25-word run-on.

**Numbers** — at least 3 specific numbers in the narration body
(headcount, time, distance, count of casualties). Numbers in the
first 30 words are the wedge.

### 4. Hat 3 — Visual Producer (archival OR animated)

The §1.5 `render_path` lock branches this hat. Read **§4a if archival**
or **§4b if animated** — skip the other section entirely.

---

### 4a. Archival path — source + 5-7 windows

**Source-quality class** (ranked, from
`historyrecapped/learnings/channel.md`):

1. **Best — CriticalPast / Periscope Film YouTube reuploads**
   (e.g. `MHcy4dF8P00` Pearl Harbor, `5uIjqtpcMmM` Stalingrad
   surrender, `t9tGRRa2kr8` Guadalcanal, `FmfVaVDGx-Q` Reichstag,
   `lU9MgGceeJg` Bastogne stock reel). 2-10 min each. Pure period
   newsreel, no presenter, sometimes a small watermark — acceptable
   on stock reels; captions burn over it. **Default to these.**
2. **Good — colorized HD restorations of period film**
   (e.g. `zBXanMHgoLs` Omaha 4K colorized). 20-30 min, all archival
   but a single source — can run thin if you need 7 windows.
3. **Mixed — full documentaries with chapter cards**
   (e.g. `GEJPw5gkkpU` Midway 28 min, `FbAi7UAG-rU` Kursk 17 min).
   Mostly archival but periodic title cards / officer-portrait
   inserts; **MUST verify at 1fps** to skip them. Midway windows at
   440s + 700s landed on chapter title cards in v0.
4. **Bad — animated-map "history" channels** (e.g. WPhistory
   `jfPvkPz2W64` Stalingrad). 80% red-pincer animations, not real
   footage. **Skip outright.**
5. **Bad — host-on-camera documentary remixes** (e.g.
   `izy1f7ozNlY` Guadalcanal). Recurring presenter shot every
   ~45-60s, hard to sequence around. **Skip.**

**Windows shape** — 5-7 windows × 8.5-10s each = 50-65s of footage.
Match the shipped Bastogne pattern (7 × 8.5s = 59.5s) or Pointe-du-
Hoc (7 windows variable). Each window's `match_text` is the
narration line that becomes the visual cue — pin the buildup beat,
NOT the punchline (memory:
`feedback_sports_footage_cut_at_pivot.md` — same rule applies).

**1fps verification — MANDATORY before committing windows.** Sample
each candidate region with:

```bash
ffmpeg -ss <minute>:00 -t 60 -i <source.mp4> -vf fps=1,tile=10x6 \
  /tmp/<slug>_grid_<minute>.png
```

Then `Read` the grid PNG and check every cell. Reject the window
if ≥1 of the 60 cells lands on:

- Talking-head presenter / interviewee
- Modern text-overlay title card
- Chapter break / black screen
- Animated map / motion graphic
- Network / channel watermark dominating the frame

Pointe-du-Hoc v0 had 2 of 7 windows on talking heads — the 1fps
sample is what catches it. Skip this step and you ship a regression.

**Window-duration budget vs narration WPM** — Cloud Run Chatterbox
at speed 1.0 runs ~155-165 wpm. For a 165-word narration the
silent-video window total must be ≥ `165 / 165 × 60 + 3s buffer ≈
63s`. Under-budget and `_burn_video` tpads with the LAST FRAME →
visible 5-15s frozen-tail glitch at the closer (memory:
`historyrecapped/learnings/format.md` window-duration rule).

**Windows file** at `historyrecapped/shotlist/<slug>.json` (PUT via state client):

```bash
python -m pipeline.state_client put historyrecapped shotlist <slug> <<'JSON'
{
  "_comment": "<one-line source description: source name, runtime, resolution, what's in it, watermark notes>",
  "slug": "<slug>",
  "source_url": "https://www.youtube.com/watch?v=<id>",
  "windows": [
    {"in_s":  20.0, "out_s":  28.5, "match_text": "<narration line that pins this clip>"},
    {"in_s":  90.0, "out_s":  98.5, "match_text": "..."},
    ...
    {"in_s": 470.0, "out_s": 478.5, "match_text": "LIKE to honor those who served. SUBSCRIBE for more such stories."}
  ]
}
JSON
```

The last window is the closer beat. Pick a final-frame-friendly clip
(troops marching off, smoke clearing, end of action).

---

### 4b. Animated path — per-beat scene prompts (Osprey-plate aesthetic)

Use this path when §1.5 locked `render_path: animated`. The render
goes through Cloud Run FLUX.2 klein (channel YAML
`image_provider: cloudrun_flux2_klein`) using
`historyrecapped/config.yaml:image_style_prefix` as the locked
aesthetic prefix. You author **per-beat scene strings** that the
renderer concatenates with the prefix to produce the final image
prompt. The pattern mirrors `/make-ranking`'s `image_prompt_hint`
field but is more disciplined (history is fact-checked harder than
sports rankings).

**The aesthetic is locked at the channel level — don't override.**

`historyrecapped/config.yaml:image_style_prefix` reads (literal,
copy verbatim into prompt understanding):

> "vintage illustrated military history book plate, ink hatching with
> watercolor wash, muted military palette of khaki olive sepia
> charcoal and dust-red, dramatic chiaroscuro lighting, period-
> accurate uniforms and equipment, wide compositional framing,
> single subject focus, no text, sober documentary tone (never
> video-game screenshot, never fantasy battle, never modern
> operator)"

For non-military subjects (Pompeii, Curie, Magna Carta, Apollo) the
"military palette" reads weird in the abstract but works in
practice — the muted ochre/sepia/charcoal grade keeps cross-era
visual consistency on the channel. Don't fork the prefix per
subject; fork the per-beat scene strings instead.

**Per-beat scene rubric** (similar to `/make-ranking`'s 15-25 word
hint, tightened for history fact-checking):

For each of the 11 beats (0-10 inclusive), write a `key_visual` +
`scene` pair:

- **`key_visual`** — 4-10 words. The ONE thing that has to read in
  a thumbnail. Era-and-equipment-locked. e.g. `"Roman cavalry
  charging across plain"` (Cannae 216 BC), `"astronaut foot
  pressing into lunar dust"` (Apollo 11), `"bonfire of plague
  bedding outside city wall"` (Black Death 1348).
- **`scene`** — 25-35 words. Concrete nouns + verbs + period
  detail. Pin the era explicitly. Pin equipment explicitly (see
  per-era equipment lock table below).
- Drop adjectives before nouns when trimming. No editorializing
  ("a beautiful…", "a stunning…", "majestic…"). No quoted phrases
  inside the scene string (diffusion treats them as text-to-render
  targets — Devanagari diffusion-leak rule applies cross-channel).

**Per-era equipment lock table** (extend as new eras ship):

| Era / subject | Period-accurate equipment |
|---|---|
| Roman (1st-5th c. AD) | gladius / pilum / lorica segmentata / Roman cavalry / wax tablets |
| Medieval (6th-15th c.) | mail hauberk / kite shield / longbow / trebuchet / oil lamps |
| Age of Sail (16th-19th c.) | square-rigged ship / cannon / cutlass / sextant / compass |
| Industrial / Civil War (1850-1900) | rifled musket / steam locomotive / daguerreotype camera / telegraph |
| Edwardian / WW1 (1900-1918) | bolt-action rifle / biplane / period telephone / dreadnought ship |
| WW2 (1939-1945) | M1 Garand / Spitfire / T-34 tank / Lancaster bomber / Enigma machine |
| Cold War (1945-1991) | Apollo capsule / Geiger counter / IBM mainframe / Tupolev jet |
| Antiquity (pre-Roman) | bronze spearhead / chariot / parchment scroll / oil-fired temple lamp |

**Diffusion safety rules** (literal from cross-channel learning,
applies here too):

1. **NEVER ask for in-frame text** — no signs, headlines, banners,
   newspapers-readable, plaques, monuments-with-engraved-text.
   Diffusion renders them as gibberish. Show the OBJECT (a torn
   newspaper on a desk), not the TEXT (a newspaper headline reading
   "PEARL HARBOR ATTACKED").
2. **One main subject per shot.** Never "three friends", "a group",
   "several legionaries". For multi-figure scenes use silhouette
   compositions (a solitary figure foreground + blurred crowd
   background).
3. **No quoted phrases in scene strings** — diffusion treats them
   as text-to-render targets regardless of "no text" guardrails.
   `diffusion_quoted_phrase_leak.md` cross-channel.
4. **Don't reference caption / title / text-overlay regions** in
   scene strings. The renderer overlays captions post-diffusion via
   `pipeline.captions`.

**Output of §4b** is per-beat scene prompts (the renderer's
`pipeline.images.load_prompts` schema). Each entry MUST have a
`narration_line` anchor (the literal narration sentence the beat
covers — used by the renderer to align prompts to beat splitter
output) plus `key_visual` + `scene`. PUT it via the state client
under kind `prompts` (the renderer reads back from
`gs://.../<channel>/prompts/<slug>.json` and stages it into the
local `cache/<slug>/prompts.json` it expects):

```bash
python -m pipeline.state_client put historyrecapped prompts <slug> <<'JSON'
[
  {
    "narration_line": "Pompeii. Bay of Naples. 24 August AD 79.",
    "key_visual": "Mount Vesuvius silhouette at dawn, smoke rising",
    "scene": "wide compositional framing, the volcano's cone in the upper third against a muted dusk sky, small terracotta-roofed Roman city in the lower third, single small figure on a hilltop foreground, period-accurate Roman tunic"
  },
  {
    "narration_line": "At one in the afternoon, a column of ash rises seventeen miles into the sky.",
    "key_visual": "vast ash column boiling upward over the bay",
    "scene": "low-angle perspective looking up at a colossal pyroclastic ash column, tendrils of dust catching afternoon sun, Roman fishing boats in foreground silhouette, sepia and charcoal palette"
  }
]
JSON
```

Beat 0 (the place+date opener) and the closer beat both get `scene`
strings — beat 10's closer image is composited with the closer
panel by `pipeline.captions.render_closer_panel`, so leave the
closer scene general (e.g. "a hand laying flowers on a stone marker
in late-afternoon light" for the military closer; "a single
illuminated archive page on a desk" for the general closer).

**Window-duration budget** is N/A for the animated path — image-gen
produces one still per beat, the renderer cuts to it for
`beat[i].duration_s` from beat-split output. The mp4 length is
driven by the narration TTS length (same as archival path) +
closer hold. So the §6.10 window-duration gate is replaced by §6.10b
"prompts-per-beat coverage": every beat with non-zero duration must
have a `prompts.json` entry whose `narration_line` matches.

---

### 5. Output schema — `narrations/<slug>.json` (both paths)

PUT this as a `NicheVideo` payload (`niche: "history-short"`) via the
state client. The full envelope is in
[`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md);
the history-short-specific fills:

- `channel`: `"historyrecapped"`
- `niche`: `"history-short"`
- `slug`: `<event>-<year>` (e.g. `apollo-11-1969`)
- `title.options`: 3 candidate titles
- `hook`: one-line pitch (same staccato as beat 0)
- `narration.text`: full 130-138 word narration including the literal closer
- `narration.word_count`: integer (validated against [130, 138])
- `beats[]`: 11 beats with roles `place_date`, `stakes`, `subject`, `plan`,
  `complication`, `action`, `twist`, `resolution`, `cost`, `meaning`, `closer`
  (this exact role list is enforced by `validation.required_beat_roles`)
- `closer.spoken`: literal closer string (must match `validation.closer_literal_options`)
- `closer.style`: `"military"` for war/intelligence/service episodes; `"general"` otherwise
- `render.pipeline`: `"footage_only"` for archival, or `"shorts"` for animated
- `render.aspect`: `"9:16"`
- `render.tts_provider`: `"cloudrun_chatterbox"`, `tts_voice: "sarah.wav"`
- For `archival` path, every `beats[i].footage_window` is the YouTube clip pinned to that beat
- For `animated` path, every `beats[i].key_visual` + `scene` drive image gen
- `metadata.sources`: at least one Wikipedia URL
- `metadata.pronunciation_notes`: phonetic respellings per Hat 1
- Pre-fill `render` + `validation` defaults via `pipeline.niche_schema.niche_defaults("history-short")`

Then PUT:

```bash
python -m pipeline.state_client put historyrecapped narrations <slug> <<'JSON'
{ ... NicheVideo payload ... }
JSON
```

If the state API returns 422, read the structured error list and fix
the payload. The schema validates: required fields, beat ordering,
word count band, closer-literal match, banned-phrase scan, required
beat roles. No pre-validation in this skill is needed.

**Important schema rules:**

- `hook` ≠ first sentence of `narration`. The hook is a one-line
  pitch used as the YouTube description's opening line via the
  channel's `description_template`. The narration's first beat is
  the staccato place+date opener.
- `narration` MUST contain the literal closer string verbatim as
  its final sentence(s). The visual closer panel from
  `pipeline.captions.render_closer_panel` does NOT satisfy the gate;
  the spoken narration must contain it too. (Same rule as the AITA
  closer in `feedback_aita_closer_panel.md`.)
- `source` is always `"manual:historyrecapped"` — there is no
  auto-pull adapter for this channel.
- `_pronunciation_notes` is optional but strongly recommended;
  every foreign name causes a Cloud Run Chatterbox or local F5-TTS
  drift if not respelled.

### 6. Quality gates — mechanical, BEFORE renderer handoff

Run each gate against `narrations/<slug>.json` AND
`shotlist/<slug>.json`. Block on hit; fix in place or skip the
script.

1. **Closer literal match.** Last sentence(s) of `narration` MUST
   contain the literal string `"LIKE to honor those who served.
   SUBSCRIBE for more such stories."` Reject the deprecated variants
   (`"COMMENT below — which battle next?"`,
   `"LIKE if you learned something. SUBSCRIBE for more deep dives
   like this one. Could you have done it at <X>?"`). Block on hit.
2. **Banned-phrase scan.** Reject:
   - `"smash that subscribe button"`, `"vote in comments"`,
     `"hit the bell icon"` — robotic CTAs.
   - First-person pronouns in the narration body (`"I"`, `"we"`,
     `"our"`) — channel is third-person documentary voice.
   - Editorializing adjectives — `"crazy"`, `"unbelievable"`,
     `"shocking"`, `"insane"`, `"epic"`. Let the facts hit.
   - Verdict acronyms (AITA / WIBTA / YTA / NTA) — wrong channel,
     but cross-channel banned.
   - "Hi guys", "today's story is", "in this video" — vlogger
     openers; the place+date IS the opener.
3. **Hook gates** (channel-specific, NOT the AITA gates from
   `pipeline/script_check.py` which are opt-out for historyrecapped
   per `config.yaml:script_check_strict: false`):
   - **Beat 0** must be a staccato place+date fragment. ≥2 short
     sentences. e.g. `"Pointe du Hoc. Normandy. 6 June 1944."`
   - **First 30 words** must contain at least 1 specific number /
     quantity (headcount, distance, time). The wedge.
4. **Pronunciation pre-pass.** Every foreign place / unit /
   commander name in the narration must appear in
   `_pronunciation_notes` with a phonetic respelling, OR already
   exist in `pipeline/audio.py:_TATSAMA_RESPELLINGS` (the English-
   side equivalent — extend if not).
5. **Length budget.** `narration` word count must be **130-138**
   English words (≈54-58s narration → ~56-59s mp4 at Cloud Run
   Chatterbox's measured ~145 wpm chunked, speed 1.0 — see
   calibration note in §3). Hard fail outside. The narrow band
   leaves a 1-2s safety margin under the strict YouTube Shorts 60s
   cap. The earlier 220-265 single-shot calibration was a
   measurement artifact (Chatterbox truncates single-shot at ~40s)
   — do NOT use.
6. **Numbers gate.** `narration` body must contain ≥3 specific
   numbers (headcount, time, distance, casualty count). Numbers
   are the channel's wedge.
7. **Source-fidelity check.** Every named claim (units,
   commanders, casualty counts, dates) must trace to Wikipedia /
   Britannica / IWM / NA. Reject ungrounded claims. Use the dossier
   from Hat 1 as the audit trail.
8. **ContentID / source-quality scan.** `shotlist.source_url` must
   resolve to a CriticalPast / Periscope Film / public-archive
   class-1 or class-2 source per §4. Class-4 (animated maps) +
   class-5 (host-on-camera remixes) hard-fail.
9. **1fps source verification.** Each window in `shotlist.windows`
   must have been visually verified via the `ffmpeg fps=1,tile=10x6`
   grid sample (§4). The skill MUST run + read at least one grid
   per source URL before emit. Block emit if a grid hasn't been
   read in this session.
10. **Window-duration budget.** Sum of window durations
    (`out_s - in_s`) must be ≥ `narration_word_count / 145 × 60 + 3s
    buffer` (using Chatterbox's measured ~145 wpm chunked, speed 1.0
    — see §3 calibration note). For 145 words: ≥ 63s windows. For
    130 words: ≥ 57s. Under-budget hard-fails (frozen-tail glitch at
    closer).
11. **Aspect + voice + render-style match — branches by §1.5
    `render_path`.** Assert always:
    `config.yaml:output_resolution == [1080, 1920]` and
    `tts_provider == "cloudrun_chatterbox"` (with local F5-TTS
    fallback OK per `pipeline/tts/cloudrun.py`).
    Then by render path:
    - `archival` → assert `config.yaml:render_style == "footage_only"`
      (the channel default). The renderer is
      `historyrecapped/scripts/render_footage_only.py`.
    - `animated` → assert `config.yaml:image_provider ==
      "cloudrun_flux2_klein"`. The renderer is
      `scripts/make_shorts.py --channel historyrecapped/config.yaml`.
      Note: the channel YAML's top-level `render_style: footage_only`
      is overridden when `make_shorts.py` runs (it goes through the
      image-gen path regardless, since it reads
      `image_provider:` not `render_style:`). If a future YAML
      schema starts gating `make_shorts.py` on `render_style:`,
      pass `--render-style image_gen` explicitly.

    Drift blocks emit.
12. **/critique-audio gate (recommended).** TTS bugs (acronym
    mispronunciation, place-name mangling, robotic closer delivery)
    invalidate downstream caption + footage work. For brand-new
    foreign names (Bar-le-Duc, Caen, Pripyat) run `/critique-audio`
    on the synthesized wav before greenlighting the full render.
    Memory: `feedback_critique_audio_before_image_gen.md`.

### 7. Renderer handoff — Bash-execute, don't redirect to the website

Per `feedback_skills_kick_render_directly.md`: skills must
Bash-execute the renderer themselves with `run_in_background: true`.
Don't tell the user to open the website.

**Branch by §1.5 `render_path`:**

#### 7a. Archival path

```bash
.venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_footage_only.py -- \
  --channel historyrecapped \
  --slug <slug>
```

The renderer will:

1. Synthesize TTS via Cloud Run Chatterbox + sarah.wav (from
   `config.yaml:tts_voice`); falls back to local F5-TTS on cloud
   failure.
2. Whisper-transcribe + word-PNG render via `pipeline.beats` +
   `pipeline.captions`.
3. yt-dlp the source URL into `historyrecapped/footage/sources/<id>.mp4`
   (cached; subsequent renders reuse). Memory:
   `feedback_footage_yt_dlp_canonical.md` — yt-dlp is the canonical
   path; never Playwright for footage.
4. ffmpeg-trim the 5-7 windows with blurred 9:16 letterbox, mute
   source audio (`audio_mix: 0.0`), uniform 30 fps.
5. Concat windows + mux narration on top + burn captions with
   cut-aware clamping (each word ends 100 ms before its next scene
   cut so captions don't bleed).
6. Inline-flag emoji composite on relevant words (twemoji 72×72 PNGs
   side-pasted; Apple Color Emoji.ttc renders blank through PIL
   freetype on macOS).
7. Source-channel watermark scrub per
   `historyrecapped/learnings/footage_scrub_watermarks.md` and
   memory `feedback_scrub_footage_watermarks.md`.
8. Output `historyrecapped/shorts/<slug>.mp4`.
9. Auto-upload only if `--upload` is passed (channel
   `auto_upload: false` by default; user reviews + publishes).

Render time: ~1-2 min total on a warm pipe (footage_only is fast —
no AI image gen, just ffmpeg trim/concat + Cloud Run TTS).

**Archival path supports parallel renders.** The GPU one-render-at-
a-time rule (memory: `feedback_gpu_one_render_at_a_time.md`)
applies only to image-gen paths. Footage-only + Cloud Run TTS can
run multiple slugs in parallel without thrashing unified memory.

#### 7b. Animated path

```bash
.venv/bin/python -m pipeline.skill_dispatch render \
  --channel historyrecapped/config.yaml \
  --script historyrecapped/narrations/<slug>.json
```

The renderer will:

1. Synthesize TTS via Cloud Run Chatterbox + sarah.wav (same as
   archival path).
2. Whisper-transcribe + beat split via `pipeline.beats`.
3. Read `cache/<slug>/prompts.json` (authored in §4b). The renderer
   skips the LLM prompt-authoring step in `pipeline/prompts.py`
   because the file already exists. Each entry's `narration_line`
   anchor binds the prompt to the matching beat.
4. Generate one image per beat via Cloud Run FLUX.2 klein
   (Osprey-plate `image_style_prefix` auto-prepended). Falls back
   to local mflux Z-Image-Turbo on cloud failure via the
   render-level circuit breaker (memory:
   `feedback_image_cache_content_hash.md`,
   CLAUDE.md § "Render-level circuit breaker").
5. Compose images into beat-segments matched to TTS chunk
   durations, mux narration on top, burn captions, overlay closer
   panel.
6. Output `historyrecapped/shorts/<slug>.mp4`.
7. Auto-upload only if `--auto-upload` is passed.

Render time: ~6-12 min on a warm pipe (Cloud Run FLUX.2 klein
sub-second per image at 4 steps; ~30 images × 4-8s = 2-4 min image
gen + TTS + compose).

**Animated path runs in PARALLEL by default.** Cloud Run FLUX.2
klein scales horizontally — kick all N animated renders via
separate `run_in_background=true` Bash calls (memory:
`feedback_parallel_bulk_renders.md`). The local-mflux fallback
edge-case is real but rare: only triggers when Cloud Run is
simultaneously down, AND in that case the render-level circuit
breaker in `pipeline/images.py` trips on first failure so a single
render doesn't pay 30× the timeout. If a cloud outage IS active
and you've confirmed the breaker is tripping, fall back to serial.
Otherwise parallel.

### 8. Self-learning hook

After the user runs `/critique-audio` on the wav OR `/critique-video`
on the final mp4:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo in this script, this slug's window 4 landed
     on a chapter card) → fix in
     `historyrecapped/{narrations,shotlist}/<slug>.json` and
     re-render the affected stage; append a 1-line note to
     `.claude/skills/make-history-short/learnings/_index.md`.
   - **CLASS-OF-BUG** (every future History Recapped Short will hit
     this) → fix in the right place:
     - New foreign-name pronunciation collapse → extend
       `pipeline/audio.py:_TATSAMA_RESPELLINGS` (English-side) AND
       update the channel's pronunciation cheat-sheet.
     - New deprecated closer variant slipping through → tighten
       §6.1 closer-literal match.
     - New animated-map / host-on-camera channel discovered as a
       bad source class → extend
       `historyrecapped/learnings/channel.md` source-quality classes
       AND the §6.8 ContentID scan.
     - New caption-cut bleed pattern → extend
       `pipeline/captions.py` cut-aware clamping.
     - First-frame freeze on the closer → tighten §6.10 window-
       duration budget formula.
   - Then append a regression note to
     `.claude/skills/make-history-short/learnings/<topic>.md` AND
     mirror to `historyrecapped/learnings/<topic>.md` per CLAUDE.md
     dual-save rule.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in §6 that blocks emit on detection.

**FIRST-RUN watch-list** (most likely first-bug surfaces, derived
from the 12 shipped exemplars' regression history):

- New foreign name not in `_TATSAMA_RESPELLINGS` →
  Cloud Run Chatterbox / F5-TTS drift on `Bar-le-Duc`, `Caen`,
  `Pripyat`, `Tobruk`, `Kohima`. Add per-story to
  `_pronunciation_notes` first; if the name recurs across episodes,
  promote to `pipeline/audio.py`.
- Window-duration under-budget → 5-15s frozen-tail glitch at the
  closer. Bump windows to 10s each OR raise `tts_speed: 1.2` (note:
  speed bump retunes prosody — verify with `/critique-audio`).
- Window 4-5 lands on a chapter title card from a class-3 mixed-
  documentary source → 1fps verification missed. Re-sample at 0.5s
  granularity around the suspect minute.
- Closer drift to the deprecated `"COMMENT below — which battle
  next?"` variant → §6.1 catches it; update if a new deprecated
  variant emerges.

### 9. Report back

```
✓ wrote narration: gs://ytfactory-prod-v2-state/historyrecapped/narrations/<slug>.json
✓ wrote shotlist:  gs://ytfactory-prod-v2-state/historyrecapped/shotlist/<slug>.json
  subject:        <event-year>
  slug:           <slug>
  beats:          11 (place+date → stakes → mission → plan → complication → action → twist → resolution → cost → meaning → closer)
  word count:     <N> English words → ~<dur>s @ 160 wpm
  numbers:        <list 3-5 specific numbers in narration body>
  meaning beat:   "<one-line meaning>"
  closer:         "LIKE to honor those who served. SUBSCRIBE for more such stories."  ✓ literal match
  source class:   <1 CriticalPast / 2 colorized HD / 3 mixed doc>
  source URL:     <youtube url>
  windows:        N × ~<dur>s = <total>s footage  (budget ≥ <required>s ✓)
  1fps verified:  <list which minutes were grid-sampled>

next:
  .venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_footage_only.py -- \
    --channel historyrecapped \
    --slug <slug>
  [running in background — bash_id <id>]

audio gate first (recommended for new foreign names):
  same command with `--stop-after audio` (if supported), then run
  /critique-audio on the resulting wav. Caption + footage burn
  proceed only after pronunciation passes.
```

If any quality gate fails, list the failures and STOP. Do not
silently ship a History Recapped Short with a deprecated closer,
under-budget windows, or windows that land on talking heads.

## Important rules

- **100% archival YouTube footage** — NO AI image gen, NO illustrated
  plates, NO sports cut-in format. Channel-locked since 2026-05-03
  (`historyrecapped/learnings/format.md`).
- **Closer is the LITERAL string** `"LIKE to honor those who served.
  SUBSCRIBE for more such stories."` — both deprecated variants
  (`"COMMENT below — which battle next?"` and the long
  `"LIKE if you learned something…Could you have done it at <X>?"`)
  block emit.
- **10-beat documentary arc** — place+date → stakes → mission → plan
  → complication → action → twist → resolution → cost → meaning →
  closer. Beat 0 is a staccato place+date fragment, NOT a question
  or a vlogger opener.
- **Numbers are the wedge** — ≥3 specific numbers in the narration
  body. Headcount, time, distance, casualty count. First-30-words
  must include one.
- **Third-person documentary voice** — no `"I"`, `"we"`, `"our"`. No
  editorializing (`"crazy"`, `"unbelievable"`, `"shocking"`). Calm,
  declarative, era-accurate language.
- **5-7 footage windows × 8.5-10s each** = 50-65s of footage. Total
  windows budget MUST be ≥ `narration_words / 165 × 60 + 3s`. Under-
  budget → frozen-tail glitch at the closer.
- **1fps source verification before committing windows.** Sample
  every candidate minute with
  `ffmpeg -vf fps=1,tile=10x6` and `Read` the grid. Pointe-du-Hoc
  v0 had 2 of 7 windows on talking heads — the grid catches it.
- **Source-quality classes** — default to CriticalPast / Periscope
  Film reuploads (class 1). Class 4 (animated maps) + class 5
  (host-on-camera remixes) are kill-on-sight.
- **Source audio is muted** (`audio_mix: 0.0`). Unlike sports — where
  the broadcast commentator's call IS the payoff — documentary
  sources have their own competing narration that conflicts with
  ours.
- **yt-dlp is the canonical footage download** — never Playwright,
  never browser-automation hacks. Memory:
  `feedback_footage_yt_dlp_canonical.md`. Auth via
  `YTFACTORY_YTDLP_COOKIES` env var > browser extraction >
  anonymous.
- **Source-channel watermark scrub is mandatory** — Shorts AND
  long-form must strip source-channel logos before render
  (`historyrecapped/learnings/footage_scrub_watermarks.md` +
  memory `feedback_scrub_footage_watermarks.md`).
- **Every word PNG cache key is index-based, not content-hashed.**
  Re-rendering after a narration edit requires deleting
  `historyrecapped/cache/<slug>/{narration.wav,narration.voice.json,beats.json,word_*.png}`
  — stale word PNGs survive otherwise and produce mismatched
  captions (memory: `historyrecapped/learnings/format.md`).
- **Always use `.venv/bin/python`** for any helper commands.
- **Never run TTS / ffmpeg in this skill** — that's
  `pipeline.render.footage_only`'s job. This skill produces JSON
  only (and runs the 1fps grid sampler for verification).
- **Never call the Anthropic SDK directly** — LLM calls go through
  `pipeline/llm.py` (`claude -p`) per memory
  `feedback_llm_via_claude_cli.md`.
- **Skill kicks the renderer directly** via `Bash` with
  `run_in_background: true` — don't redirect the user to the
  website (`feedback_skills_kick_render_directly.md`).
- **Auto-upload defaults to OFF** — user reviews + publishes via the
  website. Only flip `--upload` when the user says "ship it".

## Learnings from prior runs

<!-- empty on day 1; appended on every regression via §8 -->

## Why this skill is separate from /make-sleep-history and /make-top10

`/make-sleep-history` is the same channel but **long-form** (60-120
min, 16:9, F5-TTS Sarah, warm-firelight grade or Z-Image graphic-
novel panels with yellow campfire anchor, sentence-level italic
yellow captions, 2 inline support asks). The schema, renderer,
duration band, voice, aspect, and visual signature are all
incompatible with 50-60s archival Shorts.

`/make-top10` is also long-form (28-32 min, 16:9, footage-only
Top-10 countdown). Schema (`ranks[]` array of 10 entries with
ranked subjects) and duration band are incompatible with single-
story Shorts.

Both share the underlying `pipeline.render.footage_only` renderer
infrastructure, the `pipeline.audio` Cloud Run / F5-TTS clients, and
the `pipeline.captions` cut-aware clamping. **Engineering-efficiency
win flagged for follow-up:** extract a shared
`pipeline/footage_short_schema.py` once 3 channels ship single-story
50-60s footage Shorts (currently: historyrecapped via this skill).
For now, the schema lives inline in this SKILL.md.

This skill replaces the retired `/make-script` for the historyrecapped
channel. `/make-script` was a cross-channel niche miner that produced
generic 50-80 word narrations from Reddit / wiki / TIH / YouTube
transcripts, with no per-channel curation rules. The 12 shipped
History Recapped Shorts were authored manually using `/make-script`
output as a starting point, with the curator hand-applying the
10-beat arc + closer literal + 1fps source verification + window-
duration budget. This skill bakes those rules in as mechanical quality
gates so the next 50 episodes don't drift.

learnings_consulted:
  - historyrecapped/learnings/channel.md (locked 100%-archival format, source-quality classes, shipped 12 episodes, 1fps verification rule)
  - historyrecapped/learnings/format.md (no sports cut-in, source-audio muted, cut-aware caption clamping, twemoji inline composite, window-duration vs narration WPM budget, cache invalidation rules)
  - historyrecapped/learnings/footage_scrub_watermarks.md (Shorts AND long-form must scrub source-channel logos)
  - historyrecapped/config.yaml (cloudrun_chatterbox + sarah.wav, 1080x1920, render_style: footage_only, script_check_strict: false, closer_format visual panel)
  - historyrecapped/narrations/pointe-du-hoc-1944.json (canonical 10-beat reference exemplar)
  - historyrecapped/narrations/battle-of-britain-few.json (second exemplar — note the deprecated old-hybrid closer that this skill blocks)
  - historyrecapped/shotlist/bastogne-1944.json (canonical 7-window shotlist exemplar with class-1 CriticalPast source)
  - historyrecapped/shotlist/pointe-du-hoc-1944.json (variable-duration windows + closer-line match_text exemplar)
  - pipeline/render/footage_only.py (channel-agnostic renderer; --channel <slug> --slug <slug>)
  - pipeline/audio.py (Cloud Run Chatterbox + sarah.wav clone; local F5-TTS fallback; _TATSAMA_RESPELLINGS English-side respelling table)
  - pipeline/captions.py (cut-aware caption clamping; word PNG render; inline emoji composite)
  - pipeline/footage.py (yt-dlp canonical download path; -r 30 fps unification)
  - pipeline/llm.py (claude -p for hook + narration drafting; never Anthropic SDK)
  - feedback_footage_yt_dlp_canonical.md (yt-dlp > Playwright for footage)
  - feedback_scrub_footage_watermarks.md (Shorts + long-form watermark scrub)
  - feedback_skills_kick_render_directly.md (Bash-execute renderer; no website handoff)
  - feedback_critique_audio_before_image_gen.md (audio gate first when in doubt)
  - feedback_engineer_class_of_bug.md (one-off vs class-of-bug classification)
  - feedback_dual_save_memory_and_docs.md (project doc + memory entry)
  - feedback_pronunciation_pretts.md (phonetic respelling pre-TTS)
  - feedback_skill_description_1024_cap.md (frontmatter description ≤1024 chars)
  - feedback_skill_description_yaml_colon_safety.md (use `description: >-` if it contains `key: value` patterns; heuristic 30c)
  - shorts_caption_emoji_density.md (one emoji per 3-5 spoken words; cross-channel)

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
