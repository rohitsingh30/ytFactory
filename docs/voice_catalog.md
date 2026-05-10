# Named voice catalog — single source of truth for `tts_voice`

Set as project-wide architecture by user 2026-05-07: every skill should
declare what voice it wants by NAME, not by hard-coded WAV path. The
pipeline resolves the name to a ref WAV + transcript via a central
catalog.

## Why named voices

Before: every channel YAML duplicated `tts_voice: pipeline/voice_refs/.../ref.wav`
and `tts_ref_text: <transcript>`. Renaming a voice meant editing N
YAMLs. Adding a new voice required hand-syncing transcript + path
across all places that referenced it. Channel configs could fall out
of sync with the actual WAV.

After: skills + channels reference a voice by name; the catalog at
`pipeline/voice_refs/catalog.yaml` is the only place voice metadata
lives. One edit propagates to every skill that uses the voice.

## How to use it

### From a channel YAML

```yaml
# Name-style (preferred)
tts_voice: hindi-female-storyteller
# tts_ref_text is no longer needed — the catalog provides it

# OR path-style (legacy, still accepted)
tts_voice: pipeline/voice_refs/.../ref.wav
tts_ref_text: <transcript>
```

### From Python

```python
from pipeline.voice_catalog import resolve_voice

wav_path, transcript = resolve_voice("hindi-female-storyteller", project_root)
```

### From a skill SKILL.md

State the voice by name and let the renderer plumbing handle resolution:

> Channel uses `tts_voice: hindi-female-storyteller` from the voice
> catalog (`pipeline/voice_refs/catalog.yaml`). Don't hard-code the
> WAV path in `narrations/<slug>.json` — author the script and the
> renderer pulls the voice from `config.yaml`.

## What's hosted, where

Each voice lives at `pipeline/voice_refs/<voice-name>/{ref.wav, ref.txt}`
(or, for legacy single-file voices, `pipeline/voice_refs/<name>.wav`
with `<name>.txt` next to it). All committed to the repo so:

- Laptop renders read directly from disk.
- Cloud-rendering containers get the WAV via base64 in the /synth
  request body — no GCS round-trip needed.
- One source of truth, no drift between cloud + laptop.

**WAV constraints** (driven by IndicF5 / F5-TTS defaults):

| Property | Value | Why |
|---|---|---|
| Duration | 5-15s | Both models accept this range natively. >15s gets internally truncated. |
| Sample rate | 24 kHz | Match model native rate; avoid resampling jitter. |
| Channels | mono | Models trained on mono — stereo gets averaged. |
| Format | PCM_16 | Standard WAV; no compression artifacts. |
| SNR | 65+ dB | Quieter background = cleaner clone. |
| Speaker pitch variance | low | Avoid tonal swings the clone will exaggerate. |

A 15-MIN reference (as occasionally proposed) does NOT improve clone
quality — F5-family models truncate at ~30s of ref input regardless
of how much you feed them. Curate ONE 8-10s clip with the speaker's
target register instead of dumping minutes.

## Initial catalog (2026-05-07)

| Name | Lang | Use cases |
|---|---|---|
| `hindi-female-storyteller` | hi | hindutavaanimated/shorts + kathaa |
| `english-female-documentary-sarah` | en | historyrecapped, sportsrecapped long-form |
| `english-male-aita-narrator-michael` | en | mystoriesanimated AITA + cliffhanger |

## Adding a new voice

1. Curate a 5-15s WAV at 24 kHz mono with the target register.
2. Drop it at `pipeline/voice_refs/<voice-name>/ref.wav` (or as a
   single file `pipeline/voice_refs/<voice-name>.wav` for legacy
   parity).
3. Write the verbatim transcript at `<same-dir>/ref.txt` (or
   `<voice-name>.txt`).
4. Add an entry to `pipeline/voice_refs/catalog.yaml` with
   `path`, `transcript`, `language`, `register`, `duration_s`,
   `use_cases`, and an optional `notes` block.
5. Reference by name from any channel config:
   `tts_voice: <voice-name>`.

## Voice-clone anchoring (default for chunked cloud TTS)

When chunked cloud synthesis is needed (input >22-30s for IndicF5 /
F5-TTS), `_synth_cloudrun_chunked` does:

1. Synth chunk 0 using the catalog ref WAV.
2. Synth chunks 1..N using **chunk 0's audio** as the ref WAV.
3. All chunks clone the speaker established in chunk 0 → consistent
   narrator across the full 60-70s render.

This is automatic for voice-clone-capable models (`indicf5`, `f5`,
`cosyvoice`, `higgs`, `chatterbox`). Description-driven providers
(`indicparler`) skip anchoring and rely on a stable seed.

## Memory

[`memory/feedback_pipeline_owns_av_sync.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_pipeline_owns_av_sync.md)
[`memory/feedback_voice_catalog_default.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_voice_catalog_default.md)
