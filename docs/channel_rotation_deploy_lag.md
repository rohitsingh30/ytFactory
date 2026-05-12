---
name: channels.yaml in_rotation flag deploy-lag
description: pipeline/channels.yaml is read by the deployed ytfactory-web service; local edits to in_rotation don't take effect until web is redeployed; document scrollpulse + rhymetimejunction drift symptoms
type: project
---

# `channels.yaml` `in_rotation` flag deploy-lag

**Symptom:** edited `pipeline/channels.yaml` to set
`channel.in_rotation: false`, but the cron keeps picking the
channel every 30 min for hours afterwards.

**Cause:** `control/core/scheduler.py::CHANNEL_ROTATION` is
constructed at import time from `pipeline.channels.channel_rotation()`,
which reads `pipeline/channels.yaml` from the running container's
file system. The cron handler runs inside `ytfactory-web` (Cloud
Run service), which has its own image; until that image is
re-built and re-deployed, the rotation list reflects whatever YAML
shipped with the LAST `gcloud run deploy` of `ytfactory-web`.

**Audited 2026-05-13:**

- `scrollpulse` was set `in_rotation: false` in local YAML but
  cron still picked it (21 attempts on `aita-hibachi` in 36 h).
  The deployed web image had stale YAML with `scrollpulse:
  in_rotation: true`.
- Same day, `rhymetimejunction` flipped to `in_rotation: false`
  locally as part of the cron-failure mitigation. **Will NOT take
  effect until `ytfactory-web` is redeployed.** Documented inline
  on the `pipeline/channels.yaml` entry.

**Symptoms operators see:**

- Dashboard `/api/queue` keeps showing tasks for the supposedly-
  disabled channel.
- Firestore `jobs/*` accumulates failures for the same anchor
  slug indefinitely (compounded by the
  [no-upload repeat loop](./scheduler_no_upload_repeat_loop.md)).

**Fix when changing `channels.yaml`:**

```bash
# 1. Edit pipeline/channels.yaml
# 2. Run the channels-lockstep tests to confirm validity:
.venv/bin/python -m pytest tests/ -k "channel_yaml or rotation" -q

# 3. Redeploy ytfactory-web so the running container sees the new YAML:
bash cloud/web-server/deploy.sh

# 4. Wait for next /api/scheduler/tick (cron fires every 30 min,
#    timezone Asia/Kolkata) — verify the disabled channel is no
#    longer enqueued in /api/queue.
```

**Cleaner long-term fix (not implemented):** read the rotation
list from `gs://<state-bucket>/_runtime/channels_rotation.json` so
the laptop edit + a single `gsutil cp` drains in seconds without a
container redeploy. Tracked as a follow-up; the 7-channel rotation
is small enough that "edit + redeploy web" is acceptable for now.

**Memory pointer:** `feedback_channel_rotation_deploy_lag.md`

**Related:**

- `pipeline/channels.yaml` — source of truth for rotation
- `control/core/scheduler.py:48-50` — import-time `CHANNEL_ROTATION`
  capture
- `pipeline/channels.py:217` — `channel_rotation()` reader
- `cloud/web-server/deploy.sh` — the redeploy command
- `docs/scheduler_no_upload_repeat_loop.md` — the upstream loop
  this lag amplifies
