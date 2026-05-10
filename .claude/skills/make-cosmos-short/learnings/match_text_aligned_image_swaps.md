---
name: Image swaps must align to spoken match_text, not cumulative shotlist time
description: pipeline/render/footage_only.py used to play windows in fixed cumulative-time order regardless of where each window's match_text was actually spoken. On the LIGO Short v6 (2026-05-08), this produced up to ±3.0s drift between visible image and spoken phrase. Fix: _aligned_window_durations(windows, beat_list, narration_end_s) reads beats.json word-stream and adapts each clip's duration so cumulative-start[N] equals match_text[N] spoken-time.
type: feedback
---

# Image swaps must align to spoken match_text, not cumulative shotlist time

**Rule:** `_build_silent_video` in `pipeline/render/footage_only.py`
must compute per-window playback durations from `beats.json` so each
window's start time matches when its `match_text` is spoken. Falls
back to shotlist `out_s − in_s` only when match_text isn't found.

**Why:** LIGO Short v3-v5 (rendered with cumulative-time order) had
up to ±3.0s drift between image and narration. Worst spots:
- W3 Hanford aerial: shotlist start 17.5s, match_text "9:50 UTC LIGO Livingston" spoken at 16.4s — image 1.1s LATE
- W5 BBH: shotlist start 28.5s, match_text "two black holes" at 29.6s — image 1.1s EARLY
- W6 BBH 2: shotlist start 34.5s, "Three solar masses" at 37.5s — image 3.0s EARLY

The user surfaced it bluntly on v5: *"you can see it is out sync a
little, just by a bit"*.

**How to apply (shipped 2026-05-08):**

`pipeline/render/footage_only.py::_aligned_window_durations(windows, beat_list, narration_end_s)`:

1. Flattens beats.json to a `[(start_s, lower_text)]` word stream
2. For each shotlist window, finds the first occurrence of its
   `match_text` first-word **walking forward from the prior window's
   match position** (avoids snapping to repeated tokens like "the"/"of")
3. Builds cumulative boundaries: window[0] anchors to t=0; window[N>0]
   anchors to its match_start_time
4. Returns per-window durations = boundaries[i+1] − boundaries[i],
   clamped to ≥1.0s

Wired into `_build_silent_video(channel, slug, shotlist, scratch, *, beat_list, narration_end_s)` — the new keyword args carry the alignment data from the calling `render()` function.

**Verified:** v6 LIGO Short produced 0.00s drift on every window
except W0 (intentional 1s pre-roll before "tunnels" is spoken).

**Author-side guidance:** every shotlist window MUST carry a
`match_text` field — pick a 1-3 word phrase from the narration that
unambiguously identifies WHERE in the audio that window's image
should land. The aligner uses the FIRST WORD of match_text for the
search; pick a phrase whose first word is distinctive (avoid "the",
"a", "of").

**Project doc:** `cosmosdecoded/learnings/match_text_aligned_image_swaps.md`.
**Skill-side mirror** (extend to history-short, mystories-short, etc.
when those channels render through `footage_only.py`): the alignment
helper is channel-agnostic, so any channel's Shorts get the behavior
once their `make-*` skill ships shotlists with match_text fields.
