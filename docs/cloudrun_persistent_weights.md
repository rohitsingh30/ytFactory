# Cloud Run TTS — persistent weights via GCS Fuse

> **Status (2026-05-06):** ENABLED for all 6 TTS services. Bucket is
> `gs://ytfactory-model-weights` (asia-southeast1, versioning ON).
> Mount is **read-write** — see "Why writable" below for the lock-dir
> reason discovered during F5 canary.
>
> Captures the user direction "we don't need to download the weight
> everytime we fire some query."

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
│ gs://ytfactory-model-weights/                                 │
│   ├── hub/                                ── HF cache layout  │
│   │   ├── models--PierrunoYT--higgs-audio-v2-...      (12 GB) │
│   │   ├── models--PierrunoYT--higgs-audio-v2-tokenizer (17 MB)│
│   │   ├── models--ai4bharat--IndicF5/                 (1.4 GB)│
│   │   ├── models--ai4bharat--indic-parler-tts/        (3.7 GB)│
│   │   ├── models--SWivid--F5-TTS/                     (1.5 GB)│
│   │   ├── models--ResembleAI--chatterbox/             (3.0 GB)│
│   │   └── models--FunAudioLLM--CosyVoice2-0.5B/       (3.0 GB)│
│   ├── flat/                                ── flat layout     │
│   │   ├── Tongyi-MAI/Z-Image-Turbo/                  (~13 GB) │
│   │   └── black-forest-labs/FLUX.2-klein-4B/          (~9 GB) │
│   └── (~46 GB total)                                          │
└───────────────────────┬───────────────────────────────────────┘
                        │ Cloud Storage FUSE (gen2)
                        │ read-write (HF lock-dir requires it)
                        ▼
       ┌────────────────────────────────────────┐
       │ Every TTS / image container at /models/hf │
       │   HF_HOME=/models/hf  ── ENV in Dockerfile│
       │                                        │
       │   TTS:    huggingface_hub.snapshot_download()
       │           transparently reads from /models/hf/hub/
       │           instead of HTTPS GET to huggingface.co
       │                                        │
       │   IMAGE:  diffusers.from_pretrained(   │
       │             "/models/hf/flat/<org>/<name>",
       │             local_files_only=True)     │
       └────────────────────────────────────────┘
```

GCS Fuse caches reads in container memory, so repeated reads of the
same model file (e.g. multiple containers spawning concurrently)
don't re-pay GCS egress. Egress between Cloud Run and same-region GCS
is **free** (asia-southeast1 → asia-southeast1).

### Two layouts under one bucket — why?

| Layout | Path | Producer | Consumer | Why |
|---|---|---|---|---|
| **`hub/`** (HF cache) | `gs://<bucket>/hub/models--<org>--<name>/` with `blobs/` + `snapshots/<sha>/` | `cloud/weights-staging/stage.py` `_stage_one()` (snapshot_download into cache_dir) | TTS services via `snapshot_download(repo_id)` — re-validates against `blobs/<hash>` so `local_files_only=False` works even if `snapshots/<sha>/` is stale | TTS-era choice; consumers tolerate broken snapshot symlinks because they hit HF Hub for re-validation if the cache layout is incomplete. |
| **`flat/`** (one real file per repo entry, no symlinks) | `gs://<bucket>/flat/<org>/<name>/` | `cloud/weights-staging/stage.py` `_stage_one_flat()` (snapshot_download into local_dir) | Image services via `from_pretrained("/models/hf/flat/<org>/<name>", local_files_only=True)` | Diffusers `from_pretrained(local_files_only=True)` requires every model file present at the canonical relpath. The HF cache layout uses symlinks `snapshots/<sha>/file → ../../blobs/<hash>` that GCS doesn't natively preserve → on the bucket those become 76-byte text files containing the symlink target string, which `from_pretrained` cannot interpret. The flat layout sidesteps this entirely. (Class-of-bug discovered 2026-05-07; see `cloud/weights-staging/validate_layout.py`.) |

A repo opts into the flat layout by being added to
`FLAT_LAYOUT_REPOS` in `stage.py`. Anything not in that set goes
through the cache-layout path. The two layouts coexist in the same
bucket, mounted at the same `/models/hf` mount point.

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

**~~`readonly=true` is critical~~** — original plan was read-only. In
practice, `huggingface_hub.hf_hub_download()` always tries to create
`/models/hf/hub/.locks/<repo>/` for cross-process download safety
(line 1200 of `file_download.py`), even when the file is already
cached. Read-only mount → `OSError: [Errno 30] Read-only file system`
→ /readyz returns 500. So we run **writable** instead, with **bucket
versioning enabled** (`gsutil versioning set on`) so any accidental
overwrite is recoverable. Discovered during F5 canary 2026-05-06.

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

For TTS / cache-layout repos (legacy):

1. Pull the new version locally:
   ```bash
   HF_HUB_DISABLE_XET=1 .venv/bin/python -c "
   from huggingface_hub import snapshot_download
   snapshot_download('ai4bharat/IndicF5', revision='<new-sha>')
   "
   ```
