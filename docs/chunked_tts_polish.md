# Chunked cloud-TTS audio polish

Cross-channel rule established 2026-05-08 from the
krishna-govardhan-leela-long iteration. Four polish fixes that
must hold for every chunked cloud-TTS render across every channel.

The implementation lives in `pipeline/tts/cloudrun.py` —
`_synth_cloudrun_chunked` (concat fades) and `_derive_chunk_prosody`
(prosody-speed mult + auto-modulation + pause cap). Both fire
automatically for `cloudrun_indicf5` / `cloudrun_indicparler` /
`cloudrun_f5` / `cloudrun_chatterbox` / `cloudrun_higgs` /
`cloudrun_cosyvoice`.

## 1. Wave-level chunk conditioning — four iterations to land it

Direct ffmpeg concat of cloud-TTS chunk WAVs produces an audible
click/pop at every paragraph boundary. Reported by user 2026-05-08
across **four** iterations (the user's patience earned the
diagnosis):

| Attempt | Approach | Result |
|---|---|---|
| 1 | concat-filter `afade` 12ms in/out | clicks reduced but not gone |
| 2 | concat-filter `afade` 30ms in/out | clicks still audible at chunk seams |
| 3 | wave-baked 60ms afade + `highpass=35` + `dynaudnorm=f=200` per chunk | "still that irritating sound when you start a sentence" |
| 4 | **drop per-chunk `dynaudnorm` → single full-track `loudnorm` post-concat** | ✓ clean |

### Diagnosis (the corrective insight)

The first three attempts assumed the artifact was a sample-level
discontinuity at chunk boundaries (DC offset, click, pop). Sample-
level analysis of the rendered narration showed:

| boundary | first 30ms post-onset peak | ref WAV's onset peak |
|---|---|---|
| chap-2 (0:50) | **0.56** | 0.085 (IIT-M anchor) |
| 1:26 | 0.13 | 0.085 |
| mid-paragraph control | 0.94 (real speech) | — |

The chunk-onset peak is **6× higher than the source ref's onset**.
The amplification was being introduced by `dynaudnorm`'s per-chunk
loudness equalization — each chunk gets independently normalized
to body loudness, so the soft consonant that begins each chunk
gets gain-pumped up to the body-loudness level. The result is an
audible "swell" or "click" at every sentence start. Textbook
dynaudnorm pumping.

The fix is to move loudness normalization **out of per-chunk and
into a single post-concat pass**. That equalizes total track
loudness without touching individual chunk onsets.

### Implementation (`pipeline/tts/cloudrun.py:_synth_cloudrun_chunked`)

**Per-chunk** (only fades + DC removal, no dynamic gain):

```python
# Probe duration via wave module — afade requires a numeric `st=`.
with wave.open(str(chunk_path)) as _w:
    chunk_dur = _w.getnframes() / _w.getframerate()
fade_dur = 0.060
fade_out_st = max(0.0, chunk_dur - fade_dur)
ffmpeg -y -i chunk.wav -af \
    "highpass=f=35,\
     afade=t=in:st=0:d=0.060,\
     afade=t=out:st=<chunk_dur-0.060>:d=0.060" \
    -ar 24000 -ac 1 -c:a pcm_s16le chunk.cond.wav
```

**Post-concat** (single full-track loudnorm):

```python
ffmpeg -y -i _concat.wav -af \
    "loudnorm=I=-18:TP=-2:LRA=11" \
    -ar 24000 -ac 1 -c:a pcm_s16le narration.wav
```

EBU R128 settings: I=-18 LUFS (broadcast target), TP=-2 dBTP (true
peak ceiling), LRA=11 (loudness range). Single-pass `linear=false`
default — good enough for narration, avoids the 2-pass measurement
step.

### Why per-chunk loudness equalization is wrong for TTS

Voice-clone anchoring (chunks 1..N use chunk_0's wav as ref) keeps
inter-chunk loudness within ~1-2 dB anyway. Per-chunk
`dynaudnorm` "fixes" a problem that isn't there, and creates a new
one (gain-pumping at every sentence start). For chunked TTS where
the speaker is held constant, **always normalize the assembled
track, never the chunks individually**.

## 2. `chunk_speed = (prosody_speed / mean) × base_speed`

The prosody-normalization step in `_derive_chunk_prosody` divides
each chunk's authored speed by the mean prosody speed → output mean
lands at 1.0. This was ERASING the channel-level `tts_speed: 0.85`
slowdown — every chunk synthesized near 1.0 (channel default
ignored). User reported as "too fast narration" on
krishna-govardhan-leela-long.

**Implementation:** multiply the normalized speed by the caller's
`speed` arg so per-chunk modulation is normalized AROUND
base_speed, not around 1.0.

```python
chunk_prosody = [
    (s / mean_speed * speed, p) for s, p in chunk_prosody
]
```

## 3. Auto-modulation for unannotated paragraphs

`narration_prosody` tables typically annotate only ~15-20
paragraphs (chapter titles, climax, lesson, CTA). The other ~80
paragraphs in a long-form fall back to speed=1.0, producing flat
read across the body. User reported as "no modulation".

**Implementation:** in `_derive_chunk_prosody`, when a paragraph
has no matching prosody entry AND some other paragraphs in the
doc DO have annotations, apply position-based variation:

| Position | Speed | Why |
|---|---|---|
| Just after an author-annotated paragraph | `0.94×` | slow-in, lets the dramatic moment land |
| Just before an author-annotated paragraph | `0.96×` | slight ritard, builds anticipation |
| Body paragraph (parity-even) | `1.03×` | gentle lift |
| Body paragraph (parity-odd) | `0.97×` | gentle dip |

This produces audible modulation across the 80+ unannotated
paragraphs without the author having to annotate every one.

## 4. Inter-paragraph pause cap = 0.40s

User feedback iterated this cap on the same render across three
days:

| Cap | Result |
|---|---|
| 0.10s | "where are the pauses" — paragraphs run together breathlessly |
| 0.30s | improved but still feels rushed at climax beats |
| **0.40s** | natural breath without dead air ✓ |

For comparison, 0.50+ → "so many pauses" feels choppy
(2026-05-07 reverse-feedback). 0.40s is the sweet spot.

**Implementation:** in `_derive_chunk_prosody`, the post-pause is
the LAST entry's `post_pause_s` from the prosody table for that
paragraph, capped at `min(last_pause, 0.40)`.

## When this applies

Every chunked cloud-TTS path. The chunker fires when:
- Provider is `cloudrun_indicf5` / `cloudrun_indicparler` /
  `cloudrun_f5` / `cloudrun_chatterbox` (and others using
  `_synth_cloudrun_chunked`)
- Input text has multiple paragraphs (`\n\n` separated) AND total
  chars > `_HINDI_CLOUD_TTS_CHUNK_THRESHOLD_CHARS`

Single-shot synthesis (single paragraph, short input) bypasses the
chunker — these fixes are only relevant when chunking fires.

## Memory

[`memory/feedback_chunked_tts_audio_polish.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_chunked_tts_audio_polish.md)
