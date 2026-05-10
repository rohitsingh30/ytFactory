---
name: make-mystories-short
description: Author a 10-20s Short for the MyStoriesAnimated channel. Picks one of nine variants (AITA-text / AITA-animated / AITA-cooking / AITA-cliffhanger × {text, animated, cooking} / TIFU / Wiki Oddities / Today-in-History) via an AskUserQuestion menu, drives `scripts/pull_stories.py` to mine + LLM-rewrite stories into the per-niche layout, runs banned-phrase + AITA-closer + length quality gates, then Bash-executes `scripts/make_shorts.py --channel <variant.yaml> --script <path>`. Use when the user says "make me a mystories short", "make me an AITA short", "AITA cooking variant", "AITA cliffhanger", "make me a TIFU short", "wiki oddities short", "today in history short", "next reddit story", or asks for a story-driven Short on the MyStoriesAnimated channel. For Hindi mythology Shorts use /make-hindutava-short. For sports countdowns use /make-ranking. For sports head-to-head use /make-last5. For long-form sleep history use /make-sleep-history.
---

# /make-mystories-short — story-driven Short for MyStoriesAnimated

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

This skill is the unified authoring path for **every** MyStoriesAnimated
variant. The channel ships nine variants today (`mystoriesanimated/variants/`):

| Variant | Source | YAML | Niche dir |
|---|---|---|---|
| AITA-text | r/AmItheAsshole | `aita_text.yaml` | `mystoriesanimated/reddit_amitheasshole` |
| AITA-animated | r/AmItheAsshole | `aita_animated.yaml` | `mystoriesanimated/reddit_amitheasshole` |
| AITA-cooking | r/AmItheAsshole | `aita_cooking.yaml` | `mystoriesanimated/aita_cooking` |
| AITA-cliffhanger-text | r/AmItheAsshole | `aita_cliffhanger_text.yaml` | `mystoriesanimated/reddit_amitheasshole_cliffhanger` |
| AITA-cliffhanger-animated | r/AmItheAsshole | `aita_cliffhanger_animated.yaml` | `mystoriesanimated/reddit_amitheasshole_cliffhanger` |
| AITA-cliffhanger-cooking | r/AmItheAsshole | `aita_cliffhanger_cooking.yaml` | `mystoriesanimated/reddit_amitheasshole_cliffhanger` |
| TIFU | r/tifu | `tifu.yaml` | `mystoriesanimated/reddit_tifu` |
| Wiki Oddities | Wikipedia (unusual deaths 21c) | `wiki_oddities.yaml` | `mystoriesanimated/wiki_oddities` |
| Today-in-History | Wikipedia "On this day" | `today_in_history.yaml` | `mystoriesanimated/today_in_history` |

You wear two hats: **Variant Picker → Script Curator**. Source mining
and LLM rewrite are autonomous (`pull_stories.py` runs them
end-to-end via `pipeline/llm.py`); your job is to pick the right
variant, set sensible flags, gate the output, and kick off the
renderer.

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

> **SCOPING RULE (2026-05-07):** Variant-pick + sub-variant-pick MUST
> use `AskUserQuestion` with **prefilled options** (the 9 variants:
> AITA-text / AITA-animated / AITA-cooking / AITA-cliffhanger ×
> {text, animated, cooking} / TIFU / Wiki Oddities / Today-in-History).
> Use a 2-stage menu: first stage narrows source (AITA / TIFU /
> Wiki / TIH), second stage narrows AITA-sub-variant when applicable.
> Never collapse to a free-text ask. Source of truth:
> [`docs/skill_prefilled_options.md`](/Users/rohit/ytFactory/docs/skill_prefilled_options.md).

### 1. Pick the variant

