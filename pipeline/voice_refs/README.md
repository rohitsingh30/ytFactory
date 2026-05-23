# Voice reference clips

Stable 5–15s WAV clips + their transcripts, used as zero-shot voice-cloning
inputs for `tts_provider: chatterbox` and `tts_provider: cloudrun_chatterbox`.

| File | Voice | Source | Duration |
|---|---|---|---|
| `sarah.wav` + `sarah.txt` | US female narrator | first 9.5s of `data/cache/anthropic-connectors-creative-apps/narration.wav` | 9.5s |

Format: 24kHz mono PCM s16le. Chatterbox accepts this.

## Channel mapping

- mystoriesanimated (parent) → `sarah.wav` (`tts_provider: chatterbox`)

## Refreshing a clip

```
ffmpeg -y -i <source.wav> -ss 0 -t 9.5 -ar 24000 -ac 1 pipeline/voice_refs/<name>.wav
```

Then update the matching `<name>.txt` to be the literal spoken transcript
of the cut window. Voice cloning is forgiving of small transcript drift
(±1–2 words at the boundary is fine) but a wrong transcript audibly
degrades cloning quality.

## 2026-05-05 — sarah.txt critical fix

Previously `sarah.txt` contained an Anthropic press-release blurb that
**did not match the actual audio** in `sarah.wav`.

This text-vs-audio mismatch caused TTS engines to leak words from the WRONG
ref text into every English render — most visibly the trailing word
"today" appearing in scripts about WWII, sports, AITA, etc.

**Fix applied 2026-05-05:** sarah.txt now contains the Whisper-verified
exact transcript of sarah.wav. The wrong file is preserved as
`sarah.txt.WRONG-2026-05-05` for forensics; do not use it.

If the ref WAV is ever re-recorded:
1. Re-derive ref text via:
   `mlx_whisper sarah.wav --model mlx-community/whisper-large-v3-mlx-4bit`
2. Save the EXACT transcript to sarah.txt — no editing for "cleanliness"
3. Smoke-test against the new ref before flipping any channel
