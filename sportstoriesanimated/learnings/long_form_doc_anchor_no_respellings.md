# narration_anchors must be respelling-free

**Class-of-bug · 2026-05-05 · Ronaldinho lost-years run**

## Symptom

`/make-sports-doc` produces a `footage_plan/<slug>.json` whose
`match_footage[].narration_anchor`, `b_roll[].narration_anchor`, and
`archival_footage[].narration_anchor` fields are verbatim phrases
copied from `narrations/<slug>.json` chapter prose. The renderer
then whisper-transcribes the synthesised narration WAV and fuzzy-
matches each anchor to find an `at_s` for the overlay.

When the prose contains pronunciation respellings (`ron-al-DEEN-yo`,
`bare-na-BAY-oo`, `at-LEH-tee-koh mee-NAY-roo`, `ess-TAH-dee-oh
az-TEH-kah`), TTS speaks the phonetic glyphs as phonemes — Kokoro
on the Ronaldinho run produced narration like *"ron alde decline
he won eight major trophies on three"* (whisper transcription of
"ron-al-DEEN-yo's decline he won eight major trophies on three
continents"). The anchor `'ron-al-DEEN-yo, in the second leg, scores'`
does not appear in the transcription as a fuzzy match because
whisper's bag-of-tokens is `['ron', 'alde']` not
`['ron', 'al', 'deen', 'yo']`.

Net effect on the Ronaldinho run: **12/34 anchors matched**, 14
match-footage / 2 archival / 3 b-roll overlays silently dropped.
The mp4 rendered to completion but with 60% of the visual layer
missing.

## Rule

**Every `narration_anchor` MUST be respelling-free.** Use only:

- Plain English words that whisper-transcribes verbatim from the
  Kokoro/F5 output.
- ASCII letters + digits + standard ASCII spaces.
- 4–8 word fragments, distinctive enough that they appear once
  in the prose (passes the imaginary cmd-F-uniqueness test).

**Do NOT use** in anchors:

- Hyphenated phonetic respellings (`ron-al-DEEN-yo`,
  `bare-na-BAY-oo`).
- Apostrophes inside words (`Newell's`, `d'Or` — whisper drops
  the apostrophe inconsistently).
- Em-dashes or ellipses.
- Diacritics (é, ã, ç) — Kokoro pronounces them but whisper may
  transcribe them without diacritics.
- Numbers in numeric form when prose uses the spelled-out form
  (or vice versa) — pick whichever the prose uses verbatim.

## Authoring

- The PROSE keeps the respellings (TTS pronunciation correctness
  per `feedback_pronunciation_pretts.md`).
- The ANCHOR uses the surrounding English context phrase. Examples
  from the Ronaldinho footage_plan:

| ❌ broken anchor | ✅ fixed anchor |
|---|---|
| `Forty-seventh minute. ron-al-DEEN-yo takes a corner` | `Forty-seventh minute. He takes a corner.` (no, "He" is too generic) → `takes a corner` is too short → use `the corner is met by Diego Tardelli on the volley` |
| `at-LEH-tee-koh win the shootout, four-three` | `win the shootout, four-three, on a penalty by Leandro Donizete` (English plus a unique proper noun whisper handles) |
| `In the twenty-third minute, ron-al-DEEN-yo scores from a free kick` | `In the twenty-third minute` (concise, distinctive numeric) |
| `BEH-loo oh-ree-ZON-tchee is not, in football terms, a glamour city` | `is not, in football terms, a glamour city` |
| `the Mexico City Estadio Azteca` | `Mexico City to play Club América` (drop the proper-noun-with-diacritic stadium name) |
| `The match is at the ess-TAH-dee-oh az-TEH-kah` | `The same stadium where Maradona scored the goal of the century` (use the surrounding context, not the respelled name) |
| `The ah-MEH-ree-kah fans applaud` | `Not after the match. Not after the second goal.` (use the rhythmic-sentence preamble) |
| `the Barcelona half — bare-na-BAY-oo standing ovation, Ballon d'Or` | `a story of two halves` (drop em-dash + respelling + apostrophe) |

## Skill rule for `/make-sports-doc`

Append to the skill's `## Important rules` section:

> **Anchors must be respelling-free.** Prose keeps phonetic
> respellings for TTS correctness; anchors use only the surrounding
> English context. Whisper transcribes Kokoro/F5's spoken phonemes,
> not the source text — anchors with `ron-al-DEEN-yo` /
> `bare-na-BAY-oo` / etc. always miss. Test in your head: would
> cmd-F find this anchor in a whisper transcript of the narration
> (NOT in the source prose)? If no, lengthen or de-respell.

## Class-of-bug also affects

- `/make-katha` (Hindi narration with phonetic forced syllables)
- `/make-sleep-history` (foreign place names like Bar-le-Duc)
- `/make-cosmos-decoder` (foreign physicist names)
- Any future long-form skill that uses pronunciation respellings.

Mirror this rule into each skill's anchor-authoring section.

## Status on Ronaldinho run

- v1 mp4 rendered with 9/23 overlays surviving:
  `sportstoriesanimated/long_form/ronaldinho-trophies-after-you-stopped-watching.mp4`
  (27.2 min · 106 MB).
- v2 fix path: rewrite all `narration_anchor` fields in
  `sportstoriesanimated/footage_plan/ronaldinho-trophies-after-you-stopped-watching.json`
  to be respelling-free, then re-render. TTS chunks are cached so
  re-render is fast (Stages 2-7 only, ~10 min).
