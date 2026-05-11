# Cloud cost incident — 2026-05-11

## TL;DR

`ytfactory-image-flux2-klein` had been running with `--min-instances=1`
and `nvidia-l4` attached since `2026-05-08T21:40` (revision `00001-q6h`).
That kept one L4 GPU + 8 vCPU + 24 GiB RAM allocated 24/7 even when
zero renders were happening — burning ~$22/day (~₹1,800/day,
~$650/month) against the $150 GCP-credit budget.

Reverted to `--min-instances=0` on 2026-05-11. New revision
`ytfactory-image-flux2-klein-00009-r56` is live and serves 100% of
traffic. Every render now pays a one-time ~5-7 min cold-load tax,
hidden behind `pipeline.images.warmup()` (background `/readyz` ping
fired on render entry).

## How we got here

- 2026-05-07: Cloud-first image migration shipped (FLUX.2 klein).
- 2026-05-08T21:40: First revision deployed with `min-instances=1`.
  CLAUDE.md was updated to call this out as "always-warm container".
- 2026-05-09 → 2026-05-11: ~3 days of always-warm L4 burn — roughly
  **$66 of GCP credit** (~₹5,500) spent on idle GPU before discovery.
- 2026-05-11: User noticed billing spike, ran `gcloud run services
  describe`, found `minScale: '1'` on FLUX.2 klein, flipped to 0.

## Why this was the dominant cost

| Resource | Rate (asia-southeast1, approx) | 24h | 30d |
|---|---:|---:|---:|
| 1× NVIDIA L4 GPU (always-allocated) | ~$0.71/hr | ~$17 | **~$510** |
| 8 vCPU (always-allocated)            | ~$0.018/vCPU-hr | ~$3.5 | ~$104 |
| 24 GiB memory (always-allocated)     | ~$0.002/GiB-hr | ~$1.2 | ~$35 |
| **Per always-warm L4 service**       |   | **~$22** | **~$650** |

Everything else combined was rounding error:

- Artifact Registry (`ytfactory-tts` repo, 107 GB): ~$10.70/mo
- GCS storage (model-weights 50 GB, cloudbuild source 16.8 GB,
  state+artifacts ~280 MB): ~$1.50/mo total
- Per-render burst GPU time on Chatterbox / IndicF5 (already
  scale-to-zero): ~$30-40/mo at current 48 renders/day cron pace
- Cloud Build minutes: low single digits / mo

The two TTS services (Chatterbox, IndicF5) and the second image
service (Z-Image-Turbo, blocked by cold-load issue) were already
scale-to-zero. Only FLUX.2 klein was bleeding.

## Fix

```bash
gcloud run services update ytfactory-image-flux2-klein \
  --project=ytfactory-prod-v2 --region=asia-southeast1 \
  --min-instances=0
```

The deploy script `cloud/image-flux2-klein/deploy.sh:50` already
specifies `--min-instances=0`, so this drift was a manual override
on the live service that the script would have re-cleared on the
next deploy anyway.

## Trade-off accepted

Every render now pays cold-load latency:
- First image of a render: ~5-7 min (model load through GCS Fuse +
  bf16 materialization + `enable_model_cpu_offload()` warmup)
- Subsequent images in the same render: ~17-19 s warm

The render-worker JOB runs every 30 min via Cloud Scheduler. Cloud
Run's default idle timeout is ~15 min, so most cron ticks will pay
the cold-load. Cost math:

- 48 cold-starts/day × 5 min × $0.71/hr ≈ **$2.84/day ≈ $86/mo**
- vs always-warm ≈ **$650/mo**
- Net savings: **~$564/mo** (~₹47k/mo)

User-visible latency hit: each render's wall-clock goes up by ~5 min,
but renders are batch / cron-driven (not user-clicked), so this is
acceptable.

## Watch-list (don't repeat this)

1. **Never set `--min-instances ≥ 1` on a GPU-backed Cloud Run
   service** without a written cost justification + an alert. Even
   one always-warm L4 ≈ a low-end full-time engineer's GCP budget.
