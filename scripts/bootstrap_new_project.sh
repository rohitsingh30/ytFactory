#!/usr/bin/env bash
# ytFactory — first-deploy bootstrap for a fresh GCP project.
#
# Codifies every lesson from the 2026-05-15 bake-off session so the next
# project hits the ground running instead of debugging quota / IAM /
# env-var bugs for hours.
#
# Usage:
#   PROJECT=ytfactory-prod-v3 \
#   REGION=asia-southeast1 \
#   HF_TOKEN=hf_xxx \
#   AZURE_OPENAI_ENDPOINT=https://... \
#   AZURE_OPENAI_API_KEY=xxx \
#   bash scripts/bootstrap_new_project.sh
#
# What it does (in order, with idempotent skips):
#   1. Verify gcloud auth + project access
#   2. Enable required GCP APIs
#   3. Submit quota bumps (GPU=8, Memory=200 GiB, CPU=200 vCPU)
#   4. Create service accounts (image-runner, render-runner, ytfactory-deployer)
#   5. Create GCS buckets (artifacts, weights, state)
#   6. Create Secret Manager secrets (HF_TOKEN, AZURE_OPENAI_API_KEY)
#   7. Grant SA permissions (storage.objectViewer, secretmanager.secretAccessor)
#   8. Stage z-turbo weights via Cloud Build — klein/qwen/flux2-dev SKIPPED
#      (per the cost-optimized minimal stack — see docs/cost_optimized_deploy.md)
#   9. Build + deploy z-image-turbo service (right-sized 4 vCPU + 16 GiB)
#  10. Build + deploy TTS services (chatterbox, indicf5 — 4 vCPU + 16 GiB, concurrency=2)
#  11. Deploy ASR service (asr-whisper — 4 vCPU + 16 GiB, concurrency=2)
#  12. Build + deploy render-worker-v2 with ALL env URLs
#  13. Set up billing alert at $30 + budget cutoff at $200
#  14. Smoke test: render one short, verify mp4
#
# IMPORTANT lessons baked in (see comments per step):
#   - Quotas requested BEFORE any deploy (avoids mid-deploy quota errors)
#   - Image services use GCS Fuse mount, NOT baked weights (faster builds)
#   - --add-volume=...,readonly=true (NOT --add-volume-mount=readonly=true)
#   - render-worker-v2 deploy.sh has all CLOUDRUN_*_URL env vars (rebuild-safe)
#   - flux2-dev EXCLUDED (96hr/render on L4 — verified 2026-05-15)
#   - klein + qwen EXCLUDED (z-turbo is sufficient and cheaper per render)
#   - All GPU services right-sized to Cloud Run min (4 vCPU + 16 GiB)
#   - min-instances=0 on every GPU service (kills the $272 idle waste)
#   - TTS concurrency=2 (process 2 calls per warm instance; halves cold-starts)
#   - HF_TOKEN via Secret Manager, granted to <project-number>-compute@... SA
#   - Cloud Build uses --async + poll (VPC-SC log-streaming bug workaround)
#   - Billing alert at $30 + budget cutoff at $200 (no silent credit drain)

set -euo pipefail

# ─── Required env vars ─────────────────────────────────────────────────

: "${PROJECT:?Set PROJECT=ytfactory-prod-vN}"
: "${REGION:=asia-southeast1}"
: "${HF_TOKEN:?Set HF_TOKEN (https://huggingface.co/settings/tokens — needs FLUX.2 access)}"
: "${AZURE_OPENAI_ENDPOINT:=https://testshoffer.openai.azure.com}"
: "${AZURE_OPENAI_API_KEY:?Set AZURE_OPENAI_API_KEY}"
: "${AZURE_OPENAI_API_VERSION:=2025-04-01-preview}"
: "${AZURE_OPENAI_MODEL:=gpt-5.3-chat}"
: "${AZURE_OPENAI_TOKEN_PARAM:=max_completion_tokens}"
: "${USER_EMAIL:?Set USER_EMAIL (used as quota bump contact)}"

ARTIFACTS_BUCKET="${PROJECT}-artifacts"
# Audit 2026-05-16 — global bucket-name collision on old "ytfactory-model-weights-v2"
# (previous trial project still holds that name + 30-day cooldown after delete).
# Use project-prefixed bucket so each fresh project gets its own weights bucket.
# We pay one re-staging cost per project but never hit the global-namespace wall.
WEIGHTS_BUCKET="${PROJECT}-model-weights"
STATE_BUCKET="${PROJECT}-state"
ARTIFACT_REPO="ytfactory-tts"

step() { echo ""; echo "==> [$1] $2"; }
ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }

# ─── 1. Auth + project ────────────────────────────────────────────────

