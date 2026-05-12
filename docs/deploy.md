# Production deploy & cutover

> **Status (2026-05-10):** ⚠ This doc was the **scaffolded plan** for
> what became Phase 4 of the cloud cutover. **The cutover is
> complete** — `ytfactory-control` has been retired and consolidated
> into `ytfactory-web`. The current 7-service / 2-job production
> reality is documented in
> [`docs/full_cloud_cutover_2026_05_09.md`](./full_cloud_cutover_2026_05_09.md)
> (which IS current). Read this doc only as historical context for
> the original 3-service plan; the architecture diagram + service
> table below describe the **pre-Phase-4 design** and no longer
> reflect production. The single canonical deploy command today is
> `./cloud/web-server/deploy.sh` (see `docs/architecture.md`
> Deployment section).

> **Original status (scaffolded 2026-05-09):** Ready to deploy. Two
> Cloud Run services + one Cloud Run Job, all in `ytfactory-prod-v2` /
> `asia-southeast1`.

## Architecture

```
                        Internet
                            │
                ┌───────────┴───────────┐
                ▼                       │
  ┌──────────────────────┐              │
  │ ytfactory-web        │              │  (a)
  │ (Cloud Run service)  │              │
  │ Next.js standalone   │              │
  │ Linear/Vercel UI     │              │
  └──────────┬───────────┘              │
             │ /api/* rewrites          │
             ▼                          │
  ┌──────────────────────┐              │
  │ ytfactory-control    │ ─── (b) ─────┘
  │ (Cloud Run service)  │
  │ FastAPI control plane│
  │ — channels API       │
  │ — render dispatch    │
  │ — job/queue API      │
  │ — Firestore + GCS    │
  └──────────┬───────────┘
             │ trigger Cloud Run Job per render
             ▼
  ┌──────────────────────┐
  │ ytfactory-render-    │
  │   worker-v2          │
  │ (Cloud Run JOB)      │
  │ — reads Firestore    │
  │ — runs 7-stage       │
  │   pipeline           │
  │ — writes Firestore + │
  │   GCS                │
  └──────────────────────┘
       │           │
       │ TTS       │ Image
       ▼           ▼
  cloudrun-      cloudrun-
  chatterbox     flux2-klein
  / indicparler  (existing)
  (existing)
```

(a) public landing + studio. (b) Same domain via the web service's
`/api/*` rewrite — the user never sees the control URL.

## Deploy order (one-time)

```bash
cd /Users/rohit/ytFactory

# 1. Render-worker JOB (the substrate that replaces the laptop).
./cloud/render-worker-v2/deploy.sh

# 2. Control plane (depends on the JOB existing for cloudrun mode).
./cloud/control-plane/deploy.sh

# 3. Web (auto-discovers the control-plane URL via gcloud).
./cloud/web-next/deploy.sh
```

Each script prints the deployed URL. The web URL is what you share
with users.

## Subsequent updates

Re-running any deploy.sh rebuilds the image and rolls the service
forward. Cloud Run keeps the previous revision; flip back with
`gcloud run services update-traffic`.

## Costs (steady state, low traffic)

| Component | Cost/month |
|---|---|
| ytfactory-web (min-instances=1, 512Mi) | ~$5 |
| ytfactory-control (min-instances=1, 1Gi) | ~$8 |
| ytfactory-render-worker-v2 (per-execution) | ~$0.02/render |
| Firestore (jobs collection) | free tier |
| GCS (mp4s, 7-day lifecycle) | <$1 |
| Existing TTS / image GPU services (already deployed) | per docs/cloudrun_*.md |

Hard ceiling stays around the **<$10/mo at low traffic** target from
README.md.

## Cutover from legacy

The legacy site at the existing prod URL (https://ytfactory-control-…
.run.app) serves `web/static/{landing,index,chat,renders}.html`. The
new product is a clean parallel deploy on a NEW URL. To cut over:

1. Test the new URL end-to-end (create → render → preview).
2. Set up a domain (e.g., `ytfactory.app`) with two routes:
   - `/` → new web service
   - `/legacy/*` → old control service (until traffic dies)
3. Update README.md "Live" section to point at the new URL.
4. After a week, retire the legacy service:
   `gcloud run services delete ytfactory-control --region asia-southeast1`
   (the old one — the new one is `ytfactory-control` too; rename one
   if you want both alive simultaneously).

## Rollback

```bash
# Roll any service back to the previous revision:
gcloud run services update-traffic ytfactory-web \
    --to-revisions=PREVIOUS=100 --region asia-southeast1
```

For the render path: `unset YTFACTORY_RENDER_BACKEND` on the control
plane (or `gcloud run services update --update-env-vars=YTFACTORY_RENDER_BACKEND=sim`)
to fall back to the in-process sim worker.

## Smoke test after deploy

```bash
WEB=https://ytfactory-web-XXX.asia-southeast1.run.app
CTRL=https://ytfactory-control-XXX.asia-southeast1.run.app

# Direct API health
curl -s "$CTRL/api/health" | jq

# Through the web proxy
curl -s "$WEB/api/health" | jq
curl -s "$WEB/api/channels" | jq '.channels | length'  # → 8

# UI
open "$WEB"
```

## Files

- `cloud/web-next/Dockerfile` + `deploy.sh` — Next.js production image
- `cloud/control-plane/Dockerfile` + `requirements.txt` + `deploy.sh`
  — FastAPI control plane production image
- `cloud/render-worker-v2/*` — render JOB (per docs/cloudrun_render_worker.md)

---

## 2026-05-11 — Verify deploy actually fired

