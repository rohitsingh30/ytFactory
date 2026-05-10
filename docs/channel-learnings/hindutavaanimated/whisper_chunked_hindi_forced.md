---
name: Chunked Whisper + force language='hi' is the structural fix for Hindi caption coverage
description: Whisper-mlx-4bit on long Hindi narration drops words mid-stream AND auto-detects wrong language (transcribed Hindi as Telugu in some chunks). Fix - split audio at silence points, force language='hi' per chunk, merge with timestamp offsets. Recovers 2-3x more words and renders clean Devanagari throughout.
type: feedback
---

## The two failure modes (both observed across hanuman + karna)

**1. Mid-stream dropout.** Whisper-mlx-4bit on a 44s+ Hindi narration
transcribes ~50% of the words and assigns 15-20s durations to the last
word it transcribed before giving up. Result: caption "stuck on हो़ी"
for 21 seconds while the audio speaks normally.

**2. Wrong-language auto-detection.** When you split audio into short
chunks (≤10s) for chunked transcription, Whisper's auto-language
detection guesses wrong on ambiguous-sounding Hindi. Specific case:
Karna recognition beat (28s) transcribed as **TELUGU** (Unicode
0x0C00-0x0C7F), with Hindi-sounding text encoded in Telugu glyphs
("అరు", "మాంగా", "వు", "కవచ్చోరు"). Result: empty boxes on screen
because Devanagari Sangam MN doesn't cover Telugu glyphs.

## The fix (live in /tmp/compose_episode.py:chunked_whisper)

Three-part:

**(a) Split audio at silence points.** Use ffmpeg silencedetect to find
silences ≥0.4s, then transcribe the speaking ranges between silences.
Sub-split any range > chunk_max_s (=10s) into roughly equal pieces.

```python
out = subprocess.run([
    "ffmpeg", "-hide_banner", "-i", str(audio_path),
    "-af", "silencedetect=n=-30dB:d=0.4",
    "-f", "null", "-",
], capture_output=True, text=True).stderr
```

**(b) Force language='hi' per chunk.** Bypass `pipeline.beats.transcribe_words()`
(which doesn't accept a language hint) and call `mlx_whisper.transcribe()`
directly:

```python
result = mlx_whisper.transcribe(
    str(chunk_path),
    path_or_hf_repo='mlx-community/whisper-large-v3-mlx-4bit',
    word_timestamps=True,
    language='hi',  # ← critical — locks the script
)
```

**(c) Merge with timestamp offsets.** Each chunk's word timestamps are
relative to chunk start; offset by `chunk_start_s` then sort all words
by absolute timestamp.

## Result on karna-kavach-kundal (54s audio, ~140 word script)

| Pass | Words returned | Visible captions |
|---|---|---|
| Plain `transcribe_words()` | 77 | First 13s only; 17s frozen on "नहीं।"; rest empty |
| Chunked, no language hint | 177 | Span whole timeline BUT Devanagari + Telugu mix → empty-box gibberish on Telugu chunks |
| **Chunked + language='hi'** | **180** | **Clean Devanagari throughout** |

## Pattern for any new non-English channel
1. If `transcribe_words(audio_provider='whisper_mlx').count() < 0.7 * script.word_count`, switch to chunked.
2. ALWAYS pass `language=<channel_lang>` per chunk — auto-detection is unreliable on short clips.
3. Remember to handle font dispatch — `pipeline.captions._find_font` currently probes for Devanagari only; extend to other scripts (Tamil, Bengali, etc.) if you add channels in those languages.

## Production TODO
Productionise this in `pipeline.beats.transcribe_words()`:
- Add a `language` parameter that gets threaded into mlx_whisper
- Add a `chunked=True` parameter that triggers the silence-split path
- Channel YAML's `tts_language` field can drive this automatically
