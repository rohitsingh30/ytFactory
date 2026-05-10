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