`cloud/<svc>/deploy.sh` is `gcloud builds submit ... && gcloud run
deploy ...` in series. If the wrapper calling the script loses its
stdout (background job that detaches, async shell that disconnects
its tee, parent shell that exits and SIGHUPs), **the build
completes successfully on Cloud Build's side but the local script
never reaches `gcloud run deploy`**. Build SUCCESS ≠ service
updated.

### How to detect this

```bash
# Right after a deploy.sh wrapper exits, confirm BOTH steps fired:
serving=$(gcloud run services describe "<svc>" \
            --region=asia-southeast1 --project=ytfactory-prod-v2 \
            --format='value(status.traffic[0].revisionName)')
latest=$(gcloud run services describe "<svc>" \
            --region=asia-southeast1 --project=ytfactory-prod-v2 \
            --format='value(status.latestReadyRevisionName)')

if [[ "$serving" != "$latest" ]]; then
  echo "DEPLOY DID NOT FIRE — serving=$serving latestReady=$latest"
fi
```

### Recovery

If only the build fired, run the deploy step manually using the
image tag from the SUCCESS build:

```bash
IMAGE=$(gcloud builds describe "${BUILD_ID}" --format='value(images[0])')
gcloud run deploy "${SERVICE}" --image="${IMAGE}" \
  --project=ytfactory-prod-v2 --region=asia-southeast1 \
  [...all the flags from the original deploy.sh...]
```

Save the original deploy.sh's flag list to `cloud/<svc>/deploy.sh`
itself — that's the deploy contract; matching it manually is
mechanical.

### Better fix (TODO)

Refactor every `cloud/<svc>/deploy.sh` to either:
- (a) split into `build.sh` + `deploy.sh` so each is independently
  re-runnable from a build artifact tag, OR
- (b) at the bottom of `deploy.sh`, ALWAYS print
  `==> Deployed: <url>` with the deploy step's exit code; if the
  wrapper sees only `==> Built:` with no `==> Deployed:`, the
  deploy step was skipped.

### See also

- Memory: `feedback_deploy_wrapper_lost_stdout_silent.md`
- `feedback_post_deploy_live_smoke_m2m.md` — what to run AFTER
  confirming the deploy fired (this rule is about confirming the
  deploy fired in the first place).

---

## 2026-05-12 — `gcloud` "Reauthentication failed" with fresh ADC: use `CLOUDSDK_AUTH_ACCESS_TOKEN`

If `gcloud run services describe …` (or any other gcloud command)
fails with:

```
ERROR: There was a problem refreshing your current auth tokens:
Reauthentication failed. cannot prompt during non-interactive
execution.
Please run:
  $ gcloud auth login
```

**don't immediately re-login.** First check whether ADC is alive:

```bash
gcloud auth application-default print-access-token | head -c 40
# If a token comes back in <2 seconds, ADC is fresh.
```

ADC tokens are minted from a **separate credential subsystem** than
gcloud's user-account creds. The org's reauth policy applies to
the user-account subsystem (1h typical), but ADC tokens minted via
`application-default` survive reauth windows for hours-to-days
because they ride a different refresh-token path.

### Bypass

**Best (2026-05-12 onwards): no manual action needed for any
`cloud/<svc>/deploy.sh`.** Every service deploy script now sources
`cloud/_shared/auth_setup.sh` immediately after `set -euo pipefail`,
which transparently exports `CLOUDSDK_AUTH_ACCESS_TOKEN` from ADC. So
this just works:

```bash
bash cloud/render-worker-v2/deploy.sh   # auth bypass auto-applied
```

The script prints `==> auth: using ADC access token …` so the operator
can see the bypass fired. If ADC is also dead, it exits cleanly with
ONE clear "run `gcloud auth application-default login`" message
instead of dying mid-build with "Reauthentication failed".

For one-off `gcloud` invocations OUTSIDE a deploy script (manual
`gcloud run jobs execute`, debugging, etc.):

```bash
export CLOUDSDK_AUTH_ACCESS_TOKEN=$(gcloud auth application-default print-access-token)
gcloud run services describe ytfactory-web-next \
  --region=asia-southeast1 --project=ytfactory-prod-v2 \
  --format='value(status.url)'
# ⇒ works
```

Or wrap the whole command:

```bash
bash -c 'source cloud/_shared/auth_setup.sh && gcloud …'
```

`CLOUDSDK_AUTH_ACCESS_TOKEN` makes gcloud use that token verbatim
and skip its own user-account refresh path entirely.

### Adding a new `cloud/<svc>/deploy.sh`

Every new deploy script MUST start with:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

# … rest of script …
```

The cloud-service playbook (`docs/cloud_service_dep_playbook.md`)
references this as a mandatory step.

### When the bypass DOESN'T work

- Operations needing an **ID token** (Cloud Run service-to-service
  invocation), not an access token. Use Python's `google-auth`
  library or follow `feedback_cloudrun_auth_gcloud_hang_bypass.md`.
- Operations requiring **service-account** signing (signed URLs,
  etc.). User-account ADC can't sign on behalf of an SA without
  explicit impersonation flags.
- If both `gcloud auth list` AND
  `gcloud auth application-default print-access-token` fail → both
  subsystems are dead, re-auth is now genuinely required.

### Class-of-bug for agent-driven deploys

Burning a session asking "please re-login" when ADC is fresh is a
recurring frustration vector — the user has a saved token, the
agent silently ignores it, the deploy stalls. Memory:
`feedback_gcloud_reauth_use_adc_bypass.md`. The triage flowchart
lives there too.

### See also

- `feedback_cloudrun_auth_gcloud_hang_bypass.md` — Python subprocess
  surface of the same root cause (gcloud user-auth fragility)
- gcloud docs:
  https://cloud.google.com/sdk/docs/authorizing#user-accounts
