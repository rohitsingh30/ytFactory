---
name: cloudrun_chatterbox silently truncates at ~40s of audio for single-paragraph inputs
description: First /make-cosmos-short run on `ligo-2015-gw150914-short` v1 sent a 154-word / 960-char single-paragraph script and got back a 40s wav containing only the first 97 words; the closer dropped silently. Root cause: `_synth_cloudrun_chatterbox` did NOT route through `_synth_cloudrun_chunked` (unlike indicparler/indicf5 which did). Fixed in `pipeline/tts/cloudrun.py` 2026-05-08 — chatterbox now chunks AND single-paragraph long inputs fall back to sentence-group chunking.
type: feedback
---

# cloudrun_chatterbox silently truncates at ~40s of audio

**Rule:** /make-cosmos-short narrations MUST contain `\n\n` paragraph
breaks (3+ paragraphs). The chunking helper splits on these and issues
multiple shorter cloud calls; without them, chatterbox returns a
40-second wav regardless of input length.

**Why:** First-run on `ligo-2015-gw150914-short` v1 produced a closer-
less mp4. mlx_whisper retranscription of `narration.wav` showed it
contained only 97 of the 154 script words, ending mid-sentence at
"…thirty-six and twenty-nine solar masses," at t=39.74s. The wav was
truly truncated — not a beats.json bug. Chatterbox cloud has an
undocumented ~40s/~95-word cap per /synth call.

**The latent fault** was that `_synth_cloudrun_chatterbox` called
`_synth_cloudrun` (single-shot) directly, while sibling functions
`_synth_cloudrun_indicparler` and `_synth_cloudrun_indicf5` both
routed through `_synth_cloudrun_chunked`. The chunking helper's docstring
explicitly named the truncation issue for indic providers; chatterbox
quietly had the same problem and nothing in the code path detected it.

**How to apply (skill-side, shipped 2026-05-08 v2):**
- Author narrations with `\n\n` between logical beats. 4-6 paragraphs
  is right for a 50-60s Short.
- If a single paragraph exceeds ~350 chars, the new sentence-group
  fallback in `_synth_cloudrun_chunked` kicks in — but explicit
  breaks are still preferred.

**How to apply (pipeline-side, shipped 2026-05-08 v2):**
- `pipeline/tts/cloudrun.py::_synth_cloudrun_chatterbox` now routes
  through `_synth_cloudrun_chunked` (parity with indic providers).
- `_synth_cloudrun_chunked` now uses a new helper `_split_for_chunked_synth`
  which: (a) prefers `\n\n` paragraph splits when available, (b) falls
  back to sentence-group splits when input is single-paragraph >350
  chars, (c) returns the unsplit text when input is short enough to
  go single-shot safely.

**Two-strikes rule:** if any future /make-cosmos-short or
/make-cosmos-long render produces a wav shorter than 80% of expected
duration (`words / wpm × 60s`), escalate to a hard pre-mux gate in
`pipeline/render/footage_only.py::main` that fails the render with
an explicit error rather than shipping a closer-less Short.

**Status (2026-05-08 v2):** LIGO Short v2.2 renders cleanly with the
closer present. Verified via mlx_whisper retranscription — last words
"Subscribe for more decoders. Like if this changed how you see physics."
land at t=75.04s in v2 (with 186 words; v2.2 trimmed to 133 words to
hit the 50-60s band).

**Project-doc mirror:** `cosmosdecoded/learnings/chatterbox_silent_truncation.md`.
