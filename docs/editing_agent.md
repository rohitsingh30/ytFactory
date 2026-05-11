# Cloud Run editing-agent — cinematic editor

> **Status:** scaffolded 2026-05-11. Service code lives at
> [`cloud/editing-agent/`](../cloud/editing-agent/); pipeline client +
> shared executor at [`pipeline/editing/`](../pipeline/editing/);
> authoring/dispatch skill at
> [`.claude/skills/editing-agent/SKILL.md`](../.claude/skills/editing-agent/SKILL.md).

## What it is

A Cloud Run **SERVICE** (not a JOB — each `/edit` request completes
in seconds-to-minutes, no Firestore needed) that takes a closed-form
EDL JSON + a map of GCS-staged inputs and returns the path to a
cinematic mp4. Same code as the laptop executor — the cloud service
is "the laptop executor + GCS bookends", nothing more.

```
   user / skill / orchestrator
            │
            │ 1. plan_edit() ── LLM (Claude CLI laptop / Azure cloud) → EDL JSON
            │ 2. validate against whitelist (filters, LUTs, transitions)
            ▼
   pipeline.editing.cloudrun.execute_edit(edl, ...)
            │
            ├── circuit-breaker open?  → laptop ffmpeg fallback
            ├── CLOUDRUN_EDITING_AGENT_URL unset? → laptop ffmpeg fallback
            │
            │ 3. stage inputs to GCS (de-duped by basename)
            │ 4. POST /edit with EDL + signed-URL refs
            ▼
   ytfactory-editing-agent (Cloud Run SERVICE)
            │
            │ 5. validate EDL again (defense in depth)
            │ 6. download inputs from GCS into a tmpdir
            │ 7. pipeline.editing.executor.execute_local(...) ← SAME CODE
            │ 8. upload result to GCS
            ▼
   {"output_uri": "gs://..."} → laptop downloads → caller
```

## EDL schema

`pipeline.editing.schema.Edl`. Whitelist-only — the LLM never emits
free-form ffmpeg fragments:

| Field group | Whitelist source |
|---|---|
| `mode` | `EditMode` enum (4 values) |
| `aspect` | hard-coded set in `_validate_aspect` |
| `lut` | `LUT_WHITELIST` (4 .cube files in `pipeline/editing/luts/`) |
| `shots[].filters[].type` | `FILTER_WHITELIST` (10 ffmpeg filters) |
| `shots[].transition_*.type` | `TRANSITION_WHITELIST` (12 transitions) |
| `audio.music.source` | `archive_pd` / `youtube_audio_library` / `path` |
| `audio.music.path` | must live under `<channel>/music/` or `pipeline/editing/music/` |

Adding a new filter requires:

1. `FILTER_WHITELIST.add(...)` in `pipeline/editing/schema.py`
2. Compile branch in `pipeline/editing/executor.py::_filter_to_str`
3. System-prompt paragraph in `pipeline/editing/planner.py`
4. Bump `EDL_VERSION`, document the migration in this file

## Modes

| Mode | Input shape | What it does |
|---|---|---|
| `polish` | 1 `.mp4` | Re-cut, color-grade, letterbox, audio loudnorm on existing video |
| `assemble-clips` | folder of `.mp4` | Scene-detect each → LLM picks best moments → cinematic montage |
| `assemble-stills` | folder of images | Ken Burns + crossfade + grade + optional music sync |
| `assemble-mixed` | mixed folder | Auto-classifies each input, weaves into one timeline |

## Build + deploy

```bash
# One-shot build + deploy:
./cloud/editing-agent/deploy.sh

# Or with explicit tag:
./cloud/editing-agent/deploy.sh v2026-05-11

# Wire the laptop / render-worker-v2 to the new service URL:
export CLOUDRUN_EDITING_AGENT_URL="$(gcloud run services describe \
  ytfactory-editing-agent \
  --project=ytfactory-prod-v2 \
  --region=asia-southeast1 \
  --format='value(status.url)')"
```

