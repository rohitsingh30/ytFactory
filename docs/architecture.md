# ytFactory Architecture

**Production URL:** https://ytfactory-control-767262167641.us-central1.run.app
(Cloud Run, project `ytfactory-prod`, region us-central1, revision auto-deployed
from this repo via `gcloud run deploy --source .`).

The target design after migration. See `legacy_pipeline.md` for what
still runs while the migration is in progress.

## Goals

1. Public end-to-end product: visitor chats → finished Short.
2. Cloud-side chat + control plane survives laptop sleep.
3. Heavy GPU/MLX work stays on the laptop (free) instead of cloud GPU ($$).
4. Cost ceiling **<$10/month** at low traffic; no surprise bills.
5. Outbound-only laptop networking — no inbound ports, no tunnels.

## High-level diagram

```
Public visitor
  │
  ▼
https://ytfactory.<domain>           ← Cloud Run (control plane)
  │  POST /api/chat ─────────────►  ChatService → Azure OpenAI (gpt-4o-mini)
  │                                   extracts {short_proposal} JSON
  │  POST /api/jobs (proposal) ──►  Firestore queue (jobs + tasks)
  │
  │                                  Cloud Run jobs (light workers)
  │                                   ├─ claude CLI: script / cast / critic / rewrite
  │                                   ├─ Reddit / Wikipedia / YT scrapers
  │                                   └─ youtube_upload (owner only)
  │
  │   ◄─ HTTPS long-poll lease ─────  Laptop agent (heavy workers)
  │   (heartbeat 15s, lease 30s)      ├─ image gen (mflux / diffusers)
  │                                   ├─ tts (Kokoro / F5)
  │                                   ├─ asr (whisper_mlx)
  │                                   ├─ ffmpeg compose
  │                                   └─ footage trim
  │
  │  Artifacts round-trip via GCS (single source of truth)
  ▼
Visitor sees preview + download (+ owner: publish to YT)
```

## Components

### Control plane (`control/`) — Cloud Run

Deployed as a single FastAPI service. Always-on, scale-to-zero, free
tier covers expected traffic.

- `server.py` — entry point, mounts `web/static/`
- `chat.py` + `chat_service.py` — ported from `~/trading/`, swap
  `strategy_proposal` → `short_proposal` schema
- `queue.py` — Firestore-backed task queue with leases
- `scheduler.py` — Cloud Scheduler webhooks (cron, part-2 watcher)
- `auth.py` — Firebase Identity Platform Google sign-in; tiers:
  anonymous / signed-in / owner
- `rate_limit.py` — per-IP, per-user, per-session, daily cost caps
- `storage.py` — GCS adapter (signed URLs for upload/download)
- `telemetry.py` — keeps the existing telemetry endpoints alive

### Light workers (`workers/light/`) — Cloud Run jobs

One container, dispatch on `TASK_KIND` env var. Pure I/O, scale to
zero, ~$0–2/mo total.

- `script.py` `critic.py` `cast.py` `rewrite.py` — claude CLI calls
- `pull_stories.py` `wiki_research.py` — scrapers
- `youtube_upload.py` — uses owner's stored OAuth refresh token

### Heavy workers (`workers/heavy/`) — laptop only

Pure functions that take a payload from GCS and write outputs to GCS.
Invoked exclusively by the laptop agent's runner.

- `images.py` — mflux / Z-Image-Turbo / SDXL via diffusers
- `tts.py` — Kokoro + F5-TTS-MLX
- `asr.py` — whisper_mlx
- `compose.py` — ffmpeg slideshow + Ken Burns
- `footage.py` — broadcast clip trim + blurred-letterbox

### Laptop agent (`agent/`)

A persistent Python daemon under launchd. Does only outbound HTTPS.

- `main.py` — heartbeat + lease loop
- `resources.py` — probes (mlx_free %, gpu_mem, kokoro/mflux warm,
  battery/AC, current local queue depth)
- `runner.py` — dispatches leased tasks to `workers/heavy/`

### Shared (`shared/`)

- `schema.py` — `ShortProposal`, `JobEnvelope`, `TaskEnvelope` types
- `llm.py` — `claude -p` CLI wrapper (existing `pipeline/llm.py`)
- `channels.py` — resolves nested `channels/<target>/{channel.yaml, formats/*.yaml}`
- `cast_router.py` `prompts.py` — existing logic, relocated

## Data flows

### Chat → render

1. Visitor posts message to `/api/chat`. ChatService loads / creates
   a Firestore session, calls Azure OpenAI, stores assistant reply.
2. When the model emits a `{"type":"short_proposal", ...}` JSON block,
   it's surfaced to the UI as a confirm button.
3. On confirm: control plane writes a `Job` doc to Firestore with the
   proposal, then enqueues the first `Task` (script generation, light
   worker).
4. Light workers pick up tasks, write intermediate artifacts to GCS,
   enqueue follow-up tasks.
