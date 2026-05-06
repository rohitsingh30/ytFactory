# Cloud Run TTS — persistent weights via GCS Fuse

> **Status (2026-05-06):** design + ops doc, not yet enabled. Captures
> the user direction "we don't need to download the weight everytime
> we fire some query" and how to implement it without breaking the
> existing 6-service deployment.

Today every TTS service (`tts-f5`, `tts-higgs`, `tts-chatterbox`,
`tts-cosyvoice`, `tts-indicparler`, `tts-indicf5`) **downloads its
model weights from HuggingFace Hub on first /readyz** of every cold
container. This costs:

- **Higgs:** ~5-8 min per cold-start (12 GB)
- **IndicF5:** ~30-60 s per cold-start (1.4 GB)
- **Indic Parler:** ~60-90 s per cold-start (3.75 GB)
- **CosyVoice:** ~60-90 s per cold-start (3 GB)
- **Chatterbox:** ~30-60 s per cold-start (3 GB)
- **F5:** ~10-20 s per cold-start (1.5 GB)

When `min-instances=0` (today), every period of inactivity → next
request pays the cold-start tax.

The fix: **mount a single GCS bucket containing all 6 services' weights
into every container at `/models/hf`** (= `HF_HOME`, which the
`huggingface_hub` library automatically reads from before it ever
contacts the network).

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│ gs://ytfactory-tts-weights/                                   │
│   ├── hub/                                                    │
│   │   ├── models--PierrunoYT--higgs-audio-v2-...      (12 GB) │
│   │   ├── models--PierrunoYT--higgs-audio-v2-tokenizer (17 MB)│
│   │   ├── models--ai4bharat--IndicF5/                 (1.4 GB)│
│   │   ├── models--ai4bharat--indic-parler-tts/        (3.7 GB)│
│   │   ├── models--SWivid--F5-TTS/                     (1.5 GB)│
│   │   ├── models--ResembleAI--chatterbox/             (3.0 GB)│
│   │   └── models--FunAudioLLM--CosyVoice2-0.5B/       (3.0 GB)│
│   └── (~24 GB total)                                          │
└───────────────────────┬───────────────────────────────────────┘
                        │ Cloud Storage FUSE (gen2)
                        │ read-only, automounted by Cloud Run
                        ▼
       ┌────────────────────────────────────────┐
       │ Every TTS container at /models/hf      │
       │   HF_HOME=/models/hf  ── ENV in Dockerfile│
       │   huggingface_hub.snapshot_download()  │
       │   transparently reads from /models/hf  │
       │   instead of HTTPS GET to huggingface.co│
       └────────────────────────────────────────┘
```

GCS Fuse caches reads in container memory, so repeated reads of the
same model file (e.g. multiple containers spawning concurrently)
don't re-pay GCS egress. Egress between Cloud Run and same-region GCS
is **free** (asia-southeast1 → asia-southeast1).

---

## One-time setup

### 1. Create the bucket

```bash
PROJECT=ytfactory-prod
REGION=asia-southeast1
BUCKET=ytfactory-tts-weights

gsutil mb -p $PROJECT -c STANDARD -l $REGION -b on gs://$BUCKET
```

`-b on` enables Uniform Bucket-Level Access (required for IAM-based
Cloud Run service-account read access).

### 2. Grant the Cloud Run runtime SA read access

```bash
RUNTIME_SA="tts-runner@${PROJECT}.iam.gserviceaccount.com"

gsutil iam ch \
  serviceAccount:${RUNTIME_SA}:objectViewer \
  gs://$BUCKET
```

### 3. Pre-populate the bucket (one-time, ~30 min from a fast laptop)

```bash
HF_TOKEN=$(cat ~/.cache/huggingface/token)
TMP=/tmp/tts-weights-stage
mkdir -p $TMP

# Download all 6 model snapshots to local hub-cache layout
.venv/bin/python -c "
import os
os.environ['HF_HOME'] = '$TMP'
from huggingface_hub import snapshot_download

for repo in [
    'PierrunoYT/higgs-audio-v2-generation-3B-base',
    'PierrunoYT/higgs-audio-v2-tokenizer',
    'ai4bharat/IndicF5',
    'ai4bharat/indic-parler-tts',
    'SWivid/F5-TTS',
    'ResembleAI/chatterbox',
    'FunAudioLLM/CosyVoice2-0.5B',
]:
    print(f'pulling {repo}...')
    snapshot_download(repo, token='$HF_TOKEN')
print('all done')
"

