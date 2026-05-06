# Cloud Run Higgs Audio v2 — service runbook + post-mortem

> **Status (2026-05-06):** Service `ytfactory-tts-higgs` is **LIVE** in
> `asia-southeast1` on NVIDIA L4. Image `tts-higgs:v12`, revision
> `00008-8b8`. Runtime weight download (~65 s cold-start). Steady-state
> RTF ≈ 1.07. **Don't switch back to the bosonai/* official checkpoints
> without re-validating** — see "the PierrunoYT lesson" below.

This doc captures the full story of getting Higgs Audio v2 running on
our self-hosted Cloud Run + L4 stack: what worked, what didn't, why a
community mirror is the canonical checkpoint, and the regression tests
that lock that in. Built from a 12+ hour debugging session on
2026-05-05 — preserved here so the next agent doesn't repeat any of it.

---

## TL;DR — operating the service today

```bash
# Service URL (project-id form is canonical; -atmlrispgq form also works)
URL="https://ytfactory-tts-higgs-767262167641.asia-southeast1.run.app"

# Warm + smoke
.venv/bin/python -c "
import sys, time; sys.path.insert(0, '.')
from pipeline.tts.cloudrun import _get_id_token
import urllib.request
tok = _get_id_token('$URL/readyz')
req = urllib.request.Request('$URL/readyz', headers={'Authorization': f'Bearer {tok}'})
print(urllib.request.urlopen(req, timeout=600).read().decode())
"

# /readyz on cold-start: ~65 s (downloads 11.5 GB weights from HF Hub)
# /readyz when warm:     ~0.05 s
# /synth steady-state RTF: ~1.07 wall/audio (vs F5's 0.21)
```

| Operation | Wall time | Notes |
|---|---:|---|
| Cold-start /readyz (first call after scale-to-zero) | ~65 s | One-time per container; downloads PierrunoYT weights to /models/hf |
| Warm /readyz | <0.1 s | Engine is already loaded |
| First /synth on a fresh engine | ~30-45 s | KV cache alloc, CUDA graph warmup |
| Steady-state /synth (5 s of audio) | ~5-6 s wall | RTF ~1.07 |

---

## Service spec

| | |
|---|---|
| Project | `ytfactory-prod` |
| Region | `asia-southeast1` (Singapore) |
| Service | `ytfactory-tts-higgs` |
| Image | `asia-southeast1-docker.pkg.dev/ytfactory-prod/ytfactory-tts/tts-higgs:v12` |
| Latest revision | `ytfactory-tts-higgs-00008-8b8` |
| URL (canonical) | `https://ytfactory-tts-higgs-767262167641.asia-southeast1.run.app` |
| URL (alt) | `https://ytfactory-tts-higgs-atmlrispgq-as.a.run.app` |
| GPU | 1 × NVIDIA L4 24 GB, no zonal redundancy |
| CPU / memory | 8 vCPU / 24 GiB (24 GiB requires 8 CPU per Cloud Run rule) |
| Concurrency | 1 (one synth at a time per container) |
| Min / max instances | 0 / 2 |
| Auth | `--no-allow-unauthenticated` (IAM ID token required) |
| Service account | `tts-runner@ytfactory-prod.iam.gserviceaccount.com` |
| GCS bucket | `gs://ytfactory-tts-io` (>5 MB WAV handoff) |
| Env vars set | `HF_TOKEN`, `GCS_BUCKET=ytfactory-tts-io`, `LOG_LEVEL=INFO` |

### Env-var overrides (no rebuild needed)

| Var | Default | Purpose |
|---|---|---|
| `HIGGS_MODEL_REPO` | `PierrunoYT/higgs-audio-v2-generation-3B-base` | Switch to a different model mirror at runtime |
| `HIGGS_TOKENIZER_REPO` | `PierrunoYT/higgs-audio-v2-tokenizer` | Switch to a different audio-tokenizer mirror |
| `HF_TOKEN` | (must be set) | HF Hub auth for weight pull on cold-start |
| `GCS_BUCKET` | `ytfactory-tts-io` | Where >5 MB WAVs land |
| `LOG_LEVEL` | `INFO` | Lift to `DEBUG` to see HF Hub progress + per-call timings |

