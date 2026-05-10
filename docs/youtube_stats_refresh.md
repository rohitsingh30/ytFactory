# YouTube stats refresh — token + cache pipeline (2026-05-10)

## TL;DR

The dashboard's per-channel stats (subscribers, views, likes) come from
two independent sources:

1. **Live API stats per video** — `control/routes/dashboard_routes.py::
   dashboard_videos` calls YouTube Data API v3 with a single
   `YOUTUBE_API_KEY`, in-memory cache (10 min TTL). No per-channel
   OAuth needed. **This is what lights up the channel cards.**
2. **Per-account research cache** — `pipeline.research.youtube.fetch_account`
   calls `channels.list` + `playlistItems.list` + `videos.list` under
   each channel's OAuth token, writes the result to
   `gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json`.
   Used by the aggregator (`pipeline.research.aggregator.build_videos`,
   `build_channels`) for the offline rollup, the research dashboard,
   and channel-level subscriber stats.

If the cards are blank: **set `YOUTUBE_API_KEY` in the prod env**
(that's it, that's the fix). If the offline aggregate is stale: the
hourly Cloud Scheduler `ytfactory-stats-refresh-cron` runs the
`ytfactory-stats-refresh` JOB which writes the GCS cache.

## Layered architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Browser (web-next)                                              │
│   GET /api/dashboard/videos                                     │
│   └── cards → stats.views / stats.likes / stats.comments        │
└─────────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────┐
│ ytfactory-web (Cloud Run service)                               │
│   control/routes/dashboard_routes.py::dashboard_videos          │
│   ├── 1) Read upload records:                                   │
│   │     - GCS first: gs://…-state/upload-records/<channel>/…   │
│   │     - Disk fallback: <root>/<channel>/uploads/…             │
│   ├── 2) Read live YT stats via YOUTUBE_API_KEY (10-min cache)  │
│   └── 3) Aggregate per-channel totals + return                  │
└─────────────────────────────────────────────────────────────────┘
              │                                  ▲
              │                                  │
              ▼                                  │
┌─────────────────────────┐         ┌────────────────────────────┐
│ ytfactory-stats-refresh │  ←───── │ Cloud Scheduler (hourly)   │
│  Cloud Run JOB          │         │  ytfactory-stats-refresh-  │
│  (cloud/stats-refresh/) │         │  cron                      │
│                         │         └────────────────────────────┘
│  python -m pipeline.    │
│    research.youtube     │
│                         │
│  per channel:           │         ┌────────────────────────────┐
│   ├── load OAuth token  │  ←───── │ Secret Manager mounts      │
│   │   from /secrets/    │         │  /secrets/youtube-token-   │
│   │   youtube-token-X/  │         │  <account>/value           │
│   │   value             │         │  (read-only)               │
│   ├── refresh access    │         └────────────────────────────┘
│   │   token if expired  │                  ▲
│   ├── if rotated, write │                  │
│   │   new version back  │  ─────────────► (D1 writeback path)
│   └── write JSON to GCS │
│                         │
│  output → gs://…-state/data/research/youtube/<account>.json     │
└─────────────────────────────────────────────────────────────────┘
              │                                  ▲
              ▼                                  │
┌─────────────────────────┐                      │
│ pipeline.research.      │                      │
│   aggregator.build_*    │                      │
│   reads the GCS cache   │                      │
└─────────────────────────┘                      │
                                                 │