If the user already named one ("AITA cooking", "TIFU", "Today in
history") skip to §2. Otherwise present the menu via
**`AskUserQuestion`** (≤4 options per question, so do it in two
stages):

**Stage 1 — pick the source**

```
What source?
  - AITA (r/AmItheAsshole) — 6 sub-variants
  - TIFU (r/tifu) — single variant
  - Wiki Oddities (unusual deaths) — single variant
  - Today in History (Wikipedia "On this day") — single variant
```

**Stage 2 — only if AITA picked, pick the visual + structure**

```
Which AITA variant?
  - AITA-animated (Z-Image-Turbo crayon, single-story)
  - AITA-text (Reddit-text-on-pastel, single-story)
  - AITA-cooking (Reddit-text on cooking footage, single-story)
  - AITA-cliffhanger (multi-part — Part 1 + auto Part 2 watcher) — then ask sub-style
```

If the user picks AITA-cliffhanger, ask one more:

```
Cliffhanger sub-style?
  - cliffhanger-animated
  - cliffhanger-text
  - cliffhanger-cooking
```

Lock the 4-line spec and echo it back:

```
variant:    aita-cliffhanger-animated
yaml:       mystoriesanimated/variants/aita_cliffhanger_animated.yaml
niche_dir:  mystoriesanimated/reddit_amitheasshole_cliffhanger
limit:      <user-specified or default 5>
```

`--limit` defaults to 5 unless the user said something like "make me
10 shorts". Never over-pull — Reddit top-of-day rotates slowly and
the skip-seen guard in `pull_stories.py` deduplicates against
existing raws.

### 2. Run the autonomous pull → rewrite → cast pipeline

`scripts/pull_stories.py` does pull + visualizability filter +
claude-CLI rewrite + claude-CLI cast author **in one command**. Use
`.venv/bin/python` and `--out .` so output lands in the per-channel
layout (not the legacy `data/intermediate/`):

> **CRITICAL flag order + env (corrected 2026-05-09):**
> `--out` is a TOP-LEVEL flag and MUST come BEFORE the
> `reddit`/`wiki`/`tih` subcommand — putting it after raises
> `unrecognized arguments: --out .`. Also: `pull_stories.py` imports
> from `pipeline.llm.*` → must be invoked from the repo root with
> `PYTHONPATH=.` (the venv doesn't include the project root in
> sys.path). Run from `/Users/rohit/ytFactory`.

| Variant | Command (pseudo) |
|---|---|
| AITA-* | `PYTHONPATH=. .venv/bin/python scripts/pull_stories.py --out . reddit --subreddit AmItheAsshole --channel <niche_dir> --limit <N>` |
| AITA-cliffhanger-* | same as AITA-* but `--channel mystoriesanimated/reddit_amitheasshole_cliffhanger` |
| TIFU | `PYTHONPATH=. .venv/bin/python scripts/pull_stories.py --out . reddit --subreddit tifu --channel mystoriesanimated/reddit_tifu --limit <N>` |
| Wiki Oddities | `PYTHONPATH=. .venv/bin/python scripts/pull_stories.py --out . wiki --page unusual_deaths_21c --channel mystoriesanimated/wiki_oddities --limit <N>` |
| Today-in-History | `PYTHONPATH=. .venv/bin/python scripts/pull_stories.py --out . tih --channel mystoriesanimated/today_in_history --limit <N>` |

Run with **Bash**, foreground (this is fast — <60s typical for
limit=5). Output paths after success:

```
mystoriesanimated/<niche_dir>/raw/<slug>.json      # source story
mystoriesanimated/<niche_dir>/scripts/<slug>.json  # hook + 50-80 word narration
mystoriesanimated/<niche_dir>/cast/<slug>.json     # narrator description
```

If the pull returns "all candidates already rendered" (skip-seen
guard), tell the user — don't silently re-run with a higher limit
unless they ask.

### 3. Hand-author override (optional, rare)

The autonomous flow handles 99% of cases. Only hand-edit
`<niche_dir>/scripts/<slug>.json` when:

- The user asked for a specific angle the LLM rewrite missed, OR
- A story dropped at the visualizability filter but the user wants
  it back, OR
- A quality gate (§4) failed and the fix is one-off (typo, missing
  closer line).

Schema (matches `pipeline/rewrite.py:_BASE_PROMPT` output):

```json
{
  "slug": "<same slug as the raw story>",
  "hook": "<first ~5-10 words>",
  "narration": "<full 50-80 word narration including the hook>",
  "title_options": ["<title A>", "<title B>", "<title C>"],
  "source_url": "<from the raw story's url field>",
  "source": "<from the raw story's source field>"
}
```

### 4. Quality gates — mechanical, BEFORE renderer handoff

Run each gate against every emitted `scripts/<slug>.json`. Block on
hit; fix in place or skip the story.

1. **Banned-phrase scan.** Reject any of:
   - **AITA verdict acronyms** in narration body — `AITA`, `WIBTA`,
     `YTA`, `NTA`, `NAH`, `ESH` (ban from
     `mystoriesanimated/learnings/no_verdict_acronyms_in_audio.md`).
     **Exception:** the closer line `LIKE if I'm YTA, COMMENT if NTA.
     AITA?` is the literal pattern that PASSES — don't strip it.
   - "smash that subscribe button" / "vote in comments" / "hit the
     bell icon" — robotic CTAs (channel learning
     `spoken_subscribe_ask.md`).
   - "Hi guys" / "today's story is" / "in this video" — vlogger
     openers; the hook IS the opener.
