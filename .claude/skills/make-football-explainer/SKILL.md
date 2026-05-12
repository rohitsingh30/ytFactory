---
name: make-football-explainer
description: Author a 10-15 min mid-form football explainer for SportsRecapped — narrator-only over real broadcast / fan / archival footage, no talking-heads, no tactics-board motion-graphics, no captions. Signature DNA is an anaphoric stat-bomb opener ("Imagine X. 40 years of Y. 40 years of Z."), one long uninterrupted thesis beat, and a single 6-word subscribe CTA. Produces sportsrecapped/narrations/<slug>.json (with render_overrides toggling captions/chapter-cards/lower-thirds OFF) + footage_plan/<slug>.json (commentary_takes=[]), then hands off to sportsrecapped/scripts/render_long_form_doc.py. Use when the user says "make me a football explainer", "Tifo-style explainer on <X>", "Mega Football style on <Y>", "10 min football breakdown on <Z>", "explainer on <club / league / financial story>", or names a football strategy / finance / governance topic. For 20-30 min talking-head sports docs use /make-sports-doc. For 50-60s Shorts use /make-script.
---

# /make-football-explainer — narrator-only mid-form football explainer

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

You become **investigative writer + footage producer + audio-first
showrunner** for SportsRecapped's mid-form band. The format is what
*Tifo Football* would look like with the line-art animation stripped
out and broadcast/fan footage in its place — calm UK narrator,
anaphoric stat-bomb opener, one long uninterrupted thesis act, single
6-word subscribe close, zero burned-in captions, zero talking-head
clips, zero AI imagery.

Output is a pair of JSONs (`narrations/<slug>.json` +
`footage_plan/<slug>.json`) that hand off to the existing
`sportsrecapped/scripts/render_long_form_doc.py`. The renderer reuses
all the /make-sports-doc plumbing — same TTS voice, same scrub-blur
rules, same anchor-lint, same upload metadata block — but the
narration JSON's `render_overrides` block flips captions /
chapter-cards / lower-thirds off so the doc-default visual chrome
doesn't bleed into the explainer band.

**Authoring pre-read (mandatory):**
- [`data/format_clones/hearts-scottish-system/ANALYSIS.md`](../../../data/format_clones/hearts-scottish-system/ANALYSIS.md)
  — the 9,000-word format-DNA analysis from the cloned Mega Football
  source. Cites every decision with section numbers.
- [`data/format_clones/hearts-scottish-system/fingerprint.json`](../../../data/format_clones/hearts-scottish-system/fingerprint.json)
  — the structured DNA artefact.
- [`sportsrecapped/learnings/explainer_format.md`](../../../sportsrecapped/learnings/explainer_format.md)
  — channel-side mirror of the format rules.
- [`sportsrecapped/learnings/channel.md`](../../../sportsrecapped/learnings/channel.md)
  — Tifo-aesthetic + footage-USP rules (inherited).
- All `sportsrecapped/learnings/long_form_doc_*.md` — anchor-lint,
  multilingual broadcast, scrub policy, etc. (inherited).

## How to run it

### 1. Confirm the spine (Stage 1, 1 question)

```
question: "What's the central fact and the unresolved question?"
header:   "Spine"
options:
  - "I'll describe the topic"     → freeform user input → claude -p parses
  - "Spin from a recent story"    → user pastes URL / headline → research stage
  - "Pick from the queue"         → reads sportsrecapped/raw/queue.json (if exists)
```

The author-LLM converts the user's spine into:
- **slug**  `<club-or-actor>-<topic-keyword>` — kebab-case, ≤32 chars, ASCII.
- **load-bearing number**  the numeric or temporal anchor for the hook
  ("40 years", "55 each", "1872", "£300m"). Required.
- **thesis**  one sentence — what the video proves.
- **unresolved question**  one sentence — what hangs at the end (the
  closer-coda hook callback).

### 2. Hat 1 — Researcher (no question; runs claude -p + wiki + web)

Run dossier research using the existing channel adapter:

```bash
.venv/bin/python -m pipeline.wiki_research \
    --raw <raw_path> --out <dossier_path> \
    --wiki-query "<club / topic>" \
    --channel-yaml sportsrecapped/config.yaml
```

Then claude-p the prose draft. The author-LLM must be instructed to
write a hook that satisfies all four:

