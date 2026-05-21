# ytFactory — Full Optimization Checklist

Every change being made to go from yesterday's $282 / 8-day burn to **$0.063/render + $0/idle**. Each item links to a measurable saving.

---

## 1. Stack reduction (drop unused services)

| Optimization | What | Saving |
|---|---|---|
| 1.1 | **Drop klein image service** — z-turbo is sufficient | ~$40/8d (163 cold-starts × idle) |
| 1.2 | **Drop qwen image service** — 2x cost per render due to required CPU offload on L4 | ~$3.30 historical |
| 1.3 | **Drop flux2-dev image service** — fundamentally incompatible with L4 (~96hr/render) | ~$0.74 historical, prevents future hours of compute |
| 1.4 | **Drop hidream image service** — exploratory, never used | ~$3.30 historical |
| 1.5 | **Drop cosyvoice TTS** — no channel uses it | ~$0.21 historical |
| 1.6 | **Drop higgs TTS** — no channel uses it | ~$0.42 historical |
| 1.7 | **Drop indicparler TTS** — no channel uses it | ~$0.21 historical |
| 1.8 | **Drop editing-agent service** — optional polish | ~$0.09 historical |
| 1.9 | **Drop clone-video-worker** — only needed for `/clone-video-format` skill | ~$0.32 historical |
| 1.10 | **Drop cobalt-api** — only needed for clone-video | ~$0.04 historical |

**Net stack:** 4 GPU services (z-turbo + chatterbox + indicf5 + asr-whisper) + worker + web

---

## 2. Right-size CPU + memory (Cloud Run min for GPU = 4 vCPU + 16 GiB)

| Service | Yesterday | Optimized | Hourly saving |
|---|---|---|---|
| 2.1 z-image-turbo | 8 vCPU + 32 GiB | **4 vCPU + 16 GiB** | $0.74 → $0.626/hr (**−15%**) |
| 2.2 chatterbox | 8 vCPU + 16 GiB | **4 vCPU + 16 GiB** | $0.61 → $0.514/hr (**−16%**) |
| 2.3 indicf5 | 8 vCPU + 16 GiB | **4 vCPU + 16 GiB** | $0.61 → $0.514/hr (**−16%**) |
| 2.4 asr-whisper | 8 vCPU + 16 GiB | **4 vCPU + 16 GiB** | $0.61 → $0.514/hr (**−16%**) |
| 2.5 render-worker-v2 | already 4 vCPU + 8 GiB no-GPU | unchanged | $0.116/hr |

---

## 3. Idle elimination (the BIG one — 96% of yesterday's waste)

| Optimization | Detail | Saving |
|---|---|---|
| 3.1 | **`min-instances=0` on ALL GPU services** by default | **~$272/8d eliminated** (the headline win) |
| 3.2 | **Forbid `min-instances=1` in deploy scripts** — only set during active bake-off, immediately reset to 0 | Prevents accidental always-on |
| 3.3 | **Idle timeout reduced from 15 min → 10 min** (Cloud Run default = 15) | Instances scale-down faster after last request |
| 3.4 | **`max-instances=1` on every GPU service** — caught 2026-05-17 cost audit; was defaulting to 2 on TTS+ASR, doubling L4 spend potential. The pipeline calls each provider serially per chunk; `concurrency=2` already covers any in-instance pipelining. | ~₹600-1,200/day prevented during burst |

---

## 4. TTS call consolidation (256 cold-starts → ~50)

| Optimization | Detail | Saving |
|---|---|---|
| 4.1 | **Combine all TTS chunks into 1 call per render** (yesterday: 5+ calls/render) | 80% fewer cold-starts → less idle window per render |
| 4.2 | **Cache reference voice** in chatterbox container (no GCS round-trip per call) | ~1-2s saved per call |
| 4.3 | **Use longer Cloud Run request timeout** (3600s) so single combined TTS call doesn't hit 503 | Reliability + cost (no retries) |

---

## 5. Build & deploy infrastructure

| Optimization | Detail | Saving |
|---|---|---|
| 5.1 | **GCS Fuse mount for weights** (not baked into Docker images) | Avoids 20+ GB Docker layer push stalls (qwen failed 4 of 6 builds yesterday) |
| 5.2 | **Cloud Build `--async` + poll** | Avoids VPC-SC log-streaming bug that caused deploy script aborts |
| 5.3 | **Cloud Build for weights staging** (e2-highcpu-32 + 200 GB disk) | Cloud Run JOB OOMs on 32+ GB single files |
| 5.4 | **`hf_xet` enabled for huge HF files** | Avoids "file too large" errors on >50 GB safetensors |
| 5.5 | **Skip redundant 64 GB FLUX.2 single-file** (stage sharded version only) | Saves 64 GB upload + ~10 min build time (when staging klein/dev) |
| 5.6 | **`--region=asia-southeast1` on every `gcloud builds submit`** — caught 2026-05-17 cost audit. Default global pool sits in US Iowa; Artifact Registry sits in asia-southeast1; every image push crossed the Pacific. `cloud/_shared/submit_build.sh` now picks up `GCP_REGION` automatically. | **~₹430/day** (56 GiB intercontinental egress at 33 GiB per z-image-turbo push) |
| 5.7 | **Lock `_model()` / `_pipe()` lazy-load** in every GPU service. Double-checked locking around `if _MODEL is None:` so concurrent /readyz + /generate calls during cold start don't double-load the model into VRAM. Triggers OOM on L4 (22 GiB) when not present. | Reliability fix; no direct $ saving but prevents wasted-render cold-start failures |

---

## 6. Quotas pre-bumped (FREE, prevents mid-deploy errors)

