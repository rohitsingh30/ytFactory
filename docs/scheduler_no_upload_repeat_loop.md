---
name: Scheduler re-renders the same slug forever (cron + --no-upload loop)
description: cron's _next_unrendered keys off uploads/<slug>.json existence, but render-worker-v2 invokes pipeline.render.shorts with --no-upload, so successful renders never advance the cron pointer; same slug renders forever
type: project
---

# Scheduler re-renders the same slug forever

**Established 2026-05-13** after Firestore audit showed the same
slug (`mystoriesanimated/aita-for-ruining-the-cake-j-abb411`)
rendered to `status=DONE` **12 times in 4 days** — 12 distinct mp4s
in GCS, zero YouTube uploads, ~4.4 hours of Cloud Run JOB compute
per slug burned for redundant work. Six other anchor slugs (one
per channel) showed the same pattern: 21-22 renders each in 36 h.

## The loop

1. **Cloud Scheduler** `ytfactory-tick` fires every 30 min →
   `POST /api/scheduler/tick` on `ytfactory-web`.
2. `control/core/scheduler.py::tick()` round-robins channels and
   calls `_next_unrendered(channel)` for each.
3. `_next_unrendered` (and `_next_unrendered_gcs` on cloud) returns
   the OLDEST `gs://<bucket>/<channel>/.../narrations/<slug>.json`
   whose corresponding `gs://<bucket>/<channel>/.../uploads/<slug>.json`
   does NOT exist.
4. Picked slug → enqueued render job → Cloud Run JOB executes
   `cloud/render-worker-v2/entrypoint.py`.
5. Worker invokes `pipeline.render.shorts` with **`--no-upload`**
   (entrypoint.py:921, comment: *"YouTube upload happens via
   /api/jobs/{id}/publish"*).
6. Render succeeds → mp4 lands in GCS → Firestore job marked `DONE`.
7. **Crucially: no `uploads/<slug>.json` is ever written** because
   the publish step is gated behind a manual user action.
8. 30 min later the cron picks the SAME oldest unrendered slug →
   loop forever.

## Why it slipped

The contract `_next_unrendered` enforces is "no upload record =
needs rendering". The worker's `--no-upload` flag breaks the
contract by separating "rendered" from "uploaded". Both halves
are individually defensible (manual publish gate respects user
control; `_next_unrendered` is the simplest possible check) but
together they livelock.

Compounding: the discover agent generates **duplicate narrations
with random hex suffixes** for the same Reddit story
(`aita-for-ruining-the-cake-j-{0fd499,133bb5,1b77ad,3944cf,...}`
— 20+ files). So even if the loop were broken by writing an
upload record per render, the backlog regenerates itself.

## Fix space (not yet implemented)

Picking ONE of these fixes — operator decision pending — closes
the loop:

**Option A — write a "rendered marker" sibling to the upload record.**
After mp4 lands in GCS, write `uploads/<slug>.rendered.json`
(distinct from the YouTube `uploads/<slug>.json` published-marker)
that `_next_unrendered` ALSO consults. Decouples "in the cron's
backlog" from "published to YouTube".

**Option B — change `_next_unrendered` to consult Firestore.**
A slug with ≥1 `jobs/<id>` doc in `status in (rendering, done,
uploading)` for the same `(channel, slug)` is no longer "unrendered".
Sidesteps the GCS marker question entirely; needs a composite
index on `(channel, slug, status)`.

**Option C — auto-publish on cron-triggered renders.**
Add a `--auto-publish` worker flag. Cron-driven renders flip to
`status=public` immediately; manual UI renders keep the gate.
Cleanest but needs the operator to actively want continuous
publishing per channel.

**Option D — treat repeated DONE on the same slug as a no-op.**
Cron picks the slug, worker checks `jobs` collection for prior
`DONE` jobs on the same `(channel, slug)`, exits early if found.
Closest to "do nothing dumb"; doesn't fix the backlog problem.

## Aux issue: discover-side duplicate narrations

The narration writer also lacks a content-hash dedup. Same Reddit
story title + body → 20+ distinct `<slug>-j-XXXXXX.json` files
(different random hashes, identical content). Even after fixing
the cron loop, this floods the backlog. Fix lives in the discover
pipeline (`pipeline/discover/`); separate post-mortem.

## Stop-the-bleeding (interim)

Until one of the options above ships, channel-level mitigation:

- Set `in_rotation: false` for channels whose anchor slug isn't
  scheduled for repeated publish.
- Manually click "Publish" on the most recent successful mp4 for
  each anchor slug to write the upload record and let the cron
  move on.
- Rotation pruning (audited 2026-05-13): `rhymetimejunction`,
  `scrollpulse` flipped to `in_rotation: false` (each had its own
  separate failure mode but the loop amplified both). Real
  rotation is now `historyrecapped + hindutavaanimated +
  mystoriesanimated + sportsrecapped + cosmosdecoded` (5 channels,
  not 7).

## Memory pointer

`feedback_scheduler_no_upload_repeat_loop.md`

## Related

- `control/core/scheduler.py:113-213` — `_uploaded_slugs` +
  `_next_unrendered` definitions (the source of truth for "needs
  rendering")
- `cloud/render-worker-v2/entrypoint.py:917-922` — `--no-upload`
  flag passed to `pipeline.render.shorts` (the missing-upload
  side)
- `pipeline/upload/upload.py:1453-1505` — `write_upload_record`
  (writes `uploads/<slug>.json`; only called from `pipeline.upload`
  CLI / `/api/jobs/{id}/publish`, NOT from the worker render path)
- `docs/full_cloud_cutover_2026_05_09.md` — original GCS-aware
  scheduler change; this loop is a regression that surfaced after
  cron started firing 24/7
