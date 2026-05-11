# Cloud Run quota — self-service via `gcloud beta quotas`

The Console UI for quota increase requests is the documented path,
but as of 2026-05 there's a self-service `gcloud beta quotas
preferences create` that auto-grants eligible quotas in **<1 minute**
without filing a manual support ticket.

Validated 2026-05-10 against `run.googleapis.com/cpu_allocation`
(asia-southeast1) — the request was granted on the first poll.

## When to bump quota

The default per-region Cloud Run CPU quota is 20 vCPU. With three
always-warm GPU services (FLUX2-klein 8 vCPU, tts-chatterbox 8 vCPU,
tts-indicf5 8 vCPU = 24 already exceeding default at the burst
boundary) plus the rolling-deploy double-image overhead during
revision swap, deploys to `ytfactory-web` started failing with:

```
ERROR: (gcloud.run.deploy) Revision 'ytfactory-web-NNNNN-xxx' is not
ready and cannot serve traffic. Quota exceeded for total allowable
CPU per project per region.
```

Request raised the limit to 60 vCPU which gives ~2× headroom for the
current service mix.

## Recipe

```bash
# 1. Enable the Cloud Quotas API (one-time; idempotent)
gcloud services enable cloudquotas.googleapis.com

# 2. Find the exact quotaId. The UI metric name + a region dimension
#    are the two pieces you need.
TOK=$(gcloud auth print-access-token)
curl -sS -H "Authorization: Bearer $TOK" \
  "https://cloudquotas.googleapis.com/v1/projects/ytfactory-prod-v2/locations/global/services/run.googleapis.com/quotaInfos" \
  | python3 -c "
import sys, json
for q in json.load(sys.stdin).get('quotaInfos', []):
    if 'cpu' in q.get('metric','').lower():
        print(q.get('quotaId'), '→', q.get('quotaDisplayName'))
"
# → CpuAllocPerProjectRegion → Total CPU allocation, in milli vCPU,
#                              per project per region

# 3. File the request. preferred-value is in milli vCPU (60000 = 60 vCPU).
#    Email is required; gcloud's currently-active account works.
EMAIL=$(gcloud config get-value account)
gcloud beta quotas preferences create \
  --service=run.googleapis.com \
  --project=ytfactory-prod-v2 \
  --quota-id=CpuAllocPerProjectRegion \
  --preferred-value=60000 \
  --dimensions=region=asia-southeast1 \
  --preference-id=run-cpu-asia-southeast1-60 \
  --email="$EMAIL" \
  --justification="Multi-service deployment: rolling deploys overlap with always-warm GPU services. Default 20vCPU triggers deploy quota errors during rollouts."

# 4. Poll until granted (<1 min for eligible quotas)
gcloud beta quotas preferences describe run-cpu-asia-southeast1-60 \
  --project=ytfactory-prod-v2 \
  --format="value(quotaConfig.grantedValue,reconciling)"
# → 60000  False    ← granted
```

The `quotaIncreaseEligibility.isEligible` field on the quotaInfo
response (step 2) tells you upfront whether self-service will work.
For non-eligible quotas (most GPU quotas), the same command files a
support case that goes through manual review.

## Other quotas worth bumping the same way

Same pattern applies to the other Cloud Run knobs we hit:

- `MemAllocPerProjectRegion` — already at 100 GiB in
  asia-southeast1 (default 40 GiB) — pre-bumped during the cloud
  cutover, no further action.
- `nvidia_l4_gpu_allocation_no_zonal_redundancy` — **3 GPUs in
  asia-southeast1 (default, project ytfactory-prod-v2)**. When/if
  we add a 4th L4 GPU service, request via the same recipe but
  expect manual review (GPU quotas typically aren't self-service-
  eligible). See § "GPU quota in asia-southeast1 = 3" below for
  the discovery story + verification recipe.

## GPU quota in asia-southeast1 = 3 (verified 2026-05-11)

**The actual quota** for `NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion`
in `asia-southeast1` for project `ytfactory-prod-v2` is **3**, not
the 5-6 that several deploy.sh comments + the older
`docs/pipeline_latency_2026.md` had claimed.

**How we found out.** Trying to bump
`ytfactory-image-flux2-klein --max-instances=2 → 4` to enable
2-way per-render image fan-out + 2-way cross-render bulk failed
at the Cloud Run deploy gate:

```text
ERROR: (gcloud.run.services.update) spec.template.metadata.annotations[autoscaling.knative.dev/maxScale]:
  Max instances must be set to 3 or fewer in order to set GPU requirements.
To request quota: g.co/cloudrun/gpu-quota
Quota violated:
  NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion requested: 4 allowed: 3
