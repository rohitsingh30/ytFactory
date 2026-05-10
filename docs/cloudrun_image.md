# Cloud Run image generation — operational runbook

> **Live status & cost trend:** see the `/app/cloud` admin tab in
> web-next (sidebar → Cloud) — populated by the daily snapshot in
> `docs/cloudrun_admin_panel.md`.

> **Status (2026-05-07):** `ytfactory-image-flux2-klein` is LIVE in
> `asia-southeast1` on NVIDIA L4 with **`--min-instances=0`** (scale
> to zero when idle, ~$0/mo idle). Cold-load is hidden via the
> pre-warmup script `cloud/warm_image_services.sh` (run before
> render windows) plus the in-pipeline `images.warmup()` background
> thread. 16 channel/variant YAMLs flipped from
> `image_provider: z_image_turbo` (local mflux) to
> `image_provider: cloudrun_flux2_klein`. Local mflux stays as
> automatic render-level circuit-breaker fallback.
>
> **Cost-conscious config:** see "Cost analysis" below. Earlier
> drafts of this doc said ~$80/mo for `min-instances=1` — that was
> wrong; actual L4 pricing is ~$700-815/mo per always-warm service.
> Production runs min=0 + pre-warm.
>
> Z-Image-Turbo cloud service exists but is BLOCKED by a cold-load
> reliability issue — see "Z-Image follow-up" below and
> `memory/feedback_zimage_cloudrun_coldload_stall.md`. Not on the
> production path; FLUX.2 klein is the sole cloud image provider.

The cloud-hosted image path lives next to the local providers in
`pipeline/`. Channel YAMLs flip a single line — `image_provider:
z_image_turbo` → `cloudrun_flux2_klein` — to move from M2 Max MLX
to L4. Local mflux stays in code as automatic fallback if the cloud
service is unhealthy.

---

## Why we moved image gen to cloud

| | Local M2 Max (Z-Image-Turbo via mflux) | Cloud Run L4 (FLUX.2 klein 4B via diffusers) |
|---|---:|---:|
| Per-image warm time (1024² 4-step) | ~12-15 s | **~3.86 s server / ~5 s e2e** |
| Stage 3 total for 7-image AITA Short | ~85-105 s | **33.4 s** (canary 2026-05-07) |
| Multiple Shorts in parallel | impossible (1 GPU) | up to 2 (max-instances=2 per L4) |
| Visual quality vs prior local output | baseline | **indistinguishable** (FLUX.2 klein > Z-Image-Turbo on most prompts; same channel aesthetic) |
| Cost at full throttle | $0 + electricity + GPU contention | **~$30-100/mo** at our Shorts volume (`min-instances=0` + pre-warm script; cold-load amortized over ~5-15 renders/day). See `docs/cloudrun_image.md` "Cost analysis" for the always-on alternative (~$700-815/mo per service). |
| Cold-load tax on first image | 0 (mflux warm in laptop venv) | ~5-7 min — hidden behind `min-instances=1` and `images.warmup()` |
| FLUX.2 klein bonus capability | n/a | T2I + image-to-image + multi-reference editing in one model |

The cost is higher than TTS (~$6/mo) because image gen needs an
always-warm GPU container — without `min-instances=1`, every render
pays the 5-7 min cold-load. See "Cost analysis" below.

---

## Architecture

```
laptop / cloud worker  ─┬─ pipeline/images.py::generate(provider="cloudrun_flux2_klein")
                        │
                        ├─> pipeline/images_cloudrun.py::_generate_cloudrun_flux2_klein
                        │        │
                        │        ├─ pipeline.cloudrun_auth.get_id_token (per-audience cache)
                        │        ├─ requests.Session().post(/generate, json=…)
                        │        │   (NOT urllib — see memory/feedback_urllib_cloudrun_stale_tcp.md)
                        │        ├─ retry-once on ConnectionError/Timeout
                        │        └─ render-level circuit breaker
                        │             on CloudRunUnavailable → all subsequent
                        │             calls in render skip cloud, go straight
                        │             to local mflux. Cleared at top of every
                        │             render entry point.
                        ▼
  Cloud Run service `ytfactory-image-flux2-klein`  (asia-southeast1, NVIDIA L4)
        │   server.py: Flux2KleinPipeline.from_pretrained(
        │     "/models/hf/flat/black-forest-labs/FLUX.2-klein-4B",
        │     torch_dtype=torch.bfloat16, local_files_only=True,
        │   ).to("cuda")
        │
        │   /readyz   → lazy-load + warm-time metric (used by warmup hook)
        │   /generate → bf16, guidance_scale=1.0, default steps=4 (range 2-8)
        │              returns inline base64 PNG (<5 MB) or GCS URI
        │
        │   --min-instances=0  → scale to zero when idle (cost)
        │   --max-instances=2  → up to 2 concurrent /generate
        │   --concurrency=1    → one /generate per container at a time
        │   pre-warm via cloud/warm_image_services.sh before render windows
        │
        │   HF_HOME=/models/hf  ← GCS Fuse mount (read-write for HF locks)
        ▼
  gs://ytfactory-model-weights/flat/black-forest-labs/FLUX.2-klein-4B/
        (24 GB / 52 files, FLAT layout for diffusers local_files_only)
```