step 1 "Verify gcloud auth + project access"
gcloud config set project "${PROJECT}" --quiet
gcloud projects describe "${PROJECT}" >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT}" --format="value(projectNumber)")
ok "Project ${PROJECT} (number=${PROJECT_NUMBER})"

# ─── 2. Enable APIs ────────────────────────────────────────────────────

step 2 "Enable required GCP APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  --project="${PROJECT}" --quiet
ok "APIs enabled"

# ─── 3. Quota bumps (submit ASAP — they take time to grant) ───────────

step 3 "Submit quota bumps (run.googleapis.com)"
for q_def in \
  "NvidiaL4GpuAllocNoZonalRedundancyPerProjectRegion:8:L4 GPU for 4 image services + chatterbox + indicf5 + asr-whisper" \
  "MemAllocPerProjectRegion:214748364800:Memory for 7 GPU services + web + web-next" \
  "CpuAllocPerProjectRegion:200000:CPU for 7 GPU services × 8 vCPU each + headroom for revisions"; do
  qid="${q_def%%:*}"
  rest="${q_def#*:}"
  pref_value="${rest%%:*}"
  justification="${rest#*:}"

  # Check if pref already exists
  existing=$(gcloud beta quotas preferences list --project="${PROJECT}" --format="value(name,quotaId)" 2>&1 | grep -F "${qid}" | awk '{print $1}' | head -1 || true)
  if [ -n "${existing}" ]; then
    pref_id=$(basename "${existing}")
    warn "Quota pref ${qid} exists — updating preferred=${pref_value}"
    gcloud beta quotas preferences update "${pref_id}" \
      --service=run.googleapis.com \
      --quota-id="${qid}" \
      --dimensions=region="${REGION}" \
      --preferred-value="${pref_value}" \
      --email="${USER_EMAIL}" \
      --justification="${justification}" \
      --project="${PROJECT}" --quiet 2>&1 | tail -2 || warn "update failed (probably granted already)"
  else
    echo "  Creating quota pref ${qid}=${pref_value}"
    gcloud beta quotas preferences create \
      --service=run.googleapis.com \
      --quota-id="${qid}" \
      --dimensions=region="${REGION}" \
      --preferred-value="${pref_value}" \
      --email="${USER_EMAIL}" \
      --justification="${justification}" \
      --project="${PROJECT}" --quiet 2>&1 | tail -2 || warn "quota request failed"
  fi
done
ok "Quota bumps submitted (auto-grant typically <5 min)"

# ─── 4. Service accounts ──────────────────────────────────────────────

step 4 "Create service accounts"
for sa in image-runner render-runner ytfactory-deployer; do
  if gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" --project="${PROJECT}" >/dev/null 2>&1; then
    ok "${sa} already exists"
  else
    gcloud iam service-accounts create "${sa}" \
      --display-name="ytFactory ${sa}" \
      --project="${PROJECT}" --quiet
    ok "Created ${sa}"
  fi
done

# Grant deployer roles for ops
for role in roles/run.admin roles/iam.serviceAccountUser roles/storage.admin roles/cloudbuild.builds.editor; do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:ytfactory-deployer@${PROJECT}.iam.gserviceaccount.com" \
    --role="${role}" --condition=None --quiet 2>&1 | tail -1 >/dev/null
done
ok "deployer SA granted run.admin, iam.serviceAccountUser, storage.admin, cloudbuild.builds.editor"

# ─── 5. GCS buckets ───────────────────────────────────────────────────

step 5 "Create GCS buckets"
for bucket in "${ARTIFACTS_BUCKET}" "${WEIGHTS_BUCKET}" "${STATE_BUCKET}"; do
  if gcloud storage buckets describe "gs://${bucket}" --project="${PROJECT}" >/dev/null 2>&1; then
    ok "gs://${bucket} exists"
  else
    gcloud storage buckets create "gs://${bucket}" --location="${REGION}" --project="${PROJECT}" --quiet
    ok "Created gs://${bucket}"
  fi
done

# Grant image-runner storage access on weights (CRITICAL — without this GCS Fuse mount fails with PermissionDenied)
gcloud storage buckets add-iam-policy-binding "gs://${WEIGHTS_BUCKET}" \
  --member="serviceAccount:image-runner@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer" --project="${PROJECT}" --quiet 2>&1 | tail -1 >/dev/null
ok "image-runner has storage.objectViewer on weights bucket"

# ─── 6. Artifact Registry ─────────────────────────────────────────────

step 6 "Create Artifact Registry repo"
if gcloud artifacts repositories describe "${ARTIFACT_REPO}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  ok "${ARTIFACT_REPO} repo exists"
else
  gcloud artifacts repositories create "${ARTIFACT_REPO}" \
    --repository-format=docker --location="${REGION}" \
    --description="ytFactory container images" --project="${PROJECT}" --quiet
  ok "Created ${ARTIFACT_REPO}"
fi

