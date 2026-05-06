# Cloud Run TTS — operational runbook

> **Status (2026-05-06):** All 5 single-model TTS services are LIVE in
> `asia-southeast1` on NVIDIA L4. Each model has its own image,
> service, and URL — the mega-image approach was abandoned after 5
> dep-conflict build failures. See per-service runbooks linked below.

The cloud-hosted TTS path lives next to the local providers in
`pipeline/tts/`. Channel YAMLs flip a single line — `tts_provider:
f5_tts` → `cloudrun_f5` — to move from M2 Max MLX to L4. Local
providers stay in code as automatic fallback if the cloud service is
unhealthy.

---

## Why we moved long-form to cloud

| | Local M2 Max (F5-MLX) | Cloud Run L4 (F5-TTS) |
|---|---:|---:|
| RTF (real-time factor) | ~1.6-2.3 | **0.21-0.24** |
| 75-min sleep video render | ~1.5-9 hr (laptop locked) | **~30-45 min, async** |
| Multiple channels in parallel | impossible (1 GPU) | up to 5 (one L4 per channel) |
| Cost at full throttle | $0 + electricity + WindowServer crash risk | **~$6/mo against credits** |
| Hindi quality | Kokoro `hf_alpha` (flat) | Indic Parler (description-driven) on cloud; CosyVoice 2 NOT useful for Hindi (proven) |

`/docs/long_form_model_inventory.md` documents the local rules
(`gpu_one_render_at_a_time`, F5 singleton, MLX heap hygiene, etc).
The cloud path makes those rules irrelevant for the channels that
opt in — every render gets its own dedicated L4 instance.

---

## Architecture (per-service)

```
                ┌─────────────────────────────────────────────┐
                │  M2 Max (renderer / orchestrator)           │
                │                                             │
                │  pipeline/audio.py::synthesize()            │
                │     ├─ provider="f5_tts"            → local │
                │     ├─ provider="kokoro"            → local │
                │     ├─ provider="chatterbox"        → local │
                │     ├─ provider="cloudrun_f5"          ──┐  │
                │     ├─ provider="cloudrun_higgs"       ──┤  │
                │     ├─ provider="cloudrun_chatterbox"  ──┤  │
                │     ├─ provider="cloudrun_cosyvoice"   ──┤  │
                │     └─ provider="cloudrun_indicparler" ──┤  │
                └──────────────────────────────────────────┼──┘
                                                           │ HTTPS POST /synth
                                                           │ (gcloud ID token, IAM)
                                                           ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  asia-southeast1                                                │
   │   ┌────────────────────────────────────────────────────────┐    │
   │   │ ytfactory-tts            (F5-TTS)               L4 24G │    │
   │   │ ytfactory-tts-higgs      (Higgs Audio v2)       L4 24G │    │
   │   │ ytfactory-tts-chatterbox (Chatterbox)           L4 24G │    │
   │   │ ytfactory-tts-cosyvoice  (CosyVoice 2)          L4 24G │    │
   │   │ ytfactory-tts-indicparler(Indic Parler-TTS)     L4 24G │    │
   │   └────────────────────────────────────────────────────────┘    │
   │                                                                 │
   │  All services: --no-cpu-throttling --concurrency=1              │
   │                --no-allow-unauthenticated (IAM)                 │
   │                --memory=24Gi --cpu=8 --execution-environment=gen2│
   │                                                                 │
   │  WAV output: ≤5 MB inline (base64), >5 MB → gs://ytfactory-tts-io│
   └─────────────────────────────────────────────────────────────────┘
```

---

## Per-service URLs (current)

| Service | URL (canonical, project-id form) | Provider name | Status |
|---|---|---|---|
| `ytfactory-tts` (F5) | `https://ytfactory-tts-767262167641.asia-southeast1.run.app` | `cloudrun_f5` | ✅ LIVE |
| `ytfactory-tts-higgs` | `https://ytfactory-tts-higgs-767262167641.asia-southeast1.run.app` | `cloudrun_higgs` | ✅ LIVE (v12, PierrunoYT mirror) |
| `ytfactory-tts-chatterbox` | `https://ytfactory-tts-chatterbox-767262167641.asia-southeast1.run.app` | `cloudrun_chatterbox` | ✅ LIVE |
| `ytfactory-tts-cosyvoice` | `https://ytfactory-tts-cosyvoice-767262167641.asia-southeast1.run.app` | `cloudrun_cosyvoice` | ✅ LIVE (English only — Hindi proven gibberish) |
| `ytfactory-tts-indicparler` | `https://ytfactory-tts-indicparler-767262167641.asia-southeast1.run.app` | `cloudrun_indicparler` | ✅ LIVE (Hindi, description-driven) |

**`-atmlrispgq-as.a.run.app` form also resolves** to the same services;
both formats are valid.