5. When the next task is heavy (image gen / TTS / compose), it sits in
   the queue waiting for the laptop agent to lease it.

### Laptop agent lease protocol

```
agent → POST /agent/heartbeat
        body: { mlx_free, gpu_mem, kokoro_warm, mflux_warm,
                whisper_warm, battery_state, local_queue }

agent → POST /agent/lease?caps=image,tts,asr,compose,footage
        (long-poll up to 30s)
        ← 200 { task_id, kind, input_gcs_uri, output_gcs_uri, payload }
        ← 204 (no work)

agent → POST /agent/ack/{task_id}
        body: { status: ok|error, output_uri?, error? }
```

Lease has a TTL (default 5 min) — if the laptop crashes / lid closes,
the task auto-requeues. Server prefers tasks whose model is already
warm on the agent (e.g. routes another image task to the same agent
that just rendered one).

### Storage layout (GCS bucket `ytfactory-prod-artifacts`)

```
jobs/<job_id>/
  proposal.json
  script.json
  cast.json
  prompts.json
  beats/00.png ... NN.png
  voice.wav
  captions.srt
  footage/cut_NN.mp4
  short.mp4
  thumb.png
```

Firestore stores the metadata + queue; GCS stores the bytes.

## Auth + abuse mitigation

| Tier | Sign-in | Chat msgs/day | Renders/day | Per-session turns |
|---|---|---|---|---|
| Anonymous | none | 20 / IP | 1 / IP | 50 |
| Signed-in | Google | 100 / user | 5 / user | 50 |
| Owner | hard-coded email | unlimited | unlimited | unlimited |

Plus a global daily Azure spend cap of $5; chat returns 503 when hit.

## Cost (low-traffic baseline)

| Item | Mo cost |
|---|---|
| Cloud Run (control + light, scale-to-zero) | $0–2 |
| Firestore | $0 (free tier) |
| GCS (10–50 GiB) | $0.30–$1.50 |
| Azure OpenAI (gpt-4o-mini, ~50 sessions) | $1–3 |
| Identity Platform | $0 (free <50K MAU) |
| Egress | ~$1 |
| **Total** | **~$3–8** |

Hard rules to keep it there:
- No GKE, no persistent VM, no Vertex, no GPU on cloud, ever.
- No Pub/Sub (use Firestore queue — simpler, free).
- No Cloud SQL (Firestore is the only state store).
- No always-on minimum instances on Cloud Run.

## Deployment

The control plane image is built from `Dockerfile` (slim Python 3.13, only
`requirements-control.txt` deps — no torch/diffusers/kokoro). Cloud Build
buildpack handles the build + push to `cloud-run-source-deploy` Artifact
Registry repo.

Deploy command (idempotent, re-run for updates):

```bash
gcloud run deploy ytfactory-control \
  --source . \
  --region us-central1 \
  --project ytfactory-prod \
  --allow-unauthenticated \
  --port 8080 \
  --max-instances 3 \
  --min-instances 0 \
  --memory 512Mi \
  --cpu 1 \
  --concurrency 40 \
  --timeout 60 \
  --set-env-vars "YTFACTORY_QUEUE_BACKEND=firestore,YTFACTORY_BUCKET=ytfactory-prod-artifacts,GOOGLE_CLOUD_PROJECT=ytfactory-prod,AZURE_OPENAI_ENDPOINT=...,AZURE_OPENAI_API_VERSION=...,AZURE_OPENAI_MODEL=..." \
  --set-secrets "AZURE_OPENAI_API_KEY=azure-openai-api-key:latest,YTFACTORY_AGENT_TOKEN=ytfactory-agent-token:latest"
```

Secrets (`azure-openai-api-key`, `ytfactory-agent-token`) live in Secret
Manager. The default compute SA (`<project_number>-compute@developer.gserviceaccount.com`)
has the IAM roles needed:

- `roles/datastore.user` — Firestore queue
- `roles/storage.objectAdmin` — GCS bucket
- `roles/secretmanager.secretAccessor` — both secrets

## Operating principle: cloud is canonical

Everything runs against the deployed Cloud Run URL. Do not run a parallel
local production server. Local dev is only for iterating on control-plane
code; once it lands, redeploy.

This makes Playwright MCP automation straightforward: point the browser
at the production URL, drive the chat UI, observe the proposal + queued
job. No tunnels, no localhost, no port forwards.

## Trade-offs we accepted

- **Public chat works without laptop, public render does not.** When
  the laptop is offline the job sits in the queue and the user gets
  a "your render is queued, check back later" message. This is the
  cost of running render on free hardware.
- **Single laptop = single render at a time.** Concurrency is bounded
  by the laptop's GPU. Scaling out means buying a second machine.
  Acceptable for a personal product.
- **Azure OpenAI is the chat dependency.** If their endpoint is down,
  chat is down. We could add Anthropic API as a fallback later.
