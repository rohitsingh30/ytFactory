# Hindi TTS — research findings 2026-05-06

> **Status:** research complete. **Top recommendation: `ai4bharat/IndicF5`**
> as the next Hindi TTS service to deploy. Three candidates pre-screened
> across license, Hindi-coverage, and Cloud Run hostability gates.

This research was commissioned to find FREE / open-source / cheap Hindi
TTS models that could replace or complement our currently-deployed
`cloudrun_indicparler` service for the `hindutavaanimated` channel
(Hindu mythology kathaa narration, 60-120 min long-form Hindi).

The top 3 picks below are deployable on our existing Cloud Run + L4
stack in `asia-southeast1`. The disqualified models are listed at the
bottom for reference (so the next person doesn't re-investigate).

---

## TL;DR — top 3 to deploy next, in order

### 🥇 1. AI4Bharat IndicF5 — `ai4bharat/IndicF5`
- **License:** MIT (more permissive than Apache 2.0)
- **Active:** Sep 2025 commit, Mar 2026 HF update
- **Architecture:** F5-TTS flow-matching (same as our deployed `cloud/tts-f5/`)
- **Hindi training:** 1,417 hours of curated Indian speech (Rasa,
  IndicTTS-IITM, LIMMITS24, IndicVoices-R)
- **Languages:** 11 Indic (As, Bn, Gu, **Hi**, Kn, Ml, Mr, Or, Pa, Ta, Te)
- **API:** WAV-clone (5-15s reference WAV + reference text)
- **VRAM:** ~2-3 GB (vs 4 GB for Indic Parler, 12 GB for Higgs)
- **RTF on L4 (estimated):** **0.20-0.30** — 2-4× faster than Indic Parler
- **Voice cloning:** ✅ Yes — pick ANY 15-second devotional narrator clip,
  clone it consistently across 60-120 min of katha
- **Cloud Run effort:** ~2-3 hours (clone `cloud/tts-f5/`, swap model id)
- **Risks:** No published Hindi MOS scores; HF gated=auto (needs HF_TOKEN)

### 🥈 2. Fix the existing Indic Parler service first (zero infra cost)
- Bump `max_new_tokens` 4096 → 8192 or 16384 in `cloud/tts-indicparler/server.py`
- Add caller-side chunking for inputs >800 chars
- Unblocks the 30-second-per-call ceiling without any new deployment
- **Effort:** ~1 hour, no new container

### 🥉 3. Orpheus 3B Hindi — `canopylabs/3b-hi-ft-research_release`
- **License:** Apache 2.0 ✅
- **Active:** Apr 2025 HF, Dec 2025 repo commit
- **Architecture:** LLaMA-3.2 3B + SNAC audio codec + vLLM inference
- **Hindi training:** dedicated Hindi pretrain → finetune
- **API:** WAV-clone + emotion tags (`<laugh>`, `<sigh>`, `<gasp>`, …)
- **VRAM:** ~6-8 GB (BF16); ~3.5 GB at 4-bit
- **RTF on L4 (estimated):** ~0.30-0.50
- **Voice cloning:** ✅ Yes (zero-shot)
- **Cloud Run effort:** Higher than IndicF5 — vLLM dependency adds
  Dockerfile + memory overhead
- **Risks:** "research_release" label, only 615 downloads, no published
  benchmarks. A/B candidate AFTER IndicF5 is validated.

---

## Master comparison table (best Hindi quality first)

| # | Model | HuggingFace ID | License | Last Active | Hindi Evidence | API Style | VRAM | RTF L4 | Clone? | Hostable? |
|---|-------|---------------|---------|-------------|----------------|-----------|-----:|-------:|--------|-----------|
| 1 | **IndicF5** ⭐ | `ai4bharat/IndicF5` | **MIT** ✅ | Sep 2025 / Mar 2026 | 1417h Indic-specific | WAV-clone (F5) | ~2-3 GB | **~0.15-0.25** | ✅ | ✅ |
| 2 | **Indic Parler-TTS** (deployed) | `ai4bharat/indic-parler-tts` | **Apache 2.0** ✅ | Sep 2025 | 1806h Indic+EN; Hindi NSS 84.79% | Description-driven | ~4 GB | ~0.4-0.8 | ❌ | ✅ deployed |
| 3 | **Orpheus 3B Hindi** ⭐ | `canopylabs/3b-hi-ft-research_release` | **Apache 2.0** ✅ | Apr 2025 / Dec 2025 | dedicated pretrain + finetune | WAV-clone + emotion | ~6-8 GB | ~0.3-0.5 | ✅ | ✅ |
| 4 | Somya-IndicTTS | `somyalab/Somya-IndicTTS` | MIT ✅ | Dec 2025 | 10 Indic; Orpheus deriv. | LLM (SNAC) | ~8 GB | ~0.5 | ✅ | ✅ |
| 5 | Higgs Audio v2 (deployed) | `PierrunoYT/higgs-audio-v2-generation-3B-base` | Apache 2.0 ✅ | Jan 2026 | multilingual zero-shot; Hindi unaudited | WAV-clone | ~12 GB | ~0.4-0.6 | ✅ | ✅ deployed |
| 6 | AI4Bharat Indic-TTS (older) | Bhashini platform | MIT ✅ | ICASSP 2023 | FastPitch+HiFiGAN; 13 Indic | Preset | ~0.5 GB | <0.05 | ❌ | ✅ |
| 7 | OpenVoice V2 | `myshell-ai/OpenVoiceV2` | MIT ✅ | Apr 2024 | Hindi NOT in training (cross-lingual only) | WAV-clone (style) | ~1 GB | <0.1 | accent only | ✅ |
| 8 | F5-TTS Hindi (community) | `telecmiusa/F5_TTS_Hindi` | unknown | Feb 2025 | community Hindi finetune; no metrics | WAV-clone | ~2-3 GB | ~0.15 | ✅ | ⚠️ |
| 9 | Kokoro hf_alpha (fallback) | `hexgrad/Kokoro-82M` | Apache 2.0 ✅ | Active | single Hindi voice, flat | Preset | 0.3 GB | <0.05 (CPU) | ❌ | ✅ |

### Disqualified (license)

| Model | License problem |
|---|---|
| Meta MMS-TTS-hin (`facebook/mms-tts-hin`) | CC-BY-NC 4.0 — non-commercial |
| Bark (`suno/bark`) | "Research purposes only" |
| XTTS v2 (`coqui/XTTS-v2`) | Coqui CPML — non-commercial; Coqui shut down |
| Spark-TTS 0.5B (`SparkAudio/Spark-TTS-0.5B`) | CC BY-NC-SA + no Hindi training data |

### Disqualified (no Hindi support)

| Model | Reason |
|---|---|
| CosyVoice 2 0.5B (`FunAudioLLM/CosyVoice2-0.5B`) | CN/EN/JP/KR + EU only — Hindi proven gibberish (Whisper detects Korean) |
| MaskGCT (`amphion/MaskGCT`) | EN+ZH only (Emilia dataset) |
| MeloTTS (`myshell-ai/MeloTTS-*`) | No Hindi model exists |
| KaniTTS "Hindi" (`kapilkarda/kani-tts-hindi`) | Repo name misleading; actual languages EN/DE/ZH/KR/AR/ES |

### Not self-hostable

| Model | Reason |
|---|---|
| Sarvam Bulbul V3 | API-only (SaaS); claims to beat ElevenLabs on Indic, but no open weights as of June 2026 |

---

## Indic Parler today vs IndicF5 — head-to-head

| Dimension | Indic Parler (today) | IndicF5 (proposed) |
|---|---|---|
| Hindi naturalness | NSS 84.79% (official) | unscored, but trained on higher-quality curated Indic |
| Voice variety | 4 preset Hindi voices (Rohit, Divya, Aman, Rani) | **unlimited** — any WAV → voice |
| Voice cloning | ❌ description-only | ✅ zero-shot 5-15s WAV |
| Katha narrator consistency | via named speaker | **superior** — clone narrator once, reuse 120 min |
| Latency (30s output) | ~12-24s wall (autoregressive) | **~6-9s wall** (flow-matching, RTF ~0.25) |
| Cost / hour at L4 | ~$0.60-0.80 | **~$0.12-0.18** (5× cheaper) |
| VRAM | ~4 GB (BF16) | **~2-3 GB** |
| Max single synthesis | ~30s per call (token limit) | no hard limit |
| Hinglish handling | tokenizer can stumble on EN words | character-level, less script-fragile |
| Integration difficulty | ✅ done | low — clone `cloud/tts-f5/`, swap model id |
| License | Apache 2.0 | MIT |
| Last updated | Sep 2025 | Mar 2026 |

**Bottom line:** for 60-120 min Hindu mythology katha, **IndicF5 would
be transformative** — voice cloning + 5× faster + no token ceiling.
Keep Indic Parler as the description-driven Shorts variant where we
want quick voice variation without needing a reference WAV on hand.

---

## Open uncertainties (validate before betting production on IndicF5)

1. **No published Hindi MOS / FLEURS / INDIC SUPERB scores for IndicF5.**
   Run 10-20 sample syntheses on a Hindi katha script before flipping
   `hindutavaanimated/config.yaml`.

2. **HF `gated: auto`** — requires HF_TOKEN env var on the Cloud Run
   service. Already wired via `~/.cache/huggingface/token` in our
   build pipeline.

3. **Higgs Audio v2 Hindi unaudited.** Already deployed but Hindi
   quality unknown. Run the same evaluation script to see if Higgs
   alone could cover the Hindi need (would let us drop a service).

4. **Orpheus Hindi training data size unpublished.** Canopy Labs
   doesn't say how many hours of Hindi audio. The "research_release"
   label means treat as preview.

5. **Sarvam Bulbul open-source future.** Sarvam has open-sourced LLMs
   (sarvam-1) before. If they release Bulbul weights under permissive
   license, it leapfrogs everything on this list. Watch
   `github.com/sarvamai`.

6. **Indic MaskGCT.** No Indic-trained MaskGCT today. If AI4Bharat
   releases one, it would be SOTA for zero-shot Hindi cloning quality.

---

## Integration plan (for IndicF5)

1. Clone `cloud/tts-f5/` → `cloud/tts-indicf5/`
2. In `Dockerfile`: change model_id arg from F5-TTS base to
   `ai4bharat/IndicF5`. Add `trust_remote_code=True` if needed.
3. In `server.py`: `AutoModel.from_pretrained("ai4bharat/IndicF5",
   trust_remote_code=True)`. Verify the same `/synth` request body
   schema works (ref_audio_b64 + ref_text + text).
4. Add HF_TOKEN env var to deploy command.
5. Add `cloudrun_indicf5` provider entry in `pipeline/tts/cloudrun.py`
   and `pipeline/audio.py::synthesize`. Fallback path: local
   `kokoro hf_alpha`.
6. Run a 10-20 clip Hindi katha audition. If quality > Indic Parler,
   flip `hindutavaanimated/config.yaml` to `cloudrun_indicf5`.
7. Update `docs/tts_stack.md` and this file.

Estimated effort: **3-4 hours** for steps 1-5; ~1 hour for the
audition; ~30 min for doc updates.

---

## Sources

Primary:
- `huggingface.co/ai4bharat/IndicF5`
- `huggingface.co/ai4bharat/indic-parler-tts`
- `github.com/AI4Bharat/IndicF5`
- `huggingface.co/canopylabs/3b-hi-ft-research_release`
- `github.com/canopyai/Orpheus-TTS`

License checks:
- `huggingface.co/coqui/XTTS-v2/blob/main/LICENSE.txt` (CPML)
- `huggingface.co/SparkAudio/Spark-TTS-0.5B` (CC-BY-NC-SA)
- `huggingface.co/facebook/mms-tts-hin` (CC-BY-NC 4.0)
- `huggingface.co/suno/bark` (research-only)

Comparison/SaaS:
- `sarvam.ai/apis/text-to-speech` (SaaS, no open weights)

In-repo:
- `cloud/tts-indicparler/server.py` — current Indic Parler service
- `cloud/tts-higgs/server.py` — current Higgs service (also multilingual)
- `cloud/tts-f5/server.py` — F5 service (architectural template for IndicF5)
