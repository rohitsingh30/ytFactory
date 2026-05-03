---
name: Sports footage cuts use a black-intro suspense beat before the broadcast
description: For high-stakes sports moments, the narrator's setup line plays over a black screen for ~1-2s before the broadcast cuts in — builds tension and anchors the cut as a deliberate edit, not a hard slam
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
The narrator's pivot beat (e.g. "Last kick of the season") should play over a **pure-black screen** for the duration of that beat's narration, then the broadcast clip cuts in. This is the cinematic suspense convention used in football documentaries and Netflix sports edits — the silence + black builds tension, then the broadcast hits with full force.

User direction (2026-05-02 on the v12 Aguero render): "last kick of the game should be with a black screen behind for building suspense, and then this clip".

**Why:** without the suspense beat, the narration "Last kick of the season" plays simultaneously with Tyler's broadcast call ("Balotelli, Aguero!") and the visuals jump straight into action — the cut feels like a slam, not an earned beat-drop. The black-intro:
- isolates the narrator's setup line so the viewer registers it
- makes the broadcast cut feel intentional ("we waited for it")
- gives the editor a 1-2s breath to build viewer pulse before payoff

**How to apply:**
- The script.footage[] entry sets `"black_intro": true`. `make_shorts.py` reads this and computes `pre_pad_s = beats[i].end - beats[i].start` (the beat's natural narration duration), then passes it to `pipeline/footage.py:fetch_clip(pre_pad_s=...)`.
- `fetch_clip` runs a SECOND ffmpeg pass: generates a `color=c=black` + `anullsrc` clip of `pre_pad_s` duration, then concat-encodes it with the trimmed broadcast.
- `pipeline/compose.py:compose_clips` reads `b.footage["pre_pad_s"]`. When set, the narration-duck window for that beat starts at `beats[i].end` (after narration ends) instead of `video_start[i]` — so the setup line stays audible over the black, and the duck only kicks in once the broadcast starts.
- Combined effect: the FIRST `pre_pad_s` seconds of the beat shows BLACK + plays narration audio; the REMAINING video plays the broadcast with original commentary at `audio_mix` volume; narration is silenced during the broadcast portion via the existing extension-silence mechanism.
- Default: `black_intro: false` (broadcast plays from t=0 of the beat, current behavior). Opt in per footage entry.
- **Tail-breath buffer.** ASR (whisper) can underestimate word-final consonants by 50-200ms — the rendered audio's "-n" tail of "season" extends a bit past `beat.end`. Without padding, the broadcast hard-cut covers the tail and the word sounds chopped. `make_shorts.py` adds a 300ms buffer by default: `pre_pad_s = (beat.end - beat.start) + 0.30`. Override via `script.footage[].black_intro_buffer_s` (e.g. `0.5` for languages with longer trailing consonants).
- **Silence-insertion seam = next beat's start, not THIS beat's end.** `pipeline/compose.py:_extend_beats_for_footage` inserts the extension silence at `beats[i+1].start` — the natural inter-beat seam. Inserting at `beats[i].end` (the previous default) cut Kokoro's word-tail mid-consonant because ASR underestimates by ~250ms. Inserting at the next beat's start preserves the entire actual word audio, including the ASR-invisible tail.