For Higgs-specific operations see [`docs/cloudrun_higgs.md`](./cloudrun_higgs.md).

---

## Live infrastructure

| Resource | ID | Purpose |
|---|---|---|
| GCP project | `ytfactory-prod` | Already used for Firestore + Cloud Scheduler + GCS |
| Billing account | `012E39-E4ECEB-7F119F` (INR) | $150 credits — covers ~10 months at full throttle |
| Region | `asia-southeast1` (Singapore) | L4 open-access; ~50ms from India |
| Quota: nvidia_l4_gpu_allocation_no_zonal_redundancy | **5** (auto-approved) | 5 services × max 2 instances each = 10 desired, but burst-capped at 5 concurrent |
| Artifact Registry | `asia-southeast1-docker.pkg.dev/ytfactory-prod/ytfactory-tts` | Single repo, multiple tags |
| GCS bucket | `gs://ytfactory-tts-io` | >5 MB WAV handoff; 7-day delete lifecycle |
| Service account | `tts-runner@ytfactory-prod.iam.gserviceaccount.com` | Cloud Run runtime SA; bucket Object Admin |

### Image tags per service

| Service | Image |
|---|---|
| F5 | `…/ytfactory-tts/server:vN` |
| Higgs | `…/ytfactory-tts/tts-higgs:vN` (current: **v12**) |
| Chatterbox | `…/ytfactory-tts/tts-chatterbox:vN` |
| CosyVoice | `…/ytfactory-tts/tts-cosyvoice:vN` |
| Indic Parler | `…/ytfactory-tts/tts-indicparler:vN` |

---

## Channel routing (cloud-first per-channel matrix, 2026-05-06)

This table reflects the **production target** post-Higgs-deployment.
Today's actual reality is in `docs/tts_stack.md` — the two should
converge as channel YAMLs are flipped.

| Channel | Format | TODAY | TARGET | Why |
|---|---|---|---|---|
| historyrecapped | sleep long-form (60-120 min) | f5_tts (local) | **cloudrun_f5** | Speed: 1.5h → 30 min wall |
| historyrecapped | Shorts (60s) | kokoro `am_michael` | unchanged | Already fast + free + good |
| sportstoriesanimated | doc long-form (20-40 min) | f5_tts (local) | **cloudrun_f5** | Speed |
| sportstoriesanimated | Shorts (60s) | f5_tts (local) | **cloudrun_f5** for hooks/closers, f5_tts for body | Hybrid keeps middle chunks fast on M2 Max while energy chunks lift on cloud |
| cosmosdecoded | long-form (25-40 min) | f5_tts (local) | **cloudrun_f5** | Speed |
| cosmosdecoded | Shorts | kokoro `am_michael` | unchanged | Fast + free + good |
| mystoriesanimated | AITA (Shorts) | chatterbox (local) | **cloudrun_chatterbox** (per user audition 2026-05-05) | Same emotion model, 6× faster |
| mystoriesanimated | non-AITA variants | f5_tts (local) | **cloudrun_f5** | Speed |
| hindutavaanimated | kathaa (long-form Hindi) | kokoro `hf_alpha` | **cloudrun_indicparler** (current best Hindi) | Indic Parler proven on Hindi; CosyVoice does NOT speak Hindi |
| airecap | Shorts | kokoro `am_michael` | unchanged | News-style, no payoff to switching |
| rhymetimejunction | sung Suno | n/a (sunoapi.org) | unchanged | Audio is sung, not TTS |

**Higgs niche (after audition):** Higgs is in the matrix as an
audition option, not a channel default. Use cases where Higgs may
beat Chatterbox/F5 (pending bench-listening): emotional sleep
narration, multilingual special projects. See
`data/_bench/decision-matrix-v2/20260505-205057/` for side-by-side
WAVs.

**Auto-fallback** is enabled by default. If the cloud service is
unreachable, `cloudrun_f5` falls back to `f5_tts` (local), and
`cloudrun_chatterbox` falls back to `chatterbox` (local).
`cloudrun_higgs`, `cloudrun_cosyvoice`, and `cloudrun_indicparler`
have no local equivalent — on cloud failure they raise so the caller
can re-route to `chatterbox` / `f5_tts` / `kokoro hf_alpha`.

---

## Per-service runbooks

For service-specific operations (build, deploy, debug, env-var
overrides) see:

- **F5:** this file (top section), plus `cloud/tts-f5/README.md`
- **Higgs Audio v2:** [`docs/cloudrun_higgs.md`](./cloudrun_higgs.md) — has full PierrunoYT-mirror post-mortem and regression tests
- **Chatterbox:** `cloud/tts-chatterbox/README.md`
- **CosyVoice:** `cloud/tts-cosyvoice/README.md` and
  [§"CosyVoice 2 standalone service"](#cosyvoice-2-standalone-service-ytfactory-tts-cosyvoice) below
- **Indic Parler:** `cloud/tts-indicparler/README.md`

---

## CosyVoice 2 standalone service (`ytfactory-tts-cosyvoice`)

CosyVoice 2 lives in its **own** Cloud Run service + image. Reason:
CosyVoice's dep tree pins `torch 2.3.1+cu121`, `openai-whisper` from
source, `deepspeed`, `lightning`, `gradio`, and the `tensorrt-cu12`
wheel family; F5 and Higgs run `torch 2.4.1+cu124`. After 5 build
iterations trying to coexist on a combined image, isolating CosyVoice
was the cleaner path.

The HTTP contract is **identical** to every other service —
`POST /synth` with the same body schema. Only the URL env var
differs.

**Hindi gotcha (proven 2026-05-05):** CosyVoice 2 0.5B does NOT
speak Hindi. Trained on CN/EN/JP/KR + some EU languages. Feeding
Hindi text → produces gibberish that Whisper detects as Korean.
Use Indic Parler for Hindi. CosyVoice is on the cloud only because
its English voice quality is competitive with F5 and may win some
auditions; not because it gives us anything for Hindi.

| | `ytfactory-tts` (F5) | `ytfactory-tts-cosyvoice` |
| --- | --- | --- |
| Image base | `nvidia/cuda:12.4.0-runtime-ubuntu22.04` | `nvidia/cuda:12.1.0-devel-ubuntu22.04` |
| Torch | `2.4.1+cu124` | `2.3.1+cu121` |
| Models | F5 only | CosyVoice 2 only |
| Source dir | `cloud/tts-f5/` | `cloud/tts-cosyvoice/` |
| Image tag | `ytfactory-tts/server:vN` | `ytfactory-tts/tts-cosyvoice:vN` |
| Max instances | 2 | 2 |
| Min instances | 0 | 0 |
| Concurrency | 1 | 1 |
| Memory / CPU | 24 Gi / 8 | 24 Gi / 8 |
| Auth | IAM (no-allow-unauthenticated) | IAM (no-allow-unauthenticated) |
| GCS bucket | `ytfactory-tts-io` | `ytfactory-tts-io` (shared) |
| Service account | `tts-runner@…` | `tts-runner@…` (shared) |

### Env vars (laptop side)

```bash
# laptop env routes per provider
export CLOUDRUN_TTS_F5_URL="https://ytfactory-tts-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_HIGGS_URL="https://ytfactory-tts-higgs-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_CHATTERBOX_URL="https://ytfactory-tts-chatterbox-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_COSYVOICE_URL="https://ytfactory-tts-cosyvoice-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_INDICPARLER_URL="https://ytfactory-tts-indicparler-767262167641.asia-southeast1.run.app"

# Legacy alias — points at F5 today; safe to leave set
export CLOUDRUN_TTS_URL="$CLOUDRUN_TTS_F5_URL"
```

### Build + deploy (CosyVoice example)

```bash
cd cloud/tts-cosyvoice
./deploy.sh v3
```

`deploy.sh` runs `gcloud builds submit` and then `gcloud run deploy`
with the GPU/concurrency/instance flags above. End-to-end
~25-40 min depending on whether the image cache is warm.

### Smoke test

```bash
URL=$(gcloud run services describe ytfactory-tts-cosyvoice \
        --region=asia-southeast1 --project=ytfactory-prod \
        --format='value(status.url)')

# IAM-authed call via cloudrun.py's helper (handles user vs SA token)
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from pipeline.tts.cloudrun import _get_id_token
import urllib.request
url = '$URL/readyz'
tok = _get_id_token(url)
req = urllib.request.Request(url, headers={'Authorization': f'Bearer {tok}'})
print(urllib.request.urlopen(req, timeout=600).read().decode())
"
```

For a real Hindi /synth call see
[`cloud/tts-cosyvoice/README.md`](../cloud/tts-cosyvoice/README.md)
(uses `pipeline/voice_refs/sarah.wav` + a Bhagavad Gita verse).

### Rollback

```bash
gcloud run services delete ytfactory-tts-cosyvoice \
  --region=asia-southeast1 --project=ytfactory-prod
unset CLOUDRUN_TTS_COSYVOICE_URL
```

The other services are unaffected. Channels that were flipped to
`cloudrun_cosyvoice` should be reverted to their previous provider.

---

## Day-to-day operations

### Set the per-service URLs (one-time per shell)

Add to `.env` (or `~/.zshrc`):

```bash
export CLOUDRUN_TTS_F5_URL="https://ytfactory-tts-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_HIGGS_URL="https://ytfactory-tts-higgs-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_CHATTERBOX_URL="https://ytfactory-tts-chatterbox-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_COSYVOICE_URL="https://ytfactory-tts-cosyvoice-767262167641.asia-southeast1.run.app"
export CLOUDRUN_TTS_INDICPARLER_URL="https://ytfactory-tts-indicparler-767262167641.asia-southeast1.run.app"
```

If a per-service URL is unset, `pipeline/tts/cloudrun.py` raises a
clear error pointing at the missing env name. Fix by setting the
right one or by switching the channel back to its local provider.

### Switch a channel to cloud

Edit the channel YAML:

```yaml
# was:
tts_provider: f5_tts
# now:
tts_provider: cloudrun_f5    # or cloudrun_higgs / cloudrun_chatterbox / etc
```

Done. Local providers remain in code; rolling back is a 1-line revert.

### Roll back the entire migration

```bash
unset CLOUDRUN_TTS_F5_URL CLOUDRUN_TTS_HIGGS_URL CLOUDRUN_TTS_CHATTERBOX_URL \
      CLOUDRUN_TTS_COSYVOICE_URL CLOUDRUN_TTS_INDICPARLER_URL CLOUDRUN_TTS_URL
# or in .env, comment out the lines and re-source.
```

`pipeline/tts/cloudrun.py` raises a clear error with the env name. The
auto-fallback path then routes to the channel's previous local
provider for `cloudrun_f5` / `cloudrun_chatterbox`. **No code change
required.**

### Tail Cloud Run logs

```bash
gcloud run services logs tail ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1
```

(Replace `ytfactory-tts-higgs` with whichever service you want to
tail.)

### Cost dashboard

```
https://console.cloud.google.com/billing/012E39-E4ECEB-7F119F/reports?project=ytfactory-prod
```

Filter to **Service: Cloud Run** to see GPU vs CPU vs egress
breakdown.

### Force-warm before a known-burst render

```bash
.venv/bin/python -c "
import sys, time, os
sys.path.insert(0, '.')
from pipeline.tts.cloudrun import _get_id_token
import urllib.request
for var, name in [('CLOUDRUN_TTS_F5_URL','f5'), ('CLOUDRUN_TTS_HIGGS_URL','higgs')]:
    url = os.environ[var] + '/readyz'
    tok = _get_id_token(url)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {tok}'})
    t0 = time.time()
    body = urllib.request.urlopen(req, timeout=600).read().decode()
    print(name, '%.1fs' % (time.time()-t0), body)
"
```

### Check GPU quota & usage

```bash
gcloud beta quotas info describe \
  NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion \
  --service=run.googleapis.com --project=ytfactory-prod
```

---

## Rollout history

| Date | Event |
|---|---|
| 2026-05-05 | Quota auto-approved (5/5 in asia-southeast1) |
| 2026-05-05 | F5-TTS standalone service deployed. Smoke confirmed RTF=0.24, ~10× speedup vs M2 Max |
| 2026-05-05 | Laptop-side `pipeline/tts/cloudrun.py` shipped + 5 unit tests + 1 live smoke |
| 2026-05-05 | Mega-image attempt (F5+Higgs+Chatterbox+CosyVoice in one image) abandoned after 5 dep-conflict build failures |
| 2026-05-05 | Pivot to **5 separate services**, one per model: `tts-{f5,higgs,chatterbox,cosyvoice,indicparler}` |
| 2026-05-05 | Chatterbox + CosyVoice + Indic Parler deployed cleanly (Indic Parler with `HF_TOKEN` for ToS gate) |
| 2026-05-05 | CosyVoice **proven NOT to speak Hindi** (Whisper detects Korean from Hindi-text rendering) |
| 2026-05-05 | Higgs v3-v8 build failures: cycled through `audiotools`, `dac`, `tensorboard`, `markdown_it_py`, `protobuf` cherry-picks |
| 2026-05-05 | Created `docs/cloud_service_dep_playbook.md` — the 6-step rule that should've prevented the cycle |
| 2026-05-05 | Higgs v9 IMPORT_SMOKE_OK passed (locally validated dep set), but Cloud Build manifest-commit hung on 14 GB image |
| 2026-05-05 | Higgs v10 (no weights baked) deployed; /readyz crashed with `Padding_idx must be within num_embeddings` |
| 2026-05-05 | Higgs v11: `padding_patch.py` monkey-patch added; built; deployed; /readyz then crashed with weight shape mismatches (hidden_size 4096 vs 3072) |
| 2026-05-06 | **Discovered root cause: `bosonai/*` checkpoint refactored its config schema without updating their GitHub code.** `model_type` renamed, `text_config` stripped to `null`. |
| 2026-05-06 | Higgs **v12: switched to PierrunoYT/* community mirror** which preserves OLD-schema config. Deployed clean. /readyz 65 s cold-start. |
| 2026-05-06 | All 6 channel cells rendered in `data/_bench/decision-matrix-v2/20260505-205057/` with Higgs |

---

## Known operational notes

* **`/healthz` returns 404 on some services.** Cloud Run's frontend
  reserves that path on certain configurations; use `/readyz` for
  both liveness + warm-up. The synth path `/synth` works fine
  everywhere.
* **Cold-start varies by service:**
  * F5: ~10 s (small image, fast HF download)
  * Higgs: ~65 s (11.5 GB weights download + load to GPU)
  * Chatterbox / CosyVoice / Indic Parler: 15-30 s
* **Concurrency hard-pinned at 1.** Mirrors the
  `gpu_one_render_at_a_time` rule from
  `docs/long_form_model_inventory.md`. Two synth calls on one L4
  would corrupt CUDA memory the same way two MLX jobs corrupt
  unified memory on M2 Max.
* **Max instances varies:** 5 total quota across services. Today set
  to 2 each to leave headroom for parallel renders.
* **Cloud Run frontend kills long /readyz calls.** Even though the
  container `--timeout=3600`, the frontend has a shorter request
  timeout. If /readyz takes >8 min, the client sees a timeout but
  the model load may still complete server-side. Workaround: poll
  /readyz with backoff after the first request returns; the second
  call will hit a warm engine.
* **HF_TOKEN is required for gated models.** Indic Parler is ToS-gated.
  Higgs's PierrunoYT mirror is NOT gated but the variable is set on
  every service for consistency. Read from `~/.cache/huggingface/token`.

---

## Files

| Path | What |
|---|---|
| `cloud/tts-f5/` | F5-TTS service (Dockerfile, server, deploy.sh) |
| `cloud/tts-higgs/` | Higgs Audio v2 service. See [`docs/cloudrun_higgs.md`](./cloudrun_higgs.md) for full post-mortem |
| `cloud/tts-chatterbox/` | Chatterbox service |
| `cloud/tts-cosyvoice/` | CosyVoice 2 service |
| `cloud/tts-indicparler/` | Indic Parler service |
| `pipeline/tts/cloudrun.py` | Laptop-side HTTP client + auto-fallback + per-service URL routing |
| `pipeline/audio.py` | Dispatcher router (lists `cloudrun_*` providers) |
| `tests/test_cloudrun_tts_provider.py` | 5 unit + 1 live smoke |
| `scripts/bench_decision_matrix_v2.py` | Audition harness — same script across all providers |
| `scripts/bench_higgs_fillin.py` | Render only Higgs cells into existing bench dir |
| `docs/cloudrun_tts.md` | This file |
| `docs/cloudrun_higgs.md` | Higgs-specific runbook (PierrunoYT mirror, padding_idx history, regression tests) |
| `docs/cloud_service_dep_playbook.md` | Pre-build dep-resolution playbook (mandatory reading) |
| `docs/tts_stack.md` | Per-channel routing (laptop + cloud) |
| `docs/long_form_model_inventory.md` | Local M2 Max model rules (still authoritative for local-only providers) |

---

## Why we moved long-form to cloud

| | Local M2 Max (F5-MLX) | Cloud Run L4 (F5-TTS) |
|---|---:|---:|
| RTF (real-time factor) | ~1.6-2.3 | **0.21-0.24** |
| 75-min sleep video render | ~1.5-9 hr (laptop locked) | **~30-45 min, async** |
| Multiple channels in parallel | impossible (1 GPU) | up to 5 (one L4 per channel) |
| Cost at full throttle | $0 + electricity + WindowServer crash risk | **~$6/mo against credits** |
| Hindi quality | Kokoro `hf_alpha` (flat) | Same locally; CosyVoice v4 unlocks pro Hindi |

`/docs/long_form_model_inventory.md` documents the local rules
(`gpu_one_render_at_a_time`, F5 singleton, MLX heap hygiene, etc).
The cloud path makes those rules irrelevant for the channels that
opt in — every render gets its own dedicated L4 instance.

---

## Architecture

```
                 ┌─────────────────────────────────────────┐
                 │  M2 Max (renderer / orchestrator)       │
                 │                                         │
                 │  pipeline/audio.py::synthesize()        │
                 │     ├─ provider="f5_tts"      → local   │
                 │     ├─ provider="kokoro"      → local   │
                 │     ├─ provider="chatterbox"  → local   │
                 │     ├─ provider="cloudrun_f5" ───┐      │
                 │     ├─ provider="cloudrun_higgs" ┤      │
                 │     └─ provider="cloudrun_chatterbox" ┐ │
                 └──────────────────────────────────┼┼┼───┘
                                                    │││ HTTPS POST /synth
                                                    │││ (gcloud ID token)
                                                    ▼▼▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Cloud Run service `ytfactory-tts`     (asia-southeast1)        │
   │  ────────────────────────────────────────────────────────────   │
   │  URL: https://ytfactory-tts-767262167641.asia-southeast1.run.app│
   │  Image: asia-southeast1-docker.pkg.dev/ytfactory-prod/          │
   │         ytfactory-tts/server:vN                                 │
   │  ────────────────────────────────────────────────────────────   │
   │  GPU: 1 × NVIDIA L4 24GB (no zonal redundancy)                  │
   │  CPU: 4 vCPU   Memory: 16 GiB                                   │
   │  Concurrency: 1   Min instances: 0   Max instances: 5           │
   │  SA: tts-runner@ytfactory-prod.iam.gserviceaccount.com          │
   │  Auth: --no-allow-unauthenticated (IAM)                         │
   │  ────────────────────────────────────────────────────────────   │
   │  Models (in v3, pending build):                                 │
   │   * F5-TTS         (singleton, ~2 GB VRAM, hot)                 │
   │   * Higgs Audio v2 (lazy, ~12 GB VRAM)                          │
   │   * Chatterbox     (lazy, ~3 GB VRAM)                           │
   │  Models (deferred to v4): CosyVoice 2 (Hindi-best)              │
   │                                                                 │
   │  WAV output: ≤5 MB inline (base64), >5 MB → gs://ytfactory-tts-io│
   └─────────────────────────────────────────────────────────────────┘
```

---

## Live infrastructure

| Resource | ID | Purpose |
|---|---|---|
| GCP project | `ytfactory-prod` | Already used for Firestore + Cloud Scheduler + GCS |
| Billing account | `012E39-E4ECEB-7F119F` (INR) | $150 credits — covers ~10 months at full throttle |
| Region | `asia-southeast1` (Singapore) | L4 open-access; ~50ms from India |
| Quota: nvidia_l4_gpu_allocation_no_zonal_redundancy | **5** (auto-approved) | Allows 5 channels to render in parallel |
| Artifact Registry | `asia-southeast1-docker.pkg.dev/ytfactory-prod/ytfactory-tts` | Cloud Run image repo |
| Cloud Run service | `ytfactory-tts` | The TTS endpoint |
| Cloud Run service | `ytfactory-tts-cosyvoice` | CosyVoice 2 standalone (separate image; see section below) |
| GCS bucket | `gs://ytfactory-tts-io` | >5 MB WAV handoff; 7-day delete lifecycle |
| Service account | `tts-runner@ytfactory-prod.iam.gserviceaccount.com` | Cloud Run runtime SA; bucket Object Admin |

---

## Channel routing (cloud-first per-channel matrix)

This table reflects where the migration **WILL** land once v3 deploys
and the audition picks winners. Today's reality is in
`docs/tts_stack.md` — the two should converge.

| Channel | Format | TODAY | TARGET (post-v3) | Why |
|---|---|---|---|---|
| historyrecapped | sleep long-form (60-120 min) | f5_tts (local) | **cloudrun_f5** | Speed: 1.5h → 30 min wall-clock |
| historyrecapped | Shorts (60s) | kokoro `am_michael` | unchanged | Already fast + free + good |
| sportstoriesanimated | doc long-form (20-40 min) | f5_tts (local) | **cloudrun_f5** | Speed |
| sportstoriesanimated | Shorts (60s) | f5_tts (local) | **cloudrun_f5** for hooks/closers, f5_tts for body | Hybrid keeps middle chunks fast on M2 Max while energy chunks lift on cloud |
| cosmosdecoded | long-form (25-40 min) | f5_tts (local) | **cloudrun_f5** | Speed |
| cosmosdecoded | Shorts | kokoro `am_michael` | unchanged | Fast + free + good |
| mystoriesanimated | AITA (Shorts) | chatterbox (local) | **cloudrun_higgs** if Higgs > Chatterbox in audition; else `cloudrun_chatterbox` | Better emotion or 6× speedup |
| mystoriesanimated | non-AITA variants | f5_tts (local) | **cloudrun_f5** | Speed |
| hindutavaanimated | kathaa (long-form Hindi) | kokoro `hf_alpha` | **cloudrun_higgs** (interim); CosyVoice 2 in v4 | Higgs claims multilingual; CosyVoice is best-in-class for Hindi |
| airecap | Shorts | kokoro `am_michael` | unchanged | News-style, no payoff to switching |
| rhymetimejunction | sung Suno | n/a (sunoapi.org) | unchanged | Audio is sung, not TTS |

**Auto-fallback** is enabled by default. If the cloud service is
unreachable, `cloudrun_f5` falls back to `f5_tts` (local), and
`cloudrun_chatterbox` falls back to `chatterbox` (local). `cloudrun_higgs`
has no local equivalent — on cloud failure it raises so the caller can
re-route to `chatterbox` or `f5_tts`.

---

## CosyVoice 2 standalone service (`ytfactory-tts-cosyvoice`)

CosyVoice 2 lives in its **own** Cloud Run service + image, separate
from the main `ytfactory-tts`. Reason: CosyVoice's dep tree pins
`torch 2.3.1+cu121`, `openai-whisper` from source, `deepspeed`,
`lightning`, `gradio`, and the `tensorrt-cu12` wheel family; the main
service runs `torch 2.4.1+cu124` for F5/Higgs/Chatterbox. After 5
build iterations trying to coexist on a combined image, isolating
CosyVoice was the cleaner path.

The HTTP contract is **identical** to the main service —
`POST /synth` with the same body schema. Only the URL env var
differs.

| | `ytfactory-tts` (main) | `ytfactory-tts-cosyvoice` |
| --- | --- | --- |
| Image base | `nvidia/cuda:12.4.0-runtime-ubuntu22.04` | `nvidia/cuda:12.1.0-devel-ubuntu22.04` |
| Torch | `2.4.1+cu124` | `2.3.1+cu121` |
| Models | F5, Higgs, Chatterbox | CosyVoice 2 only |
| Source dir | `cloud/tts/` | `cloud/tts-cosyvoice/` |
| Image tag | `ytfactory-tts/server:vN` | `ytfactory-tts/cosyvoice:vN` |
| Max instances | 5 (matches quota) | 2 (leaves 3 for main) |
| Min instances | 0 | 0 |
| Concurrency | 1 | 1 |
| Memory / CPU | 16 Gi / 4 | 16 Gi / 4 |
| Auth | IAM (no-allow-unauthenticated) | IAM (no-allow-unauthenticated) |
| GCS bucket | `ytfactory-tts-io` | `ytfactory-tts-io` (shared) |
| Service account | `tts-runner@…` | `tts-runner@…` (shared) |

### Env vars (laptop side)

```bash
# main service (F5 / Higgs / Chatterbox)
export CLOUDRUN_TTS_URL="https://ytfactory-tts-XXXXXXX.asia-southeast1.run.app"

# cosyvoice service — set this for the cloudrun_cosyvoice provider
export CLOUDRUN_TTS_COSYVOICE_URL="https://ytfactory-tts-cosyvoice-XXXXXXX.asia-southeast1.run.app"
```

If `CLOUDRUN_TTS_COSYVOICE_URL` is unset, `cloudrun_cosyvoice` falls
through to `CLOUDRUN_TTS_URL`. The main service today returns a
clear 400 for `model=cosyvoice` (handler advertises only F5, Higgs,
Chatterbox), so a missing env var produces an actionable error
rather than silent wrong-model output.

### Build + deploy

```bash
cd cloud/tts-cosyvoice
./deploy.sh v1
```

`deploy.sh` runs `gcloud builds submit` (default global pool, no
custom region/machine-type — main session's hard-won lesson) and
then `gcloud run deploy` with the GPU/concurrency/instance flags
above. End-to-end ~25-40 min depending on whether the image cache
is warm.

### Smoke test

```bash
URL=$(gcloud run services describe ytfactory-tts-cosyvoice \
        --region=asia-southeast1 --project=ytfactory-prod \
        --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token)

curl -H "Authorization: Bearer $TOKEN" "$URL/healthz"
curl -H "Authorization: Bearer $TOKEN" "$URL/readyz"   # ~5-8 s warm
```

For a real Hindi /synth call see
[`cloud/tts-cosyvoice/README.md`](../cloud/tts-cosyvoice/README.md)
(uses `pipeline/voice_refs/sarah.wav` + a Bhagavad Gita verse).

### Rollback

```bash
gcloud run services delete ytfactory-tts-cosyvoice \
  --region=asia-southeast1 --project=ytfactory-prod
unset CLOUDRUN_TTS_COSYVOICE_URL
```

The main `ytfactory-tts` service is unaffected. Channels that were
flipped to `cloudrun_cosyvoice` should be reverted to
`kokoro hf_alpha` (the previous Hindi default).

---

## Day-to-day operations

### Set the service URL (one-time per shell)

```bash
export CLOUDRUN_TTS_URL="https://ytfactory-tts-767262167641.asia-southeast1.run.app"
```

Add to `.env` (or `~/.zshrc`) so all renderer entrypoints inherit it.

### Switch a channel to cloud

Edit the channel YAML:

```yaml
# was:
tts_provider: f5_tts
# now:
tts_provider: cloudrun_f5
```

Done. Local providers remain in code; rolling back is a 1-line revert.

### Roll back the entire migration

```bash
unset CLOUDRUN_TTS_URL
# or in .env, comment out the line and re-source.
```

`pipeline/tts/cloudrun.py` raises a clear error with the env name. The
auto-fallback path then routes to the channel's previous local
provider. **No code change required.**

### Deploy a new image

```bash
cd cloud/tts
./deploy.sh v4         # explicit tag
./deploy.sh            # tag = timestamp
```

The deploy script builds via Cloud Build (default global pool, free
first 120 min/day) and deploys via `gcloud run deploy` with the right
GPU + concurrency flags. End-to-end ~25-50 min depending on image
size.

### Tail Cloud Run logs

```bash
gcloud run services logs tail ytfactory-tts \
  --project=ytfactory-prod --region=asia-southeast1
```

### Cost dashboard

```
https://console.cloud.google.com/billing/012E39-E4ECEB-7F119F/reports?project=ytfactory-prod
```

Filter to **Service: Cloud Run** to see GPU vs CPU vs egress
breakdown.

### Force-warm before a known-burst render

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" \
  "${CLOUDRUN_TTS_URL}/readyz"
# Returns {"status":"ready","warm_s":5.6} after F5 loads to GPU.
```

### Check GPU quota & usage

```bash
gcloud beta quotas preferences list --project=ytfactory-prod
gcloud beta quotas info describe \
  NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion \
  --service=run.googleapis.com --project=ytfactory-prod
```

---

## Rollout history

| Date | Event |
|---|---|
| 2026-05-05 | Quota auto-approved (5/5 in asia-southeast1) |
| 2026-05-05 | v2 deployed (F5-TTS only). End-to-end smoke confirmed RTF=0.24, ~10× speedup vs M2 Max |
| 2026-05-05 | Laptop-side `pipeline/tts/cloudrun.py` shipped + 5 unit tests + 1 live smoke |
| 2026-05-05 | v3 baked Higgs + Chatterbox + (deferred) CosyVoice; CosyVoice deferred to v4 due to deepspeed/torch dep tree fights |
| TBD | v3-6 deploys → audition F5 vs Higgs vs Chatterbox → channel flip decisions |

---

## Known operational notes

* **`/healthz` returns 404.** Cloud Run's frontend reserves that path;
  use `/readyz` for both liveness + warm-up. The synth path `/synth`
  works fine. Will rename in a future image (cosmetic).
* **Cold-start ~40 s.** Image pull (~30 s for v2 6 GB image; ~60 s
  for v3 14 GB) + uvicorn boot (~3 s) + F5 weight upload to GPU
  (~5-8 s). After first call, warm calls are ~2-5 s for a 15 s
  chunk.
* **Concurrency hard-pinned at 1.** Mirrors the
  `gpu_one_render_at_a_time` rule from
  `docs/long_form_model_inventory.md`. Two synth calls on one L4
  would corrupt CUDA memory the same way two MLX jobs corrupt
  unified memory on M2 Max.
* **Max instances capped at 5** (matches our quota). 5 channels can
  render simultaneously, each on its own L4.
* **First v3 model swap.** When the v3 image deploys and the laptop
  flips a config from `cloudrun_f5` to `cloudrun_higgs` for the
  first time, expect a one-time ~10-15 s "load Higgs onto GPU" delay
  on the first request. Subsequent requests for that container are
  warm.

---

## Files

| Path | What |
|---|---|
| `cloud/tts/Dockerfile` | Container build recipe |
| `cloud/tts/server.py` | FastAPI: `/healthz`, `/readyz`, `/synth` |
| `cloud/tts/handler.py` | Model router with LRU eviction |
| `cloud/tts/models/f5.py` | F5-TTS PyTorch synth (matches local MLX params) |
| `cloud/tts/models/higgs.py` | Higgs Audio v2 synth (Phase 3) |
| `cloud/tts/models/chatterbox.py` | Chatterbox synth (Phase 3) |
| `cloud/tts/models/cosyvoice.py` | CosyVoice 2 synth — present but disabled in main service v3-6; live in standalone service |
| `cloud/tts-cosyvoice/` | Standalone CosyVoice 2 service (Dockerfile, server, handler, deploy.sh) |
| `cloud/tts/deploy.sh` | Build + push + deploy script (main service) |
| `cloud/tts/README.md` | Container README |
| `pipeline/tts/cloudrun.py` | Laptop-side HTTP client + auto-fallback |
| `pipeline/audio.py` | Dispatcher router (lists `cloudrun_*` providers) |
| `tests/test_cloudrun_tts_provider.py` | 5 unit + 1 live smoke |
| `scripts/bench_tts_all_providers.py` | Audition harness — same script across all providers |
| `docs/cloudrun_tts.md` | This file |
| `docs/tts_stack.md` | Per-channel routing (laptop + cloud) |
| `docs/long_form_model_inventory.md` | Local M2 Max model rules (still authoritative for local-only providers) |