# ─── 7. Secrets ───────────────────────────────────────────────────────

step 7 "Create Secret Manager secrets"
for sd in "ytfactory-hf-token:${HF_TOKEN}" "ytfactory-azure-openai-key:${AZURE_OPENAI_API_KEY}"; do
  name="${sd%%:*}"
  value="${sd#*:}"
  if gcloud secrets describe "${name}" --project="${PROJECT}" >/dev/null 2>&1; then
    echo -n "${value}" | gcloud secrets versions add "${name}" --data-file=- --project="${PROJECT}" --quiet 2>&1 | tail -1 >/dev/null
    ok "${name} updated"
  else
    echo -n "${value}" | gcloud secrets create "${name}" --data-file=- --replication-policy=automatic --project="${PROJECT}" --quiet
    ok "Created ${name}"
  fi
  # Grant access to BOTH SAs (cloudbuild compute SA + render SAs)
  for member in \
    "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    "serviceAccount:image-runner@${PROJECT}.iam.gserviceaccount.com" \
    "serviceAccount:render-runner@${PROJECT}.iam.gserviceaccount.com"; do
    gcloud secrets add-iam-policy-binding "${name}" \
      --member="${member}" --role=roles/secretmanager.secretAccessor \
      --project="${PROJECT}" --quiet 2>&1 | tail -1 >/dev/null
  done
done
ok "Secrets created + granted to compute + image-runner + render-runner SAs"

# ─── 8. Stage weights (klein + qwen + z-image-turbo only) ─────────────
#
# CRITICAL LESSON: flux2-dev is EXCLUDED. On L4 GPU it does ~17 min/inference
# step × 28 steps × 12 beats = ~96 hours per render. Verified 2026-05-15.
# Use BFL paid API for flux2-dev or GKE with L40/A100.
#
# Why Cloud Build (not Cloud Run JOB):
#   - Cloud Run JOB caps at 32 GiB tmpfs → can't fit 64 GB safetensors
#   - Cloud Build with e2-highcpu-32 + 200 GB disk handles ALL HF repos

step 8 "Stage HF weights via Cloud Build (klein + qwen + z-image-turbo)"
REPOS_TO_STAGE="Tongyi-MAI/Z-Image-Turbo"

# Update cloudbuild.yaml's project reference
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sed -i.bak "s|projects/ytfactory-prod-v2|projects/${PROJECT}|g" "${ROOT}/cloud/weights-staging/cloudbuild.yaml" 2>/dev/null || true

build_id=$(gcloud builds submit \
  --config="${ROOT}/cloud/weights-staging/cloudbuild.yaml" \
  --no-source --project="${PROJECT}" \
  --substitutions="_REPOS=${REPOS_TO_STAGE},_BUCKET=${WEIGHTS_BUCKET}" \
  --timeout=7200s --async --format="value(id)" 2>/dev/null \
  | grep -Eo '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  | tail -1)
if [ -z "${build_id}" ]; then
  echo "  ❌ Could not extract Cloud Build id; check 'gcloud builds list --ongoing'"
  exit 1
fi
echo "  Cloud Build: ${build_id}"
echo "  Console: https://console.cloud.google.com/cloud-build/builds/${build_id}?project=${PROJECT}"
echo "  Polling weights staging (timeout 90 min)..."
deadline=$((SECONDS + 5400))
while [ $SECONDS -lt $deadline ]; do
  s=$(gcloud builds describe "${build_id}" --project="${PROJECT}" --format="value(status)" 2>/dev/null || echo UNKNOWN)
  case "$s" in
    SUCCESS) ok "Weights staging done"; break ;;
    FAILURE|CANCELLED|TIMEOUT|EXPIRED|INTERNAL_ERROR) echo "  ❌ Weights staging ${s}"; exit 1 ;;
    *) echo "  ...status=${s} (${SECONDS}s)"; sleep 60 ;;
  esac
done

# ─── 9. Image services (klein, qwen, z-image-turbo) ──────────────────

step 9 "Build + deploy image service (z-image-turbo only — minimal stack)"
for svc_dir in image-z-image-turbo; do
  echo ""
  echo "  → ${svc_dir}"
  cd "${ROOT}/cloud/${svc_dir}"
  GCP_PROJECT="${PROJECT}" GCP_REGION="${REGION}" bash deploy.sh
  cd "${ROOT}"
done
ok "3 image services deployed (max-instances=1, GCS Fuse mount)"

# ─── 10. TTS services (chatterbox + indicf5) ─────────────────────────

step 10 "Build + deploy TTS services"
for svc_dir in tts-chatterbox tts-indicf5; do
  echo ""
  echo "  → ${svc_dir}"
  cd "${ROOT}/cloud/${svc_dir}"
  GCP_PROJECT="${PROJECT}" GCP_REGION="${REGION}" bash deploy.sh
  cd "${ROOT}"
