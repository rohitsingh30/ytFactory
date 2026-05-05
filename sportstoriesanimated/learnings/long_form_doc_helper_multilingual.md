# find_match_clips fuzzy-match collapses on multilingual broadcasts

**Class-of-bug learning · 2026-05-05 · Ronaldinho lost-years run**

## Symptom

`scripts/sportstoriesanimated/find_match_clips.py` whisper-aligns each
source clip then fuzzy-matches a list of moment phrases against the
transcript. On the Ronaldinho doc (Portuguese GLOBO + Spanish TUDN
+ Spanish Telefuturo + English skills compilations across 7
sources), two passes returned:

- Pass 1 (English moment phrases): **1/12** matches.
- Pass 2 (broadcast-language phrases — `gol`, player names, etc.):
  **6/14** matches, but **5 of the 6** were coincidental — fuzzy
  match landed on the right TOKEN in the wrong CONTEXT (an
  intro-graphic frame, a different player, a different match in the
  same source). Only **1/14** was a usable narration anchor.

In other words: when the source-pool spans multiple broadcast
languages, the helper's value drops from "validates windows" to
"adds noise the user must filter."

## Root cause

The fuzzy matcher is bag-of-tokens over a normalized transcript. It
has no semantic notion of:

- Which language each source is in (so cross-language lookups
  succeed against any near-spelling token).
- Match-context — `Tardelli` appears in every Atlético broadcast
  intro; `gol` appears every 8 seconds; `tiro libre` appears in
  every Spanish football broadcast.
- Time-locality — a 1-token match in a 4881s source has the same
  score as a 5-token match in a 132s clip.

For mono-broadcast docs (the Benzema run was English-heavy with
some Spanish La Liga), this is fine. For docs whose source pool is
intentionally multilingual (any subject playing across South America
+ Europe + Asia), it breaks down.

## Treatment

### Authoring side (this skill — `/make-sports-doc`)

- **Don't trust pass-1 helper output** for multilingual subjects.
  Use the helper to confirm download success + confirm transcripts
  are clean (no `¡gracias × N` hallucinations); manually pick
  windows from the source structure (highlight compression ratio,
  known goal-minute → highlight-minute translation, end-of-clip =
  trophy lift / penalty shootout).
- **Reserve the helper for distinctive multi-token phrases**
  ("tres a cero al noventa y cuatro" / "gol gol gol de Ronaldinho
  Gaúcho") that contain a numeric scoreline or named player +
  minute. Single-token searches (`Tardelli`, `gol`) will fire
  everywhere and match nothing usefully.
- **Author footage_plan windows from broadcast structure** by
  default. The renderer aligns overlays to NARRATION via whisper
  on the narration audio; the source-side `(in_s, out_s)` just has
  to bracket the right action with adequate buildup. ±5s of slop
  on the source-side is fine.

### Helper side (Phase 2 — `find_match_clips.py`)

Three improvements that would make the helper trustworthy on
multilingual sources:

1. **Per-source language tag** — pass `--source-langs` (one per
   URL) and let whisper run with a forced language; raise the score
   threshold and require N-gram (not single-token) matches.
2. **Reject low-context matches** — score = 0 unless the matched
   phrase's transcript window contains at least 3 distinct
   content-bearing tokens. Suppress matches in the first/last 3% of
   the source duration (intro graphics, end credits).
3. **Per-language phrase translations** — accept a JSON mapping
   `{moment: {lang: phrase}}` instead of a flat comma-separated
   string. Match each source's transcript against its language's
   phrase only.

(File a separate task before the next non-English long-form run.)

## Cross-references

- See `feedback_sports_footage_align_to_commentary.md` (channel
  default — the helper's intended use case).
- See `feedback_sports_footage_window_must_include_buildup.md` for
  the buildup-bracketing rule that makes ±5s slop acceptable.
- The Ronaldinho run footage_plan
  (`sportstoriesanimated/footage_plan/ronaldinho-trophies-after-you-stopped-watching.json`)
  has v1 estimated windows that ignore the helper output for the
  reason above.