The service is registered in `pipeline/cloud/services.py` so the
`/app/cloud` admin tab picks it up automatically — no extra UI wiring.

## Env vars (laptop client + cloud service)

| Var | Where | Purpose |
|---|---|---|
| `CLOUDRUN_EDITING_AGENT_URL` | laptop / render-worker-v2 | Service URL. Unset → laptop fallback. |
| `CLOUDRUN_EDITING_AGENT_TIMEOUT` | laptop / render-worker-v2 | Per-call HTTP timeout in seconds (default 600). |
| `CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK` | laptop / render-worker-v2 | Set `1` to hard-error instead of falling back to local ffmpeg. Use in canary. |
| `YTFACTORY_BUCKET` | both | GCS bucket for input/output staging. |
| `GOOGLE_CLOUD_PROJECT` | service | Defaults to `ytfactory-prod-v2`. |
| `LOG_LEVEL` | service | `INFO` default. |
| `IMAGE_SHA` | service | Set by `deploy.sh`; surfaced via `/version`. |

## Optional 8th orchestrator stage

When `proposal.editing.enabled` is truthy on a job doc, the
render-worker-v2 inserts an `editing_agent` stage between `compose`
and `upload`:

```
rewrite → cast → images → tts → asr → compose → editing_agent → upload
```

Implementation: `cloud/render-worker-v2/entrypoint.py::_stages_for_job`
returns the per-job stage list; the main loop walks that list (not the
static 7-stage `STAGES`). The Firestore `timeline:` field reflects the
8th pill from job creation, so the UI's poll loop shows it without
extra plumbing.

Per-render circuit-breaker pattern means a single editing-agent
failure trips the rest of the batch to laptop fallback rather than
multiplying the per-call timeout 10× across a Shorts batch.

## Health (admin tab)

`/app/cloud` shows green/yellow/red live for editing-agent alongside
TTS + image services. Health URL = `<service>/readyz`, which verifies:

- `ffmpeg` is on PATH
- All 4 whitelisted LUTs exist on disk

Cold-load is ~10-15 s (no model weights to load — just FastAPI +
ffmpeg). Pre-warm via `pipeline.cloud.warm.warm_for_channel(channel,
include_editing=True)`.

## Smoke test

Once deployed:

```bash
URL="$(gcloud run services describe ytfactory-editing-agent \
  --project=ytfactory-prod-v2 --region=asia-southeast1 \
  --format='value(status.url)')"
TOKEN="$(gcloud auth print-identity-token --audiences=$URL)"

# Health
curl -H "Authorization: Bearer $TOKEN" "$URL/readyz"
# {"ready":true,"luts":["cinematic.cube","noir.cube","teal-orange.cube","warm-doc.cube"]}

# Version
curl -H "Authorization: Bearer $TOKEN" "$URL/version"
```

## Rollback

The editing pass is opt-in per job (default off), so no rollback is
needed for ordinary renders. To revert opted-in jobs:

```bash
# Either:
# 1) Set proposal.editing.enabled = false in the website's render form.
# 2) Trip the breaker globally on the worker:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=0  # default

# To uninstall the service entirely:
gcloud run services delete ytfactory-editing-agent \
  --region asia-southeast1 --project ytfactory-prod-v2
# Plus remove the entry from pipeline/cloud/services.py.
```

## Cross-refs

- [`docs/cloudrun_render_worker.md`](./cloudrun_render_worker.md) — orchestrator pattern this rides on
- [`docs/cloud_prerender_hook.md`](./cloud_prerender_hook.md) — pre-warm contract
- [`docs/admin_panel_first.md`](./admin_panel_first.md) — admin-tab visibility rule
- [`docs/cloud_service_dep_playbook.md`](./cloud_service_dep_playbook.md) — mandatory pre-build checklist (followed for the v1 build)
- [`.claude/skills/editing-agent/SKILL.md`](../.claude/skills/editing-agent/SKILL.md) — authoring/dispatch skill
