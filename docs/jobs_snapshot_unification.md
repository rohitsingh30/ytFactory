# Jobs snapshot — unified `/api/jobs/{id}` surface + Firestore persistence

> **Established 2026-05-10** after the user reported `/app/render/<id>`
> 404'ing on every render submitted via a `/make-*` skill. Two stacked
> bugs, both in `web/server.py`. Fix shipped in revision
> `ytfactory-web-00021-cg7` (asia-southeast1).

## TL;DR

`web/server.py` keeps **three parallel job stores** fronting a single
read endpoint:

- `JOBS` — niche-driven (legacy in-memory; `POST /api/render` legacy path)
- `SCRIPT_JOBS` — skill-driven (Firestore-mirrored; `POST /api/jobs/from_script`)
- **Control-plane Firestore `jobs/`** — web-next "Create" form
  (`POST /api/render` modern path → `control.core.jobs._enqueue_render_job`)

The web-next render-detail page (`/app/render/<id>`) polls a single
endpoint, `GET /api/jobs/{id}`. **That endpoint must fall through
across all three stores on miss**, and the same handler tree must
adapt the three record shapes into a single response that satisfies
the new UI's `JobView` AND legacy `job_snapshot` consumers.

Separately: SCRIPT_JOBS was an in-memory dict, so Cloud Run revision
rollover wiped jobs mid-render. The **prod control plane runs with
`YTFACTORY_QUEUE_BACKEND=firestore`** (set in
`cloud/web-server/deploy.sh`), so `web/script_jobs_store.py` mirror-
writes through to a `script_jobs` Firestore collection, hydrates on
boot, and flushes on graceful shutdown. The control-plane store
(`control.core.jobs._FirestoreJobs`) is also Firestore-backed end-to-
end; same rollover safety, no shutdown flush needed because every
write is synchronous.

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
        # Tier 2: skill-submitted, Firestore-backed.
        rec = SCRIPT_JOBS.get(job_id)
        if rec:
            return _script_job_to_snapshot(rec)
        # Tier 3: control-plane Firestore `jobs/` — wrapped in
        # try/except so a Firestore outage can't turn legacy 404s
        # into 500s. ID-shape gate (32-hex) keeps typo'd legacy IDs
        # 404'ing cleanly while real control-plane IDs surface 502
        # if Firestore truly fails.
        try:
            from control.core import jobs as control_jobs
            doc = control_jobs.get_job(job_id)
        except Exception as e:
            logger.warning("control-plane lookup failed", exc_info=True)
            if _CONTROL_JOB_ID_RE.fullmatch(job_id):
                raise HTTPException(502, f"control-plane lookup failed: {e}")
            doc = None
        if doc:
            return _control_job_to_snapshot(job_id, doc)
        raise HTTPException(404, "job not found")
    # ... legacy JOBS-shape response ...
```

Both `_script_job_to_snapshot(rec)` and `_control_job_to_snapshot(job_id, doc)`
return a dict with **both** the legacy keys (`state`/`niche`/`mp4_url`/`events`)
AND the new `JobView` keys (`status`/`channel`/`topic`/`preview_url`/
`created_at`/`log_tail`/`proposal`). UIs that don't know a key
ignore it; everything keeps working with no client coordination.

### Why two adapters and not one

Control-plane docs use **datetime objects** (from
`control.core.jobs._utcnow()`), while SCRIPT_JOBS records use **epoch
floats** (`time.time()`). The legacy `_isoformat(epoch)` helper does
`float(epoch)`, which raises on a `datetime`. The fix is a
**`_isoformat_value()`** helper that handles `datetime` (naive→UTC,
aware→UTC), epoch numeric, ISO string, and `None` — used by
`_control_job_to_snapshot`. `_script_job_to_snapshot` keeps using
the original `_isoformat()` so its existing test contract is
unchanged.

Control-plane statuses (`pending/rendering/uploading/researching/
done/failed/cancelled`) are already in JobView vocabulary; only
the legacy `state` slot needs remapping (see table below).

Preview URL gating: the control-plane fall-through sets
`preview_url = /api/jobs/{id}/preview.mp4` ONLY when the doc actually
has a servable artifact — i.e. ``short_uri`` is set OR
``preview_local_path`` exists. Mirrors `control.routes.render_routes._doc_to_view`.
Pre-2026-05-12 this was gated on ``status in {done, uploading}`` —
but the worker flips ``status="uploading"`` BEFORE the actual GCS
upload completes, so ``short_uri`` is still None in that window and
the dashboard's `<video>` element fired a noisy 404 GET. Memory:
`feedback_preview_url_artifact_gate.md`. SCRIPT_JOBS uses
`/api/jobs/{id}/short` (different endpoint, different handler) so
the two adapters point at different URLs.

State→status maps for both fall-through tiers:

| SCRIPT_JOBS `state`   | JobView `status` |
|-----------------------|------------------|
| `running`             | `rendering`      |
| `done`                | `done`           |
| `done_no_mp4_found`   | `done`           |
| `failed`              | `failed`         |
| `cancelled`           | `cancelled`     |

| Control `status` | Legacy `state` |
|------------------|----------------|
| `pending`        | `queued`       |
| `rendering`      | `running`      |
| `uploading`      | `running`      |
| `researching`    | `running`      |
| `done`           | `done`         |
| `failed`         | `failed`       |
| `cancelled`      | `cancelled`    |

### The shadowing trap (FastAPI route precedence)

`control/routes/render_routes.py` ALSO declares
`@router.get("/api/jobs/{job_id}")`. After the Phase-4 cutover
(2026-05-09), `web/server.py` includes that router via
`app.include_router(_control_render_router)` near the bottom of
the file (~line 5332). The native `@app.get("/api/jobs/{job_id}")`
at line ~4431 is registered FIRST as Python evaluates the module
top-to-bottom — and Starlette/FastAPI matches the **first**
registered route on identical templates.

Net effect: the included control-router handler is **dead code**
for this path. The fall-through MUST live inside the surviving
`web/server.py:job_snapshot`, not in the include. Pre-fix, this
exact mismatch is what 404'd every chat-confirmed render even
though the control router would have resolved them correctly in
isolation. The audit recipe to detect future cases:

```bash
# Find @app.{get,post,...} declarations that share a template
# with an `@router.…` declaration in any included router.
grep -rn '@app\.\(get\|post\|put\|delete\|patch\)("' web/ control/ \
  --include='*.py' | sort -u