To swap mirrors without rebuilding: redeploy the same image with new
env vars:

```bash
gcloud run services update ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1 \
  --update-env-vars=HIGGS_MODEL_REPO=other-org/higgs-fork,\
HIGGS_TOKENIZER_REPO=other-org/higgs-fork-tokenizer
```

---

## The PierrunoYT lesson — why we don't use bosonai/* checkpoints

**The 12+ hour debugging cycle on 2026-05-05** boiled down to this:
the official `bosonai/higgs-audio-v2-generation-3B-base` checkpoint
**no longer loads with Boson AI's own GitHub code at
`github.com/boson-ai/higgs-audio`.**

In late 2026 Boson refactored the checkpoint configs without updating
the github code:

### What changed in bosonai/* official

| Field | OLD (still in github code) | NEW (in bosonai/* HF checkpoint) |
|---|---|---|
| `config.json` `model_type` | `"higgs_audio"` | `"higgs_audio_v2"` |
| `config.json` `architectures` | `["HiggsAudioModel"]` | `["HiggsAudioV2ForConditionalGeneration"]` |
| `config.json` `text_config` | populated dict (`hidden_size: 3072`, `vocab_size: 128256`, `pad_token_id: null`, …) | **`null`** |
| Top-level `pad_token_id` | absent (lives in `text_config`) | `128001` |
| Top-level `hidden_size` / `vocab_size` | absent (lives in `text_config`) | `3072` / `128256` |
| Tokenizer `config.json` schema | `n_filters`, `target_bandwidths` (acoustic encoder spec) | `acoustic_model_config: {…}` (new container) |

### What our installed code expects

`/opt/higgs-audio/boson_multimodal/model/higgs_audio/__init__.py`
registers:

```python
AutoConfig.register("higgs_audio", HiggsAudioConfig)
AutoModel.register(HiggsAudioConfig, HiggsAudioModel)
```

— so it can **only** load checkpoints with `model_type == "higgs_audio"`
and `architectures` containing `"HiggsAudioModel"`. The new official
checkpoint silently triggers transformers' generic loader, which then
crashes deep inside model construction.

### Failure cascade we hit

1. **Try bosonai/* (v3-v8 builds):** dep tree thrash, eventually got past
   imports → `nn.Embedding(32000, 4096, padding_idx=128001)` →
   `AssertionError: Padding_idx must be within num_embeddings`. Why?
   Because `text_config: null` in JSON → HiggsAudioConfig falls through
   to `CONFIG_MAPPING["llama"]()` → vocab_size=32000 (LlamaConfig
   default), but top-level `pad_token_id=128001`.
2. **Patch torch's nn.Embedding** to clamp out-of-range padding_idx →
   model now constructs → load weights → `RuntimeError: size mismatch`
   on every layer. Same root cause: `text_config: null` →
   `hidden_size=4096` (Llama default) instead of the checkpoint's actual
   `3072`. Tens of layers wrong.
3. **The fix:** **switch to the PierrunoYT mirror** which preserves the
   OLD config schema. `model_type=higgs_audio`, `text_config` populated
   correctly with `hidden_size=3072` and `vocab_size=128256`. Loads on
   first try.

The padding patch was treating a SYMPTOM. The real bug was the
checkpoint/code version mismatch. Removed the patch entirely in v12.

### Why the PierrunoYT mirror is safe to depend on

| Risk | Mitigation |
|---|---|
| PierrunoYT updates their config to mirror bosonai/* | `cloud/tts-higgs/test_higgs_config.py::test_pierrunoyt_model_config_matches_installed_code_schema` fails loudly |
| PierrunoYT deletes the repo | Other community mirrors exist (`mzbac/`, `eustlb/`); same model weights, can re-host |
| Boson eventually fixes their official checkpoint | When that happens, set `HIGGS_MODEL_REPO=bosonai/...` env var and rerun the regression tests; if green, redeploy and PR-update the default in `server.py` |
| Boson eventually publishes a v3 TTS (currently v3 is STT only) | Cloud Run service stays put; spin up a second service `ytfactory-tts-higgs-v3` rather than blowing this one away |

---

## Architecture

```
                ┌──────────────────────────────────────────────────────┐
                │  M2 Max (renderer / orchestrator)                    │
                │                                                      │
                │  pipeline/audio.py::synthesize()                     │
                │     └─ provider="cloudrun_higgs"                     │
                │         │                                            │
                │         ▼ POST /synth + IAM ID token                 │
                └────────────────────────┼─────────────────────────────┘
                                         │
                                         │ HTTPS, ≤5 MB inline /                  >5 MB → GCS
                                         ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │ Cloud Run service `ytfactory-tts-higgs`     (asia-southeast1)     │
   │ ────────────────────────────────────────────────────────────────  │
   │  GPU: 1 × NVIDIA L4 24 GB                                         │
   │  CPU: 8 vCPU   Memory: 24 GiB                                     │
   │  Concurrency: 1   Min: 0   Max: 2                                 │
   │  Auth: --no-allow-unauthenticated (IAM)                           │
   │ ────────────────────────────────────────────────────────────────  │
   │  uvicorn → server.py (FastAPI)                                    │
   │     ├─ /readyz : warms HiggsAudioServeEngine onto cuda            │
   │     │           (downloads 11.5 GB weights from HF Hub on cold-   │
   │     │            start to /models/hf)                             │
   │     └─ /synth  : zero-shot voice clone with WAV reference         │
   │ ────────────────────────────────────────────────────────────────  │
   │  Models loaded:                                                   │
   │   * PierrunoYT/higgs-audio-v2-generation-3B-base   (~6 GB FP16)   │
   │   * PierrunoYT/higgs-audio-v2-tokenizer            (~17 MB)       │
   └───────────────────────────────────────────────────────────────────┘
```

---

## Build + deploy

### Build a new image

```bash
cd cloud/tts-higgs
gcloud builds submit \
  --project=ytfactory-prod \
  --tag=asia-southeast1-docker.pkg.dev/ytfactory-prod/ytfactory-tts/tts-higgs:vN \
  --machine-type=e2-highcpu-32 \
  --disk-size=200 \
  --timeout=3600s \
  --async \
  .
```

`--machine-type=e2-highcpu-32 --disk-size=200` is required because
default Cloud Build workers OOM/disk-full on >5 GB images.

### Deploy

```bash
HF_TOKEN=$(cat ~/.cache/huggingface/token)
gcloud run deploy ytfactory-tts-higgs \
  --project=ytfactory-prod \
  --region=asia-southeast1 \
  --image=asia-southeast1-docker.pkg.dev/ytfactory-prod/ytfactory-tts/tts-higgs:vN \
  --gpu=1 --gpu-type=nvidia-l4 --no-gpu-zonal-redundancy \
  --no-cpu-throttling --memory=24Gi --cpu=8 \
  --concurrency=1 --max-instances=2 --min-instances=0 \
  --timeout=3600 --no-allow-unauthenticated \
  --execution-environment=gen2 \
  --set-env-vars="HF_TOKEN=$HF_TOKEN,GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO"
```

### Watch logs

```bash
gcloud run services logs tail ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1
```

### Rollback

Cloud Run keeps prior revisions:

```bash
# List
gcloud run revisions list --service=ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1

# Roll back
gcloud run services update-traffic ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1 \
  --to-revisions=ytfactory-tts-higgs-<old-revision>=100
```

To remove the service entirely:

```bash
gcloud run services delete ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1
```

Channels using `cloudrun_higgs` will fall through to whatever the
provider router does (today: hard-fail with a clear error so the
caller can route to `chatterbox` or `f5_tts`).

---

## Tests that must always pass

`cloud/tts-higgs/test_higgs_config.py` has 4 tests, all run on a
clean Python 3.10 venv (no GPU, no torch needed for unit tests):

```bash
/tmp/higgs-validate/bin/python -m pytest cloud/tts-higgs/test_higgs_config.py -v
```

| Test | What it locks in |
|---|---|
| `test_server_defaults_point_at_pierrunoyt_mirror` | `HIGGS_MODEL_REPO` and `HIGGS_TOKENIZER_REPO` defaults in `server.py` are the PierrunoYT mirror; nobody can silently re-introduce bosonai/* |
| `test_pierrunoyt_model_config_matches_installed_code_schema` | Live HF check: PierrunoYT model config still has `model_type=higgs_audio`, populated `text_config`, `hidden_size=3072`, `vocab_size=128256`, and `pad_token_id < vocab_size` (no padding patch needed) |
| `test_pierrunoyt_tokenizer_config_uses_old_schema` | Live HF check: PierrunoYT tokenizer config still uses the OLD `n_filters`/`target_bandwidths` schema; NOT the new `acoustic_model_config` schema |
| `test_env_var_override_works` | `server.py` reads `HIGGS_MODEL_REPO` and `HIGGS_TOKENIZER_REPO` from env so we can swap mirrors without a rebuild |

The 2 network tests (`@pytest.mark.network`) require internet but no
auth and complete in <2 s. CI should run them on every PR that touches
`cloud/tts-higgs/`.

### Build-time gate

`cloud/tts-higgs/Dockerfile` ends with an `IMPORT_SMOKE_OK` `RUN`
line that walks the entire boson_multimodal import chain at build
time. If a future dep update silently breaks an import, the build
fails in ~7 min instead of after a 30-min push + 13-min pull cycle.

---

## Common operational issues

### `/readyz` returns 500 — investigate immediately

99% of the time this is one of:

1. **HF_TOKEN missing.** Check `gcloud run services describe ytfactory-tts-higgs --project=ytfactory-prod --region=asia-southeast1 --format='value(spec.template.spec.containers[0].env)'`. Must contain `HF_TOKEN`.
2. **Mirror schema regressed.** Run the test suite. If
   `test_pierrunoyt_model_config_matches_installed_code_schema` fails,
   PierrunoYT updated their config; either pick a new mirror or update
   our installed `boson_multimodal` to match.
3. **OOM during model load.** Check logs for `CUDA out of memory`.
   Model needs ~12-14 GB VRAM; L4 has 24 GB. If we ever change the
   model and OOM, drop concurrency=1 isn't enough — we'd need a
   bigger GPU SKU (no L4 alternative — would have to migrate region
   to one with A100).
4. **CUDA driver mismatch.** Image base is `nvidia/cuda:12.4.0-runtime`.
   Cloud Run L4 driver supports CUDA 12.4. If we bump base to >=12.5
   without Cloud Run support, will get `CUDA driver version is
   insufficient` at engine load.

### `/synth` returns 500

Almost always reference WAV issues:

- `bad ref_audio_b64`: the base64 didn't decode to a valid WAV.
  Check `_ref_audio_to_path` in server.py.
- `synth error: ...`: read the full error from logs. Common ones:
  - `Reference audio too short`: ref WAV needs ≥1.5 s
  - `Sampling rate mismatch`: Higgs internally resamples but very
    weird sample rates (e.g. 8 kHz telephone) can fail

### Slow steady-state RTF (>2)

Higgs's RTF is ~1.07 at steady state. If you see >2 sustained, check:

- Container is on a different GPU (rare; L4 is only SKU for our region)
- KV cache eviction thrashing (long inputs > 4096 tokens — chunk the
  text caller-side)
- Service is scaling up new container per request (concurrency=1, so
  burst traffic spawns multiple containers each paying cold-start cost)

### Cold-start takes >2 min

Should be ~65 s. Causes of >2 min:

- HF Hub network slow (rare). Logs show `huggingface_hub` GET
  progress lines.
- New revision cold-starting **and** weights-cache cleared (Cloud
  Run periodically wipes /tmp). First request after a deploy is the
  slowest; run a `/readyz` smoke test post-deploy to absorb it.

---

## Bench output reference

The decision-matrix bench renders Higgs alongside F5/Chatterbox/Indic
Parler/Kokoro for every channel. The latest run on Higgs is:

```
data/_bench/decision-matrix-v2/20260505-205057/
├── 01_mystoriesanimated_aita/higgs_neutral.wav
├── 02_historyrecapped_sleep/higgs_neutral.wav
├── 03_historyrecapped_short/higgs_neutral.wav
├── 04_sportstoriesanimated_doc/higgs_neutral.wav
├── 05_cosmosdecoded_doc/higgs_neutral.wav
└── 06_hindutavaanimated_kathaa/higgs_neutral.wav
```

To re-render only the Higgs cells (without rebuilding the other 65
WAVs):

```bash
.venv/bin/python scripts/bench_higgs_fillin.py --run 20260505-205057
.venv/bin/python scripts/bench_higgs_fillin.py --run 20260505-205057 --force  # overwrite
```

---

## Files

| Path | What |
|---|---|
| `cloud/tts-higgs/Dockerfile` | Container build recipe; ends with `IMPORT_SMOKE_OK` gate |
| `cloud/tts-higgs/server.py` | FastAPI: `/readyz`, `/synth`; PierrunoYT mirror via env-overridable `HIGGS_MODEL_REPO` / `HIGGS_TOKENIZER_REPO` |
| `cloud/tts-higgs/requirements.txt` | Pinned deps; install with-deps for `descript-audiotools==0.7.2 descript-audio-codec==1.0.0` (NOT `--no-deps`) |
| `cloud/tts-higgs/test_higgs_config.py` | 4 regression tests locking in the mirror choice |
| `cloud/tts-higgs/deploy.sh` | (legacy) wrapper for `gcloud builds submit` + `gcloud run deploy` |
| `pipeline/tts/cloudrun.py` | `_synth_cloudrun_higgs`, `_get_id_token`, env routing |
| `pipeline/audio.py` | `_synth_cloudrun_higgs` provider entry |
| `scripts/bench_higgs_fillin.py` | Render only Higgs cells into existing bench dir |
| `scripts/bench_decision_matrix_v2.py` | Full bench harness; defines `HIGGS_VARIANTS`, `render_higgs`, `SERVICE_URLS` |
| `docs/cloud_service_dep_playbook.md` | Pre-build playbook (must read before adding any new model service) |
| `docs/cloudrun_tts.md` | Top-level Cloud Run TTS runbook |
| `docs/tts_stack.md` | Per-channel routing (laptop + cloud) |

---

## Decision history

| Date | Choice | Reason |
|---|---|---|
| 2026-05-05 (mid-day) | Bake Higgs + Chatterbox + CosyVoice into one mega-image | 5 builds failed on dep conflicts; abandoned |
| 2026-05-05 | Split into 5 separate services (`tts-{f5,higgs,chatterbox,cosyvoice,indicparler}`) | Each model has its own dep universe; no shared image |
| 2026-05-05 | Try `bosonai/higgs-audio-v2-generation-3B-base` checkpoint | It's the official Boson AI release |
| 2026-05-05 (eve) | Add `padding_patch.py` monkey-patch | nn.Embedding asserted on out-of-range pad_id; symptomatic fix |
| 2026-05-06 (00:30) | Discover model_type rename + text_config: null in bosonai/* | Real root cause; bosonai/* unloadable with current code |
| 2026-05-06 (00:36) | Switch to `PierrunoYT/higgs-audio-v2-generation-3B-base` mirror | Preserves OLD-schema configs that match installed code |
| 2026-05-06 (00:38) | **Drop padding_patch entirely** | Verified: PierrunoYT pad_id=128001 < vocab_size=128256, in-range, patch was no-op |
| 2026-05-06 (01:17) | v12 deployed | Clean cold-start in 65 s, /synth returns 200, RTF ~1.07 |
| 2026-05-06 (01:25) | All 6 channel cells rendered in bench | Higgs is production-ready |