2. **Hook gates** (literal from `pipeline/script_check.py`, runs at
   render time and will fail the build, not warn — bake in up front):
   - **First 8 words** must contain at least one of: a `?`, an
     "Am I wrong"-frame (`Am I wrong`, `Was I wrong`, `Am I out of
     line`, `Was I out of line`, `am I the one in the wrong here`),
     a script-check claim verb (`refused`, `told`, `caught`, `threw`,
     `broke`, `banned`, `divorced`, `adopted`, `moved`, `texted`,
     `kicked`, `dumped`, …), OR a structural subject-verb opener.
     Easiest path: `"Am I wrong for <verb>ing …?"`. **Note: literal
     `AITA` / `WIBTA` is NOT accepted by the hook gate** (BANNED in
     spoken narration per `pipeline/llm/script_check.py` L27-30,
     stripped pre-TTS by `audio.normalize_for_tts`). The earlier
     skill text claiming `AITA for …?` opens are valid was wrong —
     corrected 2026-05-09 after the canary `"AITA for asking…"`
     failed `weak_hook`.
   - **First 30 words** must contain a number/quantity (e.g.
     `2-bedroom`, `three bottles`, `$400`, `two weeks`). The wedge.
3. **AITA closer gates** (AITA variants only, both single + cliff):
   - **Last 2 sentences combined** must contain the literal split
     `LIKE if I'm YTA, COMMENT if NTA` (per
     `aita_closer_panel.md`). The visual closer panel from
     `pipeline.captions.render_closer_panel` does NOT satisfy this —
     the spoken narration MUST contain it. Validated regression.
   - **Last sentence** must be a vote-prompt CTA — `AITA?`, `WIBTA?`,
     `Was I wrong?`, `What would you do?`. The LIKE/COMMENT line
     alone doesn't count — append a separate `AITA?` sentence after
     it. Canonical: `"… LIKE if I'm YTA, COMMENT if NTA. AITA?"`
4. **Profanity sanitize.** `audio.normalize_for_tts` strips
   `_RE_PROFANITY_ASSHOLE` + `_ACRONYM_PHRASES` automatically pre-TTS
   (per `aita_profanity_sanitize.md`) so monetization is protected.
   Don't try to undo this in the narration text — write
   `assh*le` / `asshole` freely; the pipeline handles it.
