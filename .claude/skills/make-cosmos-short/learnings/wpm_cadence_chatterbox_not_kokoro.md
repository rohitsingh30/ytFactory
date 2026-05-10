---
name: /make-cosmos-short word budget was wrong — channel uses cloudrun_chatterbox+sarah.wav (~231 wpm), not Kokoro am_michael (~165 wpm)
description: First /make-cosmos-short run on `ligo-2015-gw150914-short` shipped a 42s mp4 — under the 50-60s target band. Root cause: SKILL.md inheritance contract claimed Kokoro am_michael / 145-165 word budget, but the channel `cosmosdecoded/config.yaml shorts:` block actually uses cloudrun_chatterbox + sarah.wav at ~231 wpm. Real word budget is 190-230 words. SKILL.md patched 2026-05-08.
type: feedback
---

# Word budget was wrong — chatterbox+sarah is faster than Kokoro

**Rule:** /make-cosmos-short narrations are 190-230 words for 50-60s,
NOT 145-165 words. The channel uses `cloudrun_chatterbox` +
`pipeline/voice_refs/sarah.wav` at speed 1.0, not Kokoro am_michael.

**Why:** First-run on `ligo-2015-gw150914-short` shipped at 42s with
154 words = 231 wpm. The skill's inheritance contract claimed
Kokoro am_michael at ~165 wpm, which would have predicted ~56s for
154 words — but reality is much faster. The 145-165 word budget
came from /make-script's older Kokoro-on-laptop assumption that no
longer applies to Cosmos Decoded after the channel adopted cloud
Chatterbox.

**How to apply (immediate, author-side):**
- Target ~200 words for 50-60s Cosmos Decoded Shorts.
- Always cat `cosmosdecoded/config.yaml shorts:` block at the start
  of authoring to confirm the live TTS provider and voice. Skill
  inheritance text can drift from config.
- After narration TTS renders, ffprobe the wav: `ffprobe -v error
  -show_entries format=duration <wav>`. If under 50s or over 60s,
  rewrite.

**How to apply (skill-side, shipped 2026-05-08):**
- /make-cosmos-short SKILL.md inheritance-contract table corrected.
- Section 5a "word budget" line corrected.
- Section 6 quality gate #3 updated to 190-230 words.

**Status (2026-05-08):**
- LIGO short SHIPPED at 42s — slightly under-band (closer at ~40s,
  ~2s of trailing video). Not catastrophic; the actual mp4 plays
  cleanly. User decision needed on whether to rewrite at ~200 words
  for band-compliance.
- Skill patched so the next /make-cosmos-short author hits the right
  word budget.

**Two-strikes rule:** if the next /make-cosmos-short emits at the
wrong word budget (because the author skipped reading the live
config), escalate to a pre-emit quality gate that runs ffprobe on
a sample TTS chunk and rejects narrations whose extrapolated
duration falls outside [50, 60]s.

**Project-doc mirror:** `cosmosdecoded/learnings/wpm_cadence_chatterbox_not_kokoro.md`.