The flat layout (one real file per repo entry, no symlinks) is
required because `from_pretrained(local_files_only=True)` cannot
follow GCS-Fuse-stored "symlinks" (they end up as 76-byte text
files containing the link target string). See
`memory/feedback_gcs_staging_symlink_layout.md` for the full
post-mortem; staging code lives in `cloud/weights-staging/stage.py`
under `_stage_one_flat`.

---

## One-time setup (already done)

- Bucket: `gs://ytfactory-model-weights` (asia-southeast1, versioning ON)
- Service account: `tts-runner@ytfactory-prod.iam.gserviceaccount.com`
  with `roles/storage.objectViewer` on the bucket
- Weights staged via `cloud/weights-staging/stage.py` Cloud Run Job
  (with `HF_HUB_DISABLE_XET=1` — see
  `memory/feedback_hf_xet_disable_for_asia_southeast1.md`)

To re-stage weights after a model upgrade:

```bash
gcloud run jobs execute ytfactory-weights-staging \
  --project=ytfactory-prod --region=asia-southeast1 \
  --args="black-forest-labs/FLUX.2-klein-4B" --async
```

(Streaming per-file refactor — see
`memory/feedback_cloudrun_job_tmpfs_per_file_streaming.md`.)

---

## Service deployment

```bash
cd cloud/image-flux2-klein
./deploy.sh                  # default service name + auto tag
./deploy.sh ytfactory-image-flux2-klein v1   # explicit
```

`deploy.sh` does:

1. `gcloud builds submit . --tag=…` — Cloud Build packages the
   image (Dockerfile pins torch 2.5.1+cu124, transformers 4.55.0,
   diffusers 0.38.0; build-time import check verifies
   `Flux2KleinPipeline` resolves before push)
2. `gcloud run deploy ytfactory-image-flux2-klein …` with:
   - `--gpu=1 --gpu-type=nvidia-l4 --no-gpu-zonal-redundancy`
   - `--cpu=8 --cpu-boost --memory=24Gi`
   - `--concurrency=1 --max-instances=2 --min-instances=1`
   - `--add-volume=name=weights,type=cloud-storage,bucket=ytfactory-model-weights`
   - `--add-volume-mount=volume=weights,mount-path=/models/hf`
   - `--set-env-vars=GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO`

After deploy, add the URL to laptop `.env`:

```bash
CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=https://ytfactory-image-flux2-klein-…run.app
```

(Already present.)

---

## Validated dep pin combo (the iteration cost without the playbook)

Discovered during P3 build: torch 2.4.x crashes on diffusers 0.38's
FlashAttention-3 schema decorator; transformers 4.46 lacks
`Qwen3ForCausalLM` which FLUX.2 klein's text embedder imports. The
working combo:

```
torch==2.5.1+cu124
torchvision==0.20.1+cu124
transformers==4.55.0
diffusers==0.38.0
fastapi==0.115.6 + uvicorn[standard]==0.32.1 + pydantic==2.9.2
pillow==10.4.0 + safetensors>=0.4.0 + huggingface-hub>=0.25.0,<2.0
accelerate==0.34.2 + sentencepiece==0.2.0 + einops>=0.7.0 + ftfy>=6.0
google-cloud-storage==2.18.2
```

**Validate via dep-probe Cloud Build BEFORE the real builds** — a
4-min throwaway image with just the pip install + import-check
catches conflicts cheaper than re-iterating the full GPU image. See
`docs/cloud_service_dep_playbook.md`.

---

## Per-call timings (canary 2026-05-07)

