---
name: SportsStoriesAnimated long-form documentary format (20-30 min)
description: New 2026-05-04 format for SportsStoriesAnimated — 20-30 min sports docs with real broadcast match footage + commentator/YouTuber talking-head clips + b-roll + chapter cards + lower-thirds. Distinct from Shorts format and from historyrecapped sleep mode.
type: project
---

This channel ships THREE formats in addition to Shorts:

1. **Single-moment Short** (15-25s) — `/make-script`
2. **Top-N tier-list / Last-N rivalry recap** (50-60s) — `/make-ranking`,
   `/make-rivalry-recap`
3. **Long-form documentary** (20-30 min) — `/make-sports-doc` ← THIS FILE

Long-form is **NOT a longer Short**. It's a different production with a
different center of gravity: footage drives, prose serves, editing
binds. The 20-param skill enforces this — every knob a user has
reached for on Shorts and needed but couldn't tune is pre-declared.

## What makes this format

- **20-30 min duration** (Shorts are 15-60s).
- **16:9 horizontal 1080p** (Shorts are 9:16 portrait).
- **Real broadcast match footage** at climactic moments — same USP as
  Shorts, but multiplied across many cuts within one doc.
- **Talking-head clips** — commentators, podcasters, other YouTubers
  giving spicy takes. THIS IS NEW. Lower-thirds attribute every clip.
- **B-roll** — stadium exteriors, fans, training, celebrations,
  city skylines — fills space between match cuts and talking heads.
  Cycled in the background; replaced when a pinned overlay fires.
- **Archival footage** for era anchoring (a 1986 Maradona cut to
  contextualize a 2022 Argentina piece).
- **Chapter cards** every 4-6 min — full-frame title cards, deep-teal
  slab + orange chapter number + bold white title, 3s with crossfade.
- **Lower-thirds** during talking-head clips — speaker name + handle.
- **Captions ON** (white bold, NOT yellow italic — the yellow italic is
  Sleepy Time History's signature, and we're a different niche).

## How it's distinct from historyrecapped sleep mode

Same channel-org (one OAuth, one upload account), but a different
config block (`long_form_doc:` vs. `long_form:`) because:

| dimension | sleep history | sports doc |
|---|---|---|
| duration | 60-120 min | 20-30 min |
| voice | Sarah (soft female F5) | Theo (measured male F5) |
| cadence | ~135 wpm (atempo 0.85) | ~155 wpm (atempo 1.0) |
| captions | yellow italic | white bold |
| music bed | single ambient loop | section-mood cycling |
| visuals | comic-illustrated panels OR archival w/ warm grade | real footage + b-roll cycling |
| chapter cards | none | every 4-6 min |
| lower-thirds | none | every talking-head clip |
| asks | 2 inline (early + closer) | 3 inline (early like + mid sub + closer comment) |
| support-ask tone | soft ASMR reverence | engagement-driving ("if you remember where you were…") |

## Authoring flow

1. User triggers `/make-sports-doc <subject>`.
2. Skill walks 20 parameters across 4 stages (Subject+Spine →
   Voice+Hook → Footage → Visual+Audio+Distribution).
3. For Bucket D (footage), the skill calls helper scripts:
   - `scripts/sportstoriesanimated/find_match_clips.py` — scores
     broadcast clips against narration moment phrases via whisper.
   - `scripts/sportstoriesanimated/find_commentary_takes.py` — scores
     pundit/podcaster soundbites with spice-token boosting.
   - `scripts/sportstoriesanimated/find_b_roll.py` — picks evenly-
     spaced windows across stadium/fan/training source URLs, with
     scene-cut snapping.
4. Output two JSONs:
   - `sportstoriesanimated/narrations/<slug>.json` (prose + chapters
     + asks + meta + tone + thesis)
   - `sportstoriesanimated/footage_plan/<slug>.json` (4 footage
     arrays + motion_graphics)
5. User reviews both, redirects if needed, then renders:
   ```
   caffeinate -i .venv/bin/python -u \
       scripts/sportstoriesanimated/render_long_form_doc.py \
       --channel sportstoriesanimated --slug <slug>
   ```

## Render pipeline (7 stages)

1. **TTS** — chunked F5/Theo with tone-aware speed/atempo. Reuses
   `synth_long_narration` from historyrecapped's renderer.
2. **Anchor alignment** — whisper-transcribes narration.wav, fuzzy-
   matches `narration_anchor` strings from footage_plan + chapter
   first-sentences to populate `at_s` for every overlay.
3. **Footage prep** — yt-dlp + `_trim_clip_letterbox` for each
   match/talking/archival/pinned-broll entry. Stream-copy short-
   circuit when src is already 1080p 16:9.
4. **Filler track** — background b-roll (entries WITHOUT
   narration_anchor) cycled to fill total narration duration.
5. **Overlays** — chapter cards + match + talking + archival +
   pinned b-roll, sorted by `at_s` LAST-first, each replacing the
   filler in its window via concat-demux.