2. Sync up:
   ```bash
   gsutil -m rsync -r ~/.cache/huggingface/hub/ gs://ytfactory-model-weights/hub/
   ```
3. The next cold-start picks up the new files automatically. No
   redeploy needed.

For image / flat-layout repos (preferred):

```bash
# The Cloud Run staging Job already has HF_HUB_DISABLE_XET=1 baked in
# (see cloud/weights-staging/deploy.sh). One execution per repo so
# they run concurrently:
gcloud run jobs execute ytfactory-weights-staging \
  --project=ytfactory-prod --region=asia-southeast1 \
  --args="black-forest-labs/FLUX.2-klein-4B" --async
gcloud run jobs execute ytfactory-weights-staging \
  --project=ytfactory-prod --region=asia-southeast1 \
  --args="Tongyi-MAI/Z-Image-Turbo" --async
```

To **delete** an old model from the bucket (saves storage):
```bash
gsutil -m rm -r gs://ytfactory-model-weights/flat/<org>/<old-name>
gsutil -m rm -r gs://ytfactory-model-weights/hub/models--<old-org>--<old-name>
```

### Why `HF_HUB_DISABLE_XET=1` is mandatory in asia-southeast1

`huggingface_hub>=1.0` defaults to the new **Xet protocol**, which
serves large-file blobs from `cas-bridge.xethub.hf.co/xet-bridge-us/`
(direct us-east-1 S3, no Asia POP). From asia-southeast1 that's
~40-50 Mbps cross-Pacific. Without the flag, a 13 GB Z-Image-Turbo
download took 50+ min and silently approached the 2 h task timeout.
With the flag (regular HF/CloudFront path with Asia POPs),
**FLUX.2-klein-4B (23.74 GB) staged in 13.5 min total** (download
394 s + upload 413 s), and the same flag applies to the laptop
(BLR→HF: 45 → 250 Mbps, ~5×). See
`memory/feedback_hf_xet_disable_for_asia_southeast1.md` for the full
post-mortem.

The flag is harmless on non-Xet repos (regular CDN was already the
fast path for the existing 6 TTS services).

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

## Implementation checklist (2026-05-06 rollout)

- [x] Create `gs://ytfactory-model-weights` bucket in asia-southeast1
- [x] Grant `tts-runner@` SA `objectAdmin` (writable mount needed for `.locks/`)
- [x] Pre-populate bucket via Cloud Run Job `ytfactory-weights-staging`
      (`cloud/weights-staging/`) — staged 7 of 8 repos (38.1 GiB total).
      Qwen-Image deferred to image phase due to 32 GiB Job memory limit
      vs 20+ GiB tmpfs footprint of HF blob expansion.
- [x] Stage `charactr/vocos-mel-24khz` (F5 vocoder dep, missed initial pass)
- [x] Patch all 6 TTS `deploy.sh` files with `--add-volume*` flags
      (read-write, no `readonly=true`)
- [x] F5 canary: revision `ytfactory-tts-00005-cvz`, /readyz cold ~50-68s warm 0.3s
- [x] Roll out remaining 5 services (`gcloud run services update --add-volume*`,
      no rebuild needed — every Dockerfile already declares `HF_HOME=/models/hf`)
- [x] Real laptop-client smoke test via `pipeline/tts/cloudrun.py`:
      F5 / Higgs / CosyVoice / IndicParler / IndicF5 all returned valid WAVs
