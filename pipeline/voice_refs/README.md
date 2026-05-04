# Voice reference clips

Stable 5–15s WAV clips + their transcripts, used as zero-shot voice-cloning
inputs for `tts_provider: f5_tts` and `tts_provider: chatterbox`.

| File | Voice | Source | Duration |
|---|---|---|---|
| `theo.wav` + `theo.txt` | US male documentary narrator (Cartesia "Theo" identity) | first 9.5s of `historyrecapped/cache/dunkirk-1940/narration.wav` | 9.5s |
| `sarah.wav` + `sarah.txt` | US female narrator (Cartesia "Sarah" identity) | first 9.5s of `data/cache/anthropic-connectors-creative-apps/narration.wav` | 9.5s |

Format: 24kHz mono PCM s16le. Both F5-TTS-MLX and Chatterbox accept this.

## Channel mapping

- historyrecapped (Shorts) → `theo.wav` (`tts_provider: f5_tts`)
- sportstoriesanimated → `theo.wav` (`tts_provider: f5_tts`)
- mystoriesanimated → `sarah.wav` (`tts_provider: chatterbox`)

## Refreshing a clip

```
ffmpeg -y -i <source.wav> -ss 0 -t 9.5 -ar 24000 -ac 1 pipeline/voice_refs/<name>.wav
```

Then update the matching `<name>.txt` to be the literal spoken transcript
of the cut window. Voice cloning is forgiving of small transcript drift
(±1–2 words at the boundary is fine) but a wrong transcript audibly
degrades cloning quality.