| Stage | Time |
|---|---|
| Cold-load (one-time, on `min-instances=1` boot) | 5-7 min (24 GB through GCS Fuse + bf16 materialization + .to("cuda")) |
| Warm /generate (4-step, 768×1344 vertical) | **3.86 s server-side** (1.1 s/step) |
| End-to-end client wall (incl. network + base64 decode) | ~5 s |
| Stage 3 for 7-image AITA Short | 33.4 s (~5 s/image, sequential) |

Compare to local mflux Z-Image-Turbo on M2 Max:
- Per-image warm: 12-15 s (8 NFE × ~1.5-1.8 s/step)
- Stage 3 for 7 images: 85-105 s

**Measured speedup: ~3-4× per image.**

---

## Cost analysis (back-of-envelope)

**Honest update 2026-05-07:** my earlier ~$80/mo estimate was wrong.
Actual Cloud Run NVIDIA L4 pricing in `asia-southeast1`:

| Component | Hourly | Monthly (730 hr always-on) |
|---|---|---|
| L4 GPU | ~$0.71-0.84 | ~$520-615 |
| 8 vCPU | ~$0.19 | ~$140 |
| 24-32 GiB memory | ~$0.06-0.08 | ~$44-60 |
| **Per always-warm service** | **~$1.00/hr** | **~$700-815/mo** |
| Both FLUX + Z-Image always-warm | ~$2.00/hr | **~$1,400-1,600/mo** |

That'd burn ~$150 of GCP credits in **~3 days**. So:

### Production policy: scale-to-zero + scheduled warmup

Both image services run with **`--min-instances=0`** (scale to zero
when idle). Idle cost ≈ $0/mo. Each render pays only the actual
compute time (~5 s × 30 images × $0.001/s ≈ ~$0.15/Short on FLUX).

Cold-load tax (5-7 min FLUX, 15-25 min Z-Image) is hidden via:

1. **Pre-warming via `cloud/warm_image_services.sh`**:
   ```bash
   ./cloud/warm_image_services.sh                 # FLUX only (default)
   ./cloud/warm_image_services.sh flux zimage     # both, in parallel
   ```
   Run 5-10 min BEFORE a queued render. Idempotent (already-warm
   container returns instantly).
2. **Cloud Scheduler cron** (recommended for batch render windows):
   ```bash
   # Pre-warm at 09:55 IST every weekday, 5 min before our 10:00 IST
   # batch-render slot
   gcloud scheduler jobs create http warm-flux-pre-batch \
     --project=ytfactory-prod --location=asia-southeast1 \
     --schedule="55 9 * * 1-5" --time-zone="Asia/Kolkata" \
     --uri="https://ytfactory-image-flux2-klein-767262167641.asia-southeast1.run.app/readyz" \
     --http-method=GET \
     --oidc-service-account-email=tts-runner@ytfactory-prod.iam.gserviceaccount.com \
     --oidc-token-audience="https://ytfactory-image-flux2-klein-767262167641.asia-southeast1.run.app"
   ```
   Cloud Scheduler is free (3 jobs/mo), and a /readyz that times out
   the scheduler (>15 min) doesn't waste budget — the GPU minutes
   are billed regardless of who triggered the warmup.
3. **In-pipeline `images.warmup("cloudrun_flux2_klein")`** in
   `make_short` fires `/readyz` on a background thread the moment
   the renderer starts → cold-load happens during TTS+ASR+beats
   (~2-3 min of CPU work) so by stage 3 the container is warm
   IF the render started fresh. With min=0 + no scheduler this
   only saves the wait when the renderer happens to launch a fresh
   container, not when none exists. The pre-warmup script above is
   more reliable for batch-render scenarios.

### Per-render marginal cost (min=0)

| Render scenario | Cold-load | Stage-3 (30 images) | Cost |
|---|---|---|---|
| Already warm (warmed within last ~15 min) | 0 | ~150s GPU | ~$0.04 |
| Cold (no recent activity) | ~7 min | ~150s GPU | ~$0.18 |
| Pre-warmed via cron | 0 | ~150s GPU | ~$0.04 + ~$0.20 for the cron warmup itself |

At our current Shorts volume (~5-15/day), even cold-loading every
single render costs ~$1-3/day = **~$30-100/mo** — roughly 10× cheaper
than always-on min=1.

### Switching to min=1 (when?)

Justified only if Shorts volume gets to >50/day OR latency-sensitive
interactive use cases (live demo, web UI showcase, etc). At that
volume, the always-on container is amortized.

