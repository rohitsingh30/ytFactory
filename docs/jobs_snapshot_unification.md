# Jobs snapshot — unified `/api/jobs/{id}` surface + Firestore persistence

> **Established 2026-05-10** after the user reported `/app/render/<id>`
> 404'ing on every render submitted via a `/make-*` skill. Two stacked
> bugs, both in `web/server.py`. Fix shipped in revision
> `ytfactory-web-00021-cg7` (asia-southeast1).

## TL;DR

`web/server.py` keeps **two parallel job stores**:

- `JOBS` — niche-driven (web form → `POST /api/render`)
- `SCRIPT_JOBS` — skill-driven (skills → `POST /api/jobs/from_script`)

The web-next render-detail page (`/app/render/<id>`) polls a single
endpoint, `GET /api/jobs/{id}`. **That endpoint must fall through to
SCRIPT_JOBS on JOBS-miss**, and the same handler tree must adapt the
two record shapes into a single response that satisfies the new UI's
`JobView` AND legacy `job_snapshot` consumers.

Separately: SCRIPT_JOBS was an in-memory dict, so Cloud Run revision
rollover wiped jobs mid-render. The **prod control plane runs with
`YTFACTORY_QUEUE_BACKEND=firestore`** (set in
`cloud/web-server/deploy.sh`), so `web/script_jobs_store.py` mirror-
writes through to a `script_jobs` Firestore collection, hydrates on
boot, and flushes on graceful shutdown.

## The class-of-bug

When two in-process job registries share a path prefix
(`/api/jobs/...`) but only one is queried by the read endpoint, every
record from the other store 404s in the UI. This is a generic
"namespace mismatch on a unified read endpoint" — easy to repeat
when any of these other in-memory stores in `web/server.py` get a
new caller:

| store | created via | currently visible at |
|---|---|---|
| `JOBS` | `POST /api/render` | `GET /api/jobs/{id}` |
| `SCRIPT_JOBS` | `POST /api/jobs/from_script` | `GET /api/jobs/{id}` (since 2026-05-10) + `GET /api/jobs/from_script/{id}` |
| `CRITIQUE_JOBS` | server-side critique | `GET /api/critique/{id}` only |
| `UPLOAD_JOBS` | per-render publish | `GET /api/upload/{id}` only |
| `CRON_JOBS` | scheduled drain | `GET /api/cron/drain/{id}` only |

If any future change exposes one of those last three at
`/api/jobs/{id}`, apply the same fall-through pattern below.

## The pattern: fall-through + unified-shape adapter

```python
# web/server.py
@app.get("/api/jobs/{job_id}")
async def job_snapshot(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        rec = SCRIPT_JOBS.get(job_id)
        if rec:
            return _script_job_to_snapshot(rec)
        raise HTTPException(404, "job not found")
    # ... legacy JOBS-shape response ...
```

`_script_job_to_snapshot(rec)` returns a dict with **both** the
legacy keys (`state`/`niche`/`mp4_url`/`events`) AND the new
`JobView` keys (`status`/`channel`/`topic`/`preview_url`/
`created_at`/`log_tail`/`proposal`). UIs that don't know a key
ignore it; everything keeps working with no client coordination.

State→status map for SCRIPT_JOBS (the two surfaces use different
vocabularies):

| SCRIPT_JOBS `state`   | JobView `status` |
|-----------------------|------------------|
| `running`             | `rendering`      |
| `done`                | `done`           |
| `done_no_mp4_found`   | `done`           |
| `failed`              | `failed`         |
| `cancelled`           | `cancelled`      |

Channel/topic best-effort parse from the renderer cmd
(`--channel <yaml>` → first path component;
`--script <json>` → file stem). See
`web/server.py::_channel_topic_from_cmd`.