- [x] Chatterbox: separate optimization track (see "Chatterbox cold-start
      deep dive" below) — landed `assign=True` monkey-patch in
      `cloud/tts-chatterbox/server.py`, cold-start 14 min → 4.4 min (3.2x)
- [ ] Image phase: bigger-memory staging Job for Qwen-Image (20 GB blob
      expansion needs 64+ GiB tmpfs or non-tmpfs disk), then enable GCS
      mount on `cloud/image-{hidream,qwen}/`
- [ ] Update `docs/cloudrun_tts.md` with new cold-start numbers
- [ ] (optional) **eager-load** patch for chatterbox to convert the new
      70s `T3 → cuda` Fuse-mmap-paging cost into ~5-10s eager RAM read.
      Insert `for k, v in state.items(): state[k] = v.contiguous().clone()`
      between `load_safetensors(...)` and `load_state_dict(...)` to force
      pages into RAM before assign+GPU. Not yet implemented; would push
      total cold-start ~4.4 min → ~3 min.

## Lessons learned 2026-05-06

1. **HF blob cache is bigger than the model size.** `Qwen/Qwen-Image`
   is "20 GB" but expands to 25+ GB on disk because HF keeps multiple
   tensor formats side-by-side. Plan storage for 1.5x the official
   model size.
2. **tmpfs in Cloud Run = container memory.** Don't rely on `/tmp`
   for large staging unless the Job memory budget covers it. For
   Qwen-Image we'll need 64+ GiB or a non-tmpfs disk.
3. **Read-only GCS Fuse mount breaks `huggingface_hub`.** Even on a
   pure cache hit, hf_hub_download tries to create `.locks/`. Always
   mount writable + enable bucket versioning for safety.
4. **GCS Fuse is slower than HF Hub for small files.** F5 (1.5 GB)
   went from ~10-20s HF download → ~50s GCS Fuse cold-load. Net loss
   for small models. The win is decisive only for 5+ GB models.
5. **F5-TTS has a hidden dep on `charactr/vocos-mel-24khz`.** Anything
   the production code imports lazily must be staged. Discover via
   first /readyz attempt; the trace names the missing repo.
6. **Cold-start TTS routing is unchanged by this work.** The bucket-mount
   shaved per-call HF download cost; it does not affect inference RTF.
   Per-channel TTS routing in `docs/tts_stack.md` remains canonical.

---

## Chatterbox cold-start deep dive (2026-05-06)

**TL;DR:** chatterbox's cold-start was 14 min, **not** because of weight
download (Fuse mount is fine — files arrive in <30s). 91% of the time
was being burned in two `nn.Module.load_state_dict()` calls on T3
(8 min) and S3Token2Wav (3 min). PyTorch 2.1+'s `assign=True` flag
swaps tensor pointers instead of copying — fixed via monkey-patch in
`cloud/tts-chatterbox/server.py`.

### Before / after (single L4 GPU container, asia-southeast1)

```
                                                     BEFORE      AFTER
chatterbox import                                       23s        23s
hf_hub HEAD round-trips (5 files)                       3s         3s
ve.safetensors (5 MB)                                   1s         1s
t3_cfg.safetensors read from Fuse (2 GB)               16s        16s
T3.load_state_dict()                                  484s       0.1s   ← assign=True
T3 → cuda                                             0.6s        70s   ← bottleneck migrated
s3gen.safetensors read from Fuse (1 GB)                34s        34s
S3Token2Wav.load_state_dict()                         194s       0.1s   ← assign=True
S3Token2Wav → cuda + perth + finalize                  ~5s        85s
─────────────────────────────────────────────────────────────────────
Total cold load                                       ~15 min    ~4.4 min
First /synth (subsequent)                               ~7s        ~7s   (unchanged)
```

### Why `assign=True` works and what changes

Standard `load_state_dict()` copies every tensor in the state dict
into the parameter buffer position-by-position. For a 2 GB transformer,
that's millions of small Python tensor.copy_() calls — single-threaded,
GIL-bound, ~4 MB/s effective throughput.

`assign=True` skips the copy: the parameter slot's `.data` pointer is
swapped to point at the loaded state-dict tensor directly. Effectively
free (~0.1s for a 2 GB module).

**Trade-off:** the loaded state-dict tensors live in mmap'd safetensors
space (or wherever they were created). When you later call `.to(cuda)`,
those bytes have to be paged in for the first time — over Fuse → through
Python heap → over PCIe to GPU. The 70s `T3 → cuda` is this paging cost.

### The monkey-patch (in `cloud/tts-chatterbox/server.py`)

```python
_ASSIGN_TARGET_CLASSES = {"T3", "S3Token2Wav"}
_orig_lsd = nn.Module.load_state_dict
def _patched_lsd(self, *a, **kw):
    cls = type(self).__name__
    if cls in _ASSIGN_TARGET_CLASSES and "assign" not in kw:
        kw["assign"] = True
    return _orig_lsd(self, *a, **kw)
nn.Module.load_state_dict = _patched_lsd
```

Whitelist by class name to avoid affecting any other model on the same
container that might rely on copy-semantics (e.g., dtype conversion,
strict-mode validation, weight tying).

### Future: eager-load to defuse the 70s `T3 → cuda`

If we want sub-3-minute cold starts, after `load_safetensors(file)`
returns the state dict, force each tensor into RAM with a `.clone()`:

```python
state = load_safetensors(ckpt_dir / "t3_cfg.safetensors")
state = {k: v.contiguous().clone() for k, v in state.items()}  # eager
t3.load_state_dict(state)  # assign=True via monkey-patch
t3.to("cuda")  # now PCIe-bound, ~1-2s for 2 GB
```

This requires forking chatterbox or another monkey-patch on
`safetensors.torch.load_file`. Estimated: 70s → 5-10s for T3 cuda
upload, total cold-start 4.4 min → ~3 min. Not yet implemented.

### Why we *didn't* fix the 23s of Python imports

`from chatterbox.tts import ChatterboxTTS` pulls in chatterbox + perth +
diffusers + transformers — all heavy. Could be reduced via lazy
imports inside `_model()` rather than top-level, but the savings are
single-digit seconds and the code change is invasive. Skipped.

### Verification

Cold start re-measured 2026-05-06:
- /readyz: HTTP 200 in 242s (was 900s+ timeout)
- /synth: HTTP 200 in 25.5s, 142 KB WAV, RTF 8.3 (text 50 chars → 3s audio)
- Server-side `from_pretrained` log: 219s (was ~700s)