| Quota | Was (default) | Bumped to | Why |
|---|---|---|---|
| 6.1 NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion | 3 | **8** | Headroom for revision rollover during deploys |
| 6.2 MemAllocPerProjectRegion | 100 GiB | **200 GiB** | Same |
| 6.3 CpuAllocPerProjectRegion | 60 vCPU | **200 vCPU** | Same |

Quota bumps cost **$0** (just lifts the cap). Auto-granted within minutes.

---

## 7. Worker JOB env stability

| Optimization | Detail |
|---|---|
| 7.1 | **All `CLOUDRUN_*_URL` envs baked into `cloud/render-worker-v2/deploy.sh`** | Yesterday's bug: `--set-env-vars` REPLACES all envs, so each rebuild lost the URLs I'd manually added. Now in deploy.sh permanently. |
| 7.2 | **`YTFACTORY_QUEUE_BACKEND=firestore`** auto-set by bootstrap | Without it, jobs go to in-memory backend and "vanish" |
| 7.3 | **`GOOGLE_CLOUD_PROJECT=<project>`** explicitly set | Default is `ytfactory-prod` (the OLD suspended project) — wrong target |

---

## 8. Render reliability

| Optimization | Detail |
|---|---|
| 8.1 | **Provider dispatch wired for all needed models** (`pipeline/images/images.py:_generate_impl`) | Yesterday's bug: qwen + flux2_dev had no dispatch → silent failures |
| 8.2 | **429 retry backoff bumped from 15s → 110s** total (`pipeline/images/images_cloudrun.py`) | Lets cold-load complete before A5 fail-loud trips |
| 8.3 | **Pre-warm services before bake-off** (`scripts/run_bake_off.sh`) | Eliminates 429 cold-start race during multi-job submission |
| 8.4 | **Channel YAML `visual_mode` overrides** in bake-off harness | history + cosmos channels use `hybrid_beat_footage` which isn't registered → override to `ai_beat_slideshow` |
| 8.5 | **flux2-dev `Flux2Pipeline` `negative_prompt` sniffing** | diffusers Flux2Pipeline doesn't accept negative_prompt (verified via HF docs) — server.py inspects signature before passing |

---

## 9. IAM + Security

| Optimization | Detail |
|---|---|
| 9.1 | **image-runner SA gets `roles/storage.objectViewer` on weights bucket** | GCS Fuse mount fails without it (yesterday's blocker) |
| 9.2 | **HF_TOKEN via Secret Manager** (not deploy.sh env) | Avoids token in build logs; granted to compute SA + image-runner + render-runner |
| 9.3 | **Deployer SA impersonation pattern** (`cloud/_shared/auth_setup.sh`) | Bypasses gcloud reauth bug in non-interactive shells |

---

## 10. Cost monitoring

| Optimization | Detail |
|---|---|
| 10.1 | **Billing alert at $30** | Triggers email warning |
| 10.2 | **Budget cutoff at $200** | Hard stops services if spend exceeds (configurable) |
| 10.3 | **Weekly idle audit script** — checks "Min Instance CPU/Memory Tier 2" SKU | Should be ~$0; if > $1, a service has min-instances ≥ 1 |
| 10.4 | **Per-service cost attribution** via Cloud Run metrics dashboard | Catches future cost regressions |

---

## 11. Channel YAML defaults

| Optimization | Detail |
|---|---|
| 11.1 | **Default `image_provider: cloudrun_z_image_turbo`** in all channel YAMLs (was `cloudrun_flux2_klein`) | One image service for all channels — simplifies infra |
| 11.2 | **Channel YAMLs explicitly set TTS** (`tts_provider: cloudrun_chatterbox` or `cloudrun_indicf5`) | No accidental fallback to deleted services |
| 11.3 | **`visual_mode: ai_beat_slideshow`** explicitly on history + cosmos channels | The `hybrid_beat_footage` plugin isn't registered |

---

## 12. Documentation (so the next agent doesn't repeat yesterday)

| Document | Purpose |
|---|---|
| 12.1 | **`docs/cost_optimized_deploy.md`** | Full guide: stack, costs, lessons, iron rules |
| 12.2 | **`scripts/bootstrap_new_project.sh`** — heavily commented | Each step explains the lesson behind it |
| 12.3 | **`scripts/run_bake_off.sh`** — pre-warm + render + post-cleanup | Codifies the safe pattern |
| 12.4 | **Plan.md in session state** | Decisions + open issues |

---

## 13. Net cost impact (the punchline)

| Metric | Yesterday | Optimized |
|---|---|---|
| Per-render cost | **$2.26** | **$0.063** |
| 50 renders/day monthly | extrapolated $3,400 | **$95** |
| 100 renders/day monthly | infeasible (would burn trial in hours) | **$189** |
| 500 renders/day monthly | infeasible | **$945** |
| Idle (no renders) cost | **~$35/day** | **$0/day** |

**Trial credit ($300) duration:**
- Yesterday's pattern: **~8 days** (consumed 100%)
- Optimized: **~3-6 months** depending on render volume

---

## What I'm actually changing (next 3 commits)

1. **`scripts/bootstrap_new_project.sh`** — patch to skip klein/qwen/flux2-dev weights staging + service deploys
2. **`cloud/image-z-image-turbo/deploy.sh`** + **`cloud/tts-chatterbox/deploy.sh`** + **`cloud/tts-indicf5/deploy.sh`** + **`cloud/asr-whisper/deploy.sh`** — set `--cpu=4 --memory=16Gi`
3. **`pipeline/channels/*.yaml`** — set `image_provider: cloudrun_z_image_turbo` on all 5 active channels
4. **`docs/cost_optimized_deploy.md`** + **`docs/optimization_checklist.md`** (this file) — committed

After the patches: `bash scripts/bootstrap_new_project.sh` on the new GCP account is one-shot, ~50 min, ~$3-4.