# Copy to GCS preserving the hub-cache layout
gsutil -m rsync -r $TMP/hub/ gs://$BUCKET/hub/
```

The `gsutil -m rsync` is parallel and resumable — safe to interrupt
and re-run.

### 4. Update each service deploy command

Add these two flags to every `gcloud run deploy ...` invocation
(F5 / Higgs / Chatterbox / CosyVoice / Indic Parler / IndicF5):

```bash
--add-volume=name=weights,type=cloud-storage,bucket=ytfactory-tts-weights,readonly=true \
--add-volume-mount=volume=weights,mount-path=/models/hf
```

Example for Higgs:

```bash
gcloud run deploy ytfactory-tts-higgs \
  --project=ytfactory-prod \
  --region=asia-southeast1 \
  --image=...tts-higgs:v12 \
  --gpu=1 --gpu-type=nvidia-l4 --no-gpu-zonal-redundancy \
  --no-cpu-throttling --memory=24Gi --cpu=8 \
  --concurrency=1 --max-instances=2 --min-instances=0 \
  --timeout=3600 --no-allow-unauthenticated \
  --execution-environment=gen2 \
  --set-env-vars="HF_TOKEN=$HF_TOKEN,GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO" \
  --add-volume=name=weights,type=cloud-storage,bucket=ytfactory-tts-weights,readonly=true \
  --add-volume-mount=volume=weights,mount-path=/models/hf
```

`--execution-environment=gen2` is required (gen1 doesn't support
GCS Fuse). All 6 services already deploy with gen2.

`readonly=true` is critical — prevents the container from accidentally
writing into the canonical weights bucket.

---

## Expected impact

| Service | Cold-start before | Cold-start after | Steady-state RTF |
|---|---:|---:|---:|
| F5 | ~10-20 s | **~5 s** | unchanged (~0.21) |
| Higgs | ~5-8 min | **~10-30 s** | unchanged (~1.07) |
| Chatterbox | ~30-60 s | **~10 s** | unchanged |
| CosyVoice | ~60-90 s | **~10-15 s** | unchanged |
| Indic Parler | ~60-90 s | **~10-15 s** | unchanged |
| IndicF5 | ~30-60 s | **~5-10 s** | unchanged |

The first warm-up after a deploy still pays the GCS Fuse "lazy mount"
delay (~few seconds while it discovers files), but no HuggingFace
HTTPS round-trip and no 12 GB download.

---

## Cost analysis

| Item | Cost / month |
|---|---:|
| GCS Standard storage in asia-southeast1, 24 GB | $0.024 × 24 = **~$0.58** |
| GCS egress to Cloud Run same-region | **$0** (free) |
| GCS Fuse Class A operations (LIST) on each cold-start | ~$0.005 / 1000 ops; negligible |

**Net new cost: ~$0.60/mo**, against:
- ~10× faster Higgs cold-start (5-8 min → 10-30 s)
- Eliminates HF Hub rate-limit risk (we currently do many parallel
  downloads when 5 services cold-start simultaneously)

---

## Rollback

If GCS Fuse causes any operational issue (rare, but possible if
Cloud Storage has an outage in the region), drop the volume from
the deploy:

```bash
gcloud run services update ytfactory-tts-higgs \
  --project=ytfactory-prod --region=asia-southeast1 \
  --remove-volume=weights --remove-volume-mount=/models/hf
```

The container then falls back to its existing behavior: `huggingface_hub`
sees an empty `/models/hf` and downloads from the network. No code
change needed.

---

## Refresh workflow (when a new model version ships)

1. Pull the new version locally:
   ```bash
   .venv/bin/python -c "
   from huggingface_hub import snapshot_download
   snapshot_download('ai4bharat/IndicF5', revision='<new-sha>')
   "
   ```
2. Sync up:
   ```bash
   gsutil -m rsync -r ~/.cache/huggingface/hub/ gs://ytfactory-tts-weights/hub/
   ```
3. The next cold-start picks up the new files automatically. No
   redeploy needed.

To **delete** an old model from the bucket (saves storage):
```bash
gsutil -m rm -r gs://ytfactory-tts-weights/hub/models--<old-org>--<old-name>
```

---

## When NOT to use this

- **Service is in active development and the model id is changing
  weekly.** The bucket-pre-populate step is wasted effort if you
  rebuild the image with a new model id every other day. Use runtime
  download until the model id stabilises, then enable the volume mount.
- **Different Cloud Run region than the bucket.** Cross-region GCS
  egress is $0.02-0.12 per GB; would dominate the savings. Keep
  bucket in the same region as the services.

---

## Implementation checklist

- [ ] Create `gs://ytfactory-tts-weights` bucket in asia-southeast1
- [ ] Grant `tts-runner@` SA Object Viewer role
- [ ] Pre-populate bucket with all 6 model snapshots
- [ ] Update `cloud/tts-{f5,higgs,chatterbox,cosyvoice,indicparler,indicf5}/deploy.sh`
      scripts to include the two `--add-volume*` flags
- [ ] Re-deploy each service one at a time, verify /readyz cold-start
      drops as expected
- [ ] Update `docs/cloudrun_tts.md` with the new cold-start numbers
- [ ] Document the refresh workflow in each service's README

Estimated effort: **~2 hours** for the one-time setup; ~1 hour each
time we add a new model service.
