# /make-cosmos-short — learnings index

Append-only log of regressions caught by `/critique-audio` or
`/critique-video` on outputs of this skill, classified ONE-OFF vs
CLASS-OF-BUG per CLAUDE.md and skill Section 9.

Format: `YYYY-MM-DD — slug — type — what fired — fix location`

Types:
- `ONE-OFF` — fix in the failing JSON, no pipeline/SKILL.md change
- `CLASS-OF-BUG` — fix in pipeline code or SKILL.md prompt; mirror to
  cosmosdecoded/learnings/<topic>.md

When a CLASS-OF-BUG fires twice, escalate: add a Section 6 pre-render
quality gate that blocks emit on detection.

This skill was split out from the retired `/make-cosmos-decoder` on
2026-05-08; learnings prior to that date were authored against the
paired-output skill but apply equally to Short-only outputs.

---

<!-- entries below — newest first -->

2026-05-08 v2 — ligo-2015-gw150914-short — CLASS-OF-BUG — cloudrun_chatterbox silently truncates at ~40s of audio (~97 words at sarah.wav cadence) when given single-paragraph long inputs. v1 shipped closer-less. Fix: `_synth_cloudrun_chatterbox` now routes through `_synth_cloudrun_chunked` + new `_split_for_chunked_synth` helper handles single-paragraph long inputs via sentence-group fallback. Plus skill SKILL.md gate #4 requires `\n\n` breaks. See [chatterbox_silent_truncation.md](chatterbox_silent_truncation.md); mirrored to cosmosdecoded/learnings/.

2026-05-08 v2 — ligo-2015-gw150914-short — ONE-OFF correction to a CLASS-OF-BUG — the wpm_cadence learning below claimed "Chatterbox+sarah ≈ 231 wpm". WRONG. The 231 number was derived from the truncated v1 wav (97 words / 40s = 145 wpm; I mis-read script word count instead of wav word count). Re-measured on v2 unpatched: 186 words / 75.5s = 148 wpm. Actual word budget: 125-150 words for 50-60s, NOT 190-230. SKILL.md updated; below entry kept for trail-of-tears history.

2026-05-08 — ligo-2015-gw150914-short — CLASS-OF-BUG (superseded by entry above) — SKILL.md inheritance contract said Kokoro am_michael ~165 wpm, channel actually runs cloudrun_chatterbox+sarah.wav. Fix in [wpm_cadence_chatterbox_not_kokoro.md](wpm_cadence_chatterbox_not_kokoro.md); mirrored to cosmosdecoded/learnings/. Inheritance text can drift from config — always cat the live config.yaml.

2026-05-08 — ligo-2015-gw150914-short — CLASS-OF-BUG — render_footage_only.py downloads .svg files directly from Wikimedia and ffmpeg can't decode SVG (`no decoder found for: svg`). Mitigated by swapping the strain-plot URL from `.svg` to `.png` in the shotlist. Class rule: shotlist source_url should prefer `.png` / `.jpg` / `.webp` over `.svg` / `.tif`.

2026-05-08 — ligo-2015-gw150914-short — CLASS-OF-BUG — Wikimedia .webm video URLs return JPEG thumbnails through `pipeline/cosmos_footage_prep.py`, raising RuntimeError on `source_type: video`. Mitigated by swapping to a still JPEG. Fix in [webm_video_jpeg_thumbnail.md](webm_video_jpeg_thumbnail.md); mirrored to cosmosdecoded/learnings/. Two-strikes rule active.

2026-05-07 — eht-2019-m87-short — CLASS-OF-BUG — shotlist Wikimedia URL guesses miss rate (2/7 MANUAL on Short — small absolute count but same root cause as the long-form 35/56) — fix in [shotlist_url_guess_miss_rate.md](shotlist_url_guess_miss_rate.md); mirrored to cosmosdecoded/learnings/. Inherited from retired /make-cosmos-decoder on skill split (2026-05-08).
