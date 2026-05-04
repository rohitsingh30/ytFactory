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
  - `list_upload_records()` — yields `(channel, slug, record)`
- `pipeline/upload.py:_mirror_record_to_gcs` — best-effort GCS push.
  Disable via `YTFACTORY_DASHBOARD_GCS_SYNC=0`.
- `control/dashboard_routes.py:_enumerate_uploads` — GCS-first read
  with disk fallback. Surfaces `record_source` in the API payload
  (e.g. `"gcs:18 disk:1"` or `"gcs:err(...) disk:19"`) for debugging.
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
