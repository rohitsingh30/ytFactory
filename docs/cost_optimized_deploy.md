# ytFactory — Cost-Optimized Deploy Guide

**Target:** Minimum viable production stack on GCP that costs **$0.063/render** and **$0/idle**.

Distilled from the 2026-05-15 bake-off session that burned the $300 free trial in 8 days. Net savings vs that pattern: **97% per render**.

---

## TL;DR

```bash
PROJECT=ytfactory-prod-v3 \
REGION=asia-southeast1 \
HF_TOKEN=hf_xxx \
AZURE_OPENAI_ENDPOINT=https://testshoffer.openai.azure.com \
AZURE_OPENAI_API_KEY=xxx \
USER_EMAIL=you@example.com \
bash scripts/bootstrap_new_project.sh
```

**Total bootstrap time:** ~50 min  
**Bootstrap cost:** ~$3-4  
**Recurring storage:** ~$0.85/mo  
**Per render:** $0.063  
**Idle cost:** $0

---

## What this stack includes

| Service | Purpose | Spec | $ when active | $ when idle |
|---|---|---|---|---|
| **z-image-turbo** | Image gen for ALL channels | 4 vCPU + 16 GiB + L4 | $0.626/hr | $0 |
| **chatterbox** | English TTS (mystories, sports, history, cosmos) | 4 vCPU + 16 GiB + L4 | $0.514/hr | $0 |
| **indicf5** | Hindi TTS (hindutava) | 4 vCPU + 16 GiB + L4 | $0.514/hr | $0 |
| **asr-whisper** | Caption alignment | 4 vCPU + 16 GiB + L4 | $0.514/hr | $0 |
| **render-worker-v2** | Render orchestration | 4 vCPU + 8 GiB no-GPU | $0.116/hr | $0 |
| web + web-next (optional) | Admin UI | tiny | tiny | < $0.50/mo |

### What this stack DOES NOT include (deliberately)

| Service | Reason for excluding |
|---|---|
| **editing-agent** | Optional polish stage. Restore later if needed. |
| **clone-video-worker** | Only needed for `/clone-video-format` skill. |

---

## Per-render cost breakdown ($0.063 total)

| Stage | Active time | Resource | Hourly | Per render |
|---|---|---|---|---|
| Worker JOB orchestration | ~10 min | 4 vCPU + 8 GiB no-GPU | $0.116/hr | **$0.019** |
| TTS (chatterbox or indicf5, 1 combined call) | ~60s | L4 + 4 vCPU + 16 GiB | $0.514/hr | **$0.0086** |
| z-turbo image gen × 12 beats | ~120s | L4 + 4 vCPU + 16 GiB | $0.626/hr | **$0.0209** |
| ASR alignment | ~30s | L4 + 4 vCPU + 16 GiB | $0.514/hr | **$0.0043** |
| Cold-start overhead (1 service) | ~60s | L4 + 4 vCPU + 16 GiB | $0.626/hr | **$0.010** |
| Compose + GCS upload | <1s | (worker, free in-region egress) | n/a | $0 |
| **TOTAL** | | | | **$0.063** |

---

## Production cost projections

| Renders/day | $/day | $/month | $/year |
|---|---|---|---|
| 10 | $0.63 | **$19** | $228 |
| 25 | $1.58 | **$47** | $570 |
| 50 | $3.15 | **$95** | $1,134 |
| 100 | $6.30 | **$189** | $2,268 |
| 200 | $12.60 | **$378** | $4,536 |
| 500 | $31.50 | **$945** | $11,340 |
| 1000 | $63.00 | **$1,890** | $22,680 |

### Profitability check vs YouTube monetization

At ~$0.50-2.00 RPM (typical Shorts CPM) × 1000 views/video:
- $0.063 cost vs $0.50-2.00 revenue per mp4 = **8-32x profitable** even at low view rates
- Break-even: any video that hits 60+ views earns its compute back

---

## The 16 lessons baked into this stack