6. **Music bed** — looped curated wav from `music/` if present, else
   synthesized ambient placeholder. Phase 2: section-mood cycling.
7. **Final mux** — narration + music + composed video, with watermark
   + lower-thirds + captions overlaid via single ffmpeg
   `filter_complex` pass.

Resumable: every stage caches to `cache/<slug>/`; interrupt + restart
picks up at the last completed substage. **Always wrap with
`caffeinate -i` and run with `python -u`** (per
`/docs/feedback_long_form_render_caffeinate.md`).

## Production-run learnings (live)

Banked while authoring the first doc (Benzema, 2026-05-04). The
canonical home for these is the skill itself —
`~/.claude/skills/make-sports-doc/SKILL.md` § "Learnings from production runs".
Read that section before starting a new doc. Quick index of what's
in there:

1. **Path A (prose first) vs Path B (helpers first)** — make this a Stage-0 ask
2. **vidlens query phrasing** — score + "extended highlights" beats abstract phrasing; run searches in parallel
3. **Vintage soundbites** — paraphrase + attribute, don't chase 15-year-old isolated clips
4. **Pronunciation respelling in prose** — F5/Theo mangles foreign names; respell DURING drafting (table of common names in the skill)
5. **narration_anchor design** — 5-10 distinctive words, first-occurrence wins
6. **Engagement-ask placement** — like at end of Ch1, sub at mid-doc pivot inside a chapter (not between), comment in closer; conditional clauses beat bare imperatives
7. **The "receipts" beat** — TELL the audience to verify; don't screenshot
8. **Direct-address visual cues** — "Watch this" needs a 1-2s narration pause before the cut (Phase 2: `lead_silence_s` field)
9. **Pinned-overlay budget** — ~25 max in v1's concat-demux loop; drop b-roll first
10. **Spine chapter** gets ~22-25% of word budget, not equal distribution
11. **Talking-head selection** — same-speaker contradiction across years is the strongest cut
12. **Schema gaps** — `embedded_in` for asks, `lead_silence_s` for clips, `normalize_for_tts` plumbing through long-form path

## Phase 1 (this commit) vs Phase 2 (next ship)

**Phase 1 — landed 2026-05-04 (this commit):**
- Skill at `.claude/skills/make-sports-doc/SKILL.md`
- Config block `long_form_doc:` in `sportstoriesanimated/config.yaml`
- Render orchestrator at `scripts/sportstoriesanimated/render_long_form_doc.py`
- Helpers: `find_match_clips.py`, `find_commentary_takes.py`, `find_b_roll.py`
- This learnings doc + memory entry

**Phase 2 — TODO after first ship:**
- `pipeline/stat_card.py` — animated stat overlays (#goals, xG, km run)
- `pipeline/pitch_diagram.py` — top-down tactical animations (Tifo-style)
- `pipeline/career_timeline.py` — animated horizontal timeline
- Section-mood music cycling — pick wav per chapter from
  `music/<mood>/` and crossfade
- Narration-ducking sidechain — proper sidechain compression instead of
  v1's amix weights
- Thumbnail builder fork — `scripts/sportstoriesanimated/build_long_form_thumbnail.py`
- `/critique-video` extension — long-form-specific lenses (footage
  density, chapter-card cadence, talking-head balance)

## Pinned rules (read these every time)

These are the same as Shorts — long-form does NOT exempt from them:

- `footage_is_usp.md` — channel without real footage cuts is incomplete
- `footage_window_must_include_buildup.md` — windows must show buildup
  + climax, not just climax
- `footage_cut_at_pivot.md` — cut to broadcast at buildup beat, not
  punchline beat
- `footage_align_to_commentary.md` — pin in_s/out_s to commentator's
  word timestamps via whisper
- `footage_blurred_letterbox.md` — 16:9→9:16 keeps full broadcast width
  (less relevant for 16:9 doc output, but still applies for 4:3 archival)
- `footage_scrub_watermarks.md` — strip source-channel logos
- `no_narrator_on_screen.md` — `narrator_visual_mode: voice_only` is
  channel-locked; long-form does NOT depict the narrator either
- `feedback_pronunciation_pretts.md` — Mbappé → Em-BA-pay before TTS

Long-form-specific (NEW):
- Talking-head clips ALWAYS duck the narrator — two voices on top is
  amateur-hour. Lower-third makes the speaker visible while their
  voice owns the bed.
- Talking-heads should CONTRADICT each other where possible — a doc
  where every clip says the same thing isn't an argument, it's
  agreement, and agreement is boring.
- Chapter cards every 4-6 min — long-form without chapter cards reads
  as one undifferentiated block.
- Three engagement asks for 25-min docs (early like ~3-4 min,
  mid-doc subscribe ~13-15 min, closer comment_prompt). Sleep mode's
  TWO-asks rule does NOT apply.