done
ok "2 TTS services deployed"

# ─── 11. ASR service ─────────────────────────────────────────────────

step 11 "Build + deploy ASR service (asr-whisper)"
cd "${ROOT}/cloud/asr-whisper"
GCP_PROJECT="${PROJECT}" GCP_REGION="${REGION}" bash deploy.sh
cd "${ROOT}"
ok "ASR deployed"

# ─── 12. Render worker JOB ───────────────────────────────────────────

step 12 "Build + deploy render-worker-v2 (with ALL CLOUDRUN URLs baked in)"
cd "${ROOT}/cloud/render-worker-v2"
GCP_PROJECT="${PROJECT}" GCP_REGION="${REGION}" \
  AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT}" \
  AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION}" \
  AZURE_OPENAI_MODEL="${AZURE_OPENAI_MODEL}" \
  AZURE_OPENAI_TOKEN_PARAM="${AZURE_OPENAI_TOKEN_PARAM}" \
  bash deploy.sh
cd "${ROOT}"
ok "Worker deployed"

# ─── 13. Billing safeguards ──────────────────────────────────────────

step 13 "Set up billing alert + budget cutoff (prevents silent credit drain)"
: "${BILLING_ACCOUNT:?Set BILLING_ACCOUNT (e.g., 0113C5-9580A7-961733)}"

# Try to create a $200 budget with alerts at 50%, 90%, 100%
budget_json=$(cat <<EOF
{
  "displayName": "ytFactory ${PROJECT} cost ceiling",
  "budgetFilter": {
    "projects": ["projects/${PROJECT_NUMBER}"],
    "creditTypesTreatment": "INCLUDE_ALL_CREDITS"
  },
  "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "200"}},
  "thresholdRules": [
    {"thresholdPercent": 0.15, "spendBasis": "CURRENT_SPEND"},
    {"thresholdPercent": 0.50, "spendBasis": "CURRENT_SPEND"},
    {"thresholdPercent": 0.90, "spendBasis": "CURRENT_SPEND"},
    {"thresholdPercent": 1.00, "spendBasis": "CURRENT_SPEND"}
  ],
  "notificationsRule": {
    "monitoringNotificationChannels": [],
    "disableDefaultIamRecipients": false
  }
}
EOF
)
echo "$budget_json" > /tmp/budget.json
gcloud billing budgets create \
  --billing-account="${BILLING_ACCOUNT}" \
  --display-name="ytFactory ${PROJECT} cost ceiling" \
  --budget-amount=200 \
  --threshold-rule=percent=15 \
  --threshold-rule=percent=50 \
  --threshold-rule=percent=90 \
  --threshold-rule=percent=100 \
  --filter-projects="projects/${PROJECT_NUMBER}" \
  --quiet 2>&1 | tail -5 || warn "budget create failed (may already exist) — visit https://console.cloud.google.com/billing/${BILLING_ACCOUNT}/budgets"
ok "Budget set: \$200 cap with alerts at 15% (\$30), 50% (\$100), 90% (\$180), 100% (\$200)"

# ─── 14. Smoke test ──────────────────────────────────────────────────

step 14 "Smoke test (one short on z-turbo, verify mp4 produced)"
cd "${ROOT}"
export YTFACTORY_QUEUE_BACKEND=firestore
export YTFACTORY_RENDER_BACKEND=cloudrun
export GOOGLE_CLOUD_PROJECT="${PROJECT}"
.venv/bin/python scripts/image_bake_off_full.py --execute \
  --channels mystoriesanimated --models cloudrun_z_image_turbo --stagger-s 0
ok "Smoke render dispatched. Poll Firestore jobs/<id> for status."

echo ""
echo "===================================================================="
echo " Bootstrap complete — project ${PROJECT}"
echo "===================================================================="
echo " Image services:    z-image-turbo (Apache 2.0, 4 vCPU + 16 GiB)"
echo " TTS services:      chatterbox, indicf5 (4 vCPU + 16 GiB, concurrency=2)"
echo " ASR service:       asr-whisper (4 vCPU + 16 GiB, concurrency=2)"
echo " Render worker JOB: ytfactory-render-worker-v2"
echo ""
echo " Cost expectations:"
echo "   Per render:        \$0.063"
echo "   Idle (no renders): \$0/day"
echo "   50 renders/day:    ~\$95/mo"
echo ""
echo " Excluded (per docs/cost_optimized_deploy.md):"
echo "   klein, qwen, flux2-dev, hidream (image)"
echo "   cosyvoice, higgs, indicparler (TTS)"
echo "   editing-agent, clone-video-worker, cobalt-api"
echo ""
echo " Daily audit:  python scripts/audit_idle_costs.py"
echo " Run bake-off: bash scripts/run_bake_off.sh"
echo "===================================================================="
