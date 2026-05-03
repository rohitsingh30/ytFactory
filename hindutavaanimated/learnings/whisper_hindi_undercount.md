---
name: Whisper-mlx-4bit undercounts dense Hindi narration ~50%
description: Cartesia renders the full Hindi script but Whisper-mlx-4bit transcribes only half the words and hallucinates 20+ second word durations on the missing range. Symptom — caption "stuck on हो़ी" for 21 seconds.
type: feedback
---

## Symptom (hanuman-sanjivani-parvat v3, 2026-05-03)
44-second Cartesia Sneha narration of a 130-word Hindi script came back from
`pipeline.beats.transcribe_words(provider='whisper_mlx')` as **65 words**.
The last transcribed word before the unmapped section was assigned a
21.5-second duration:

```
19.48s-41.04s  होगी।    ← 21 seconds for one word
41.06s-43.40s  जय
43.40s-43.72s  शीराम
```

The user-visible bug: the word PNG for "होगी" stayed on screen from t=19.48s
to t=41.04s — **21 seconds of the same caption frozen** while the audio
spoke the entire CTA ("Like करें, Comment में लिखें, Subscribe करें…").

## Root cause
Whisper-mlx-4bit's quantisation hurts Hindi recall. ffmpeg silencedetect
shows MANY natural pauses scattered through 21-41s but no contiguous silence
> 1s — Cartesia is speaking continuously, Whisper just stops transcribing
mid-narration and absorbs the missing audio into the previous word's
duration.

## Two-part v0 mitigation (shipped in `/tmp/compose_episode.py`)

**1. Clamp word durations.** Any Whisper word with `end - start > 1.5s` gets
its `end` reset to `start + 1.5s`. Better to have NO caption for the
un-transcribed range than the wrong caption frozen on screen.

```python
MAX_WORD_S = 1.5
for w in words:
    if w.end - w.start > MAX_WORD_S:
        w.end = w.start + MAX_WORD_S
```

**2. Time-proportional beat allocation by SCRIPT word count, not Whisper
word indices.** When Whisper's word count is ~half the script's, mapping
"cumulative script word index → corresponding Whisper word index" piles
audio time into whichever beat had Whisper words at the end. Just
proportionally allocate audio_dur by script word distribution:

```python
counts = [_wc(b['narration']) for b in script_beats]
total = sum(counts)
durs = [audio_dur * n / total for n in counts]
```

Then clamp each beat ≥2.5s and redistribute the deficit from longer beats
so short-narration centerpieces still hold long enough on screen.

## Real fix (not yet shipped — TODO)
Chunk the audio at silence points (use ffmpeg's silencedetect output) and
run Whisper on each chunk independently. Or switch to a non-quantised
Whisper variant (mlx-community/whisper-large-v3-mlx full precision) for
Hindi channels. Or use whisperX which has phoneme-level forced alignment.
The clamp+proportional approach is correct for image timing but leaves the
CTA section caption-less.

## How to detect this regression on future channels
After Whisper transcription, log `(whisper_word_count, script_word_count)`.
If `whisper_word_count < 0.7 * script_word_count`, surface a warning. The
caption layer is degraded but image timing still works via the proportional
fallback.
