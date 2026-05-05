---
name: make-rivalry-recap
description: Author a "last N head-to-head" rivalry recap Short — produces a script.json that countdowns the most recent N fixtures between two entities (#N → #1), one fixture per rank, real broadcast footage on every beat (NO AI-generated imagery). Use when the user asks for a "last 5 meetings" / "head-to-head" / "rivalry recap" Short — e.g. "Man Utd vs Liverpool last 5 matches", "Real Madrid vs Barca last 5 Clasicos", "Nadal vs Federer last 5 matches". For a single iconic moment use /make-script; for a generic ranked list (top 5 worst refs etc) use /make-ranking; for cinematic shot-by-shot use /make-movie-short.
---

# /make-rivalry-recap — last-N head-to-head footage-only Short

This skill produces **rivalry recap** Shorts: pick two entities, take their
last N fixtures, and countdown one fixture per rank (#N → #1, most recent =
#1) with **real broadcast footage on every beat**. No animated illustrations,
no AI image gen — the whole Short is footage clips with narration on top
plus the rank-chip overlay and closer panel.

It is a SPECIALISATION of `/make-ranking`, but with TWO key differences:

1. **Curatorial constraint** — five ranks must be the **last five fixtures**
   (chronological, most recent = #1), not five hand-picked items across
   history.
2. **Visual mode** — footage-only. No `ranks[i].kit` / `.face` /
   `.image_prompt_hint` fields, no per-beat Z-Image-Turbo render. One
   footage clip per rank, long enough to cover the rank's entire narration
   (setup + buildup + climax + reaction beats).

This makes the format the **lowest-memory** Short on the channel — no
diffusion model loaded, no GPU image queue, just yt-dlp + ffmpeg trim +
TTS + compose.

You wear two hats: **Researcher → Writer**.

## How to run it

### 1. Confirm the matchup + N

If the user named both entities (`"Man Utd vs Liverpool last 5 matches"`),
proceed. If not, ask. Confirm:

- **Entity A and Entity B** — two clubs, two players, two national
  sides; football only on this channel. Tennis/cricket need a different
  channel/variant.
- **N** — 3 or 5. Default 5.
- **Fixture filter** — all comps, league only, knockouts only? Default:
  all competitive fixtures (PL + cups + Europe), exclude friendlies.
- **Recency window** — "last 5 ever" or "last 5 in this competition"?
  Default: last 5 competitive meetings, period. State your assumption
  back to the user before researching.

### 2. Hat 1 — Researcher: pull the N most recent fixtures

For each fixture you need: date, competition, venue, score, and the
defining moment (the one frame the highlight reel cuts to). The
"can be anything" rule from the user's brief: the moment is the
**most-replayed beat** of that fixture — winning goal, comeback
equaliser, red card, missed penalty, controversial VAR, brawl, late
winner.

Run the helper:

```bash
.venv/bin/python sportstoriesanimated/scripts/research_rivalry.py \
    --entity-a "Manchester United" --entity-b "Liverpool" --last 5 \
    --slug "mufc-vs-lfc-last5" \
    --out sportstoriesanimated/ranked
```

The helper writes `sportstoriesanimated/ranked/raw/<slug>.json` with
per-fixture date / venue / score / key_moment / key_player + a YouTube
search query string per fixture.

**WARNING — verify what the helper returned. This is not optional.**
The rivalry's main Wikipedia page summarises *notable* matches, NOT
the literal last N. The 2026-05-04 mufc-vs-lfc-last5 run learned this
the hard way: the helper output stopped at 2023-03-05 (Liverpool 7-0
United, the last "iconic" entry on the Wikipedia rivalry page) and
silently dropped FIVE-PLUS subsequent fixtures (Sep 2024, Jan 2025,
Oct 2025, May 2026, etc).

**Always do this verification before authoring narration:**

1. WebSearch the matchup with explicit recent years
   ("<Entity A> vs <Entity B> head to head fixtures 2024 2025 2026").
2. Cross-check each helper-reported fixture against ESPN match pages,
   PremierLeague.com fixture pages, club official match centres
   (manutd.com/matches, liverpoolfc.com/matches, etc).
3. If the helper output is iconic-rather-than-recent, OVERWRITE
   `raw/<slug>.json` with the verified literal last-N before moving
   on. The narration in step 5 must reference real fixtures, scores,
   and goal timings — not what the helper extracted.

Pass `--wiki-list-url` pointing at a "List of <X> matches" page when
one exists (e.g. List of El Clásico matches), but even that needs
spot-checking against fixture databases — Wikipedia list pages can
lag a season behind.

### 3. Hat 1 — Researcher: find one footage clip per fixture

For each of the N fixtures, find ONE broadcast clip on YouTube that
covers the fixture's defining moment + enough surrounding context to
sustain the rank's narration duration (~10s). Tests:

- **Single visual punchline** — one clean buildup → climax → reaction
  arc. ≤12 seconds of broadcast footage. If a fixture's drama is a
  slow burn (five tactical phases), drop it — that's a long-form
  piece, not a Short rank.
- **Real broadcast clip exists** — channel USP. UEFA / Premier League /
  league-official uploads on YouTube preferred. If a fixture has no
  clean clip available, flag it: "Fixture 3 of 5 has no broadcast clip
  on YouTube — drop it and step back to the 6th-most-recent, or ship
  with 4 ranks?" Don't silently ship a sports rivalry recap with
  missing cuts.
- **Honours the rivalry frame** — "Liverpool 7-0 United" must be the
  Liverpool side of the moment. If a fixture's defining moment is a
  non-rivalry incident (e.g. a serious injury to a third player), pick
  the next most-replayed beat instead.

Use the vidlens MCP `findVideos` tool (or yt-dlp search fallback) on
the per-fixture `broadcast_research_query` strings the helper produced.

### 4. Rank order — most recent fixture is #1

Unlike `/make-ranking` (severity gradient), rivalry recaps order
**chronologically**: the oldest of the N is #5, the most recent is #1.
This matches viewer intuition ("counting back to the latest one") and
the closer beat lands on the freshest meeting — what the comments
section is most likely to argue about.

If a fixture in the middle is dramatically more iconic than #1
(e.g. Liverpool 7-0 United is fixture #3 in the last-5), DO NOT promote
it. Keep chronology. Mention it in the hook instead:
"Last five — including the seven-nil." That preserves the contract
with the rank-chip overlay (chips line up with chronological position)
while still flagging the headline moment.

### 5. Hat 2 — Writer: 50-60s narration

Total 150-180 words = ~55-60s spoken (TTS at ~0.98x). DO NOT shorten.

Strict structure:

```
<HOOK 1-2 sentences, 8-15 words, names BOTH entities>
Number five. <date / venue / comp>. <score>. <buildup line>. <verdict outro>.
Number four. ...
Number three. ...
Number two. ...
Number one. ...
LIKE if you were there. COMMENT next rivalry to recap.
```

**Rules:**

- **Hook (first 1.5s, ~8-15 words)** — must name BOTH entities and
  frame the rivalry. Hook patterns that work:
  - *Contrarian Take:* "These five matches show why United fans never
    sleep against Liverpool."
  - *Mistake Callout:* "Four of these last five, one side bottled it."
  - *Stakes:* "Last five Clasicos — three trophies decided in the
    final minute."
  - NOT "Today we're counting down…", NOT "Let's look at…", NOT
    "POV: you're a Reds fan".
- **Each rank opens with the literal "Number five" / "Number four" /
  "Number three" / "Number two" / "Number one"** — non-negotiable
  contract with the rank-chip renderer
  (`make_shorts.py:1674-1697`). Do NOT use "First up" / "Coming in at
  five" / "At #5" — the chips will silently skip.
- **Each rank's body (~25-35 words):**
  1. *Setup:* `<date / venue / competition>. <final score>.`
     e.g. "March 2023, Anfield, Premier League. Seven-nil."
  2. *Buildup line:* the action immediately before the moment. This
     is the `match_text` for the footage cut. Must be a literal
     substring of the narration so the beat aligner finds it.
  3. *Verdict outro:* one short sentence on the consequence — title
     swing, manager fired, end of an era. Keeps narration moving over
     the celebration / reaction frames at the tail of the footage clip.
- **Closer** — must be the literal `LIKE if you were there. COMMENT
  next rivalry to recap.` (or close paraphrase that asks for a
  judgment + an opt-in for next video). Keeps the channel's
  judgment-bait pattern (memory:
  `feedback_closer_caption_style.md`).
- Conversational, present tense, no editorialising ("crazy match!").
- Phonetic-respell foreign names in narration only; preserve original
  spelling in `ranks[].subject` so captions still read correctly.

### 6. Output: script.json schema (footage-only)

Path: `sportstoriesanimated/ranked/narrations/<slug>.json`. Slug shape:
`<entity-a-short>-vs-<entity-b-short>-last<N>`, e.g.
`mufc-vs-lfc-last5` or `madrid-vs-barca-last5`.

```json
{
  "slug": "mufc-vs-lfc-last5",
  "hook": "Last five United-Liverpool — three of them swung the title race.",
  "narration": "Last five United-Liverpool — three of them swung the title race. Number five. <fixture #5 narration>. Number four. <fixture #4>. Number three. <fixture #3>. Number two. <fixture #2>. Number one. <fixture #1, most recent>. LIKE if you were there. COMMENT next rivalry to recap.",
  "title_options": [
    "Last 5 Man Utd vs Liverpool — every iconic moment",
    "Manchester United vs Liverpool — the last 5 meetings ranked",
    "Every Man Utd vs Liverpool moment from the last 5 matches"
  ],
  "source_url": "https://en.wikipedia.org/wiki/Liverpool_F.C.%E2%80%93Manchester_United_F.C._rivalry",
  "source": "manual:rivalry_recap",
  "rivalry": {
    "entity_a": "Manchester United",
    "entity_b": "Liverpool",
    "fixtures_window": "last 5 competitive meetings",
    "as_of_date": "2026-05-04"
  },
  "ranks": [
    {
      "rank": 5,
      "subject": "<oldest of the N — fixture summary one line>",
      "match_text": "Number five",
      "winner": {
        "team": "Draw",
        "emoji": "🤝",
        "label": "DRAW 2-2"
      }
    },
    { "rank": 4, "subject": "<...>", "match_text": "Number four",
      "winner": {"team": "Liverpool", "emoji": "🔴", "label": "LIVERPOOL WIN 3-0"} },
    { "rank": 3, "subject": "<...>", "match_text": "Number three",
      "winner": {"team": "Draw", "emoji": "🤝", "label": "DRAW 2-2"} },
    { "rank": 2, "subject": "<...>", "match_text": "Number two",
      "winner": {"team": "Manchester United", "emoji": "🔴", "label": "UNITED WIN 2-1"} },
    { "rank": 1, "subject": "<...>", "match_text": "Number one",
      "winner": {"team": "Manchester United", "emoji": "🔴", "label": "UNITED WIN 3-2"} }
  ],
  "footage": [
    {
      "match_text": "Number five",
      "url": "https://www.youtube.com/watch?v=...",
      "in_s": 50.0,
      "out_s": 62.0,
      "audio_mix": 0.5,
      "black_intro": true,
      "covers_full_rank": true,
      "_design_notes": "<source channel + duration + key goal timings + which moment your in_s/out_s window targets. Without watching the clip you can't pick exact timestamps; document your best-guess so a reviewer can verify by sampling the clip at 1fps>"
    },
    { "match_text": "Number four", "covers_full_rank": true, "...": "..." },
    { "match_text": "Number three", "covers_full_rank": true, "...": "..." },
    { "match_text": "Number two",  "covers_full_rank": true, "...": "..." },
    { "match_text": "Number one",  "covers_full_rank": true, "...": "..." }
  ],
  "cta_overlay": {
    "buttons": ["LIKE", "SUBSCRIBE"],
    "position": "top",
    "y_offset_pct": 8,
    "persistent": true,
    "_render_note": "Persistent LIKE + SUBSCRIBE chip overlay near top of frame (~8% y-offset — below YouTube's title bar but above the rank-chip zone). Stays on for the entire Short, not just the closer. Different from the existing closer panel."
  }
}
```

**Per-rank winner chip** (`ranks[i].winner`) is the new viewer-signal
field added 2026-05-04. Three-tuple `{team, emoji, label}` — the
renderer can draw a result chip alongside the existing rank chip
during that rank's footage window so the viewer instantly sees who
won each fixture without having to parse the spoken score. Use:

- `team` — `"<Entity A name>"`, `"<Entity B name>"`, or `"Draw"`.
- `emoji` — visual marker. Both Premier League rivals are red, so the
  emoji alone won't disambiguate teams; the `label` field carries the
  text. Sensible defaults: `🔴` for either-team-wins (paired with
  team name in label), `🤝` for draws.
- `label` — short caption text e.g. `"UNITED WIN 3-2"`,
  `"LIVERPOOL WIN 3-0"`, `"DRAW 2-2"`. Two-up format works well in a
  Shorts top-third overlay.

**Persistent CTA overlay** (`cta_overlay`) added 2026-05-04. Top-of-
frame LIKE + SUBSCRIBE chips that stay on for the whole Short.
Specifically NOT flush to the very top (collides with YouTube's title
bar) — the `y_offset_pct: 8` parks the chip just below the topmost
edge. This is in addition to (not replacing) the closer panel.

Note the `ranks[]` shape is now MUCH thinner than `/make-ranking`'s —
no `kit`, `face`, `image_prompt_hint`, `scorer_match_text` fields.
Those were per-beat illustration controls; with footage covering
every beat, they're moot.

The footage `match_text` is the **rank phrase** ("Number five" / ... /
"Number one") — same anchor as the rank chip. The clip plays for the
whole rank's duration (~10s), so we anchor it to the rank's opening
beat, not a buildup substring partway through.

`covers_full_rank: true` is the renderer flag (LANDED 2026-05-05) that
spans the broadcast clip across all beats from this rank's opening
through the next rank's opening. Each beat in the span gets its own
proportionally sliced `in_s` / `out_s` window. Image gen is a no-op
for those beats — Metal GPU isn't touched for footage-covered ranks.
Implementation: `_attach_footage_to_beats` in `scripts/make_shorts.py`
+ early-continue in the image-gen loop.

### 7. Cast / character handling — minimal

Footage-only format; cast.json stays trivial. Just narrator.

```json
{
  "narrator": {
    "description": "calm analytical sports historian",
    "default_emotion": "measured",
    "age_band": "middle-aged",
    "gender": "unspecified"
  },
  "supporting": []
}
```

Path: `sportstoriesanimated/ranked/narrations/<slug>.cast.json`.

### 8. Renderer follow-up (TODO — flag in handoff)

Two forward-compat schema fields remain TODO. They don't block render
— the script produces a watchable Short without them — but they're
the intended viewer experience. Path A (span-aware footage attach)
LANDED 2026-05-05 and is documented for reference.

- **Path A — span-aware footage attach** (`covers_full_rank`).
  **LANDED 2026-05-05** in `scripts/make_shorts.py`:
  - `_attach_footage_to_beats` reads `covers_full_rank: true` and
    spans `[anchor, next_anchor)` beats, slicing `in_s` / `out_s`
    proportionally to each beat's audio duration. `black_intro` is
    kept on the leading beat only.
  - The footage-attach call moved BEFORE the image-gen loop so
    `b.kind == "footage"` is set before GPU work begins. The image
    loop early-continues for footage beats. Pre-fix, a 25-beat
    rivalry recap ran 25 image gens and timed out the Metal GPU
    around beat 15; post-fix, only non-footage beats (typically the
    hook + closer) hit Z-Image-Turbo.

- **Path C — winner chip overlay** (`ranks[i].winner`).
  Draw a result chip ({team, emoji, label}) alongside the existing
  rank chip during each rank's footage window. Suggested position:
  top-third, opposite side from rank chip. Chip styling: solid color
  background per team (red for the winning side, neutral grey for
  draws) + label text. Stays on for the full rank window, fades with
  the rank chip on transition. Without this, the result is conveyed
  via the spoken verdict outro — which works, but slows comprehension
  for a viewer scrolling on mute.

- **Path D — persistent CTA overlay** (`cta_overlay`).
  Render `cta_overlay.buttons` as a chip pair (e.g. 👍 LIKE +
  🔔 SUBSCRIBE) parked at `y_offset_pct` from the top of the frame
  for the full duration of the Short. Subtle pulse animation OK.
  Specifically NOT flush to the very top edge — it would collide with
  the YouTube Shorts title/handle bar. The chip should be above the
  rank chip / winner chip / captions zone but below the YT chrome.
  This is in addition to the closer panel, not a replacement.

If you ship without these, the Short still works:
- Path C missing → spoken verdict outro carries the result.
- Path D missing → only the closer panel asks for LIKE/SUBSCRIBE.

### 9. Kick off the render directly — DO NOT hand off to the website

The skill **starts the render itself** via a Bash tool call after writing
the JSONs. Do not tell the user to "go to http://127.0.0.1:8765 and click
Generate" — that handoff is dead. The render command is:

```bash
.venv/bin/python -u scripts/make_shorts.py \
    --script sportstoriesanimated/ranked/narrations/<slug>.json \
    --channel sportstoriesanimated/variants/ranked.yaml \
    --slug <slug>
```

**Do NOT pass `--out`.** As of 2026-05-05 `make_shorts.py` resolves the
output root from `--channel` via `pipeline.niches.NICHE_CHANNEL` —
`sports_ranked` lands cache + shorts + uploads under
`sportstoriesanimated/ranked/`. The legacy `data/` root is the
fallback only when nothing matches, with a loud warning. Pre-2026-05-05
runs that wrote to `data/shorts/<slug>.mp4` were a bug; per-channel
layout is the rule (memory: `feedback_channels_subdir_layout`).

Run this with `run_in_background: true` on the Bash tool. Shorts take
roughly 5-15 min end-to-end (TTS + footage trim + compose + critic +
upload). The user will be notified when the render completes; you do
NOT need to poll or sleep.

Output mp4 lands at `sportstoriesanimated/ranked/shorts/<slug>.mp4`.
If you see `data/shorts/<slug>.mp4` in the log, something failed
upstream of channel-dir resolution and you must investigate before
shipping.

Channel YAML's `upload.auto_upload` controls whether Stage 8 ships to
YouTube on success. If you want to force-disable upload for a dry run,
add `--no-upload`. The skill default is to honour the channel YAML.

If you want a console preview before kicking off the render, also write
out (don't just tell the user) a short summary:

```
✓ wrote rivalry recap to sportstoriesanimated/ranked/narrations/mufc-vs-lfc-last5.json
  rivalry: Manchester United vs Liverpool — last 5 competitive meetings
  hook: "Last five United-Liverpool — three of them swung the title race."
  ranks (chronological #5 → #1):
    #5  2022-04-19  Anfield        PL          Liverpool 4-0 United
    #4  2022-08-22  Old Trafford   PL          United 2-1 Liverpool
    #3  2023-03-05  Anfield        PL          Liverpool 7-0 United  ← headline
    #2  2024-04-07  Old Trafford   PL          United 2-2 Liverpool
    #1  2025-01-05  Anfield        PL          Liverpool 2-2 United
  duration target: ~58s
  footage cuts: 5/5 sourced ✓
  visual mode: footage-only (no AI image gen)
✓ render kicked off in background — job_id <id>; you'll be notified
  on completion. Until renderer Paths A/C/D land (SKILL.md §8), expect
  mixed footage + animated frames within each rank.
```

If any fixture is missing footage, flag it explicitly and DO NOT start
the render. **Don't silently ship a rivalry recap with <N cuts.**

## Important rules

- Always use `.venv/bin/python` for any helper commands.
- **After writing the JSONs, kick off the render via `scripts/make_shorts.py`
  directly with `run_in_background: true`.** Do not direct the user to the
  web UI at :8765 — that handoff was retired 2026-05-05 because telling
  the user "open this link and click Generate" is friction the skill
  should absorb. The skill owns the full author → render path.
- **Never pass `--out data` (or any `--out`) when invoking
  `make_shorts.py`.** The script resolves the per-channel state root
  from `--channel`. Passing `--out data` was the legacy default and
  silently violated the per-channel layout rule. If you find yourself
  about to set `--out`, stop — figure out why the channel resolution
  is failing and fix that instead. Memory: `feedback_channels_subdir_layout`.
- The skill itself only authors narration + cast + footage plan; the
  TTS / footage trim / compose / critic / upload stages run inside
  `make_shorts.py`. You are NOT writing those.
- The literal `Number five` / ... / `Number one` phrasing is the
  rank-chip contract — break it and chips silently drift off.
- Footage `match_text` is the **rank phrase** in this footage-only
  format (different from `/make-ranking` where it's the buildup line).
- Chronological order, not severity. Most recent = #1.
- Football only on `sportstoriesanimated_ranked`. Tennis / cricket
  rivalries need a different channel/variant.
- N footage cuts required. <N is incomplete and should not ship.
- Don't fabricate fixtures. If you can't verify the score / scorer /
  date from a primary source, ask the user or skip the fixture.
- No `kit` / `face` / `image_prompt_hint` fields on `ranks[]` in this
  format — those are for `/make-ranking`. Including them here is
  harmless (renderer ignores them when footage covers the rank) but
  signals you forgot which skill you're in.
- **Footage `in_s` / `out_s` are best-guess without watching the
  clip.** State your goal-timing reasoning in `_design_notes` per
  footage entry (e.g. "Maguire 84' header — in 119s edit, late drama
  ~85-95s, window 85-97 best-guess; verify by sampling at 1fps").
  The user / renderer / next agent will refine.
- **Verdict outro must convey who won in spoken word.** "A draw that
  flattered nobody.", "Three-nil Liverpool.", "United's first Anfield
  win in nine years." Not "an entertaining encounter" / "a tight
  affair" — those don't tell a mute viewer the result. The winner
  chip (Path C) is forward-compat; until it ships, narration carries
  the result.

## Learnings from prior runs

- **2026-05-04, mufc-vs-lfc-last5 (initial draft)** — research helper
  hit Wikipedia rivalry article, returned Liverpool 7-0 United (2023)
  as the most recent fixture even though the run date was 2026-05-04.
  Five-plus subsequent fixtures (2024 Old Trafford x2, 2025 Anfield
  x2, 2026-05-03 Old Trafford) were silently dropped. Resolution:
  WebSearch the explicit recent years, cross-check ESPN / PL / club
  fixture pages, OVERWRITE `raw/<slug>.json` with verified data
  before authoring narration. Codified above in §2 ("This is not
  optional").
- **2026-05-04** — added forward-compat schema fields:
  `ranks[i].winner: {team, emoji, label}` (per-rank result chip) and
  top-level `cta_overlay: {buttons, position, y_offset_pct,
  persistent}` (persistent LIKE/SUBSCRIBE chip near top of frame).
  Renderer follow-up tracked as Paths C and D in §8.
- **2026-05-05** — retired the website-handoff (`render via
  http://127.0.0.1:8765`). The skill now kicks off the render itself
  via `scripts/make_shorts.py --script ... --channel
  sportstoriesanimated/variants/ranked.yaml --slug ...` with
  `run_in_background: true` on the Bash tool. Reason: telling the user
  to switch to a browser tab and click Generate is friction; the skill
  owns the full author → render path. The website is still available
  for ad-hoc renders and dashboard-style monitoring, but no longer
  the skill's exit point.

## Why this skill is separate from /make-ranking

`/make-ranking` is a generic ranked-list authoring tool — any
dimension, any source pool, severity-ordered, animated illustrations
with footage as the climactic accent. `/make-rivalry-recap` constrains
the curatorial step to "the last N head-to-head fixtures between two
named entities", switches order from severity-descending to
chronological, and goes **footage-only** (no AI imagery at all). This
also makes it the lowest-memory Short on the channel: no diffusion
model, no GPU image queue, just yt-dlp + ffmpeg + TTS.
