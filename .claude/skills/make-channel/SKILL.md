---
name: make-channel
description: Spin up a NEW ytFactory channel — author `pipeline/channels/<slug>.yaml` + the first variant overlay under `pipeline/variants/<slug>/<variant>.yaml`, scaffold `data/<slug>/learnings/`, and register the slug in `pipeline/channels.yaml` (in-rotation or out-of-rotation per the user's choice). Walks the user through 9 closed-form AskUserQuestion stages — slug + display name + handle, niche, output format, audio mode, visual mode, source adapter(s), TTS provider, first-variant niche, branding — then enforces the ≥60-word style + ≥50-word character richness gate before writing. Use when the user says "make me a new channel", "create channel for <niche>", "add a YouTube channel called <name>", "spin up channel X", "add a new ytFactory channel". For DRIVING the YouTube create-channel UI (sign in, click through Google forms) use `/create-youtube-channel` instead — that skill creates the actual YouTube account; THIS skill scaffolds the ytFactory repo config that lets the pipeline render for that account.
---

# /make-channel — scaffold a new ytFactory channel

This skill spins up a brand-new ytFactory channel from scratch. It is the
repeatable workflow promised in `/ai/onboarding-qa.md` Q12 ("future
workflow: user will ask me to create new channels") and is the P6.2
action item in `/ai/refactor-plan.md`.

Boundaries (per `/ai/onboarding-qa.md`):

- **3 visual modes** — AI image-gen (Z-Image-Turbo) / motion video / archival footage (Q17-Q18)
- **2 audio modes** — TTS narration / sung Suno song (Q29)
- **2 output formats** — 9:16 Shorts (≤90s) / 16:9 long-form (5-120 min) (Q30)
- **3+ source types** — Reddit auto-pull / Wiki+archive / manual / multi-source mix (Q31, Q74 P5.3)
- **"new channel ready" = YAML + variants + branding** (Q38)
- **All 7 channels must be functional** — no paused channels (Q14)
- **Z-Image-Turbo is the canonical image model** — never `klein` (Q67, locked principle #6)
- **LLM as editor on real material** — every channel must declare ≥1 source adapter (Q42, locked principle #2)

You wear two hats: **Configurator → Scaffold Author**. Configurator
gathers 9 closed-form decisions; Scaffold Author writes the four files
on disk and prints the manual-step checklist.

You become a CHANNEL ARCHITECT — your job is not "ask the user a series
of trivia questions" but to design a channel that fits within the
ytFactory production envelope (one of the 3 visual modes × one of the 2
audio modes × one of the 2 formats × one of the source types), then
encode that design into the four canonical files. Wrong answers in the
configurator stage produce a broken channel; that is why every stage is
closed-form (AskUserQuestion with 2-4 prefilled options).

## How to run it

### Stage 0 — Confirm intent + dedup

Before any AskUserQuestion: read `pipeline/channels.yaml`. If the
user-implied slug already exists there, STOP and tell the user "channel
`<slug>` already exists; did you mean to add a variant via
`/make-skill`, or rename the slug?" Do not silently overwrite.

If the user says "create channel" but obviously means "create the
YouTube account on Google", redirect them to `/create-youtube-channel`
and STOP. The two skills compose: `/create-youtube-channel` creates the
Google-side account; `/make-channel` (this skill) creates the
ytFactory-side config. The user usually wants both, in that order.

### Stage 1 — Slug + display name + handle (AskUserQuestion)

Ask the user three values in one AskUserQuestion block. Closed-form
where possible; the slug must be kebab-case, single-word preferred,
lowercase, ASCII only — match the convention of the existing 7
(`historyrecapped`, `cosmosdecoded`, `mystoriesanimated`, …). Reject
slugs with spaces, capitals, underscores, or punctuation other than
hyphen.

```
slug:         <kebab-case, single word preferred>      e.g. "cookingstoriesanimated"
display_name: <YouTube channel display name>           e.g. "Cooking Stories Animated"
handle:       <YouTube @handle>                         e.g. "@cookingstoriesanim"
```

Hard-fail if `pipeline/channels/<slug>.yaml` already exists on disk.

### Stage 2 — Niche / topic area (AskUserQuestion, single sentence)

One sentence describing what this channel is ABOUT. This becomes the
top comment block in the channel YAML and the seed for the
`image_style_prefix` + `character_description` blocks. Examples:

- "Hindi-language Mahabharat episodes for a faith-curious audience"
- "Physics+space how-we-knew deep dives for a science-curious adult audience"
- "Top-5 sports countdowns + tweet-reaction Shorts for football fans"

If the answer is vague ("just videos"), reject and ask again — every
channel rule downstream is derived from this sentence.

### Stage 3 — Output format (AskUserQuestion, 3 options)

```
Option A: Shorts only        (9:16, 50-60s; duration_target_s: [50, 60])
Option B: Long-form only     (16:9, 5-120 min; long_form: block, no Shorts top-level)
Option C: Both               (Shorts top-level + long_form: block)
```

Locked to the 2 output formats in Q30. No third aspect ratio, no fourth
duration band — Shorts and long-form are the only shapes the pipeline
renders.

### Stage 4 — Audio mode (AskUserQuestion, 3 options)

```
Option A: TTS narration              (tts_provider + tts_voice in YAML)
Option B: Sung Suno song              (audio_provider: sunoapi)
Option C: Mixed — sung + TTS bridges  (rhymetimejunction-style)
```

Locked to the 2 audio modes in Q29. If Option B/C, the channel inherits
the `rhymetimejunction.yaml` Suno wiring (see lines 41-80 of that file)
and the user MUST also pick a Suno style prompt template in Stage 7. If
Option A, proceed to Stage 7 for TTS provider selection.

### Stage 5 — Visual mode (AskUserQuestion, 4 options)

```
Option A: AI image-gen   (Z-Image-Turbo, image_provider: cloudrun_z_image_turbo)
Option B: Motion video    (image-to-video via motion_provider; rhymetimejunction-style)
Option C: Archival footage (render_style: footage_only; shotlist sidecar required per slug)
Option D: Mixed — per variant (top-level: AI image-gen; variants can override per niche)
```

Locked to the 3 visual modes in Q17-Q18. Never reference `klein`,
`flux2`, `qwen`, or `hidream` — those services have been retired
(CLAUDE.md, locked principle #6 in onboarding-qa.md). The ONLY image
provider for new channels is `cloudrun_z_image_turbo`.

If Option C, the channel YAML gets `render_style: footage_only` +
`narrator_visual_mode: voice_only` (matches `cosmosdecoded.yaml` and
`historyrecapped.yaml`). The user must also commit, in Stage 6, to a
source palette (NASA / Wikimedia / archive.org / paid stock) — image-gen
is OFF for that channel by design and must never be flipped on later.

### Stage 6 — Source adapter(s) (AskUserQuestion, 5 options)

```
Option A: Reddit auto-pull       (source_adapter: reddit_video; per-niche subreddit picked in Stage 8)
Option B: Wiki + today-in-history (source_adapter: wiki_oddities / today_in_history / per-niche)
Option C: Manual curator          (source_adapter: <slug>_manual; user hand-authors raw JSON per slug)
Option D: YouTube long-form mine  (source_adapter: youtube_longform_mine; clone-video pipeline)
Option E: Multi-source mix         (per Q74 P5.3 — declared in variant YAML, multiple adapters)
```

Locked to the 3 source types in Q31 + the multi-source mixing tech-debt
item in Q74. If Option E, the channel YAML still names one
`source_adapter:` (typically `<slug>_manual` as the harness), and EACH
variant YAML declares its own `sources:` list. The LLM rewrite stage
synthesizes across them (per locked principle #2 — LLM as editor on
real material).

### Stage 7 — TTS provider per voice (AskUserQuestion, 4 options)

Only ask this if Stage 4 = A or C (any TTS path at all).

```
Option A: cloudrun_chatterbox  (English, sarah.wav by default — used by 5 of 7 channels)
Option B: cloudrun_indicf5      (Hindi/Indic, hindi-female-storyteller-calm by default)
Option C: Mixed per-niche      (variant YAMLs override top-level — e.g. English+Hindi mixed channel)
Option D: Defer — pick via /voice-bench  (top-level emits cloudrun_chatterbox placeholder + checklist item)
```

If Option B, the user must also confirm via AskUserQuestion that the
channel's narration LANGUAGE is non-English (Hindi / Hinglish / other
Indic). Wrong-language TTS produces gibberish (memory:
`project_indicf5_ref_audio_text_bug`).

Voice-ref WAVs live at `pipeline/voice_refs/`. Available:

- `sarah.wav` — female English, default for 5 of 7 production channels
- `michael.wav` — male English, sports-doc default
- IndicF5 catalog names (resolved server-side): `hindi-female-storyteller-calm`, `hindi-female-storyteller-dramatic`, `hindi-female-storyteller-devotional`

If the user wants a NEW voice ref, instruct them to drop a 5-15s 24kHz
mono WAV at `pipeline/voice_refs/<name>.wav` AFTER this skill finishes
and run `/voice-bench` on it — don't block channel creation on it.

### Stage 8 — First variant slug + niche descriptor (AskUserQuestion, 2 options)

```
Option A: One default variant     (created at pipeline/variants/<slug>/default.yaml as a minimal overlay)
Option B: Named first variant      (user supplies the variant slug + niche key — e.g. "aita_animated" + "aita")
```

The variant overlay convention: `pipeline/variants/<slug>/<variant-slug>.yaml`
(see existing examples under `pipeline/variants/mystoriesanimated/` and
`pipeline/variants/sportsrecapped/`). A variant overlay is a partial
YAML — only the fields that DIFFER from the channel YAML need to be
present. Common overrides: `closer_format`, `image_style_prefix`,
`character_description`, `tts_voice`, `duration_target_s`,
`source_adapter`.

If Option A, write a tiny placeholder variant that inherits everything
from the channel YAML and only changes `name:` to "<DisplayName> —
default variant". The user can author the first real variant later via
`/make-skill` (when adding a new `/make-<channel>-short` command) or
manually.

### Stage 9 — Rotation + branding (AskUserQuestion, 3 options)

```
Option A: In rotation now    (in_rotation: true in channels.yaml — scheduler will pick it)
Option B: Out of rotation     (in_rotation: false — channel exists but scheduler skips it; user can render manually via UI)
Option C: Production-paused   (in_rotation: false, in_production: false — like scrollpulse pre-launch)
```

Then ask the user via AskUserQuestion whether branding assets are
ready:

```
Option A: I have icon_800.png + banner_2560x1440.png — I'll drop them at data/<slug>/branding/ now.
Option B: Not yet — add to manual checklist.
```

Branding assets are NOT generated by this skill (Q38: "YAML + variants
+ branding" — branding is a user deliverable, not a render output). The
skill just records the requirement in the checklist printed at the end.

## Outputs (the four files this skill writes)

After Stages 1-9 are locked, write the following four artifacts. Use
the `Write` tool (not Edit; these are new files). Do not run the
pipeline; do not trigger any renders; do not call any Cloud Run
services. Authoring only.

### Output 1 — `pipeline/channels/<slug>.yaml`

Structural template: copy the layout of
`/Users/rohit/ytFactory/pipeline/channels/mystoriesanimated.yaml`
verbatim (it's the most representative — AI image-gen + Shorts +
long-form + Reddit-fetched source + upload block), then mutate
per-stage answers. Required keys, in order:

```yaml
# <Display Name> — <one-sentence niche from Stage 2>
# Channel slug `<slug>`. Created via /make-channel on <YYYY-MM-DD>.
# Output format(s): <from Stage 3>
# Visual mode: <from Stage 5>
# Audio mode: <from Stage 4>
# Source: <from Stage 6>

name: <Display Name>
source_adapter: <Stage 6 answer>

# ---- TTS (omit this block if audio mode = sung Suno song only) -------
tts_provider: <Stage 7 answer>      # cloudrun_chatterbox / cloudrun_indicf5 / ...
tts_voice: <wav path or catalog name>
tts_ref_text: "<10-30 word transcript of the ref clip>"
tts_speed: 1.0

# ---- Audio provider (only if audio mode = sung Suno song) ------------
# audio_provider: sunoapi
# (see rhymetimejunction.yaml lines 41-80 for the full Suno wiring)

# ---- Image gen (only if visual mode = AI image-gen) ------------------
image_provider: cloudrun_z_image_turbo   # NEVER klein, NEVER flux2 — z-turbo is canonical
image_style_prefix: |
  <≥60 words, structured per Z-Image-Turbo prompt guide: technique tokens
  + palette tokens + lighting tokens + anti-text tokens. See
  mystoriesanimated.yaml lines 35-50 for the canonical template.>
character_description: |
  <≥50 words, structured per Z-Image-Turbo guide: age + face geometry +
  clothing + signature props + cel-shading rule. See
  mystoriesanimated.yaml lines 59-73 for the canonical template.>
image_seed: 42
image_steps: 9              # Z-Image-Turbo server sweet spot (cloud/image-z-image-turbo/server.py:149)
image_width: 768            # Shorts vertical default; flip to 1344 for long-form 16:9
image_height: 1344

# ---- Footage-only (only if visual mode = archival footage) -----------
# render_style: footage_only
# narrator_visual_mode: voice_only

# ---- Beat shape + output --------------------------------------------
beat_target_s: <2.0 for Shorts AITA-style, 3.0 for Shorts physics-style>
beat_max_s: <3.2 for Shorts AITA-style, 4.5 for Shorts physics-style>
duration_target_s: [<lo>, <hi>]       # [50, 60] for 50-60s Shorts; [10, 20] for ultra-short
output_resolution: [1080, 1920]       # 9:16 Shorts; [1920, 1080] for 16:9 long-form

# ---- Closer CTA --------------------------------------------------------
closer_format: "<two-clause CTA matching the channel niche>"
closer_hold_s: 1.0

# ---- Upload (Stage 8 of the render pipeline, not this skill) ---------
upload:
  account: <slug>
  privacy: private          # promote to public after format sign-off
  made_for_kids: false
  category_id: "<24 Entertainment / 27 Education / 28 Sci-Tech / etc>"
  auto_upload: false
  tags: [<5-15 channel-relevant tags>]
  description_template: |
    {script.hook}

    ━━━━━━━━━━━━━━━━━━━━
    <closer ask repeated in description>
    ━━━━━━━━━━━━━━━━━━━━

    #shorts #<niche>

# ---- Long-form block (omit if Stage 3 = A Shorts-only) ---------------
long_form:
  render_mode: image_panels        # or archival_footage for footage-only
  tts_provider: <usually same as Shorts>
  tts_voice: <usually same as Shorts>
  tts_speed: 0.95
  tts_post_atempo: 0.92
  duration_target_s: [600, 1800]
  output_resolution: [1920, 1080]
  output_fps: 30
  captions_enabled: true
  # (full long_form: block mirrors mystoriesanimated.yaml lines 150-207)
```

### Output 2 — `pipeline/variants/<slug>/<first-variant>.yaml`

A minimal overlay. Required at minimum:

```yaml
name: <Display Name> — <Variant Name>
source_adapter: <variant-specific override, if any>
# Only re-declare fields that DIFFER from the channel YAML.
```

If Stage 8 = Option A (default variant), write a 5-line stub that just
re-declares `name:` and inherits everything else. If Stage 8 = Option B
(named variant), write what the user supplied.

### Output 3 — `data/<slug>/learnings/.gitkeep`

Empty file at `data/<slug>/learnings/.gitkeep`. Touch only — no content.
This is the per-channel learnings directory (per CLAUDE.md repo layout
and the `<channel>/learnings/` convention referenced by `/update-docs`,
`/critique-audio`, `/critique-video`).

### Output 4 — Register in `pipeline/channels.yaml`

Append a new row to `pipeline/channels.yaml` (the channel manifest)
following the exact shape of the existing 7 rows. Required fields:

```yaml
  - slug: <slug>
    youtube_title: <Display Name>
    youtube_channel_id: <placeholder — user fills in after /create-youtube-channel completes>
    in_rotation: <Stage 9 answer>
    in_production: <Stage 9 answer>
    config_yaml: pipeline/channels/<slug>.yaml
    niches:
      <niche_key>: ["<state_subdir>", "<variant-filename>.yaml"]
```

Use the `Edit` tool to insert the new row at the END of the
`channels:` list (preserve existing rows verbatim). Do NOT reorder the
existing 7 rows. Do NOT touch `pipeline/channels.py` — the registry
loads from YAML at import time (line 187: `CHANNELS = _load_channels()`).

If Stage 9 = Option A (in rotation now), validation at load time will
fail if `config_yaml` doesn't exist (line 174-182 of channels.py) — so
make sure Output 1 is written BEFORE Output 4. Ordering matters.

## Quality gates

Run BEFORE writing any of the 4 outputs. Hard-fail with a descriptive
error if any gate fires. Do not write partial state.

### Gate 1 — Richness gate

Per the locked memory entry `project_channel_richness_gate.md`:

- `image_style_prefix` must contain **≥60 words** (count
  whitespace-separated tokens).
- `character_description` must contain **≥50 words**.
- Both must include required token classes per the channel's visual
  mode. For AI image-gen on Z-Image-Turbo, the required classes are:
  TECHNIQUE (e.g. "watercolor", "ink line work"), PALETTE (≥3 named
  colors), LIGHTING (e.g. "warm key light", "soft diffused daylight"),
  ANTI-TEXT (e.g. "ZERO printed text").
- For footage-only channels (Stage 5 = Option C), the `image_style_prefix`
  + `character_description` blocks are absent and the gate skips them;
  instead require `render_style: footage_only` + `narrator_visual_mode:
  voice_only`.

If the gate fires, print the offending block + the missing token
class(es) and ask the user (via AskUserQuestion) whether to (a) rewrite
the block now or (b) abort. Never silently write a sub-richness
config — every existing channel YAML passes this gate, and
`pipeline/llm/prompts.py` enforces it at author-time.

### Gate 2 — Required-field gate

Every channel YAML MUST contain (after the user's stage answers are
applied):

- `name:`
- `source_adapter:`
- One of: (`tts_provider:` + `tts_voice:`) OR `audio_provider:`
- One of: `image_provider: cloudrun_z_image_turbo` OR `render_style:
  footage_only` OR `motion_provider:` (case for Stage 5 = Option B
  motion video)
- `beat_target_s:` + `beat_max_s:`
- `duration_target_s:`
- `output_resolution:`
- `closer_format:` + `closer_hold_s:`
- `upload:` block with at minimum `account:`, `privacy:`,
  `category_id:`, `tags:`, `description_template:`

Hard-fail with the missing field(s) named. Do not write the file.

### Gate 3 — Slug uniqueness gate

Re-read `pipeline/channels.yaml` immediately before writing. Confirm
the chosen slug is still absent. (Defensive — the user may have created
the channel in a parallel session.)

### Gate 4 — Image provider gate

If `image_provider:` is set, it MUST be exactly `cloudrun_z_image_turbo`.
Hard-fail any other value (klein, flux2, qwen, hidream, mflux, sdxl).
Locked principle #6: Z-Image-Turbo is the canonical image model. New
channels do not get to pick a different one; the retired services have
been removed from the cloud deploy (CLAUDE.md `cloud/` section).

### Gate 5 — Audio-language gate

If `tts_provider: cloudrun_indicf5`, the user must have confirmed
non-English narration language in Stage 7. If `tts_provider:
cloudrun_chatterbox`, the channel niche sentence (Stage 2) must NOT
mention Hindi / Hinglish / Sanskrit / Tamil / Telugu / Marathi /
Bengali / Gujarati. Mismatch → hard-fail with the suggested correction.

## Locked principles

These are non-negotiable and come from `/ai/onboarding-qa.md` (the
cofounder Q&A). Do not violate them even if the user asks; instead
push back and explain.

- **All 7 channels must be functional** (Q14). When creating channel
  #8, the new channel inherits the same expectation — it MUST be
  rendering-capable from day one. Don't scaffold a channel YAML that
  can't pass `.venv/bin/python -c "from pipeline.channels import
  CHANNELS; print([c.slug for c in CHANNELS])"` at import time.
- **LLM as editor on real material, not author from nothing** (Q42,
  locked principle #2). Every channel must declare ≥1 source adapter
  (Stage 6). Reject "the LLM will just make stuff up" channels —
  they always drift into hallucination.
- **Z-Image-Turbo is the canonical image model — never klein**
  (locked principle #6, Q67). New channel YAMLs that mention klein,
  flux2, qwen, or hidream as `image_provider:` are wrong by
  construction.
- **One LLM call combines pick+synthesize+write** (locked principle
  #3). Don't scaffold a channel with a separate `picker` stage; the
  `rewrite` stage in the pipeline does fetch→pick→synthesize in one
  call (per Q43).
- **Gates STAY** (locked principle #1). When the richness gate fires,
  rewrite the block; never lower the threshold to make the gate pass.

## Manual-step checklist (printed at end)

After all 4 outputs are written, print this exact block (filled in
with the channel slug + Stage answers). Do not omit any item; the user
needs the full list to take the channel from "scaffolded" to "shipping
videos".

```
✓ wrote: pipeline/channels/<slug>.yaml
✓ wrote: pipeline/variants/<slug>/<first-variant>.yaml
✓ wrote: data/<slug>/learnings/.gitkeep
✓ registered: pipeline/channels.yaml (row appended; in_rotation=<Stage 9>)

Remaining manual steps:

1. YouTube account.
   If the YouTube channel does not yet exist, run /create-youtube-channel
   to create it on the desired Google account. Then update the
   `youtube_channel_id:` field in pipeline/channels.yaml with the real
   channel ID (currently <placeholder>).

2. Branding assets.
   Drop the channel avatar at  data/<slug>/branding/icon_800.png  (800×800)
   and the channel banner at  data/<slug>/branding/banner_2560x1440.png
   (2560×1440, mobile-safe content fitted to 1546×423). The pipeline
   will not upload without these.

3. Voice bench (if Stage 7 = Option D — deferred).
   Run  /voice-bench --channel <slug>  to A/B every TTS voice ref against
   a 30s sample. The bench picks the winner; patch the channel YAML's
   `tts_voice:` + `tts_ref_text:` with the winning ref.

4. OAuth + upload token.
   Once the YouTube channel exists, run
       .venv/bin/python upload.py auth --account <slug>
   signed into the Google account that owns the channel. The OAuth
   token caches at  ~/.config/ytfactory/youtube_token_<slug>.json
   and Stage 8 of the pipeline picks it up automatically.

5. First render (smoke test).
   Once steps 1-4 are done, the channel is rendering-capable. Open the
   /app/create wizard, pick the channel, and trigger a Shorts render.
   The first render is unlikely to ship cleanly (per Q26 the pipeline
   is not yet at MVP); use /critique-video on the output to surface
   class-of-bug fixes.

6. Authoring skill (optional).
   If this channel needs its own /make-<channel>-short authoring skill
   (most channels do), run /make-skill and pass the channel slug. The
   meta-skill walks the 51-heuristic checklist and inherits the closer
   + banned-phrase rules from this channel's YAML.

7. Register the slug with /update-docs.
   Run /update-docs once after the first /critique-video so any
   class-of-bug findings land in data/<slug>/learnings/ and the
   refactor-plan / decision-log get the ADR entry.
```

## Important rules

- **Never run the renderer or pipeline** from this skill. It is
  authoring-only. The first render happens later, via the /app/create
  wizard, after the user has completed the manual checklist above.
- **Never reference klein / flux2 / qwen / hidream / mflux / sdxl**
  as `image_provider:`. Only `cloudrun_z_image_turbo` is allowed for
  new channels.
- **Never silently lower the richness gate.** The ≥60-word style +
  ≥50-word character thresholds are locked. If a stage answer
  produces a sub-richness block, ask the user to rewrite — do not
  trim the threshold to pass.
- **Never reorder the existing 7 rows in `pipeline/channels.yaml`**
  when appending the new row. Other modules cache lookup order; a
  reorder can mask a stale-cache bug elsewhere.
- **Never skip Stage 6 (source adapter).** Every channel must declare
  ≥1 source — channels with no source adapter cannot run the
  `rewrite` stage and will fail at render time (per locked principle
  #2: LLM as editor on real material).
- **Never write to `pipeline/channels.py`.** The registry is data-
  driven from `pipeline/channels.yaml` (loaded at import time).
  Adding a channel = YAML edit, no Python change.
- **Always use the existing channel YAML as a structural template.**
  Default template: `pipeline/channels/mystoriesanimated.yaml`
  (covers Shorts + long-form + AI image-gen + Reddit source + upload
  block — the most general shape). For footage-only channels use
  `pipeline/channels/cosmosdecoded.yaml` as the template. For
  Suno-audio channels use `pipeline/channels/rhymetimejunction.yaml`.
- **Always run Gate 1 (richness) before writing.** Every existing
  channel passes this gate; new channels must too.
- **Always print the manual-step checklist at the end.** A scaffolded
  channel without the checklist is a half-done channel; the user
  needs the remaining 7 steps explicit.

## Why this skill is separate from /create-youtube-channel

`/create-youtube-channel` drives the YouTube UI via Playwright to
create the actual YouTube channel on a Google account (sign in, click
through the create-channel form, OAuth). It is a UI-driving skill
focused on the Google side.

`/make-channel` (this skill) scaffolds the ytFactory repo config that
lets the pipeline render videos FOR that YouTube channel — the YAML +
variant overlay + learnings dir + channels.yaml registration.

The two skills are complementary and usually run in sequence (Google
account first, then ytFactory config). They share no code and have
disjoint outputs.

## Learnings from prior runs

(empty on day 1; the skill's own learnings get appended here after the
first invocation. Per `/make-skill` heuristic F39-F44, every
class-of-bug surfaced during a channel scaffold gets mirrored here AND
into `/ai/known-fragility.md` via `/update-docs`.)
