---
name: make-ranking
description: Author a Top-5 countdown tier-list Short — produces a script.json with the 50-60s narration in countdown structure (#5 → #1) plus a `ranks[]` array of {rank, subject, image_prompt_hint, footage?}. Each rank = one beat. Use when the user asks for a "tier list", "ranking", "top 5", "countdown", "worst X of all time", or "best X ever" Short — e.g. "make me a top-5 worst PL refereeing decisions short", "rank the most savage AITAs of last week", "top 5 stoppage-time goals". For single-story Shorts use /make-script; for cinematic shot-by-shot use /make-movie-short.
---

# /make-ranking — Top-5 countdown tier-list Short

This skill produces ranked-list Shorts in the format that's blowing up
in 2026: **show the payoff frame in frame 1, then countdown #5 → #1**.
Each rank is exactly one beat; the rank chip overlay is timed via
`beat[i].start` tokens — zero renderer changes needed.

You wear three hats: **Curator → Writer → Prompter**.

## How to run it

### 1. Pick the niche + ranking dimension

If the user named a dimension ("worst PL refereeing decisions",
"funniest AITA verdicts of 2025"), use it. If not, ask. Confirm:

- **Niche** — sports (channel: `sportstoriesanimated_ranked`) or
  reddit / aita / wiki (use a flat ranked variant of the matching
  base channel).
- **Ranking dimension** — what's being ranked, time period if any,
  bias/angle the user wants (controversial, wholesome, savage, etc).
- **Source pool** — the user may already have a slug pool in
  `data/intermediate/<channel>/raw/`, or you may need to pull fresh.

### 2. Hat 1 — Curator: pull a candidate pool of ~10-15 items

Need 10-15 candidates so you can pick the strongest 5.

For sports rankings, raw stories already exist in
`sportstoriesanimated/raw/` — list them and pick.
If the user wants a topic the existing pool doesn't cover, hand-author
new raw JSON files (one per moment) — schema is the same as
`/make-script`'s output. Optionally run `pipeline/wiki_research` per
moment to get pronunciation + cast dossier.

For Reddit-flavoured rankings, pull more than usual:

```bash
.venv/bin/python scripts/pull_stories.py reddit --subreddit AmItheAsshole --limit 15 --no-llm
```

`--no-llm` skips per-story rewrite (you don't want individual scripts
— you want raw bodies to compare).

### 3. Hat 1 — Curator: pick the 5 + assign rank order

Read the candidate raws. Pick **5 items** that:

- Span a clear severity gradient (#5 = mildly notable, #1 = the
  jaw-dropper). Don't pick 5 things of equal magnitude — the climb
  from #5 → #1 is the format's emotional engine.
- Each have a **single visual punchline** (one frame that captures
  the moment — VAR screen, freeze-frame celebration, screenshot of
  the comment etc). If a candidate needs 30s of context to land,
  drop it.
- For sports: each MUST have a real broadcast clip available on
  YouTube (the channel USP). If you can't find footage for one of
  the 5, swap it out — don't ship a sports ranking without footage.

### 4. Hat 2 — Writer: 50-60s narration in countdown structure

For each rank, narration follows a 4-line shape:
1. `Number X.` (rank label)
2. Setup: year + venue + minute + stakes
3. Buildup line — the action immediately before the goal. This is
   the `match_text` for the footage cut. Final word triggers the
   black-intro suspense pause, then the broadcast clip plays.
4. **Scorer outro** — single short sentence naming the scorer +
   what they did. Plays AFTER the footage as a separate beat with
   an animated illustration. This line is critical for muted viewers
   (80% of Shorts viewers), who never hear the broadcast. The
   `scorer_match_text` (e.g. `"Solskjaer pokes"`) is the substring
   that triggers the kit_lock injection on this beat.

Example:
```
Number three. 1999 Champions League final at Camp Nou, ninety-third
minute. Manchester United losing the treble. Beckham steps up to the
corner. Solskjaer pokes it in. United win the treble.
```
- match_text:        `"Beckham steps up to the corner"` → footage
- scorer_match_text: `"Solskjaer pokes"`               → animated kit-locked frame post-footage

**Total 150-180 words = ~55-60s spoken (Kokoro at 0.98x).** This is
the new sweet spot for 2026 — DO NOT shorten to 30s.

Strict structure:

```
<HOOK 1-2 sentences, 8-15 words>
Number five: <subject>. <one-sentence why it ranks>. <one-sentence colour/aftermath>.
Number four: <subject>. ...
Number three: <subject>. ...
Number two: <subject>. ...
Number one: <subject>. <one-sentence why it's #1>. <punchline-line that lands>.
LIKE if you agree with number one. COMMENT who you'd swap in.
```

**Rules:**

- **Hook (first 1.5s, ~5-10 words)** — must be a **Contrarian Take**
  or **Mistake Callout** (the 2026-winning hook patterns). Examples:
  - "Most of these refs should never work again."
  - "Number one on this list still hasn't apologised."
  - "These five mistakes cost teams entire seasons."
  - NOT "Today we're counting down…", NOT "Hi guys", NOT "POV:".
- **Each rank opens with the literal phrase "Number five" / "Number
  four" / "Number three" / "Number two" / "Number one"**. The beat
  splitter relies on these as clause boundaries; the rank-chip overlay
  in the channel YAML is timed off `beat[1..5].start` so the chips
  line up. Do NOT use "Coming in at five" or "At #5" — break the
  contract and the chips drift.
- **Closer** — must be the literal `LIKE if you agree … COMMENT who
  you'd swap in` (or close paraphrase). Survives the 2026 engagement-
  bait classifier because it asks for a JUDGMENT, not a vote.
- Conversational, present tense, no editorialising ("crazy story!").
- For sports: phonetic-respell foreign names (Aguero → ah-GWAIR-oh)
  in narration only; preserve original spelling in `ranks[].subject`
  so captions still read correctly.

### 5. Hat 3 — Prompter: image_prompt_hint per rank

For each rank, write a **15-25 word visual hint** that the autonomous
`pipeline/prompts.py` author will refine into a full per-beat prompt.
The hint should specify ONE main subject + one piece of action —
follow DESIGN.md §14 #11/#12 (one subject per beat, ≤50 tokens).

Bad: "England players angry about VAR ruling and the crowd booing"
(two subjects).

Good: "VAR review screen, soccer pitch behind, England striker's
disbelieving face in foreground"

For sports rankings, include the era kit / team colours so the
diffusion model gets the right look ("Sergio Aguero in Manchester
City 2011-12 sky blue home kit, arms outstretched mid-celebration").

### 6. Sports footage — find the broadcast clip per rank (REQUIRED)

For `sportstoriesanimated_ranked` only: every rank needs a `footage`
entry. Without footage, the channel's USP collapses. Format:

```json
"footage": {
  "match_text": "Number five",
  "url": "https://www.youtube.com/watch?v=<id>",
  "in_s": 12.4,
  "out_s": 15.0,
  "audio_mix": 0.5
}
```

`match_text` MUST be the literal "Number five" / "Number four" /
etc. — that's what `_load_footage_overrides` in make_shorts.py greps
for. `in_s` / `out_s` are seconds into the source video; aim for
2.5-3.5s per cut. `audio_mix: 0.0` keeps narration as the only
audio (v1 contract — see PIPELINE.md §"Stage 2 — script").

If you can't find a clip for a particular rank, drop that rank and
pick another from the candidate pool. **A sports ranking that ships
without 5 footage cuts is incomplete** (memory:
`feedback_sports_footage_is_usp.md`).

### 7. Output: script.json schema

Path: `data/intermediate/<channel>/scripts/<slug>.json` where slug is
e.g. `top5-pl-refereeing-2010s` or `top5-savage-aita-may2026`.

```json
{
  "slug": "top5-pl-refereeing-2010s",
  "hook": "Most of these refs should never work again.",
  "narration": "Most of these refs should never work again. Number five: ... Number four: ... Number three: ... Number two: ... Number one: ... LIKE if you agree. COMMENT who you'd swap in.",
  "title_options": [
    "Top 5 worst Premier League refereeing decisions",
    "5 ref decisions that ruined seasons",
    "The 5 refs the PL wants you to forget"
  ],
  "source_url": "https://en.wikipedia.org/wiki/...",
  "source": "manual:sports_ranking",
  "ranks": [
    {
      "rank": 5,
      "subject": "Sterling's goal vs France ruled out by VAR",
      "image_prompt_hint": "VAR review screen, soccer pitch behind, England striker's disbelieving face in foreground",
      "match_text": "Number five",
      "footage": {
        "match_text": "Number five",
        "url": "https://www.youtube.com/watch?v=...",
        "in_s": 12.4,
        "out_s": 15.0,
        "audio_mix": 0.0
      }
    },
    { "rank": 4, ... },
    { "rank": 3, ... },
    { "rank": 2, ... },
    { "rank": 1, ... }
  ],
  "footage": [
    { "match_text": "Number five", "url": "...", "in_s": 12.4, "out_s": 15.0, "audio_mix": 0.0 },
    { "match_text": "Number four", "url": "...", "in_s": 0.0, "out_s": 0.0, "audio_mix": 0.0 },
    { "match_text": "Number three", "url": "...", "in_s": 0.0, "out_s": 0.0, "audio_mix": 0.0 },
    { "match_text": "Number two",  "url": "...", "in_s": 0.0, "out_s": 0.0, "audio_mix": 0.0 },
    { "match_text": "Number one",  "url": "...", "in_s": 0.0, "out_s": 0.0, "audio_mix": 0.0 }
  ]
}
```

The top-level `footage[]` array is a flat copy of each rank's
`footage` block — that's the array `make_shorts.py:_load_footage_overrides`
already reads. Keeping both lets the spec template / future tooling
introspect `ranks[]` without reparsing the flat array.

### 8. Cast / character handling

For sports rankings, the cast moves per-beat (different player /
referee / manager each rank), so the per-character-seed mechanism
from `feedback_sports_character_consistency.md` is less load-bearing.
Still write a minimal `cast.json` so make_shorts.py doesn't fall back
to channel YAML defaults:

```json
{
  "narrator": {
    "description": "calm analytical sports historian, line-art editorial style",
    "default_emotion": "measured",
    "age_band": "middle-aged",
    "gender": "unspecified"
  },
  "supporting": []
}
```

Path: `data/intermediate/<channel>/cast/<slug>.json`.

### 9. Report back

```
✓ wrote ranking script to sportstoriesanimated_ranked/scripts/top5-pl-ref-decisions.json
  hook: "Most of these refs should never work again."
  ranks: #5 Sterling-vs-France · #4 ... · #3 ... · #2 ... · #1 Lampard-ghost-goal
  duration target: ~58s
  footage cuts: 5/5 sourced ✓
next: render via http://127.0.0.1:8765 — pick `sportstoriesanimated_ranked`
and hit Generate. The rank chips and closer panel are wired in the
channel YAML; nothing more to do.
```

If any rank is missing footage, flag it explicitly in the summary —
**don't silently ship a sports ranking with <5 footage cuts**.

## Important rules

- Always use `.venv/bin/python` for any pull commands.
- Never run stages 4-7 (TTS, image gen, ffmpeg) in this skill — stop
  at writing script.json + cast.json.
- The literal `Number five` / `Number four` / ... / `Number one`
  phrasing is a contract with the rank-chip renderer (it greps beats
  for these phrases to time the chip overlays). Break the phrasing →
  rank chips drift off the wrong beats.
- Sports ranking without 5 footage cuts is incomplete — never ship it
  to the user as "done."
- For Reddit/AITA rankings, copy `sportstoriesanimated/variants/ranked.yaml`
  to a new file (swap the image_style_prefix, TTS voice, and upload
  account); keep `ranked_chips: true` and the `closer_format` line.

## Rank-chip overlay — wired

Rank chips ("#5" → "#1") render automatically when the channel YAML
sets `ranked_chips: true`. The wiring lives in:

- `pipeline/captions.render_rank_chip(rank, out_path)` — yellow-border
  pill with stroked "#N", parallel to `render_closer_panel`.
- `pipeline/compose.py` — `compose`, `compose_clips`, and
  `compose_hybrid` all accept an optional `rank_chips: list[(beat_idx,
  png_path)]`. Each chip is overlaid at `(60, 200)` with
  `enable=between(t, beat[i].start, beat[i+1].start)`.
- `make_shorts.py` — detects ranked beats by scanning for the literal
  "Number five/four/three/two/one" prefixes, renders chip PNGs into
  `data/cache/<slug>/rank_chip_N.png`, threads them into compose.

If a beat list doesn't open with the rank phrases, chips are silently
skipped (the contract belongs to /make-ranking, not to the renderer).
Cache wipe in `_wipe_stale_per_beat_artefacts` clears
`rank_chip_*.png` on every recompose so a chip never drifts across a
beat re-split.
