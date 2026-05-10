# Cloud dashboard reads upload records from GCS

The Cloud Run dashboard at `/dashboard` lists every uploaded video.
Originally it walked baked-in `<channel>/uploads/**/*.json` from the
Docker image, which meant every new laptop upload required a redeploy
to appear on the cloud. Now the laptop mirrors each record to GCS on
write, and the cloud reads from there — no redeploy needed.

## Data flow

```
laptop:  pipeline.upload.write_upload_record(...)
            ├─ writes <channel>/uploads/[<niche>/]<slug>.json   (source of truth)
            └─ pushes gs://<bucket>/upload-records/
                       <channel>/[<niche>/]<slug>.json          (mirror)
                                                                       │
                                                                       ▼
cloud:   control.dashboard_routes._enumerate_uploads()
            └─ lists gs://<bucket>/upload-records/
                ├─ falls back to disk if GCS empty/unreachable
                └─ dedupes (channel, slug) across both sources
```

## Files

- `control/storage.py`
  - `upload_record_uri(rel_key)` — builds the gs:// URI
  - `upload_record_rel_key(local_path, project_root)` — strips repo
    root + the literal `/uploads/` middle segment
  - `list_upload_records()` — returns `[(channel, slug, record), ...]`.
    **60 s in-process TTL cache** keyed by bucket name + 16-worker
    parallel `download_bytes` fan-out (2026-05-10 perf pass — see
    `docs/web_perf_pass_2026_05_10.md`).
  - `bust_upload_records_cache()` — public hook for writers. Called
    from `pipeline/upload/upload.py::_mirror_record_to_gcs` after a
    successful mirror so the new record appears within one poll cycle.
- `pipeline/upload/upload.py:_mirror_record_to_gcs` — best-effort GCS push.
  Disable via `YTFACTORY_DASHBOARD_GCS_SYNC=0`. On success also calls
  `control.core.storage.bust_upload_records_cache()` (cross-module bust:
  writer uses `control.storage`, dashboard reads via
  `control.core.storage` — they're parallel modules with separate
  caches).
- `control/dashboard_routes.py:_enumerate_uploads` — GCS-first read
  with disk fallback. Surfaces `record_source` in the API payload
  (e.g. `"gcs:18 disk:1"` or `"gcs:err(...) disk:19"`) for debugging.
- `web/server.py::_iter_all_uploads` + `_list_uploads_gcs` — separate
  enumeration over `YTFACTORY_STATE_BUCKET`'s
  `<channel>/uploads/<slug>.json` layout. Same TTL+parallel pattern:
  60 s `_DASHBOARD_UPLOADS_CACHE` keyed by bucket; 8-worker channel
  fan-out + 16-worker per-channel fan-out. Bust via
  `_bust_dashboard_uploads_cache()`.
- `scripts/sync_upload_records_to_gcs.py` — one-shot backfill of
  existing records on disk to GCS.

## GCS layout

`gs://<bucket>/upload-records/<channel>/[<niche>/]<slug>.json`

Mirrors the on-disk layout one-to-one, minus the literal `/uploads/`
middle segment (implicit in the prefix). Examples:

| local                                                                   | gcs                                                                     |
|-------------------------------------------------------------------------|-------------------------------------------------------------------------|
| `historyrecapped/uploads/battle-of-britain-few.json`                    | `upload-records/historyrecapped/battle-of-britain-few.json`             |
| `mystoriesanimated/uploads/reddit_amitheasshole/foo.json`               | `upload-records/mystoriesanimated/reddit_amitheasshole/foo.json`        |

## First-time setup on the laptop

GCS writes need Application Default Credentials:

```
gcloud auth application-default login
```

Then either let the next pipeline upload mirror itself, or backfill
existing records in one go:

```
.venv/bin/python scripts/sync_upload_records_to_gcs.py            # all
.venv/bin/python scripts/sync_upload_records_to_gcs.py --dry-run  # preview
.venv/bin/python scripts/sync_upload_records_to_gcs.py --channel historyrecapped
```

## Dockerfile note

The `<channel>/` COPY lines in `Dockerfile` still bake configs +
narrations + uploads into the image because **the scheduler**
(`control/scheduler.py:_uploaded_slugs`) still reads them from disk.
Once the scheduler is migrated to GCS too, the COPY lines can drop.
For now, the dashboard staleness problem is solved without touching
the scheduler.