┌────────────────────────────┐                   │
│ Cloud Scheduler (daily)    │ ──────────────────┘
│  ytfactory-token-health-   │
│  cron                      │   GET /api/cron/token-health
└────────────────────────────┘   (alerts on broken tokens)
```

## Where things live

| Component | Path |
|---|---|
| Dashboard endpoint (canonical) | `control/routes/dashboard_routes.py::dashboard_videos` |
| In-process YT stats cache | `control/routes/dashboard_routes.py::_STATS_CACHE` (10-min TTL) |
| Per-account YT research cache fetcher | `pipeline/research/youtube.py::fetch_account` |
| Per-account YT research cache reader | `pipeline/research/youtube.py::_load_cache` (60 s in-process TTL fast-path skips GCS HEAD on warm hits — see `docs/web_perf_pass_2026_05_10.md`) |
| Per-account YT research cache (cloud) | `gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json` |
| Per-account YT research cache (laptop) | `data/research/youtube/<account>.json` (gitignored since 2026-05-10) |
| OAuth token (cloud) | `/secrets/youtube-token-<account>/value` (Secret Manager mount) |
| OAuth token (laptop) | `~/.config/ytfactory/youtube_token_<account>.json` |
| OAuth flow (interactive) | `pipeline/upload/upload.py::authenticate(interactive=True)` |
| Token rotation write-back | `pipeline/upload/upload.py::_persist_token` (calls `add_secret_version` in cloud) |
| Token health endpoint (admin UI) | `web/server.py::admin_token_health` → `GET /api/admin/token-health` |
| Token health endpoint (M2M cron) | `web/server.py::cron_token_health` → `GET /api/cron/token-health` |
| Refresh-trigger endpoint | `web/server.py::dashboard_refresh_research_cache` → `POST /api/dashboard/refresh-research-cache` |
| Cloud Run JOB (stats refresh) | `cloud/stats-refresh/Dockerfile` + `deploy.sh` |
| Cloud Scheduler (stats refresh) | `cloud/stats-refresh/wire_scheduler.sh` (hourly) |
| Cloud Scheduler (token health) | `cloud/iam/wire_token_health_cron.sh` (daily) |
| IAM: secret rotation grant | `cloud/iam/grant_token_writeback.sh` |
| Channel discovery | `pipeline/research/youtube.py::iter_channel_configs` (reads `pipeline/channels/*.yaml`) |

## Common operations

### "Channel cards on prod show no stats"

Most likely the env var isn't set:

```bash
gcloud run services update ytfactory-web \
  --project=ytfactory-prod-v2 --region=asia-southeast1 \
  --update-env-vars=YOUTUBE_API_KEY=<your-api-key>
```

Once set, the next dashboard load fetches live stats from YouTube
Data API v3. Verify:

```bash
AGENT_TOKEN=$(gcloud secrets versions access latest --secret=ytfactory-agent-token --project=ytfactory-prod-v2)
curl -s -H "Authorization: Bearer $AGENT_TOKEN" \
     https://ytfactory-web-7hwnzw7lya-as.a.run.app/api/dashboard/videos | jq .totals
```

> **Two independent code paths feed the dashboard (2026-05-10
> learning).** The cards (`/api/dashboard/videos`) and the channels
> gallery (`/api/channels`) are separate stacks that BOTH need stats.
> Setting `YOUTUBE_API_KEY` lights up the cards via
> `control/routes/dashboard_routes.py::dashboard_videos`. The channels
> gallery (used by the dashboard hero cards AND the channels tab AND
> the create wizard) goes through
> `pipeline/schemas/customization.py::list_channels` →
> `_personality_for(account)` → `pipeline.research.youtube.load_account`.
> When the GCS YT cache is empty (cold cloud start, before the hourly
> stats-refresh JOB has run), `_personality_for` falls back to
> `_live_channel_via_api_key()`, which discovers the channel id via
> the `youtube-channel-ids` Secret Manager mount and hits
> `channels.list` with `YOUTUBE_API_KEY`. The fallback caches in
> process for 10 min. So both surfaces work cold even before the JOB
> backs everything up.
>
> When fixing a "no stats" symptom, ALWAYS verify both endpoints
> return real data, not just `/api/dashboard/videos`. Sweep recipe
> for any third surface that returns subscriber counts:
> `grep -rn "subscriber_count\|youtube_video_count\|total_views" web/ control/ pipeline/schemas/`.

### "Add a new YouTube account / channel"

1. Create the channel YAML at `pipeline/channels/<slug>.yaml` with an
   `upload.account: <slug>` field.
2. Run OAuth on the laptop:

   ```bash
   python -m pipeline.upload.upload auth refresh --account <slug>
   ```

   This now opens your default Chrome (the Google session you're
   already signed into). Override with `YTFACTORY_CHROME_PROFILE_DIR`
   if you have multiple Google accounts:

   ```bash
   YTFACTORY_CHROME_PROFILE_DIR="$HOME/Library/Application Support/Google/Chrome" \
     python -m pipeline.upload.upload auth refresh --account <slug>
   ```

   Or stay headless with `YTFACTORY_OAUTH_OPEN_BROWSER=0`.
3. Mirror the new token to Secret Manager:

   ```bash
   gcloud secrets create youtube-token-<slug> \
     --project=ytfactory-prod-v2 \
     --data-file="$HOME/.config/ytfactory/youtube_token_<slug>.json"
   ```
4. Grant the runtime SA both read + version-add:

   ```bash
   ./cloud/iam/grant_token_writeback.sh
   gcloud secrets add-iam-policy-binding youtube-token-<slug> \
     --project=ytfactory-prod-v2 \
     --member=serviceAccount:tts-runner@ytfactory-prod-v2.iam.gserviceaccount.com \
     --role=roles/secretmanager.secretAccessor
   ```
5. Mount the secret on the relevant Cloud Run JOBs / services
   (`ytfactory-stats-refresh`, `ytfactory-render-worker-v2`,
   `ytfactory-web`):

   ```bash
   gcloud run jobs update ytfactory-stats-refresh \
     --project=ytfactory-prod-v2 --region=asia-southeast1 \
     --update-secrets=/secrets/youtube-token-<slug>/value=youtube-token-<slug>:latest
   ```

### "An OAuth token is dying — alert!"

The daily `ytfactory-token-health-cron` should already be alerting.
Manually inspect:

```bash
# Laptop (uses local config):
python -m pipeline.research.youtube --token-status

# Cloud (uses Secret Manager mounts):
AGENT_TOKEN=$(gcloud secrets versions access latest --secret=ytfactory-agent-token --project=ytfactory-prod-v2)
curl -s -H "Authorization: Bearer $AGENT_TOKEN" \
     https://ytfactory-web-7hwnzw7lya-as.a.run.app/api/cron/token-health | jq
```

> **`source: missing` for every account?** That means
> ytfactory-web doesn't have the `youtube-token-<account>` secrets
> mounted. Wire all 9 onto the service:
>
> ```bash
> MOUNTS=$(printf '/secrets/youtube-token-%s/value=youtube-token-%s:latest,' \
>   mystoriesanimated cosmosdecoded historyrecapped hindutavaanimated \
>   sportsrecapped scrollpulse rhymetimejunction afddfdf zgsbhqszdheo \
>   | sed 's/,$//')
> gcloud run services update ytfactory-web \
>   --project=ytfactory-prod-v2 --region=asia-southeast1 \
>   --update-secrets="$MOUNTS"
> # then bake into cloud/web-server/deploy.sh per the
> # cloud_run_set_secrets_destructive.md rule
> ```

Fix the broken account:

```bash
python -m pipeline.upload.upload auth refresh --account <broken_slug>
gcloud secrets versions add youtube-token-<broken_slug> \
  --project=ytfactory-prod-v2 \
  --data-file="$HOME/.config/ytfactory/youtube_token_<broken_slug>.json"
```

Subsequent cloud refreshes pick up the new version on next cold start
(or restart the JOB / service).

### "Force a stats refresh now"

Two options:

```bash
# Manual trigger via the JOB
gcloud run jobs execute ytfactory-stats-refresh \
  --project=ytfactory-prod-v2 --region=asia-southeast1 --wait

# Or via the dashboard API (admin-only on the web service)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     -X POST \
     https://ytfactory-web-7hwnzw7lya-as.a.run.app/api/dashboard/refresh-research-cache
```

The dashboard's `latest_fetch` field updates within a minute of the
JOB completing.

### "Pause / resume the hourly cron"

```bash
./cloud/stats-refresh/wire_scheduler.sh --pause
./cloud/stats-refresh/wire_scheduler.sh --resume
```

## Rollback

To disable the GCS-backed cache and fall back to the local FS:

```bash
gcloud run services update ytfactory-web \
  --project=ytfactory-prod-v2 --region=asia-southeast1 \
  --remove-env-vars=YTFACTORY_STATE_BUCKET
```

`pipeline.research.youtube` falls through to `data/research/youtube/`
on disk; works for laptop dev. The dashboard endpoint also falls back
because `_dashboard_state_bucket()` returns None when the env is unset.

## History — what the post-mortem fixed (2026-05-10)

Before this work, the chain had four compounding bugs:

1. **`pipeline/research/youtube.py::PROJECT_ROOT`** resolved one level
   too deep so `YOUTUBE_DIR` pointed at a non-existent path → every
   `load_account()` returned `None`.
2. **`iter_channel_configs()`** walked `PROJECT_ROOT.iterdir()` for
   per-channel root dirs, which the prod image no longer has after
   the 2026-05-10 hygiene pass → returned `[]` on prod (and the
   laptop).
3. **A test fixture** (`VideosBatchingTest::_PointAtTmp` in
   `tests/test_pipeline_youtube_stats.py`) didn't fully isolate
   `YOUTUBE_DIR` so `fetch_account()` wrote `Biggie/UC_b/V_000`
   placeholder data into the real laptop cache, which got committed
   to git, which got baked into every prod deploy.
4. **`web/server.py::dashboard_videos`** shadowed the GCS-aware
   canonical handler from `control/routes/dashboard_routes.py`,
   returning the FS-walk version that finds nothing on prod.

All four are now closed:

- `pipeline.paths.PROJECT_ROOT` is the single source of truth.
- `iter_channel_configs()` reads `pipeline/channels/*.yaml`.
- `tests/conftest.py` autouse fixture + `_assert_safe_to_write()`
  guard turns the corruption attempt into a loud `RuntimeError`.
- The shadow handler is deleted; the canonical control router wins.

Plus a self-healing loop on top:

- **Token-rotation write-back** (`_persist_token`) keeps Secret
  Manager versions current as the cloud refreshes access tokens.
- **`RefreshTokenLost`** typed exception turns silent token-degrade
  into an actionable alert.
- **`/api/admin/token-health`** + daily cron + alert webhook
  surfaces broken accounts within 24h of a refresh-token failure.
- **`/api/dashboard/refresh-research-cache`** lets the operator
  trigger an out-of-band refresh without blocking the dashboard
  request thread.

## Anti-patterns observed (2026-05-10 deploy session)

The first prod deploy after the post-mortem surfaced four secondary
gotchas that aren't bugs in the design above but are easy to step on
again. Captured here so the next operator's hour isn't burned by the
same wires.

1. **Two separate code paths feed the dashboard.** Setting
   `YOUTUBE_API_KEY` lit up the cards (`/api/dashboard/videos`) but
   left the channels gallery (`/api/channels`, used by the dashboard
   hero cards AND the channels tab AND the create wizard) returning
   `subscribers=None`. Fixed by adding
   `_live_channel_via_api_key()` in
   `pipeline/schemas/customization.py` as the cold-start fallback
   for `_personality_for(account)`. **Always verify both endpoints
   return real data** when fixing a "no stats" symptom.

2. **`gcloud run services update --set-secrets` is destructive.** The
   first redeploy of `ytfactory-web` after wiring `YOUTUBE_API_KEY`
   via `--update-secrets` blanked the cards because
   `cloud/web-server/deploy.sh`'s `--set-secrets` line didn't include
   the key. Same wire would fire on every subsequent deploy. Now the
   key + the `youtube-channel-ids` mount are baked into the deploy
   script. **Rule documented in
   [`cloud_run_set_secrets_destructive.md`](./cloud_run_set_secrets_destructive.md);
   audit recipe there enumerates every Cloud Run service.**

3. **Always validate secret payload before pushing.** A first
   `gcloud secrets create youtube-api-key --data-file=-` happily
   accepted `__FILL_IN__` (14 chars, the `.env` template placeholder)
   and made it version 1. Caught + destroyed the version before
   anything mounted it, but the right gate is to validate length /
   format before push:
   ```bash
   if [ ${#YT_KEY} -ne 39 ]; then echo "WRONG LENGTH ${#YT_KEY}"; exit 1; fi
   ```
   Real Google API keys are 39 chars and start with `AIza`. OAuth
   tokens are JSON blobs with `refresh_token`. Either way: cheap
   client-side check before `gcloud secrets`.

4. **Diagnose env vars BEFORE rebuilding the image.** The 5-min
   `gcloud builds submit` cycle is wasted when the actual fix is a
   `--update-env-vars` / `--update-secrets` change. First-pass
   diagnosis recipe for any prod symptom:
   ```bash
   gcloud run services describe <svc> --project=<p> --region=<r> \
     --format=json | jq '.spec.template.spec.containers[0].env'
   ```
   That payload reveals missing keys / mounted secrets / project +
   region drift in one command. Image rebuild is the LAST step, not
   the first.

