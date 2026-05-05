# Voice reference clips

Stable 5–15s WAV clips + their transcripts, used as zero-shot voice-cloning
inputs for `tts_provider: f5_tts` and `tts_provider: chatterbox`.

| File | Voice | Source | Duration |
|---|---|---|---|
| `sarah.wav` + `sarah.txt` | US female narrator | first 9.5s of `data/cache/anthropic-connectors-creative-apps/narration.wav` | 9.5s |

Format: 24kHz mono PCM s16le. Both F5-TTS-MLX and Chatterbox accept this.

`theo.wav` was lost in the 2026-05-04 recovery wipe (only `theo.txt`
survived). Channels that previously cloned a male Theo voice now run
sarah.wav until a male reference is re-recorded.

## Channel mapping

- sportstoriesanimated (parent + variants/ranked + long_form_doc) → `sarah.wav` (`tts_provider: f5_tts`)
- mystoriesanimated (parent) → `sarah.wav` (`tts_provider: chatterbox`)
- mystoriesanimated/variants/* → `sarah.wav` (`tts_provider: f5_tts`)
- historyrecapped (long-form sleep) → `sarah.wav` (`tts_provider: f5_tts`)
- rhymetimejunction (TTS rarely invoked; Suno is main path) → `sarah.wav` (`tts_provider: f5_tts`)

## Refreshing a clip

```
ffmpeg -y -i <source.wav> -ss 0 -t 9.5 -ar 24000 -ac 1 pipeline/voice_refs/<name>.wav
```

Then update the matching `<name>.txt` to be the literal spoken transcript
of the cut window. Voice cloning is forgiving of small transcript drift
(±1–2 words at the boundary is fine) but a wrong transcript audibly
degrades cloning quality.

## Recording a new male reference

To restore the male documentary voice (channels that previously used
Theo are running sarah.wav as a stop-gap):

1. Capture a 5-15 s 24 kHz mono male WAV in the desired voice. Read a
   sentence ~20-30 words long from any narration script (`historyrecapped/
   narrations/*.json` works well).
2. Save as `pipeline/voice_refs/theo.wav`.
3. Save the literal spoken transcript as `pipeline/voice_refs/theo.txt`.
4. Update `tts_voice` + `tts_ref_text` in the relevant YAMLs back to
   `pipeline/voice_refs/theo.wav` / its transcript.
5. Re-add `"theo"` to `EXPECTED_REFS` in
   `tests/test_audio_tts_providers.py`.
