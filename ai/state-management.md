# State Management

Where state lives — Firestore collections, GCS path conventions, local
channel cache, and the env-var that swaps state backends. Cite the file
that owns each.

## Firestore collections

Project: `ytfactory-prod-v3` (per `cloud/render-worker-v2/deploy.sh:106`).
Local dev tests use the in-memory backend (`control/core/queue.py:91`).

### `jobs/<job_id>` — the user-visible render state

Owner: `control/core/jobs.py`. Created by `create_job`
(`jobs.py:169`); mutated by `mark_stage` (`:210`), `mark_done`
(`:215`), `mark_failed` (`:236`). The render worker writes to the
same doc directly via `_set_stage` and friends in
`cloud/render-worker-v2/entrypoint.py`.

**Schema** (from `jobs.py:5-17`):

```
{
  job_id, channel, topic, owner_uid?, slug, render_kind,
  created_at, updated_at,
  status: "pending" | "rendering" | "uploading" | "researching"
        | "done" | "failed",
  stage:  "queued" | "dispatching" | "rewrite" | "cast" | "images"
        | "tts" | "asr" | "compose" | "gcs_upload" | "youtube_upload"
        | "research_handoff" | "done",
  proposal: <ShortProposal dict>,
  cloud_execution: <full Cloud Run execution name>,
  render_spec: <RenderSpec.to_dict()>,
  notes: [<spec note strings>],
  artifacts: [<live preview artifact records>],
  short_uri: "gs://..."  (when render completes),
  thumb_uri: "gs://..."  (when thumb ready),
  youtube_url: "https://youtu.be/..."  (when YT publish done),
  error: <truncated to 2000 chars on failure>,
  internal_only: bool,   # test-fixture auto-flag (jobs.py:344)
}
```

**Lifecycle transitions** (`status` field, ordered):

```
pending  ──► rendering  ──► uploading  ──► done
   │              │              │
   ▼              ▼              ▼
 failed        failed         failed
```

`researching` is set when the rewrite stage is offloaded to the
research handoff flow.

### `tasks/<task_id>` — the work queue

Owner: `control/core/queue.py`. Used only when
`YTFACTORY_RENDER_BACKEND ∈ {sim, laptop}` — the `cloudrun` path
bypasses the queue entirely and triggers the Cloud Run Job directly
(`control/core/jobs.py:394-405`).

Schema lives in `control/core/schema.py::TaskEnvelope`. Statuses:
`queued`, `leased`, `done`, `failed`.

### `scheduler_state/main` — round-robin cursor

Owner: `control/core/scheduler.py:41` (`SCHEDULER_STATE_DOC`).
One singleton doc. Read at `scheduler.py:356`, written at `:381`.

Shape: `{last_channel, last_slug, last_enqueued_at}`. Used by Cloud
Scheduler ticks (POST /api/scheduler/tick) to pick the next channel
in rotation. The tick *does NOT* fire renders directly — per
`onboarding-qa.md` Q36, every render is wizard-triggered. The
scheduler exists for upload scheduling.

### `ratelimits/<date>/...` — per-IP daily quota

Owner: `control/core/rate_limit.py:69-84`. Two subcollection shapes:

```
ratelimits/<date>/ips/<ip>/actions/<action>   # per-IP action count
ratelimits/<date>/global/spend                 # global spend tracker
```

Read+incremented by `rate_limit.check_and_increment` (called from
`control/routes/render_routes.py:43` and friends). One doc per IP per
action per day; daily docs get pruned by garbage-collection elsewhere.

### `chat_sessions/<session_id>` — disabled feature

Owner: `control/core/schema.py:128`. Schema persisted but the "AI
chat" submode at `/app/create` is currently disabled per
`onboarding-qa.md` Q20. Collection kept for the future.

## GCS path conventions

Bucket: `gs://ytfactory-prod-v3-artifacts/` (set via
`YTFACTORY_BUCKET=ytfactory-prod-v3-artifacts` in `deploy.sh:106`).

Default in source still says `ytfactory-prod-v2-artifacts`
(`cloud/render-worker-v2/entrypoint.py:139`) — drift; the env var
wins in prod.

### `jobs/<job_id>/` — per-job artifact tree

```
gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/
├── short.mp4            # final mp4 (set at entrypoint.py:460,3112)
├── thumb.jpg            # final thumbnail (entrypoint.py:665)
├── state.json           # job state snapshot for the v1 GCS-driven path
│                        #   (entrypoint.py:3002,3017)
├── renderer.log         # cloud render stdout (entrypoint.py:3075)
├── script/              # envelope + narration JSON live previews
├── video/               # intermediate mp4 previews (separate prefix from short.mp4
│                        #   so the two uploads don't collide — video.py:404-405)
├── panels/              # per-panel PNG previews
├── narration/           # narration.wav preview
├── beats/               # beats.json preview
└── envelope/            # the long-form ScriptEnvelope JSON
```

Live artifact preview emission is owned by
`pipeline.render.artifacts.emit_artifact`, called from
`pipeline/render/video.py:253,267,374,412` (long-form) and the
worker's post-render sweep in `entrypoint.py`.

### Other GCS prefixes

- `gs://<bucket>/jobs/<job_id>/short.mp4` — the canonical upload from
  `_upload_mp4_to_gcs` (`entrypoint.py:459`).
- `gs://<bucket>/jobs/<job_id>/video/<filename>.mp4` — live-preview
  copies. Different prefix from `short.mp4` so the two upload writes
  don't collide.

## Local channel cache layout

Owner: `pipeline/paths.py::RenderPaths` (`paths.py:157`). Single
source of truth for any disk path under `<channel>/`.

