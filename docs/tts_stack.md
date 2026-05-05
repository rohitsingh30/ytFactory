# TTS stack

ytFactory ships three TTS providers, all free and local:

- **F5-TTS-MLX** — primary English narration, zero-shot voice cloning
  from a 5-15 s reference WAV. Production default.
- **Kokoro** — zero-API fallback for Shorts where F5 is overkill, and
  the only Hindi-capable provider (via `hf_alpha`).
- **Chatterbox** — kept on the MyStoriesAnimated parent for its
  emotion-exaggeration knob (the AITA arc benefits from it).

## Active provider per channel / variant

| Channel / variant | Provider | Voice |
| --- | --- | --- |
| sportstoriesanimated (parent) | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| sportstoriesanimated/variants/ranked.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| sportstoriesanimated `long_form_doc` block | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| historyrecapped (Shorts) | `kokoro` | `am_michael` |
| historyrecapped `long_form` block | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated (parent) | `chatterbox` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_animated.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_animated_motion.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_cliffhanger_animated.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_cliffhanger_part2_animated.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_cliffhanger_part2_text.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_cliffhanger_text.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/aita_text.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/tifu.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/today_in_history.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| mystoriesanimated/variants/wiki_oddities.yaml | `f5_tts` | `pipeline/voice_refs/sarah.wav` |
| airecap | `kokoro` | `am_michael` |
| hindutavaanimated | `kokoro` | `hf_alpha` (Hindi female) |
| rhymetimejunction | `f5_tts` | `pipeline/voice_refs/sarah.wav` |

## Voice references on disk

`pipeline/voice_refs/` holds the F5-TTS conditioning clips:

- `sarah.wav` (24 kHz mono, 9.5 s) + `sarah.txt` — the only English
  ref currently on disk. Re-clipped 2026-05-04 from a long-form WW1
  raw chunk after the recovery wipe.
- `theo.txt` only (no .wav). The male reference was lost in the
  2026-05-04 wipe. Channels that previously used Theo are running
  sarah.wav until a male WAV is re-recorded.

To re-record Theo:

1. Capture a 5-15 s 24 kHz mono WAV in the desired voice.
2. Save as `pipeline/voice_refs/theo.wav`.
3. Update `tts_voice` + `tts_ref_text` in the relevant YAMLs back to
   `theo.wav` / its transcript.

## Singleton + ref-audio cache

`pipeline/audio.py` holds two module-level caches:

- `_F5_MODEL` — the loaded `F5TTS.from_pretrained(...)` instance,
  reused across every chunk in a render.
- `_F5_REF_CACHE` — keyed by `ref_audio_path`, stores the loaded
  mlx array + ref duration so the WAV is read + RMS-normalised once.

**Never call `f5_tts_mlx.generate.generate()` per-chunk.** Upstream's
`generate()` runs `F5TTS.from_pretrained` inside the function body
(cfm.py:131) — every call reloads the 1.35 GB checkpoint and re-triggers
mx.compile of the ODE step. On a 178-chunk long-form render that turns
~30 s/chunk into ~3-5 min/chunk and pushes total time from ~1.5 hr to
~9 hr. `pipeline/audio._synth_f5_tts` replicates the upstream body
without the per-call load.

## Hindi / Hinglish

F5-TTS-MLX as configured (`lucasnewman/f5-tts-mlx`) is English-only.
Channels that need Hindi (hindutavaanimated; potential rhymetimejunction
spoken bridges) run Kokoro `hf_alpha` (the only Hindi voice in Kokoro).
An Indic Parler venv is a future option — the code path lives in
`pipeline/audio.py` but transformers pin conflicts with diffusers /
mflux keep it out of the main venv.
