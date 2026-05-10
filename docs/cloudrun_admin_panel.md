# Cloud admin panel (`/app/cloud`)

End-to-end runbook for the **Cloud** tab in `web-next` and its
backing daily snapshot. Replaces four short-lived skills
(`cloud-cost`, `cloud-health`, `deploy-cloud-service`, `warm-cloud`)
that were authored on 2026-05-10 and immediately retired the same
day for not fitting the project's `/make-*` skill taxonomy. Their
functionality consolidated into:

- **Backend:** `pipeline/cloud/` (one importable module — used by
  the FastAPI control plane, the daily cron, and the renderer warm
  hook).
- **API:** `control/routes/cloud_routes.py` — mounted in
  `control/server_dev.py` (and the production `web/server.py`).
- **Frontend:** `web-next/app/app/cloud/` — three stacked sections
  (Health, Cost, Deploys).
- **Cron:** `scripts/cloud_daily_snapshot.py` +
  `control/com.ytfactory.cloud-snapshot.plist` (daily at 02:00).

## What you get

| Section | What it shows | Data source | Refresh cadence |
|---|---|---|---|
| **Health** | Per-service red/yellow/green pill, latency, warm_s, cold flag, last error. Filter chips by kind. | Live `/readyz` curl per service (parallel) — see `pipeline/cloud/health.py` | Every 30 s while tab is visible (`useVisiblePoll`) |
| **Cost** | Today / MTD / drift-flag StatCards, 30-day stacked-bar `$/day` chart, per-service breakdown table | Daily snapshot from BigQuery billing export — see `pipeline/cloud/cost.py` | Daily 02:00 cron, on-demand via Refresh |
| **Deploys** | Last build per service (status pill, image digest, link to log), 6-step playbook prep status | `gcloud builds list` + `cloud/<svc>/.deploy_prep/` markers — see `pipeline/cloud/deploys.py` | Daily 02:00 cron, on-demand via Refresh |

Two action buttons in the page header:

- **Refresh** — calls `POST /api/cloud/refresh`, runs `snapshot_all()`
  on the spot.
- **Warm** — calls `POST /api/cloud/warm`, fires
  `cloud/warm_{tts,image}_services.sh` against the default chatterbox
  + flux pair. Pass `?channel=<slug>` for a channel-specific warm.

## One-time setup

### 1. BigQuery billing export (required for the Cost section)

By default the Cost section shows "Cost data not yet wired" because
the BigQuery export isn't enabled. Enable it once:

```
GCP Console → Billing → Billing export → Detailed usage cost
  → BigQuery export
  → Project: ytfactory-prod-v2
  → Dataset:  billing_export   (create if missing — region = US is fine)
```

Wait ~24 hours for the first table partition to populate. The standard
table name is `gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`
(dashes → underscores). Our billing account id is
`012E39-E4ECEB-7F119F` (documented in `docs/cloudrun_tts.md`).

Set the env override only if your dataset name differs:

```bash
# .env (override only if you renamed the dataset)
BIGQUERY_BILLING_DATASET=billing_export
BILLING_ACCOUNT_ID=012E39-E4ECEB-7F119F
```

The runtime credentials need `roles/bigquery.dataViewer` on that
dataset. Laptop-dev runs use your gcloud user creds; the production
Cloud Run service needs the same role on its service account.

### 2. Install the daily snapshot cron

```bash
# Load the launchd plist (runs daily at 02:00 local).
cp control/com.ytfactory.cloud-snapshot.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.ytfactory.cloud-snapshot.plist

# Verify it's scheduled
launchctl list | grep com.ytfactory.cloud-snapshot

# Tail the cron output
tail -f /tmp/ytfactory-cloud-snapshot.log
```

To unload: `launchctl unload -w ~/Library/LaunchAgents/com.ytfactory.cloud-snapshot.plist`.

### 3. Optional: prep dir bootstrap (for the Deploys section)