---

## Local fallback policy

| Cloud provider fails (5xx / timeout / network) | Falls back to |
|---|---|
| `cloudrun_flux2_klein` | local `_generate_z_image_turbo` (mflux) |
| `cloudrun_z_image_turbo` (when shipped) | local `_generate_z_image_turbo` (mflux) |

Implemented in `pipeline/images_cloudrun.py::_local_fallback`. Both
fall back to the SAME local provider because Z-Image-Turbo (mflux)
is the only laptop image option we ship; FLUX.2 klein has no MLX
equivalent.

**Render-level circuit breaker** (the key divergence from the TTS
client): the FIRST `CloudRunUnavailable` in a render trips a
module-global flag → all subsequent images in the same process
skip cloud and go straight to local mflux. Without this, a 30-image
Short during a cloud outage would pay 30 × `CLOUDRUN_IMAGE_TIMEOUT`
(900 s × 30 = 7.5 hr) of timeouts. Reset hook fires at the top of
every render entry point (`make_short`, `render` for footage_only,
long_form `main`, sports_doc `main`).

Disable the breaker for canary work:

```bash
CLOUDRUN_IMAGE_DISABLE_FALLBACK=1 ./make_shorts ...
```

---

## Rollback

If FLUX.2 klein cloud has a bad day (style regression, cost spike,
ops issue), flip channel YAMLs back:

```bash
cd /Users/rohit/ytFactory
for f in $(grep -rlE "^[[:space:]]*image_provider: cloudrun_flux2_klein[[:space:]]*$" --include='*.yaml' .); do
  sed -i '' -E 's/^([[:space:]]*)image_provider: cloudrun_flux2_klein[[:space:]]*$/\1image_provider: z_image_turbo/' "$f"
done
```

That restores local mflux Z-Image-Turbo across all 16 sites in a
single sed. No service redeploy needed.

---

## Z-Image follow-up (P3.5)

`ytfactory-image-z-image-turbo` service exists at
`gs://ytfactory-model-weights/flat/Tongyi-MAI/Z-Image-Turbo/` (32 GB
weights staged) but cold-load through GCS Fuse keeps stalling —
Cloud Run replaces the container 3× in 17 min during the
transformer's 25 GB shard reads.

Three candidate fixes in priority order:

1. **Move pipeline load OFF the request path** via FastAPI
   `lifespan` context manager OR a one-shot background thread at
   container start. /readyz then polls a `_LOADED: bool` flag.
   Cloud Run's request timeout never fires during cold-load.
   Highest-confidence fix.
2. **Set `--min-instances=1`** so the load happens once and stays
   warm across requests. Same cost as FLUX.2 klein.
3. **Switch to `low_cpu_mem_usage=False`** AND restructure as
   per-component `.to("cuda")` calls so peak CPU stays bounded by
   largest single component (~10 GB) instead of ~33 GB total.

See `memory/feedback_zimage_cloudrun_coldload_stall.md`.

Z-Image was the parity / risk-insurance lane per
`docs/research/image_gen_2026.md`. Useful for canary A/B but not
required for production rollout — FLUX.2 klein is the primary.

---

## Adding a new image model (Qwen / HiDream / etc.)

1. Read `docs/cloud_service_dep_playbook.md` first.
2. Run `cloud/weights-staging/validate_layout.py` to confirm the
   staging job's flat layout still works for the model's
   `from_pretrained` (run after any diffusers/huggingface_hub bump).
3. Add the repo to `FLAT_LAYOUT_REPOS` in
   `cloud/weights-staging/stage.py`.
4. Run the staging Job: `gcloud run jobs execute … --args="<repo>"`.
5. Build a dep-probe Cloud Build BEFORE the real one — proves
   torch + transformers + diffusers + the new pipeline class all
   resolve and import on cu124.
6. Build the service in `cloud/image-<name>/` mirroring
   `cloud/image-flux2-klein/`. Use `--min-instances=1` from the
   start unless cost is a concern.
7. Add a `_generate_cloudrun_<name>` wrapper in
   `pipeline/images_cloudrun.py` and the matching dispatcher branch
   + capabilities entry in `pipeline/images.py`.
8. Test against the dep-probe → live smoke
   (`CLOUDRUN_IMAGE_LIVE=1 …`) → canary one channel-variant with
   `CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` → bulk flip the relevant
   YAMLs.