| # | Lesson | Why it matters |
|---|---|---|
| 1 | **Quotas requested before any deploy** (GPU 8, Mem 200 GiB, CPU 200 vCPU) | Avoids mid-deploy quota errors |
| 2 | **`--set-env-vars` REPLACES all env vars** | All `CLOUDRUN_*_URL` baked into worker deploy.sh |
| 3 | **`--add-volume-mount=readonly=true` is invalid** in newer gcloud | Use `--add-volume=...,readonly=true` instead |
| 4 | **image-runner SA needs `roles/storage.objectViewer`** on weights bucket | GCS Fuse mount fails without it |
| 5 | **HF gated models need HF_TOKEN via Secret Manager** | Granted to `<projectNumber>-compute@...` SA |
| 6 | **Cloud Build VPC-SC log-streaming exits non-zero** | Use `--async` + poll instead of streaming |
| 7 | **`gcloud auth` reauth broken in non-interactive shells** | `cloud/_shared/auth_setup.sh` impersonates deployer SA |
| 8 | **Cloud Run JOB 32 GiB cap** can't stage huge HF repos | Weights staging via Cloud Build with e2-highcpu-32 |
| 9 | **Don't bake 20+ GB weights into Docker images** | All image services use GCS Fuse mount |
| 10 | **`YTFACTORY_QUEUE_BACKEND=firestore` env required** | Otherwise jobs go to in-memory backend |
| 11 | **`GOOGLE_CLOUD_PROJECT` env required** (not `<project>-prod` default) | Bootstrap sets it explicitly |
| 12 | **`visual_mode: hybrid_beat_footage` is unregistered** | Override to `ai_beat_slideshow` |
| 13 | **min-instances=1 burns ~$18/day per always-on GPU service** | Default = `min-instances=0` for ALL services |
| 14 | **Cloud Run idle timeout = 15 min** = many cold-starts → high cost on TTS | Combine TTS chunks into 1 call |

---

## Iron rules (never break these)

1. **`min-instances=0` on every GPU service.** No exceptions during normal ops. Set =1 only during a bake-off / batch render window, then immediately reset to 0.

2. **`max-instances=1` on every GPU service.** Caught 2026-05-17: tts-chatterbox / tts-indicf5 / asr-whisper were defaulted to `max-instances=2`, doubling potential L4 spend with no throughput win (the render pipeline calls TTS sequentially per chunk). `concurrency=2` already lets two chunks share one instance if the laptop ever pipelines. Two-instance bursts spend ₹1,200+/day if traffic spikes.