The Deploys section's prep-status column shows `0/6` for every service
until you start using `cloud/<svc>/.deploy_prep/stepN.ok` markers when
running through the 6-step playbook in
`docs/cloud_service_dep_playbook.md`. Each step gets a marker file:

```bash
mkdir -p cloud/tts-chatterbox/.deploy_prep
touch cloud/tts-chatterbox/.deploy_prep/step1_upstream_reqs.ok
touch cloud/tts-chatterbox/.deploy_prep/step2_grep_imports.ok
# ... etc
```

The marker file content is irrelevant — only existence is checked.

## Where data lands

```
data/_bench/
  cloud_health/YYYY-MM-DD.json     # daily probe snapshot
  cloud_health_baseline.json        # rolling 7-day p95 latency per service
  cloud_cost/YYYY-MM-DD.json       # daily BigQuery billing rollup
  cloud_deploys/YYYY-MM-DD.json    # daily gcloud builds + prep status
```

These are cross-channel state per `docs/channel_layout.md`, hence
`data/` rather than any per-channel root.

## API surface

All endpoints mounted under `/api/cloud/*` and gated by
`YTFACTORY_AGENT_TOKEN` Bearer (off Cloud Run) or trusted IAM (on
Cloud Run, when `K_SERVICE` env is set). The Next.js proxy at
`web-next/app/api/[...path]/route.ts` mints an ID token automatically
in production.

| Verb | Path | Returns |
|---|---|---|
| GET  | `/api/cloud/services` | Catalog (15 services) |
| GET  | `/api/cloud/health` | Live probe (no cache) |
| GET  | `/api/cloud/cost?days=30` | Latest cost snapshot, optionally trimmed |
| GET  | `/api/cloud/deploys` | Latest deploy snapshot |
| POST | `/api/cloud/refresh` | Force `snapshot_all()` now |
| POST | `/api/cloud/warm?channel=<slug>` | Fire warm scripts in parallel |

## Adding a new service to the panel

Add a row to `pipeline/cloud/services.py::_SERVICES`:

```python
Service(
    name="ytfactory-image-newmodel",
    short="newmodel",
    kind=ServiceKind.IMAGE,
    env_var="CLOUDRUN_IMAGE_NEWMODEL_URL",
    fallback_url="",  # leave empty to require .env
    healthz_path="/readyz",
    notes="Brief one-liner for the panel tooltip.",
),
```

That's it — Health, Cost, Deploys all pick it up next snapshot. The
warm hook uses provider strings from the channel YAML, so wire those
in `pipeline/cloud/warm.py::_TTS_PROVIDER_TO_TARGET` /
`_IMAGE_PROVIDER_TO_TARGET` only if you want render-time pre-warm for
the new service.

## Pre-warm hook (replaces `/warm-cloud`)

Every renderer entrypoint calls `pipeline.cloud.warm.warm_async()`
immediately after argparse:

- `pipeline/render/shorts.py`
- `pipeline/render/long_form.py`
- `pipeline/render/footage_only.py`
- `pipeline/render/sports_doc.py`

Daemon thread, no-op when no `CLOUDRUN_*_URL` is set, never blocks
the renderer. Skills no longer need to fire `/warm-cloud` — that
step in `docs/cloud_prerender_hook.md` is now automatic.

For manual / cron / panel-button warming, call:

```python
from pipeline.cloud.warm import warm_for_channel
report = warm_for_channel("mystoriesanimated")
```

…or hit `POST /api/cloud/warm?channel=mystoriesanimated`.

## Cross-links

- `docs/cloud_prerender_hook.md` — what skills still need to do
  before a render (the routing-assertion step survives; the warm
  step is now in the renderer; the health step is now this panel)
- `docs/cloud_service_dep_playbook.md` — the 6-step deploy checklist
  that the prep markers track
- `docs/cloudrun_tts.md`, `docs/cloudrun_image.md`,
  `docs/cloudrun_higgs.md`, `docs/cloudrun_render_worker.md` —
  per-service runbooks
- `pipeline/cloud/__init__.py` — submodule index