5. **Length budget.** Narration must be **50-80 words** total
   (≈10-20s spoken at the channel's target wpm). Hard fail outside.
   `duration_target_s: [10, 20]` from `mystoriesanimated/config.yaml`
   is the source of truth.
6. **TIFU / Wiki / TIH variants** — no AITA closer gate (only the
   AITA family enforces the LIKE-if-YTA pattern). Use the channel's
   default closer-by-spoken-CTA from the variant YAML.
7. **Cliffhanger variants only** — Part 1 narration must end on a
   cliffhanger (NOT the closer); Part 2 auto-renders + uploads via
   the subs-gated watcher (`aita_cliffhanger.md`). Don't author Part
   2 manually — the watcher does it. If the user asks for "the
   second part", check whether the watcher already kicked it.
8. **Aspect + voice + image-provider match.** Assert
   `output_resolution: [1080, 1920]` in the variant YAML; assert
   `tts_provider == "cloudrun_chatterbox"` (or fallback per
   `pipeline/tts/cloudrun.py`); assert `image_provider` is
   `cloudrun_flux2_klein` (or `mflux` for the legacy `tifu.yaml`
   path). Drift blocks emit.
9. **`/critique-audio` gate (recommended, not required).** TTS bugs
   (acronym mispronunciation, run-on sentences, robotic CTA
   delivery) invalidate downstream image-gen work. If the user is
   shipping a brand-new variant or you suspect a pronunciation
   regression, run `/critique-audio` on the synthesized wav before
   greenlighting the image-gen run. Memory:
   `feedback_critique_audio_before_image_gen.md`.

### 5. Renderer handoff — Bash-execute, don't redirect to the website

Per `feedback_skills_kick_render_directly.md`: skills must
Bash-execute `scripts/make_shorts.py` themselves with
`run_in_background=true`. Don't tell the user to open the website.

For each emitted script, run:

```bash
.venv/bin/python -m pipeline.skill_dispatch render \
  --channel <variant_yaml> \
  --script <niche_dir>/scripts/<slug>.json
```

`<variant_yaml>` is the path resolved in §1 (e.g.
`mystoriesanimated/variants/aita_animated.yaml`). The renderer
derives `out_dir` from `--channel` via the per-channel layout — do
NOT pass `--out` (memory: `feedback_make_shorts_no_out_flag.md`).

Render time per Short on a warm pipe: ~3-6 min (Cloud Run TTS +
Cloud Run FLUX.2 klein both warm; falls back to local F5-TTS +
mflux on cloud failure per the circuit breakers).

**Parallelise bulk renders, capped at cloud `--max-instances`.**
When the pull returned N stories, kick UP TO `cloudrun_flux2_klein
--max-instances` renders in parallel (today 2). For N > 2, batch
in groups of 2 sequentially. Don't fire 5 parallel renders against
a 2-instance cloud — queue depth exceeds the 15-min read-timeout,
the render-level circuit breaker trips to local mflux fallback,
and 3+ concurrent mflux processes Metal-timeout the laptop GPU
(observed 2026-05-07 TIFU run). Memory:
`feedback_parallel_bulk_renders.md`. Project doc:
`docs/parallel_bulk_renders.md`.

**Serial-only fallback.** If a render's `image_provider: mflux`
(or `tts_provider` is a local-only one), serialize THAT render's
image-gen-bound channels, since multiple concurrent mflux processes
on the laptop GPU thrash unified memory
(`feedback_gpu_one_render_at_a_time.md`). Currently no
mystoriesanimated variant uses local mflux as its production path
(2026-05-07 cloud-image migration moved tifu.yaml off it), so
parallel is always correct here.

### 6. Auto-upload

`upload.auto_upload: false` is the channel default — renders land
locally and the user reviews + publishes via the website's "Publish"
button. Do NOT flip `auto_upload: true` from this skill. Memory:
`feedback_upload_public_default.md` covers the public-by-default
rule for when the user does upload.

If the user said "ship it" / "render and upload", invoke
`make_shorts.py` with `--auto-upload` (the renderer's CLI flag).
Otherwise leave it off.

### 7. Report back

```
✓ pulled N stories from <source>
  - <slug-1>: "<hook>"
  - <slug-2>: "<hook>"
  ...
✓ scripts: mystoriesanimated/<niche_dir>/scripts/{<slug-1>,<slug-2>,...}.json
✓ cast:    mystoriesanimated/<niche_dir>/cast/{<slug-1>,<slug-2>,...}.json
✓ quality gates: <K passed / 0 failed>

rendering (parallel — all N kicked at once via run_in_background):
  [#1 slug-1]  bash_id <id1>
  [#2 slug-2]  bash_id <id2>
  ...
  [#N slug-N]  bash_id <idN>

waiting for all to land. Cloud Run scales image+TTS gen.
```

If a quality gate failed, list the failures and STOP for that script.
Don't silently ship a regression.

### 8. Self-learning hook

After the user runs `/critique-audio` on the wav OR `/critique-video`
on the final mp4:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo in this script, this story's narrator was
     mis-cast) → fix in
     `mystoriesanimated/<niche_dir>/scripts/<slug>.json` (or
     `cast/<slug>.json`) and re-render the affected stage; append a
     1-line note to
     `.claude/skills/make-mystories-short/learnings/_index.md`.
   - **CLASS-OF-BUG** (every future MyStories Short will hit this) →
     fix in the right place:
     - New robotic-CTA pattern → extend the §4.1 banned-phrase scan
       AND `mystoriesanimated/learnings/spoken_subscribe_ask.md`.
     - New verdict-acronym leak in narration → extend
       `pipeline/audio.py:_ACRONYM_PHRASES` AND
       `mystoriesanimated/learnings/no_verdict_acronyms_in_audio.md`.
     - Profanity-sanitize miss → extend
       `pipeline/audio.py:_RE_PROFANITY_ASSHOLE` AND
       `aita_profanity_sanitize.md`.
     - Hook gate regression → tighten `pipeline/script_check.py`
       claim-verb whitelist.
     - Cooking-bg variant pacing/aspect issue → see
       `aita_cooking_format.md` + extend.
   - Then append a regression note to
     `.claude/skills/make-mystories-short/learnings/<topic>.md` AND
     mirror to `mystoriesanimated/learnings/<topic>.md` per CLAUDE.md
     dual-save rule.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in §4 that blocks emit on detection.

## Important rules

- **Channel is locked to `mystoriesanimated/`.** This skill does NOT
  author for sportsrecapped, historyrecapped, hindutavaanimated,
  rhymetimejunction, cosmosdecoded, or scrollpulse. Different channel ⇒
  different skill.
- **Variant menu is two-stage AskUserQuestion** (source → AITA
  sub-variant). Don't list all 9 in one prompt — `AskUserQuestion`
  caps at 4 options per question.
- **Always use `.venv/bin/python`** for `pull_stories.py` and
  `make_shorts.py`. The repo's venv has `requests`,
  `youtube-transcript-api`, and the cloud-TTS / cloud-image clients.
- **Always pass `--out .` to `pull_stories.py`** so output lands in
  the per-channel layout (`mystoriesanimated/<niche_dir>/...`), not
  legacy `data/intermediate/`.
- **Never pass `--out` to `make_shorts.py`** — the renderer derives
  per-channel root from `--channel` via `NICHE_CHANNEL` (memory:
  `feedback_make_shorts_no_out_flag.md`).
- **Spoken AITA closer is non-negotiable** — `LIKE if I'm YTA,
  COMMENT if NTA. AITA?` (literal). The visual closer panel doesn't
  satisfy the gate; the narration must too.
- **No verdict acronyms in the narration body** — only in the closer
  line (`no_verdict_acronyms_in_audio.md`). The pipeline expands
  `AITA → "Am I the Asshole"` etc. via `_ACRONYM_PHRASES`, so
  acronyms in body text get spoken as letter-spelled.
- **No "smash subscribe" / "vote in comments"** — robotic CTAs.
  Channel uses the 3-part audio CTA + visual closer panel
  (`spoken_subscribe_ask.md`).
- **Cliffhanger Part 2 is NOT authored manually** — the subs-gated
  watcher renders + uploads it (`aita_cliffhanger.md`). Don't
  duplicate work.
- **Bulk renders run in PARALLEL up to cloud `--max-instances`**
  — kick UP TO N=`max-instances` (today 2) via separate
  `run_in_background=true` Bash calls; batch in groups of 2 for
  larger pulls. Going wider blows past cloud read-timeout, trips
  the circuit breaker to local mflux, and Metal-timeouts the
  laptop GPU (observed TIFU 4-way 2026-05-07).
  `feedback_parallel_bulk_renders.md` /
  `docs/parallel_bulk_renders.md`.
- **Skill kicks the renderer directly** via `Bash` with
  `run_in_background=true` — don't redirect the user to the website
  (`feedback_skills_kick_render_directly.md`).
- **Never call the Anthropic SDK directly** — `pull_stories.py`
  shells out to `claude -p` via `pipeline/llm.py`
  (`feedback_llm_via_claude_cli.md`).
- **Auto-upload defaults to OFF** — user reviews + publishes via the
  website. Only flip `--auto-upload` when the user says "ship it".

## Learnings from prior runs

<!-- empty on day 1; appended on every regression via §8 -->

## Why this skill exists

Before 2026-05-07 this channel's authoring path was the cross-channel
`/make-script`. That skill produced the right narration JSON but had
two structural mismatches:

1. **No variant picker.** `/make-script` knew about niches (`aita`,
   `tifu`, `oddities`, `tih`) but not visual variants
   (`aita_text` vs `aita_animated` vs `aita_cooking`), so the
   renderer always defaulted to `aita_animated.yaml` — the cooking +
   text variants got rendered with the wrong style unless the user
   manually overrode `--channel`.
2. **Wrote to legacy `data/intermediate/`.** The per-channel layout
   (canonical 2026-05-05 spec, `docs/channel_layout.md`) puts narration
   under `<channel>/<niche>/scripts/`; `/make-script` put it under
   `data/intermediate/<channel>/scripts/`, requiring a manual move
   step before render.

This skill fixes both: variant menu picks the right YAML up front,
and `--out .` writes directly into the per-channel layout the
renderer + dashboard read. The cinematic cousin `/make-movie-short`
was retired in the same change — the directorial shot-list payload
overlapped 80% with `/make-script` and got used twice in six months.

learnings_consulted:
  - mystoriesanimated/learnings/aita_cooking_format.md (cooking-bg pacing + aspect rules)
  - mystoriesanimated/learnings/aita_cliffhanger.md (Part 1 ships + Part 2 auto-renders)
  - mystoriesanimated/learnings/aita_closer_panel.md ("LIKE if YTA / COMMENT if NTA" — never "vote in comments")
  - mystoriesanimated/learnings/aita_profanity_sanitize.md (_ACRONYM_PHRASES + _RE_PROFANITY_ASSHOLE pre-TTS)
  - mystoriesanimated/learnings/per_story_character.md (cast/<slug>.json owns character_description)
  - mystoriesanimated/learnings/no_verdict_acronyms_in_audio.md (AITA/WIBTA/YTA/NTA banned in narration body)
  - mystoriesanimated/learnings/spoken_subscribe_ask.md (3-part audio CTA; no "smash subscribe")
  - mystoriesanimated/learnings/youtube_upload_quota.md (daily uploadLimitExceeded; stop the loop)
  - mystoriesanimated/learnings/captions_emoji_density.md (cross-channel emoji density rule)
  - mystoriesanimated/config.yaml (cloudrun_chatterbox + cloudrun_flux2_klein routing, 1080x1920)
  - pipeline/script_check.py (hook claim-verb whitelist, first-30-words number-or-quantity gate, AITA closer gate)
  - pipeline/niches.py (NICHE_CHANNEL — niche → channel_dir + variant YAML routing)
  - feedback_make_shorts_no_out_flag.md (never --out on make_shorts.py)
  - feedback_skills_kick_render_directly.md (Bash-execute renderer; no website handoff)
  - feedback_parallel_bulk_renders.md (bulk renders kicked in parallel; cloud scales)
  - feedback_gpu_one_render_at_a_time.md (only when local-mflux fallback in play)
  - feedback_critique_audio_before_image_gen.md (audio gate first when in doubt)
  - feedback_engineer_class_of_bug.md (one-off vs class-of-bug classification)
  - feedback_dual_save_memory_and_docs.md (project doc + memory entry)
  - feedback_upload_public_default.md (public when user does upload; off by default)
  - feedback_skill_description_1024_cap.md (frontmatter description ≤1024 chars)

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
