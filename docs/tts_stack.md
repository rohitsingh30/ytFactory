# TTS stack

> **Cloud-first as of 2026-05-06.** Every channel except
> `hindutavaanimated` defaults to `cloudrun_chatterbox` (Cloud Run
> Chatterbox on NVIDIA L4). Local providers (`f5_tts`, `kokoro`, etc.)
> exist only as **automatic fallback** when the cloud service is
> unhealthy. **The laptop is no longer the default TTS engine for
> any channel.**

ytFactory has two tiers of TTS providers:
- **Cloud on NVIDIA L4** (`asia-southeast1`, scale-to-zero) — production default
- **Local on M2 Max** (MLX-native, the original stack) — automatic fallback only

For ops + cost + rollout history see `docs/cloudrun_tts.md`.
For Higgs-specific operations see `docs/cloudrun_higgs.md`.

---

## Available providers

| Provider | Where | Languages | Voice | License | Notes |
|---|---|---|---|---|---|
| `kokoro` | local (CoreML/ONNX) | EN + HI + 6 more | preset (`af_*`, `am_*`, `hf_alpha`, …) | Apache 2.0 | Free, fast, no GPU. **Hindi fallback** for hindutavaanimated and the **only on-laptop Hindi option**. |
| `f5_tts` | local (MLX) | EN | clone from 5-15 s ref WAV | MIT | The **on-laptop English fallback** for every cloud provider. Singleton + ref-audio cache (see below). |
| `chatterbox` | local (PyTorch MPS) | EN | clone, emotion knob | MIT | Slow on M2 Max (~6-13× real-time). NOT used as fallback (F5 is the laptop standard). |
| `styletts2` | local | EN | clone | MIT | Broken in default venv; legacy. |
| `indic_parler` | local | HI + Indic | description | Apache 2.0 | Broken in default venv; cloud variant `cloudrun_indicparler` is the production path. |
| `cloudrun_f5` | **cloud L4** | EN | clone from 5-15 s ref WAV | MIT | F5-TTS on NVIDIA L4. Auto-falls back to local `f5_tts`. |
| `cloudrun_chatterbox` | **cloud L4** | EN | clone, emotion knob | MIT | **The new English production default** (2026-05-06). Auto-falls back to local `f5_tts`. |
| `cloudrun_higgs` | **cloud L4** | EN + HI + multilingual | clone, strong emotion | Apache 2.0 | PierrunoYT mirror checkpoint (see `docs/cloudrun_higgs.md`). Auto-falls back to local `f5_tts`. |
| `cloudrun_cosyvoice` | **cloud L4** | EN + multilingual (NOT Hindi) | clone | Apache 2.0 | CosyVoice 2 0.5B. **Proven NOT to speak Hindi** — do not use for Indic content. Auto-falls back to local `f5_tts`. |
| `cloudrun_indicparler` | **cloud L4** | HI + 22 Indic + EN | description | Apache 2.0 | Description-driven (no WAV clone). **Today's Hindi production target**; better Hindi models being researched (see `docs/research/hindi_tts_2026.md`). Auto-falls back to local `kokoro hf_alpha`. |

---

## Cloud-first laptop-fallback policy (2026-05-06)

Established by user direction:

> "for laptop we keep F5 for all and kokoro for hindutava-animated.
> for cloud — all chatterbox. for hindi — currently indicparler, but
> we are going to add more and better hindi models which are free,
> do research."

Translated to code:

| Cloud provider | If Cloud Run is unreachable → falls back to | Reason |
|---|---|---|
| `cloudrun_chatterbox` | local `f5_tts` (sarah.wav) | Laptop standard; local Chatterbox is too slow on MPS |
| `cloudrun_f5` | local `f5_tts` (sarah.wav) | Identical model |
| `cloudrun_higgs` | local `f5_tts` (sarah.wav) | Higgs has no realistic local equivalent (PyTorch-only) |
| `cloudrun_cosyvoice` | local `f5_tts` (sarah.wav) | CosyVoice not installed in laptop venv |
| `cloudrun_indicparler` | local `kokoro hf_alpha` | Hindi has no F5; Kokoro is the only on-laptop Hindi |

The fallback is implemented in `pipeline/tts/cloudrun.py::_synth_cloudrun_*`
and triggers on `CloudRunUnavailable` (DNS fail, 5xx, timeout >120s).
Set `CLOUDRUN_TTS_DISABLE_FALLBACK=1` in tests to hard-error instead.

---

## Active provider per channel (cloud-first reality, 2026-05-06)