Niched channels nest per-slug subdirs under `<niche>/`:

```
<channel>/<niche>/
├── narrations/<slug>.json       # tracked — authored script
├── raw/<slug>.json              # tracked — source fetch
├── cast/<slug>.json             # gitignored — voice/visual assignments
├── shotlist/<slug>.json         # tracked — footage windows
├── uploads/<slug>.json          # tracked — YouTube upload record
├── shorts/<slug>.mp4            # gitignored
├── long_form/<slug>.mp4         # gitignored
├── cache/<slug>/
│   ├── narration.wav            # TTS output
│   ├── beats.json               # ASR alignment
│   ├── panel_*.png OR img_*.png # per-beat / per-panel images
│   └── ...                      # everything else the render emits
├── scratch/<slug>/              # gitignored — engine work_dir
└── critiques/<slug>/            # JSONs tracked, frames gitignored
```

Channel-wide subdirs (NEVER nest under `<niche>/`):

```
<channel>/
├── config.yaml                  # NOTE: actual source of truth at
│                                #   pipeline/channels/<channel>.yaml,
│                                #   not this in-tree copy
├── variants/<v>.yaml            # legacy location (canonical is
│                                #   pipeline/variants/<channel>/<v>.yaml)
├── learnings/                   # tracked
├── scripts/                     # tracked CLI helpers
├── branding/                    # *.png gitignored
├── music/                       # mood beds
├── songs/                       # Suno output for `audio_mode=song` channels
├── emoji/                       # inline-emoji assets
├── footage_plan/                # tracked
└── footage/{sources,long_sources,transcripts}/  # gitignored, opt
```

Flat channels (`cosmosdecoded`, `historyrecapped`, `hindutavaanimated`,
`rhymetimejunction`) use the same layout but without the `<niche>/`
nesting level — `RenderPaths.root == channel_root`
(`paths.py:295-306`).

## Cross-channel state under `data/`

Owner: `pipeline/paths.py:475-485`. These buckets stay genuinely
cross-channel:

```
data/
├── research/        # cross-channel YouTube research
├── telemetry/       # cross-channel render telemetry (per-render JSON lines)
├── _bench/          # TTS A/B bench output (gitignored)
└── cache/           # ML model weight cache (Kokoro, F5, Whisper)
```

Deprecated (migrating per-channel; `paths.py:104-108`):
- `data/intermediate/<channel_dir>/...` → `<channel>/[<niche>/]cache/<slug>/`
- `data/critiques/<slug>/` → `<channel>/[<niche>/]critiques/<slug>/`
- `data/shorts/<slug>.mp4` → `<channel>/[<niche>/]shorts/<slug>.mp4`

## State-backend swap via env

`YTFACTORY_QUEUE_BACKEND` selects the queue + job-state backend:

- **Jobs** (`control/core/jobs.py:142`): defaults to `memory`. Set
  to `firestore` to use Firestore.
- **Queue** (`control/core/queue.py:7`): defaults to `firestore`. Set
  to `memory` for tests.

**Inconsistent defaults** — Jobs defaults to `memory`, Queue defaults
to `firestore`. So a fresh process can have an in-memory job store
and a Firestore queue at the same time. Either is fine standalone;
the inconsistency just means tests need to be careful about which
backend is active. Documented but not normalised — a future cleanup
should align defaults.

`YTFACTORY_RENDER_BACKEND` is independent
(`control/core/cloud_run.py:55`):

- `sim` (default) — drop a task on the queue; in-process
  `control/core/sim_worker.py` consumes it.
- `cloudrun` — bypass the queue entirely, trigger Cloud Run Job.
- `laptop` — DEPRECATED, treated as `sim`.

## Firestore retry semantics

`control/core/queue.py:48-84` wraps every Firestore write in
`firestore_retry` with exponential backoff on transient errors
(`DeadlineExceeded`, `ServiceUnavailable`, `Aborted`,
`InternalServerError`, `Cancelled`, `ResourceExhausted`, `Unknown`).
4 attempts; max ~1.85s of added latency before re-raising. Used by
both `_FirestoreJobs` and `_FirestoreQueue`.

## What's structurally fragile in state management

1. **Hardcoded `ytfactory-prod-v2` defaults.** Three sites:
   `control/core/cloud_run.py:50`, `entrypoint.py:136`, `:139`. The
   env vars from `deploy.sh` rescue this in prod; a dev box with no
   env set will silently target the v2 project (likely deleted) and
   either fail with a friendly error or silently target the wrong
   resource. Single constant or deploy-manifest read would close
   this.

2. **`jobs/` doc doubles as state machine + audit log.** Each stage
   transition does `set(..., merge=True)` overwriting `stage`,
   `status`, `updated_at`. There's no append-only history of which
   stages ran. The cloud_execution name and the `artifacts[]` array
   are the only audit trail. If you need "did stage N retry?" you
   have to read the renderer.log in GCS.

3. **`<channel>/config.yaml` is a stale path.** `pipeline/paths.py:90`
   says the config lives at `<channel>/config.yaml` but the actual
   source of truth is `pipeline/channels/<channel>.yaml`
   (`pipeline.channels._channel_yaml_path`). The in-tree copy may or
   may not exist; readers shouldn't trust it.

4. **GCS path mixing**: `jobs/<job_id>/short.mp4` (final upload) and
   `jobs/<job_id>/video/*.mp4` (live previews) share the same job
   prefix. The two write paths are intentionally non-colliding
   (different filenames), but a future agent that lists
   `jobs/<job_id>/` will see both and may confuse intermediate
   preview mp4s with the canonical final one. The `short_uri` field
   on the Firestore job doc is the only signal for "this is THE
   final mp4".