The mp4 endpoint `/api/jobs/{id}/short` follows the same pattern:
fall through to SCRIPT_JOBS, then `_serve_script_job_mp4(rec)` —
local `mp4_path` → `FileResponse`, `gs://...` → 302 to a v4-signed
URL (15 min). The same helper is reused by
`/api/jobs/from_script/{id}/mp4` so the signing logic exists exactly
once.

## The persistence pattern: dict-subclass with Firestore mirror

`web/script_jobs_store.py::ScriptJobsStore`:

- Subclasses `dict` so every existing call site
  (`SCRIPT_JOBS[id] = {...}`, `rec = SCRIPT_JOBS[id]; rec["state"] =
  "done"`, `pop`/`clear`/`values`) keeps working unchanged.
- Backend chosen by **existing** `YTFACTORY_QUEUE_BACKEND` env (same
  switch the niche-driven `JOBS` Firestore wrapper already uses).
- `__setitem__` / `__delitem__` / `pop` track dirty + mirror deletes
  to Firestore.
- `_sweep_running()` re-flags every non-terminal record dirty each
  sweep — catches `rec[k] = v` in-place mutations that the existing
  renderer code does without calling `mark_dirty` explicitly.
- `flush_dirty_loop(interval_s=5)` is started once at app startup
  (`SCRIPT_JOBS.start_flush_task()` in `lifespan()`); cancellation
  does one final flush so a `kill -TERM` from Cloud Run doesn't drop
  in-flight verdicts.
- `flush(job_id)` is called explicitly at every terminal-state
  transition (`_run()` `finally:`, `_run_cloudrun()` after each
  state-write) for instant durability — the periodic loop is just a
  safety net.
- `hydrate()` runs once at app startup BEFORE traffic; loads every
  Firestore doc via `super().__setitem__` so hydrated records do
  NOT get re-flagged dirty.

### What still has the volatility bug

`JOBS`, `CRITIQUE_JOBS`, `UPLOAD_JOBS`, `CRON_JOBS` in
`web/server.py` are still plain in-memory dicts. The same
`ScriptJobsStore` template trivially extends to each — keep the
`script_jobs_store.py` module name generic enough to host more
collections (today it has only `ScriptJobsStore`; tomorrow can
factor a base class). Track:

- `JOBS` is the bigger fix because it stores `Job` *dataclasses* (not
  dicts). Either (a) serialise via `dataclasses.asdict` + rehydrate
  in `__init__`, or (b) flatten to dict at every mutation site (more
  invasive). Decide before extending.

## How to roll this out

Already shipped. For the record:

```bash
./cloud/web-server/deploy.sh    # takes ~3-4 min
```

The script already sets `YTFACTORY_QUEUE_BACKEND=firestore` (line 70
of `cloud/web-server/deploy.sh`). No env mutation, no schema
migration — the `script_jobs` Firestore collection auto-creates on
first write.

## Tests

- `tests/test_script_jobs_store.py` — 16 tests covering memory mode,
  firestore mode (with a fake firestore client), the periodic
  sweeper, and graceful-shutdown flush.
- `tests/test_jobs_id_fallthrough.py` — 18 tests covering the
  user-reported 404 fix end-to-end via `TestClient`, state-mapping,
  mp4 fall-through, and the channel/topic cmd parser.

Both must stay green when anyone touches `web/server.py:job_snapshot`,
`web/server.py:job_short`, `web/server.py:_run`, `_run_cloudrun`, or
`web/script_jobs_store.py`.

## Cross-references

- `docs/full_cloud_cutover_2026_05_09.md` § Phase 4 — original port
  of `/api/jobs/from_script` to the (then-separate) control plane.
- `docs/architecture.md` — Cloud Run service overview (note: that doc
  has a stale us-central1 / `ytfactory-prod` URL; current prod is
  asia-southeast1 / `ytfactory-prod-v2`).
- Skill consumers: `pipeline/cloud/skill_dispatch.py::submit_render`
  + `wait_for_job`. They still poll `/api/jobs/from_script/{id}` for
  log-tail; the new `/api/jobs/{id}` surface is for the UI only.