2. **The deploy script is the source of truth.** Live drifts via
   `gcloud run services update` get reverted on the next deploy
   silently. If a drift is intentional, encode it in `deploy.sh`
   and submit a PR — don't carry it as a hidden console mutation.
3. **Wire BigQuery billing export.** As of 2026-05-11 the dataset
   `ytfactory-prod-v2.billing_export` exists but the Cloud Billing
   account isn't actually exporting into it — so SKU-level cost
   debugging requires the console UI. Setup steps below.
4. **Daily budget alert.** Set a Billing → Budgets & alerts threshold
   at ₹5,000 / month / project so the next idle GPU is caught in
   hours, not days.

## Marginal cost of bumping `--max-instances` (added 2026-05-11)

After the per-render image fan-out shipped (commit `a3c4e47`,
`docs/parallel_per_beat_fanout.md`), we needed to bump
`ytfactory-image-flux2-klein --max-instances=2 → 3`. The cost
analysis surprised the right way — **bumping max-instances is
essentially compute-neutral**.

### Why bumping max-instances is (almost) free

Cloud Run with `--min-instances=0` bills **per request-handling
time**, NOT per container-uptime. So:

| Scenario | Wall (warm) | GPU-sec billed | Compute $ delta |
|---|---|---|---|
| 30 images, serial, 1 container | 150 s | 150 s | baseline |
| 30 images, 2-way fan-out, 2 containers | 75 s | **150 s (same)** | **$0** |
| 30 images, 4-way fan-out, 4 containers | 38 s | **150 s (same)** | **$0** |
| 30 images, 8-way fan-out, 8 containers | 19 s | **150 s (same)** | **$0** |

The total GPU-seconds is identical — same images, same per-image
inference time, just split across N containers. **Compute cost is
unchanged.**

### What DOES cost money: cold-load amplification

The hidden cost is that with `--min-instances=0`, every NEW
container that spins up pays its own ~5 min cold-load (model
load through GCS Fuse + bf16 materialization + .to("cuda")).

| Cold container cost | ~5 min × $0.90/hr (L4 + 8 vCPU + 24 GiB) | **≈ $0.075 per cold-start** |
|---|---|---|

So fan-out width N multiplies the cold-load tax on the FIRST
burst of a fresh batch:

| Fan-out width | Cold-load Δ on first burst | Per-day at 10 cold-batches | Per-month |
|---|---|---|---|
| Serial (today's pre-fix) | 1 × $0.075 = $0.075 | $0.75 | ~$22 |
| Workers=2 (`a3c4e47`, current) | 2 × $0.075 = $0.15 | $1.50 | **~$45** |
| Workers=4 (needs quota bump) | 4 × $0.075 = $0.30 | $3.00 | ~$90 |
| Workers=8 (needs bigger quota bump) | 8 × $0.075 = $0.60 | $6.00 | ~$180 |

The DELTA from serial → workers=2 is **~$22/mo of additional
cold-load tax** at 10 cold-batches/day. Subsequent batches inside
the warm window (~15 min after last call) pay $0 amplification.

### Mitigation: Cloud Scheduler prewarm

`cloud/warm_image_services.sh` + Cloud Scheduler crons (free tier
covers 3 jobs/mo) collapse N cold-loads/day into ONE warm-up wave
at known render windows:

```bash
# Schedule prewarm 5 min before known batch render slots
gcloud scheduler jobs create http warm-flux-pre-batch \
  --project=ytfactory-prod-v2 --location=asia-southeast1 \
  --schedule="55 9 * * 1-5" --time-zone="Asia/Kolkata" \
  --uri="https://ytfactory-image-flux2-klein-…run.app/readyz" \
  --http-method=GET \
  --oidc-service-account-email=tts-runner@ytfactory-prod-v2.iam.gserviceaccount.com \
  --oidc-token-audience="https://ytfactory-image-flux2-klein-…run.app"
```

With prewarm:

| Fan-out width | Cold-load $/mo (no prewarm) | Cold-load $/mo (with prewarm) |
|---|---|---|
| Serial | ~$22 | ~$2 (1 wave/day × 1 cold) |
| Workers=2 | ~$45 | ~$4 (1 wave × 2 cold) |
| Workers=4 | ~$90 | ~$9 (1 wave × 4 cold) |
| Workers=8 | ~$180 | ~$14 (1 wave × 8 cold) |

**The prewarm script is the cost-killer trick** — turns
fan-out from "expensive per cold start" into "negligible".
RUN PREWARM BEFORE BATCH WINDOWS — don't pay cold-load N×.

### Decision matrix: when to bump max-instances higher

| Scenario | Bump | Action |
|---|---|---|
| <5 Shorts/day, ad-hoc renders | leave at 3 | current state |
| 5-15 Shorts/day, batched windows | 3 (current) | wire prewarm cron, that's it |
| 15-30 Shorts/day, want bulk × per-render compose | 4-6 | quota request to Google |
| >30 Shorts/day OR interactive UI showcase | consider `--min-instances=1` | review against $650/mo always-warm cost first |

Don't bump `--min-instances ≥ 1` to "fix" cold-load — see
watch-list item #1 above. The 2026-05-08 → 2026-05-11 incident
(this very doc's TL;DR) burned $66 of credits in 3 days that way.

### Where this shipped

- `cloud/image-flux2-klein/deploy.sh:49` — `--max-instances=2 → 3`
  (with the comment block explaining quota ceiling + mitigation
  policy + when to file the next bump).
- Live service updated 2026-05-11 via
  `gcloud run services update --max-instances=3 --quiet` after
  the deploy gate revealed the actual quota
  (`NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion = 3`, not
  the speculative 5-6 the older docs claimed). See
  `docs/cloud_run_quota_self_service.md` § "GPU quota in
  asia-southeast1 = 3" for the discovery story + verification
  recipe.

## Wire BigQuery billing export (one-time setup)

`gcloud` does **not** support configuring billing exports — this is
console-only. Step-by-step:

1. Open https://console.cloud.google.com/billing/012FF7-AF3923-1A94C4/export
   (substitute the live billing account ID if it changes).
2. Under **BigQuery export**, click **EDIT SETTINGS** for both:
   - **Standard usage cost** — daily SKU-level cost lines.
   - **Detailed usage cost** — per-resource (per-Cloud-Run-revision)
     attribution. Detailed export is what you want for "which exact
     service is bleeding money".
3. For each: pick **Project = `ytfactory-prod-v2`**, **Dataset =
   `billing_export`**. Save.
4. Wait ~24 h for the first export. Then:

   ```sql
   -- Top SKUs last 7 days, INR
   SELECT
     service.description AS service,
     sku.description AS sku,
     ROUND(SUM(cost), 2) AS inr,
     ROUND(SUM(usage.amount), 2) AS qty,
     ANY_VALUE(usage.unit) AS unit
   FROM `ytfactory-prod-v2.billing_export.gcp_billing_export_v1_*`
   WHERE DATE(_PARTITIONTIME) >= CURRENT_DATE() - 7
   GROUP BY service, sku
   ORDER BY inr DESC
   LIMIT 25;
   ```

   ```sql
   -- Cost per Cloud Run service last 7 days (detailed export)
   SELECT
     resource.name AS resource,
     ROUND(SUM(cost), 2) AS inr
   FROM `ytfactory-prod-v2.billing_export.gcp_billing_export_resource_v1_*`
   WHERE DATE(_PARTITIONTIME) >= CURRENT_DATE() - 7
     AND service.description LIKE 'Cloud Run%'
   GROUP BY resource
   ORDER BY inr DESC;
   ```

The dataset is in `US` (default). That's fine — billing exports are
free; the dataset region only matters if you join against other
asia-southeast1 datasets later.

## See also

- `CLAUDE.md` — "Cloud-first image generation migration" section
  (updated 2026-05-11 to reflect `min-instances=0` as the default).
- `docs/cloudrun_image.md` — runbook for FLUX.2 klein, with the
  cold-load math and warm-up script reference.
- `cloud/warm_image_services.sh` — pre-warm `/readyz` pulse, can be
  scheduled via Cloud Scheduler if cold-start latency becomes user-
  visible (cents/month vs $650/mo always-warm).