```

The deploy gate is the source of truth — it enforces the actual
quota at deploy time. Stale docs / speculative arithmetic
("3 services × 2 = 6 GPUs" in `cloud/tts-chatterbox/deploy.sh:28`
and `cloud/_bench/tts-*/deploy.sh`) DO NOT match reality. The
real ceiling is 3 across the entire project.

**Total max-instances ceiling allocated across services today:**

| Service | max-instances | concurrency | Real GPU usage (peak) |
|---|---|---|---|
| `ytfactory-image-flux2-klein` | 3 (bumped 2026-05-11) | 1 | up to 3 simultaneous |
| `ytfactory-tts-chatterbox` | 2 | 1 | up to 2 simultaneous |
| `ytfactory-tts-indicf5` | 1 | 1 | 1 |
| **Total max capacity** | **6** | — | **but quota caps to 3 concurrent** |

When the cumulative load exceeds 3 GPUs (e.g. 3 in-flight image
calls + 2 in-flight TTS calls), Cloud Run queues the overflow.
Symptoms: `503 "Rate exceeded"` retries for the queued requests
(the image client has 5-attempt exponential backoff for exactly
this case — `pipeline/images/images_cloudrun.py:217-241`).

**Verification recipe — always probe before assuming docs.**

```bash
# Fast: try the bump and let the deploy gate report the real quota.
# If denied, the error message names the actual quota number.
gcloud run services update <service-name> \
  --project=ytfactory-prod-v2 \
  --region=asia-southeast1 \
  --max-instances=<higher-N> \
  --quiet
# Returns: "...allowed: <real-quota>" on quota violation.

# Or query directly via the Cloud Quotas REST API.
gcloud quotas info describe \
  --project=ytfactory-prod-v2 \
  NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion \
  --service=run.googleapis.com \
  --format="value(quotaInfo.dimensions.region,quotaInfo.containerInfo.regionalQuota,quotaInfo.dimensions)"
```

**To unlock workers=4 per-render fan-out** (would halve stage 6
again from ~17 s → ~10 s on a 7-image Short):

```bash
# 1. File a quota request (GPU quotas are NOT self-service-eligible
#    — manual review, typically 24-48h).
gcloud beta quotas preferences create \
  --service=run.googleapis.com \
  --quota-id=NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion \
  --dimensions=region=asia-southeast1 \
  --preferred-value=6 \
  --justification="Per-render image fan-out × cross-render bulk composition; \
    production batch render windows; cost impact ~\$10-50/mo (compute neutral, \
    cold-load amplification only — see docs/cloud_cost_2026_05_11.md)." \
  --project=ytfactory-prod-v2

# 2. After grant, bump cloud/image-flux2-klein/deploy.sh max-instances=4+
# 3. Redeploy via cloud/image-flux2-klein/deploy.sh
# 4. Update the doc table above + docs/pipeline_latency_2026.md +
#    docs/parallel_per_beat_fanout.md "Worker-count math" section.
```

**Stale references caught + fixed 2026-05-11 (search recipe for
the next sweep):**

```bash
grep -rnE "5.{0,2}GPU.{0,8}quota|6.{0,2}GPU.{0,8}quota|3 services × 2|matches our 5-GPU" \
  docs/ pipeline/ cloud/ --include='*.md' --include='*.py' --include='*.sh'
```

Hits as of 2026-05-11 (left intact — they're TTS deploy-script
math comments where "3 services × 2 = 6 GPUs" describes
**capacity** not quota; a single banner "real quota is 3" at the
top of each TTS deploy.sh would be the right fix when those
services next change). Tracked as ONE-OFF in
`.claude/skills/update-docs/learnings/_index.md`.

## Why this matters for ops

Every minute the laptop spends waiting for a deploy is a minute the
agent loop / state sync / engage worker is on stale code. The
self-service quota path replaces "file ticket → wait days" with "one
gcloud command + 60 s wait" for the common CPU-headroom case.

## See also

- [`docs/cloud_service_dep_playbook.md`](cloud_service_dep_playbook.md) —
  the upstream dependency-resolution playbook applied for every new
  Cloud Run service.
- [`docs/full_cloud_cutover_2026_05_09.md`](full_cloud_cutover_2026_05_09.md) —
  service mix (always-warm flux2-klein + tts services) that
  necessitated the bump.
- [`docs/parallel_per_beat_fanout.md`](parallel_per_beat_fanout.md) —
  consumer of the GPU quota; "Worker-count math" section explains
  how to extend per-render fan-out width when quota grows.
- [`docs/cloud_cost_2026_05_11.md`](cloud_cost_2026_05_11.md) —
  marginal cost matrix for bumping max-instances (compute-neutral;
  cold-load amplification only).
