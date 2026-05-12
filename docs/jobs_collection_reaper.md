# Firestore `jobs` collection — no reaper for stuck-pending docs

**Class:** CLASS-OF-BUG (orphan resource accumulation)
**Surfaced:** 2026-05-13
**Affected:** `/api/queue` Queued column (every operator who looks at
the dashboard sees the ghost forever)

## What happens

1. Operator (or agent) calls `POST /api/render/confirm` →
   `control/core/jobs.py::create_render` writes a Firestore
   `jobs/<id>` doc with `status=pending`, then calls
   `cloud_run.trigger_render_job(job_id)` which dispatches a
   Cloud Run JOB execution.
2. On success, `create_render` updates the doc:
   `stage="dispatching"`, `cloud_execution=<execution-name>`.
3. The render-worker-v2 JOB is supposed to flip
   `status` → `rendering` → `done`/`failed` as it progresses.
4. **If the worker dies before its first writeback** — e.g. Cloud
   Run reports `Internal error running task` and the container
   exits before any `jobs_mod.update(...)` call — the doc stays at
   `status=pending, stage=dispatching` **forever**.

`/api/queue` (`control/routes/render_routes.py:705`) reads:

```python
db.collection("jobs").where("status","in",["pending","rendering","uploading"])
```

Nothing in the codebase reaps stuck-pending jobs from this
collection. (The `web/server.py::_periodic_queue_reaper` loop only
calls `q.reap_expired()` on the **`agent_tasks`** collection — the
laptop-agent leasing protocol — NOT on `jobs`.) So the ghost stays
in the Queued column on every dashboard load until somebody
manually deletes the doc.

## Concrete example caught 2026-05-13

```
job_id:       26e0645dfece4691b4ca77e4f6532e38
channel:      airecap
topic:        "Live cloud-native test render via Cloud Run Job"
status:       pending
stage:        dispatching
created_at:   2026-05-09 10:51:56 UTC
updated_at:   2026-05-09 10:51:58 UTC  (never touched again)
cloud_execution: projects/.../jobs/ytfactory-render-worker-v2/executions/ytfactory-render-worker-v2-kpc67
```

The execution itself completed at 11:23 UTC the same day with:

```
status: Completed
message: Task ytfactory-render-worker-v2-kpc67-task0 failed with
         exit code: 0 and message: Internal error running task.
```

So the Cloud Run platform itself reported the failure — the
information existed, it just never flowed back into Firestore.
**The ghost survived 3.5 days** of `/api/queue` reads.

## Recovery (one-off)

```python
from google.cloud import firestore
db = firestore.Client(project='ytfactory-prod-v2')
db.collection('jobs').document('<job_id>').delete()
```

A safer alternative is to flip status to `cancelled` instead of
deleting (preserves audit trail; `_doc_to_view` keeps the doc
clickable for review):

```python
db.collection('jobs').document('<job_id>').update({
    'status': 'cancelled',
    'stage': 'cancelled',
    'error': 'orphaned by Cloud Run worker crash',
    'updated_at': firestore.SERVER_TIMESTAMP,
})
```

## Diagnostic recipe (find every stuck-pending right now)

```bash
.venv/bin/python -c "
import os
os.environ.setdefault('GOOGLE_CLOUD_PROJECT','ytfactory-prod-v2')
from google.cloud import firestore
from datetime import datetime, timedelta, timezone
db = firestore.Client(project='ytfactory-prod-v2')
cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
for snap in db.collection('jobs').where('status','in',['pending','rendering','uploading']).limit(200).stream():
    d = snap.to_dict() or {}
    upd = d.get('updated_at')
    if upd and upd < cutoff:
        print(f'{snap.id}  {d.get(\"status\")}/{d.get(\"stage\")}  {upd.isoformat()}  {d.get(\"channel\")}  {(d.get(\"topic\") or \"\")[:60]}')
"
```

Output is the candidate set for a reaper to flip.

## Mitigation options (pick one — neither shipped yet)

