---
name: SportsStoriesAnimated rivalry-recap format — last-N head-to-head, footage-only
description: New format on the channel that countdowns the last N competitive fixtures between two entities (#N → #1, chronological), one fixture per rank, real broadcast footage on every beat — NO AI image gen
type: project
---

Format scaffolded 2026-05-04. Sits on top of the existing `ranked` variant
(`sportstoriesanimated/variants/ranked.yaml`) — same renderer, same rank-chip
overlay, same channel YAML, same closer panel. **Footage-only**: no
Z-Image-Turbo / per-beat AI imagery on this format.

## What it is

A "last N head-to-head" rivalry recap Short. Two entities (clubs or players),
last N competitive fixtures, ordered **chronologically** (oldest = #N, most
recent = #1). Each rank = one fixture, with a real broadcast clip covering
the whole rank's narration duration (~10s). No animated illustrations, no
diffusion model loaded — the lowest-memory Short on the channel.

## Why this isn't just /make-ranking

`/make-ranking` orders by **severity gradient** + uses animated illustrations
with footage as the climactic accent. Rivalry recap orders **chronologically**
+ goes **footage-only** end-to-end. Splitting the skills keeps each
curatorial prompt tight rather than branching one bloated skill.

## How to apply

- **User asks for "last 5 X vs Y"** → invoke `/make-rivalry-recap`.
  - "Top 5 X" / "best X of all time" / generic ranking → `/make-ranking`.
- Run `scripts/sportstoriesanimated/research_rivalry.py` first to pull the
  last N fixtures from the rivalry's Wikipedia article.
  - **WARNING (verified the hard way 2026-05-04):** The rivalry's main
    Wikipedia page summarises *notable* matches, not strictly the last N.
    The first mufc-vs-lfc-last5 run extracted Liverpool 7-0 United (2023)
    as the most-recent fixture — three years of competitive meetings
    silently dropped. **Always WebSearch the matchup with explicit recent
    years and cross-check against ESPN/PL/club fixture pages before
    authoring.** If the helper output is iconic-rather-than-recent,
    OVERWRITE `raw/<slug>.json` with the verified data.
- Football only on this channel.
- Most recent fixture is `#1`. Don't promote a mid-list headline (e.g.
  Liverpool 7-0) to #1 — break chronology and the chips drift conceptually.
  Mention the headline in the hook instead.
- N broadcast cuts required. <N is incomplete and should not ship.
- Output paths:
  - raw → `sportstoriesanimated/ranked/raw/<slug>.json`
  - script → `sportstoriesanimated/ranked/narrations/<slug>.json`
  - cast → `sportstoriesanimated/ranked/narrations/<slug>.cast.json`

## script.json shape (footage-only)

- `ranks[]` is THIN: `{rank, subject, match_text, winner}`. NO `kit` /
  `face` / `image_prompt_hint` / `scorer_match_text` fields (those
  belong to `/make-ranking`).
- `ranks[i].winner: {team, emoji, label}` — added 2026-05-04. Per-
  fixture result chip data. `team` is one of `<entity_a>`, `<entity_b>`,
  or `"Draw"`. `emoji` is the visual marker (🤝 for draws, 🔴 / team
  colour for wins). `label` is the chip text (e.g. `"UNITED WIN 3-2"`,
  `"DRAW 2-2"`). Renderer follow-up Path C honours this.
- `footage[]` has one entry per rank, with `match_text` = the rank phrase
  (`"Number five"` / ... / `"Number one"`) and a flag
  `covers_full_rank: true` telling the renderer the clip should occupy
  every beat from this rank's opening to the next rank's opening.
- `cta_overlay: {buttons, position, y_offset_pct, persistent}` — added
  2026-05-04. Top-of-frame LIKE+SUBSCRIBE chip overlay shown for the
  whole Short, parked at `y_offset_pct` from the top edge (default 8 —
  below YouTube's title bar but above rank-chip / caption zones).
  Renderer follow-up Path D.
- `visual_mode: "footage_only"` at the top level, so dashboard / future
  tooling can introspect the format without inferring it from missing
  fields.

## Renderer follow-up — REQUIRED before this format ships clean

The current `make_shorts.py` + `compose.compose_hybrid` path attaches
footage to the SINGLE beat whose text contains the `match_text`
substring; surrounding beats still get Z-Image-Turbo images. For
footage-only rivalry recaps, every beat in a rank's window must use
that rank's footage clip. There are also two new overlays the schema
references that need rendering hooks.

- **Path A — span-aware footage attach.** Honour
  `footage[].covers_full_rank: true` by setting `b.footage = clip` on
  every beat from the rank's opening to the next rank's opening, with
  `in_s` / `out_s` re-sliced proportionally. Image gen for those beats
  becomes a no-op. (Alternate Path B: coalesce ranks into single beats —
  works unchanged but drops karaoke-caption granularity.)
- **Path C — winner chip overlay.** Read `ranks[i].winner` and draw a
  result chip (color block + emoji + label) alongside the existing
  rank chip during each rank's footage window. Position: top-third,
  opposite side from rank chip. Without this, viewers scrolling on
  mute don't get the result until the verdict outro is captioned.
- **Path D — persistent CTA overlay.** Read `cta_overlay` and render a
  LIKE + SUBSCRIBE chip pair at `y_offset_pct` from top (default 8 —
  below YT's title bar, above rank-chip zone) for the entire Short.
  Subtle pulse animation OK. In addition to the closer panel, not a
  replacement.

Paths C and D are forward-compat — the script ships fine without
them, but the per-fixture result and persistent CTA are missing the
visual layer. Until they land, the spoken verdict outro carries the
result, and the closer panel is the only LIKE/SUBSCRIBE prompt.

## Files shipped 2026-05-04

- `.claude/skills/make-rivalry-recap/SKILL.md` — the skill itself.
- `scripts/sportstoriesanimated/research_rivalry.py` — fixture-pull helper
  (Wikipedia article → claude CLI structured output → raw JSON with
  per-fixture metadata + YouTube research queries).
- `sportstoriesanimated/ranked/raw/mufc-vs-lfc-last5-template.json` +
  `sportstoriesanimated/ranked/narrations/mufc-vs-lfc-last5-template.json`
   — shape-only footage-only templates. Do NOT render as-is; placeholder
  URLs and prose.
- `sportstoriesanimated/ranked/raw/mufc-vs-lfc-last5.json` — first real
  research output (Wikipedia rivalry-page extraction; gives iconic-rather-
  than-strict-last-5; needs cross-check against recent seasons before
  authoring).

## Open follow-ups

- **Renderer Paths A, C, D** (above) — span-aware footage attach
  (`covers_full_rank`), winner chip overlay (`ranks[i].winner`),
  persistent CTA overlay (`cta_overlay`).
- Wire a YouTube clip-search MCP path (vidlens `findVideos` or yt-dlp
  `ytsearch`) into `research_rivalry.py` so candidate URLs come back
  with the fixtures, not just search query strings.
- For literal-last-N vs iconic-N — extend the helper to optionally
  pull from a fixture API (ESPN / fotmob / PL fixture endpoint) rather
  than only Wikipedia, since the rivalry main page tends to be curated
  rather than chronological. Cleaner than relying on the writer to
  WebSearch + overwrite the helper output every run.
