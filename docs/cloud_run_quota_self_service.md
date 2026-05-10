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
- `nvidia_l4_gpu_allocation_no_zonal_redundancy` — 3 GPUs, default.
  When/if we add a 4th L4 GPU service, request via the same recipe
  but expect manual review (GPU quotas typically aren't
  self-service-eligible).

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
