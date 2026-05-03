---
name: Whisper produces non-monotonic word timestamps on sung audio
description: ASR on Suno/sung input emits overlapping word timestamps (next.start < prev.end) which crash ffmpeg's trim filter; pipeline/beats.py now sanitises monotonically
type: feedback
originSessionId: eee5c9ed-b6d4-47c9-859e-7790613cd4bc
---
**Rule:** Word timestamps from `pipeline/beats.transcribe_words` are sanitised so each word's `start` is ≥ the previous word's `start`, and `end` is ≥ `start + 0.01s` AND ≥ previous word's `end`. Repairs the timeline in-place; downstream ffmpeg compose can rely on monotonic non-overlapping windows.

**Why:** Discovered 2026-05-03 on the first Rhyme Time Junction sung-audio render. Whisper-large-v3-mlx-4bit on the 155s Suno-generated nursery rhyme produced word_69 (`'हाथी'`, chorus repeat) with start=38.44s — BEFORE word_68 (`'plays.'`, end of prior verse) ended at 44.82s. ffmpeg compose computes per-word trim durations as `next.start - this.start`; that produced -4.5s and crashed: `Value -4500000.000000 for parameter 'durationi' out of range`. Sung audio with overlapping lead + choir vocals fools Whisper's word-segmentation more than spoken narration does.

**How to apply:** Fixed in `pipeline/beats.transcribe_words` at the bottom — every word gets its start clamped to `max(start, last_start)` and end clamped to `max(end, start+0.01, last_end)`. Idempotent; does nothing on already-monotonic input. Spoken narration (kokoro/cartesia) is unaffected because it's already monotonic.

**Don't:**
- Don't try to "fix" overlap by averaging or re-aligning to a phrase model — Whisper's text is right, just the timing is sloppy. Clamping preserves the right text and gives ffmpeg something it can chew.
- Don't disable per-word captions on the sung-audio path — captions are how mute viewers (80% of Shorts audience) follow along. The sanitiser is the right fix.
- Don't only fix in compose — a stale beats.json on disk would still poison any code that reads it. Sanitising at the source (transcribe_words) means every consumer downstream sees clean data.

**Coverage:** Fix applies to ALL transcribe_words callers — sports footage ASR, mahabharat_hindi, rhymetimejunction, any future channel that lets Whisper see sung or musically-overlapping audio.
