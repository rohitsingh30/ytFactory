# Full cloud cutover — 2026-05-09

After the website-died-twice incidents on 2026-05-09 (laptop uvicorn
caught a hangup when the launching shell exited; burner-channels page
500'd), the user said "start working on deploying on cloud fully".

This doc captures the end-to-end migration in one place. Plan
artifact: `/Users/rohit/.copilot/session-state/.../plan.md` (session-private).

## Final state (2026-05-09 evening)

```
                                Internet
                                    │
                  ┌─────────────────┴──────────────────┐
                  │  ytfactory-web (Cloud Run service) │  Next.js
                  │  asia-southeast1                   │  proxies /api/*
                  └─────────────────┬──────────────────┘  to control
                                    │
                  ┌─────────────────┴─────────────────┐
                  │  ytfactory-control (Cloud Run)    │  FastAPI
                  │  Firestore-backed jobs            │  87 endpoints
                  │  Reads /secrets/youtube-token-*   │
                  │  Reads gs://ytfactory-prod-v2-state/│
                  └────────┬─────────────────┬────────┘
                           │                 │
                           │ /api/jobs/      │ /agent/lease
                           │ from_script     │ (long-poll)
                           │                 │
       Cloud Scheduler ────┘                 ▼
       ytfactory-tick      ┌──────────────────────────────────┐
       */30 min            │ ytfactory-render-worker-v2       │
       OIDC SA             │ Cloud Run JOB (CPU 4, mem 8Gi)   │
                           │ Reads spec from gs://artifacts/  │
                           │ Writes mp4 + state.json back     │
                           └────────┬─────────────────────────┘
                                    │
                  ┌─────────────────┴───────────────────────┐
                  ▼                                         ▼
       ytfactory-tts-chatterbox                   ytfactory-image-flux2-klein
       ytfactory-tts-indicf5  (NVIDIA L4)         (NVIDIA L4)
                                    │
                                    ▼
                       Laptop agent (long-poll)
                       Chrome work only:
                         playwright_upload, burner_engage
```

## Five phases shipped

### Phase 0 — baseline prod (no code changes)

Confirmed already-deployed services healthy. 7 channels listed, render
backend = cloudrun, sim worker off, 1 test job in Firestore.

Gap surfaced: deployed control image was missing `burner_routes` and
`clone_video_routes` (they existed in repo, weren't in the deployed
image). Fixed by redeploy mid-Phase-1.

### Phase 1 — state migration

**1a. OAuth tokens → Secret Manager** (11 new secrets):
- `youtube-token-mystoriesanimated|cosmosdecoded|historyrecapped|hindutavaanimated|sportsrecapped|scrollpulse|rhymetimejunction|afddfdf|zgsbhqszdheo`
- `youtube-channel-ids`
- `profile-map`

Granted `roles/secretmanager.secretAccessor` to
`tts-runner@ytfactory-prod-v2.iam.gserviceaccount.com` on each.

Mounted on both `ytfactory-control` and `ytfactory-render-worker-v2`
as `/secrets/<secret-name>/value` (Cloud Run requires one secret per
directory, so the `/value` filename gives each its own dir).

Code changes:
- `pipeline/upload.py`: `_token_path()` and `_client_secret_path()`
  prefer `/secrets/<name>/value` when present, else fall back to
  `~/.config/ytfactory/`. Token writeback in `authenticate()` is
  guarded against the read-only secret mount (refresh-on-each-call
  is fine since the refresh_token is durable).
- `pipeline/burner_engage.py`: `_resolve_token_path`,
  `_resolve_profile_map`, `_resolve_channel_ids` — same pattern.
- `control/oauth_web_routes.py`: `load_token` falls back to
  `_secret_mount_path` after Firestore + disk.

**1b. State → GCS bucket** `gs://ytfactory-prod-v2-state` (3.7 MiB
initial sync). One-shot rsync of:
- `<channel>/{narrations,cast,shotlist,uploads,learnings}/`
- `<channel>/_holds.json`
- `<channel>/<niche>/{narrations,cast,shotlist,uploads}/` (niched layouts)

Skipped (per plan): heavy `cache/`, `footage/`, `scratch/`, `raw/`
(re-pulled on demand by render-worker).

**1c. Continuous laptop → GCS sync.** Launchd plist
`com.ytfactory.state-sync` with `WatchPaths` (event-driven, fires
only when narration/cast/shotlist files change) + `ThrottleInterval=60`.
Runs `scripts/sync_state_to_gcs.sh`. One-way: laptop → GCS only.

### Phase 2 — Cloud Scheduler replaces launchd cron

Created `ytfactory-tick` Cloud Scheduler job:
- Schedule: `*/30 * * * *` Asia/Kolkata
- Target: `POST https://ytfactory-control.../api/scheduler/tick`
- Auth: OIDC token via `ytfactory-scheduler@ytfactory-prod-v2.iam.gserviceaccount.com`
  (granted `roles/run.invoker` on `ytfactory-control`)

**Critical scheduler change:** `control/scheduler.py:_next_unrendered`
and `_uploaded_slugs` made GCS-aware. When `YTFACTORY_STATE_BUCKET`
env is set (it is, on cloud), they list narrations + uploads from
GCS instead of local disk. The narration "path" returned is a
`gs://...` URI wrapped as `Path` for downstream consumers.

**Auth model fix:** `control/scheduler_routes.py:_require_auth` now
checks `K_SERVICE` (set by Cloud Run runtime) and trusts IAM when
present. Cloud Scheduler invokes via `--oidc-service-account-email`
which consumes the Authorization header; we can't ALSO require an
app-level Bearer token. Same fix applied to
`control/auth.py:require_agent` for the `/agent/*` endpoints.

Verified end-to-end: scheduler ticks → control plane returns
`{"action":"skipped","reason":"no_unrendered_scripts","tried":[5 channels]}`.
Correct behavior — every narration in GCS already has an upload record.

### Phase 3 — skill cutover

`pipeline.skill_dispatch.WEBSITE_URL` already defaults to the cloud URL.
Auth via gcloud OIDC (already implemented). No client changes needed.

**Server-side gap closed:** `/api/jobs/from_script` (the skill→cloud
render entry) didn't exist on the new control plane — it lived in
`web/server.py` (the legacy monolith). Ported as
`control/script_jobs_routes.py`:

- `POST /api/jobs/from_script` accepts `{cmd: [...]}` or
  `{channel_yaml, script_path}`.
- Validates entry-point against `_ALLOWED_RENDER_CMDS` whitelist (7
  renderer scripts).
- Reads channel YAML (image-baked) + script JSON (from
  `gs://ytfactory-prod-v2-state/<rel-path>`) + optional raw.
- Writes spec.json to `gs://ytfactory-prod-v2-artifacts/jobs/<id>/`.
- Triggers `gcloud run jobs execute ytfactory-render-worker-v2
  --update-env-vars JOB_SPEC_GCS_URI=...`
- Polls `state.json` until terminal.
- `GET /api/jobs/from_script/{id}` polls status (with 8KB log tail
  fetched from GCS).
- `GET /api/jobs/from_script/{id}/mp4` 302s to a 15-min signed URL.

> **2026-05-10 follow-up — unified `/api/jobs/{id}` read surface.** The
> Phase-4 split into `JOBS` (niche) vs `SCRIPT_JOBS` (skill) namespaces
> caused the new web-next render-detail page to 404 on every
> skill-submitted render (`/app/render/<id>` polls `/api/jobs/{id}`,
> which only checked `JOBS`). `web/server.py:job_snapshot()` and
> `job_short()` now fall through to `SCRIPT_JOBS`; same call site adapts
> the record into both legacy + new `JobView` shapes. `SCRIPT_JOBS`
> itself is now Firestore-backed (`web/script_jobs_store.py`,
> `YTFACTORY_QUEUE_BACKEND=firestore`) so jobs survive Cloud Run
> revision rollover. Full pattern: `docs/jobs_snapshot_unification.md`.

Wired into `control/server_dev.py`: now 87 routes (was 65 at
session start).

### Phase 4 — laptop agent for Chrome-bound work

Two task kinds added to `control/schema.py:TaskKind`:
- `PLAYWRIGHT_UPLOAD` — drives signed-in Chrome to upload an mp4 via
  studio.youtube.com (the existing `/upload-via-playwright` skill
  flow).
- `BURNER_ENGAGE` — drives Chrome via per-account profiles to
  like/sub.

Built `pipeline/laptop_agent.py`:
- Long-polls `POST /agent/lease` with caps `["playwright_upload",
  "burner_engage"]`.
- Heartbeats every 60s (`POST /agent/heartbeat`).
- Auths via `gcloud auth print-identity-token` (no `--audiences`,
  user-account compatible).
- Token cached for 50 min.
- Falls back to `YTFACTORY_AGENT_TOKEN` env or `~/.config/ytfactory/agent_token`
  when target is `localhost` (laptop dev).

Wired up `~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist`:
- `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval=10`
- PATH includes `/opt/homebrew/bin` so launchd can find `gcloud`
- `CLOUDSDK_CONFIG=/Users/rohit/.config/gcloud` so it uses the user's
  ADC

**Stub:** `_exec_playwright_upload` raises `NotImplementedError`
pointing to `/.claude/skills/upload-via-playwright/SKILL.md` —
implementing the actual Chrome automation is a follow-on task. The
heartbeat + lease + ack loop works; the executor is just the missing
body.

`burner_engage` was claimed implemented here, but the cloud entry
point (`POST /api/burner_channels/<slug>/engage` in
`control/routes/burner_routes.py`) was still doing
`subprocess.Popen(...)` on the cloud container (no Chrome → silent
crash → UI 404 storm). Plus three more gaps surfaced 2026-05-10:
the agent middleware needed an IAM bypass for `/agent/*`, the
laptop catalog reader needed GCS-awareness, and the worker needed
to actively brand-switch before engaging (one Google account hosts
multiple burners). The full cloud cross-engage path is now
documented in
[`docs/cross_engage_cloud_v2.md`](cross_engage_cloud_v2.md). The
laptop-only `pipeline.cross_engage.burner_engage run <slug>`
direct-CLI path still works in dev when `K_SERVICE` is unset.

**Pending:** Firestore composite index on `tasks(kind,status,created_at)`
was creating at session end (~5 min build). Once live the lease will
return tasks cleanly when any are queued.

### Phase 5 — decommission laptop control plane

- Killed local uvicorn (PID 73210).
- Verified cloud control still serves `/api/health` with **both**
  agents present (laptop-Rohits-MacBook-Pro.local from the new agent
  + a test agent from manual curl).
- Updated this CLAUDE.md "Laptop role" section to reflect new state
  + the auth model + the new launchd plists.
- Archived `com.ytfactory.upload-next.plist*` files (no longer needed
  — Cloud Scheduler does this work).

## Test suite

All edits pass the auto-test rule. Pre-existing failures (3 fail / 6
errors in `test_cloudrun_auth`, `test_render_long_form_guards`,
`test_layout_parity`) unchanged — unrelated to this migration.

## Rollback (if cloud goes sideways)

1. Re-enable laptop uvicorn:
   ```bash
   cd /Users/rohit/ytFactory
   PYTHONPATH=. .venv/bin/uvicorn control.server_dev:app --host 127.0.0.1 --port 8766 &
   ```
2. Disable Cloud Scheduler:
   ```bash
   gcloud scheduler jobs pause ytfactory-tick \
     --project=ytfactory-prod-v2 --location=asia-southeast1
   ```
3. Re-enable laptop launchd cron (rename
   `~/Library/LaunchAgents/com.ytfactory.upload-next.plist.disabled-2026-05-09`
   back, then `launchctl load`).
4. The state-sync watcher + laptop agent can keep running — they're
   one-way and idempotent.

## Open follow-ups

- **Implement `_exec_playwright_upload`** — port the Playwright
  automation from `.claude/skills/upload-via-playwright` into
  `pipeline.laptop_agent`.
- **Add `/api/uploads/queue_playwright`** to control-plane — auto-fires
  when YouTube API returns 403 quotaExceeded, queueing the Playwright
  task for the laptop agent.
- **Cron uploader endpoints** — the existing `/api/cron/drain` from
  the legacy `web/server.py` was NOT ported (per-channel wrapper around
  the scheduler.tick logic). The single `ytfactory-tick` job covers
  the round-robin case; per-channel forced drains are nice-to-have.
- **Custom domain** (`ytfactory.app` or pick) instead of the
  `*-7hwnzw7lya-as.a.run.app` URL.
- **24-hour closed-lid test** — leave laptop closed, verify cloud
  ticks + uploads continue. Couldn't run in this session.