3. **`--region=asia-southeast1` on every `gcloud builds submit`.** Caught 2026-05-17: builds were defaulting to the global pool (US Iowa) while Artifact Registry sits in `asia-southeast1`, so every image push generated intercontinental egress (₹430/day on z-image-turbo's 33 GiB image alone). Both `--config=` and `--tag=` builds need this flag. `cloud/_shared/submit_build.sh` picks it up from `GCP_REGION`.

4. **Thread-safe lazy-load in every GPU service `_model()` / `_pipe()`.** Use double-checked locking with `threading.Lock()`. The TOCTOU race on `if _MODEL is None:` lets `concurrency=2` Cloud Run double-load on cold start (model gets allocated twice → OOM on the 22 GiB L4). Pattern in `cloud/{image-z-image-turbo,tts-chatterbox,tts-indicf5,asr-whisper}/server.py`.

5. **Set a billing alert at $30 + budget cutoff at $200.** This is the #1 protection against silent credit exhaustion. Yesterday's trial was burned because services were left running 24/7 and nobody noticed.

6. **Monitor "Min Instance CPU/Memory Tier 2" SKU weekly.** Should be ~$0. Anything > $1 means a service has min-instances ≥ 1.

7. **Cold-start budget per service: 60-90s.** If a model needs longer, it's a sign the model is wrong for L4.

---

## Quotas required (auto-bumped by bootstrap)

| Quota | Bumped from | Bumped to | Why |
|---|---|---|---|
| `NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion` | 3 | **8** | 4 GPU services + headroom for revision rollover |
| `MemAllocPerProjectRegion` | 100 GiB | **200 GiB** | Same headroom |
| `CpuAllocPerProjectRegion` | 60 vCPU | **200 vCPU** | Same |

These bumps are **free** — quota lifts the cap; cost is only for actual usage.

---

## What was wasted yesterday ($316 trial credit consumed)

| Waste source | $ wasted | Bootstrap avoids? |
|---|---|---|
| **Idle GPU services with min-instances=1** (96% of waste) | ~$272 | ✅ Bootstrap defaults to min=0 |
| Cancelled mid-render worker executions (15+) | ~$3 | ✅ Bootstrap pre-warms before submit |
| Quota-exceeded retry deploys | ~$1 | ✅ Quotas requested upfront |
| Web service auto-rebuilds (cron?) | ~$0.30 | (separate fix — disable cron or set webhook) |

---

## Deployment order (bootstrap script does this automatically)

1. **Verify gcloud auth + project access** (~5 sec)
2. **Enable APIs** (run, build, firestore, secrets, storage, etc.) (~30 sec)
3. **Submit quota bumps** (~2 min — auto-grant typically <5 min)
4. **Create service accounts** (image-runner, render-runner, ytfactory-deployer) (~30 sec)
5. **Create GCS buckets** (artifacts, weights, state) + grant SA permissions (~1 min)
6. **Create Artifact Registry repo** (~30 sec)
7. **Create Secret Manager secrets** (HF_TOKEN, AZURE_OPENAI_API_KEY) (~30 sec)
8. **Stage z-turbo weights via Cloud Build** (~15 min, ~$1.20)
9. **Build + deploy z-image-turbo service** (~10 min, ~$0.30)
10. **Build + deploy chatterbox + indicf5 TTS services** (~15 min, ~$0.50)
11. **Build + deploy asr-whisper service** (~5 min, ~$0.20)
12. **Build + deploy render-worker-v2 with all CLOUDRUN URLs baked in** (~10 min, ~$0.30)
13. **Smoke test: render one short, verify mp4** (~5 min, ~$0.10)

**Total bootstrap time: ~50 min**  
**Total bootstrap cost: ~$3-4**

---

## Audit + monitoring (post-bootstrap)

After bootstrap, run weekly:

```bash
# Idle cost check — should be ~$0
gcloud billing budgets list --billing-account=$BILLING_ID

# Verify all GPU services have min-instances=0
gcloud run services list --region=asia-southeast1 --project=$PROJECT \
  --format="value(metadata.name,spec.template.metadata.annotations['autoscaling.knative.dev/minScale'])" | \
  grep -v ' 0$' || echo "  ✓ All services scale-to-zero"

# Verify all GPU services have max-instances=1 (2026-05-17 cost-audit rule)
gcloud run services list --region=asia-southeast1 --project=$PROJECT \
  --format="value(metadata.name,spec.template.metadata.annotations['autoscaling.knative.dev/maxScale'])" | \
  awk '$2 != "1" {print "  ✗ " $1 " maxScale=" $2}' || echo "  ✓ All services capped at 1 instance"

# Verify recent Cloud Builds ran in asia-southeast1 (NOT global)
gcloud builds list --limit=10 --format="value(id,createTime,_BUILD_REGION)" --project=$PROJECT | \
  awk '$3 != "asia-southeast1" {print "  ✗ build " $1 " ran in region=" ($3 == "" ? "global" : $3)}' || \
  echo "  ✓ All recent builds regional"
```

If any service shows min-instances ≥ 1, fix immediately:

```bash
gcloud run services update <svc> --min-instances=0 --region=asia-southeast1
```

If any service shows max-instances ≠ 1, fix immediately (2026-05-17 cost-audit rule):

```bash
gcloud run services update <svc> --max-instances=1 --region=asia-southeast1
```

If any recent build ran in the global pool, add `--region=asia-southeast1` to the offending `deploy.sh` (search `gcloud builds submit` and add the flag). See iron rule #3 above.

---

## When to use Azure (future)

Skip Azure for now. Reconsider when:

1. **Production hits 500+ renders/day** — Azure Spot for TTS saves ~$50/mo
2. **Multi-region / DR requirements** — Azure for cross-cloud failover

Today: **GCP-only is the right call.** Adding Azure adds operational complexity that doesn't pay off below 500 renders/day.

---

## Recovery / rollback

- **Killed a service?** Re-run its `cloud/<svc>/deploy.sh`. Image is in Artifact Registry, weights are in GCS — service comes back in ~5 min.
- **Worker broken?** Check that all `CLOUDRUN_*_URL` envs are set: `gcloud run jobs describe ytfactory-render-worker-v2 --format=json | grep CLOUDRUN`. If missing, run worker `deploy.sh` again — the patched version preserves all envs.

---

## What if I want different optimization later?

| Goal | Action |
|---|---|
| Cheaper TTS | Move to Azure Spot once GPU quota lands (~$0.04/render base) |