### Option A: stale-pending sweeper (simpler; no worker-side change)

Extend `web/server.py::_periodic_queue_reaper` to ALSO walk the
`jobs` collection on its 5 min cadence:

1. Query `status in [pending, rendering, uploading]` AND
   `updated_at < now() - JOBS_REAPER_THRESHOLD_S` (default 30 min).
2. For each candidate, look up `cloud_execution` via
   `google.cloud.run_v2.ExecutionsClient`. If that execution is
   `Completed` (success or failure) but the job doc is still
   pending → flip the doc to `failed` with
   `error="orphaned: cloud execution <name> reported <state>"`.
3. If `cloud_execution` is missing entirely (dispatch never wrote
   it) AND the doc is older than 1 h → flip to `failed` with
   `error="orphaned: dispatch never assigned cloud_execution"`.

Pros: lives in one place, idempotent, recoverable from the
ground-truth (Cloud Run's execution status).
Cons: requires read access to `run.executions.get` for the SA
running ytfactory-web (web-runner needs `roles/run.viewer` if it
doesn't already have it via `run.invoker`).

Tunable via env: `YTFACTORY_JOBS_REAPER_THRESHOLD_S=1800` (30 min).
Disable entirely with `YTFACTORY_JOBS_REAPER_THRESHOLD_S=0`.

### Option B: worker `atexit` writeback (defence in depth)

Add a `signal.SIGTERM` handler + `atexit.register(...)` to
`pipeline/render/shorts.py::main` (and the other 3 render entry
points the render-worker-v2 image executes — `long_form`,
`footage_only`, `sports_doc`) that, on unhandled exit, calls
`jobs_mod.mark_failed(job_id, stage="<last-stage>", error="worker
crashed: <signal/exception>")` from a `try/except: pass` block so
the writeback itself can't cascade-fail.

Pros: catches crashes the platform-level option misses (e.g. the
Cloud Run platform reports `Completed` even on `Internal error
running task`, but the writeback would carry the exception
traceback for debugging).
Cons: doesn't help when the container OOM-killed or got SIGKILL'd
before atexit runs. Doesn't help on the dispatch-time failure
(network blip between confirm and trigger). Needs to be wired into
all 4 render entrypoints.

### Recommended

**Both, but Option A first** — it's the platform-level guarantee
that catches every failure mode (including the dispatcher itself
dying, OOM kills, container loss, etc.). Option B is icing that
buys richer error messages on the common case.

## Acceptance criteria when shipped

- A test fixture that creates a `jobs/<id>` doc with `status=pending`
  + `cloud_execution=<known-completed-execution>` + `updated_at` 1 h
  ago, runs the reaper, and asserts the doc flipped to
  `status=failed` with the right `error` substring.
- A second fixture that creates a `pending` doc with NO
  `cloud_execution` (dispatch crashed pre-update) older than 1 h,
  runs the reaper, asserts it flipped to `failed`.
- The diagnostic recipe above returns 0 rows in prod for ≥7 days
  post-deploy.

## See also

- `web/server.py::_periodic_queue_reaper` — the existing
  `agent_tasks` reaper; the new sweep would join the same lifespan
  task or run alongside.
- `control/core/jobs.py::create_render` — the dispatch path that
  writes `cloud_execution` (the field the reaper joins on).
- `control/routes/render_routes.py::get_queue_state` — the
  `/api/queue` reader; once the reaper flips orphans to `failed`
  they automatically vanish from Queued and appear in Completed.
- `feedback_laptop_agent_cloud_contract.md` flavour 6 — analogous
  reaper for the `agent_tasks` collection (already shipped). This
  doc is the `jobs/*` sibling that hasn't shipped yet.
- `docs/full_cloud_cutover_2026_05_09.md` — narrates the
  Firestore-as-canonical-queue migration that introduced this gap
  (pre-cutover the queue was in-memory and a process crash WAS the
  reaper because the docs vanished with the process).