```

If a duplicate template appears, either delete the included one
OR implement the fall-through inside the surviving handler (the
latter is what we do because the surviving handler also serves
stores the included router doesn't know about).

Channel/topic best-effort parse from the renderer cmd
(`--channel <yaml>` → first path component;
`--script <json>` → file stem). See
`web/server.py::_channel_topic_from_cmd`.

The mp4 endpoint `/api/jobs/{id}/short` follows the same pattern:
fall through to SCRIPT_JOBS, then `_serve_script_job_mp4(rec)` —
local `mp4_path` → `FileResponse`, `gs://...` → 302 to a v4-signed
URL (15 min). The same helper is reused by
`/api/jobs/from_script/{id}/mp4` so the signing logic exists exactly
once. Control-plane jobs use `/api/jobs/{id}/preview.mp4` instead
(served by `control.routes.render_routes.preview_mp4` — different
path, no shadowing, no fall-through needed).

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
- `tests/test_jobs_id_fallthrough.py` — **35 tests** (was 18 pre-
  2026-05-12) covering the user-reported 404 fix end-to-end via
  `TestClient`, state-mapping, mp4 fall-through, the channel/topic
  cmd parser, the new `_isoformat_value` normalizer (datetime/epoch/
  string/None contracts), and the control-plane fall-through across
  pending/rendering/done/failed shapes including the 502-on-Firestore-
  failure / 404-on-typo'd-legacy-id distinction.

Both must stay green when anyone touches `web/server.py:job_snapshot`,
`web/server.py:job_short`, `web/server.py:_run`, `_run_cloudrun`,
`web/server.py:_control_job_to_snapshot`, `_isoformat_value`, or
`web/script_jobs_store.py`.

## Deploy history

- **2026-05-10** — SCRIPT_JOBS fall-through (revision
  `ytfactory-web-00021-cg7`). Fixed skill-submitted render 404s.
- **2026-05-12** — Control-plane Firestore `jobs/` fall-through
  (revision `ytfactory-web-00062-thr`). Fixed chat-confirmed and
  form-confirmed render 404s. User-reported failure mode:
  `404 on /api/jobs/2f049cd27b0446558a4bb641dfd498a2` (a 32-char
  hex job_id, the format `_enqueue_render_job` mints; the legacy
  paths use `…hex[:10]` so ID shape is the diagnostic for which
  tier the job belongs to).

## Cross-references

- `docs/full_cloud_cutover_2026_05_09.md` § Phase 4 — original port
  of `/api/jobs/from_script` to the (then-separate) control plane.
- `docs/architecture.md` — Cloud Run service overview (note: that doc
  has a stale us-central1 / `ytfactory-prod` URL; current prod is
  asia-southeast1 / `ytfactory-prod-v2`).
- Skill consumers: `pipeline/cloud/skill_dispatch.py::submit_render`
  + `wait_for_job`. They still poll `/api/jobs/from_script/{id}` for
  log-tail; the new `/api/jobs/{id}` surface is for the UI only.