1. Open with the load-bearing number.
2. Restate it across **3-5 parallel clauses** using anaphora — same
   leading 1-2 tokens, varying predicates ("40 years of … 40 years
   of … 40 years in which …").
3. Each restatement adds a *different angle* (temporal → geographic
   → social → emotional → economic).
4. Land on the implicit "until now…" — but never say it out loud;
   the pivot lives in beat 2's first sentence.

### 3. Hat 2 — Architect (Stage 2, 1 question, optional)

```
question: "Beat structure?"
header:   "Beats"
options:
  - "Default: 7 beats (recommended)"
  - "Compressed: 8 beats with split thesis_setup"
  - "Long-thesis: 9 beats for multi-actor stories"
```

Beat roles (see `ANALYSIS.md` §3.3 for typical durations):

| # | role               | duration   |
|---|---------------------|------------|
| 1 | hook               | 30-50 s    |
| 2 | scene_set          | 60-150 s   |
| 3 | history            | 50-90 s    |
| 4-5 | thesis_setup     | 50-90 s    |
| 6 | thesis_main        | **240-360 s — one long beat, do not fragment** |
| 7 | thesis_resolution  | 40-60 s    |
| 8 | coda               | 30-60 s    |
| 9 | closer             | 4-6 s CTA + held-black tail |

The middle thesis beat is *deliberately* uninterrupted — see
ANALYSIS.md §3.2 for the source's 5-minute breath-free middle act.
Do not let the LLM fragment it into 50-second sub-beats.

### 4. Hat 3 — Footage producer (Stage 3, 1 question)

```
question: "How aggressive on broadcast clips?"
header:   "Footage"
options:
  - "Recommended: rights-holder-owned footage where available"
  - "Mixed: include unaffiliated broadcast, scrub-blur logos"
  - "Archival-only: skip live broadcast (lowest ContentID risk)"
```

Run the existing helpers:

```bash
.venv/bin/python -m sportsrecapped.scripts.find_match_clips \
    --slug <slug> --anchors-from <narration_path>
.venv/bin/python -m sportsrecapped.scripts.find_b_roll \
    --slug <slug> --anchors-from <narration_path>
```

**Do NOT run** `find_commentary_takes.py` — this format forbids
talking-heads.

### 5. Output JSONs

`sportsrecapped/narrations/<slug>.json`:

```json
{
  "slug": "<slug>",
  "channel": "sportsrecapped",
  "format": "football_explainer",
  "format_version": 1,
  "title": "<headline>",
  "title_seo": "<≤70 chars>",
  "thesis": "<one sentence>",
  "unresolved_question": "<one sentence>",
  "narrator_tone": "tifo-academic",
  "duration_target_s": 750,
  "wpm_target": 165,
  "hook": {
    "anaphora_target": "<the load-bearing number>",
    "clauses": [
      "<clause 1 — opens with anaphora_target>",
      "<clause 2 — restates anaphora_target with new angle>",
      "<clause 3 — restates again>",
      "<clause 4 — restates one more time>"
    ],
    "bridge": "<the ~12-18s bridge into beat 2>"
  },
  "beats": [
    {
      "index": 1,
      "role": "hook",
      "start_s_target": 0.0,
      "duration_s_target": 48,
      "transcript": "<full prose: clauses + bridge>",
      "anchor_phrases": ["<respelling-free phrases>"],
      "music_mood": "cinematic"
    }
    // … beats 2-N …
  ],
  "closer": {
    "coda_text": "<25-40 s of callback prose, ending on a question>",
    "cta_text": "Subscribe to Sports Recapped and hit the bell. See you soon.",
    "cta_duration_target_s": 6,
    "hold_black_after_s": 8
  },
  "render_overrides": {
    "captions_enabled": false,
    "chapter_card_enabled": false,
    "lower_third_enabled": false
  },
  "music_plan": {
    "default_mood": "lo-fi-piano",
    "section_overrides": [
      {"start_beat": 1, "mood": "cinematic"},
      {"start_beat": 4, "mood": "lo-fi-piano"}
    ],
    "silent_under_clip": true
  },
  "engagement_asks": [],
  "sources": [],
  "learnings_consulted": [
    "sportsrecapped/learnings/channel.md",
    "sportsrecapped/learnings/explainer_format.md",
    "sportsrecapped/learnings/long_form_doc_anchor_no_respellings.md",
    "sportsrecapped/learnings/footage_scrub_watermarks.md",
    "data/format_clones/hearts-scottish-system/ANALYSIS.md"
  ]
}
```

`sportsrecapped/footage_plan/<slug>.json`:

```json
{
  "slug": "<slug>",
  "channel": "sportsrecapped",
  "format": "football_explainer",
  "match_footage": [
    {"id": "mf01", "narration_anchor": "<respelling-free phrase>",
     "url": "<youtube-or-archive-url>", "in_s": 12.4, "out_s": 18.2,
     "audio_mix": 0.0, "scrub_policy": "auto"}
  ],
  "b_roll": [
    {"id": "br01", "kind": "stadium", "narration_anchor": "Tynecastle",
     "url": "<rights-holder-url>", "in_s": 0.0, "out_s": 5.0,
     "scrub_policy": "keep_source_logo"}
  ],
  "archival_footage": [],
  "commentary_takes": [],
  "talking_heads": [],
  "thumbnail": {
    "kind": "text_overlay",
    "headline": "<≤6 words, uppercase>",
    "face_url": "<rights-holder portrait>",
    "palette": ["#0d4d2b", "#5b1f24"]
  }
}
```

**`commentary_takes` and `talking_heads` MUST stay empty** — quality
gate `check_no_talking_heads` blocks emit otherwise.

### 6. Cast handling

For *strategy / finance / governance* explainers (Bloom-at-Hearts,
Foundation-of-Hearts, Eliott-at-Milan): **skip cast.json**. The
dossier is sufficient.

For *player-centred* explainers ("How Bellingham Re-Engineered the
10"): emit `sportsrecapped/cast/<slug>.json` matching the existing
schema in `feedback_sports_cast_locked_tokens.md` — kit + shirt # +
era tokens. Heuristic: if the dossier names ≥1 player with ≥3
mentions in the script, emit cast; otherwise skip.

### 7. Quality gates (all run BEFORE handoff)

Block emit on any error:

| #  | Gate                                           | Implementation                                         |
|----|------------------------------------------------|--------------------------------------------------------|
| 1  | banned-phrase scan                             | `pipeline.llm.script_check.check_script_text`          |
| 2  | hook anaphora ≥3 parallel clauses              | `check_hook_anaphora(narration["hook"])`               |
| 3  | hook duration in [22, 32] s (clauses only)     | duration_target × wpm_target → word-count window       |
| 4  | beat count in [7, 9]                           | `len(narration["beats"])`                              |
| 5  | total duration in [600, 900] s                 | `narration["duration_target_s"]`                       |
| 6  | WPM in [150, 185]                              | warn-only                                              |
| 7  | closer single-subscribe-CTA + channel name     | `check_single_subscribe_cta(narration["closer"], channel_display_name="Sports Recapped")` |
| 8  | coda references hook number                    | warn-only                                              |
| 9  | `commentary_takes == []` AND `talking_heads == []` | `check_no_talking_heads(footage_plan)`            |
| 10 | `render_overrides.captions_enabled == false`   | direct dict assertion                                   |
| 11 | `render_overrides.chapter_card_enabled == false` | direct dict assertion                                 |
| 12 | `render_overrides.lower_third_enabled == false` | direct dict assertion                                  |
| 13 | anchor-lint: every anchor_phrase respelling-free | `pipeline.footage_plan_lint.raise_if_any`            |
| 14 | soccer-disambiguation echo                     | inherited via channel YAML `image_style_prefix`         |
| 15 | every cast token traces to dossier (if cast)   | inherited                                               |
| 16 | pronunciation pre-pass run                     | `feedback_pronunciation_pretts` rule                    |
| 17 | source-fidelity: every named claim traces to dossier | inherited                                         |
| 18 | ContentID scan (warn)                          | inherited                                               |
| 19 | aspect = 16:9 + voice_id == tifo_devine.wav    | channel YAML check                                      |
| 20 | /critique-audio gate on cloud-Chatterbox preview | runs before final render                              |

### 8. Renderer handoff

After all gates pass:

```bash
.venv/bin/python -m pipeline.skill_dispatch render \
    --cmd sportsrecapped/scripts/render_long_form_doc.py -- \
    --channel sportsrecapped --slug <slug>
```

Echo the absolute paths of both JSONs in the chat so the user can
inspect or edit before render. Background the render via Bash
`run_in_background`. Do NOT tell the user to open the website
(`feedback_skills_kick_render_directly`).

### 9. Self-learning hook

After the user runs `/critique-audio` or `/critique-video` on the
output:

1. Classify each regression per `docs/post_upload_analysis.md`:
   - **ONE-OFF** (typo in this script's hook anaphora) → fix in
     `narrations/<slug>.json`, append 1 line to
     `.claude/skills/make-football-explainer/learnings/_index.md`.
   - **CLASS-OF-BUG** (every render mishandles non-Latin player
     names; renderer ignores `render_overrides` because the merge
     order is wrong) → fix in `pipeline/<module>.py` OR in this
     SKILL.md, then append a regression note to
     `.claude/skills/make-football-explainer/learnings/<topic>.md`
     **AND** mirror to `sportsrecapped/learnings/<topic>.md` per
     CLAUDE.md dual-save rule.
2. Update `MEMORY.md` index pointer if a new file was created.
3. If the same class-of-bug fires twice, add a new pre-render
   quality gate (§7) that blocks emit on detection.

## Important rules

Every line below is a "do not violate" — each traces to a memory or
learnings file by name; consult that file for the why.

- **Closer:** `Subscribe to Sports Recapped and hit the bell. See you
  soon.` Channel display name is **`Sports Recapped`** (two words),
  not `SportsRecapped`. ≤18 words. No "smash". No comment-prompt.
  No multi-stack engagement asks. (gate #7)
- **Banned phrases:** anything matching `_BIGRAM_TRIGGER_STOPWORDS`
  patterns from the channel banlist; "smash"; "vote in comments";
  verdict acronyms (AITA / WIBTA / YTA / NTA / NAH / ESH — irrelevant
  here but in the universal banlist).
- **Aspect:** `16:9` only. Output `1920×1080`. From channel YAML.
- **Voice:** `cloudrun_chatterbox` + `pipeline/voice_refs/tifo_devine.wav`
  + `tts_speed: 0.98`. From channel YAML `long_form_doc:` block. Do
  not override per-render.
- **Format-defining toggles (per render):** `captions_enabled: false`
  · `chapter_card_enabled: false` · `lower_third_enabled: false`. The
  renderer reads these via `apply_render_overrides()` and overrides
  channel-YAML defaults. (gates #10-12)
- **Talking-heads forbidden:** `footage_plan.commentary_takes = []`
  AND `footage_plan.talking_heads = []`. (gate #9)
- **AI imagery forbidden:** footage-only — no `image_provider` calls,
  no AI image gen at render time. Same rule as Cosmos Decoded
  long-form.
- **Anchor strings respelling-free:** narration prose can carry
  `Saint-Gilloise → "sant-gee-LWAH"` for TTS, but
  `anchor_phrases[]` must stay `Saint-Gilloise`. Per
  `long_form_doc_anchor_no_respellings.md`.
- **Hook anaphora ≥3 clauses:** parallel sentence-starts sharing the
  first 1-2 tokens. (gate #2)
- **One long thesis beat:** beat 6 may run 240-360 s. Do not let the
  LLM fragment it. Per ANALYSIS.md §3.2.
- **Footage scrub policy:** rights-holder watermarks (Hearts FC media,
  club TV channels) → `keep_source_logo`. Unaffiliated broadcaster
  logos → `auto` (channel default = blur). Per
  `feedback_channel_bug_scrub_blur_default` + `footage_scrub_watermarks`.
- **Source-content guard:** every named claim must trace to a dossier
  entry. The skill calls `pipeline.wiki_research` before authoring;
  the LLM-author cites the dossier.
- **Cloud-TTS only:** never `tts_provider: f5_tts` etc. Per
  `feedback_cloud_tts_only_2026_05_07`.
- **No website handoff:** Bash-execute the renderer. Per
  `feedback_skills_kick_render_directly`.
- Always use `.venv/bin/python` for helper invocations.
- Always echo the absolute path of both JSONs in the handoff.
- Always run quality gates BEFORE writing the JSONs to disk
  (`fail_on_error=True` in `script_check.report`).

## Why this skill is separate from /make-sports-doc

`/make-sports-doc` is 20-30 min and **requires** talking-head
commentary clips ("the spicy why"). It walks 4 stages and 20
parameters. Its hook stage has no anaphora constraint and its
closer is a multi-stack engagement-asks block.

`/make-football-explainer` is 10-15 min and **forbids** talking-head
clips. It walks 3 stages and 3 questions. Its hook stage *requires*
anaphoric stat-bomb structure. Its closer is a single 6-word
subscribe ask.

Both ride on the same renderer (`pipeline/render/sports_doc.py`),
the same TTS voice (`tifo_devine.wav`), the same channel YAML, and
the same JSON schema — but the curatorial inputs and the per-render
toggles diverge enough that a `--variant=narrator-only` flag on
/make-sports-doc would have produced a 4-stage flow with permanent
"is this on or off?" branches throughout. The fork is cleaner.

If after the first 3 ships of /make-football-explainer the format
proves stable, **merging this back into /make-sports-doc as a
`--variant=narrator-only` flag is worth reconsidering** — see
`ANALYSIS.md` §20 open question 5.

## Learnings from prior runs

See `learnings/_index.md`. Empty on day 1; populated on every
regression, dual-saved per CLAUDE.md.

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