| Channel / variant | Provider | Voice | Notes |
| --- | --- | --- | --- |
| airecap | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | Was `kokoro am_michael`; flipped to cloud Chatterbox 2026-05-06 |
| cosmosdecoded | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | Was `kokoro`; flipped 2026-05-06 |
| historyrecapped | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | Was `kokoro`; flipped 2026-05-06 |
| hindutavaanimated | `kokoro` (local) | `hf_alpha` (Hindi female) | **Stays local**; Indic Parler cloud + better Hindi models pending research |
| mystoriesanimated (parent) | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | Was `chatterbox` (local); flipped 2026-05-06 |
| mystoriesanimated/variants/aita_animated.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | Was `f5_tts`; flipped 2026-05-06 |
| mystoriesanimated/variants/aita_animated_motion.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/aita_cliffhanger_animated.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/aita_cliffhanger_part2_animated.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/aita_cliffhanger_part2_text.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/aita_cliffhanger_text.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/aita_text.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/tifu.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/today_in_history.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| mystoriesanimated/variants/wiki_oddities.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| rhymetimejunction | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06; spoken bridges only — songs are sung by Suno |
| sportstoriesanimated (parent) | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |
| sportstoriesanimated/variants/ranked.yaml | `cloudrun_chatterbox` | `pipeline/voice_refs/sarah.wav` | flipped 2026-05-06 |

---

## Voice references on disk

`pipeline/voice_refs/` holds the conditioning clips:

- `sarah.wav` (24 kHz mono, 9.5 s) + `sarah.txt` — the only English
  ref currently on disk. Re-clipped 2026-05-04 from a long-form WW1
  raw chunk. **Critical fix on 2026-05-05:** the .txt file was
  mismatched ("Anthropic plug…") to the actual audio ("Tonight we
  travel to the western front…") — caused F5 "today" leaks. Whisper-
  derived correct transcript now in place. Backup at
  `sarah.txt.WRONG-2026-05-05`.
- `theo.txt` only (no .wav). The male reference was lost in the
  2026-05-04 wipe. Channels that previously used Theo are running
  sarah.wav until a male WAV is re-recorded.

To re-record Theo:
1. Capture a 5-15 s 24 kHz mono WAV in the desired voice.
2. Save as `pipeline/voice_refs/theo.wav`.
3. Update `tts_voice` + `tts_ref_text` in the relevant YAMLs.

---

## Cost expectations (cloud-first)

| Volume | Cloud Run GPU minutes / month | Estimated cost / month |
|---|---:|---:|
| Today (~13 videos / month) | ~250 | ~₹200 ($2.50) |
| 4× current | ~1,000 | ~₹700 ($8) |
| Full throttle (~138 videos / month) | ~1,767 | ~₹1,250 ($15) |

Against $150 GCP credits → ~10 months of runway at full throttle. See
`docs/cloudrun_tts.md` for the cost dashboard URL.

---

## Singleton + ref-audio cache (LOCAL FALLBACK PATH ONLY)

> **Note (2026-05-06):** This section is now relevant only when
> Cloud Run is unhealthy and the laptop fallback path activates. In
> normal cloud-first operation these caches are cold. Keep the rules
> in case of long Cloud Run outage, but they are no longer the hot
> path.

`pipeline/audio.py` holds two module-level caches that the local
F5 fallback uses:

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

---

## Hindi / Hinglish (today + roadmap)

**Today (2026-05-06):**
- **Production default for hindutavaanimated:** local `kokoro hf_alpha`
  (the only Hindi-capable laptop voice; female; flat prosody).
- **Cloud option:** `cloudrun_indicparler` — description-driven, 22
  Indic languages, Apache 2.0. Has a 30 s truncation issue
  (max_new_tokens=4096; needs bump to ~16000 to support full kathaa
  chunks).
- **CosyVoice 2 does NOT speak Hindi** — proven empirically (Whisper
  detects Korean from Hindi-text rendering). Trained on CN/EN/JP/KR +
  EU only.

**Pending research (free / open-source / cheap Hindi models):**
- Bark, MMS-TTS Meta, MaskGCT, F5-Indic, MeloTTS Hindi, Sarvam-TTS,
  XTTS v2 Hindi voice clone, OpenVoice v2.
- Findings will land in `docs/research/hindi_tts_2026.md`. The top
  pick will be deployed as `cloudrun_<modelname>` and the routing
  matrix above updated.

---

## Rollback

To revert all channels back to local-only TTS in one shot:

```bash
unset CLOUDRUN_TTS_F5_URL CLOUDRUN_TTS_HIGGS_URL CLOUDRUN_TTS_CHATTERBOX_URL \
      CLOUDRUN_TTS_COSYVOICE_URL CLOUDRUN_TTS_INDICPARLER_URL CLOUDRUN_TTS_URL
```

Every `cloudrun_*` provider then triggers `CloudRunUnavailable` →
fallback path. Or per-channel: edit the YAML's `tts_provider:` line
back to `f5_tts` / `kokoro`.

---

## Changelog

| Date | Event |
|---|---|
| 2026-05-04 | sarah.wav re-clipped from WW1 long-form after recovery wipe |
| 2026-05-05 | sarah.txt mismatch fixed (was Anthropic blurb, audio was western-front intro) |
| 2026-05-05 | Cloud Run F5 service deployed; first cloud TTS provider live |
| 2026-05-05 | Cloud Run Chatterbox + CosyVoice + Indic Parler services deployed |
| 2026-05-06 | Cloud Run Higgs Audio v2 service deployed (PierrunoYT mirror) |
| 2026-05-06 | **Cloud-first migration complete.** All 17 production channel YAMLs flipped to `cloudrun_chatterbox` (English) or remain on `kokoro` (hindutavaanimated only). Laptop is now fallback-only. |
