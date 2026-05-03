---
name: Whisper hallucinates word timestamps over instrumental music sections
description: On sung audio, Whisper transcribes silence/quiet-guitar intros and instrumental breaks as plausible-looking words at fake timestamps; pipeline must mask Whisper words against audio RMS
type: feedback
originSessionId: eee5c9ed-b6d4-47c9-859e-7790613cd4bc
---
**Rule:** When `audio_provider in (external_song, sunoapi)`, `pipeline/beats.transcribe_words` must filter Whisper's word output against an RMS-based vocal-detection mask. Words whose `[start, end]` window falls inside a low-RMS span (RMS < -28 dB for >0.5s) are Whisper hallucinations from background instrumental and must be dropped. Without this filter, captions appear during instrumental sections with garbage Hindi/English text and the whole word-timeline gets offset by however many seconds Whisper hallucinated through.

**Why:** Discovered 2026-05-03 on the first Rhyme Time Junction sung-audio render. Suno V4_5 generated a 156s nursery rhyme with:
- 0–3.5s: quiet guitar intro (RMS -34 to -38 dB, no vocal)
- 3.5s–45s: verses + bridge (vocals, RMS -14 to -20 dB)
- 45s–134s: extended chorus + instrumental (mostly low-RMS)
- 134s–156s: final chorus + outro (vocals)

Whisper-large-v3-mlx transcribed the 0-3.5s instrumental as `'Suno bachcho, Junction par hathi raja!'` — plausible-sounding Hindi but the song is just guitar there. The real "Suno bachcho" vocal lands at ~3.5s. Result: captions on screen 25 seconds before the words are sung, plus a 91-second silent middle where Whisper found nothing.

**How to apply:** New helper in `pipeline/beats.py` — `_audio_vocal_mask(audio_path, threshold_db=-28, min_silence_s=0.5) -> list[tuple[float, float]]` returns the (start, end) windows where audio RMS is BELOW threshold. Then in `transcribe_words`, after the existing monotonic sanitiser, drop any word whose midpoint falls inside a vocal-mask span. Use `ffmpeg -af astats=metadata=1:reset=0.1,ametadata=print:key=lavfi.astats.Overall.RMS_level` to sample RMS at 100ms granularity.

**Don't:**
- Don't try to "fix" by re-running Whisper with different parameters — the hallucination is on the model/audio side, not configurable.
- Don't drop the audio energy mask in favor of relying on `no_speech_prob` from Whisper — that field is unreliable on sung input (the model thinks there IS speech when it's just music).
- Don't apply this filter to spoken-audio channels (Cartesia/Kokoro narration) — those don't have instrumental sections, and the RMS check would just slow down the render.

**Related class-of-bug from same session:**
- `feedback_whisper_sung_audio_overlapping_words.md` — overlapping word timestamps from Whisper on multi-voice sung audio
- The two fixes stack: monotonic sanitiser FIRST (reorders), THEN vocal-mask filter (drops hallucinations)

**Coverage:** Affects `rhymetimejunction` (Suno songs) + any future channel with `audio_provider in (external_song, sunoapi)`. Spoken-audio channels (every other channel today) unaffected.
