# Cloud runtime deps — the slim-image dependency contract

> **Established 2026-05-12** after the second of two stacked
> regressions where `ytfactory-web` shipped with a runtime GCP import
> not pinned in `requirements-control.txt`. The user (rightly) asked
> for test coverage that would catch this class of bug at `pytest`
> time, not at "render failed in production" time.

## TL;DR

Every `from google.cloud import X` (or `import google.cloud.X`) on
the cloud-deployed code path MUST be pinned in
`requirements-control.txt` — even if the call site is wrapped in
`try/except ImportError` with a fallback, because in production the
fallback is usually broken in a different way (no auth, no network,
no graceful degradation).

`tests/test_cloud_runtime_deps.py` enforces this mechanically.
Editing pin lists or call sites without re-reading the test will
not flow through correctly.

## What broke (2026-05-11)

`control/core/cloud_run.py:trigger_render_job` does:

```python
try:
    return _trigger_via_sdk(job_id)        # google.cloud.run_v2
except ImportError:
    return _trigger_via_cli(job_id)        # gcloud run jobs execute
```

`google-cloud-run` was missing from `requirements-control.txt`. The
cloud image hit the `ImportError` path on every render, fell through
to the CLI fallback, and that failed inside the container with:

```
ERROR: (gcloud.run.jobs.execute) You do not currently have an active
account selected. Please run: $ gcloud auth login
```

Net effect: every chat-confirmed and form-confirmed render failed at
`stage=dispatch`. The user saw `failed @ 0/7 stages`.

While auditing for this exact shape, we surfaced a sibling latent
bug: `pipeline.upload.upload._persist_token` imports
`from google.cloud import secretmanager` to rotate the YouTube OAuth
refresh-token blob. `google-cloud-secret-manager` was also missing
from `requirements-control.txt`. Pre-fix, every successful in-cloud
token refresh failed to persist (raised a clear `RuntimeError`
instead of silently degrading — at least we'd have noticed when a
publish failed). Pinned preemptively.

## The class-of-bug

"Optional import + fallback" patterns hide silent failures in deploys
that don't pin the optional dep. Three flavors:

1. **Library + CLI fallback** (the dispatcher): SDK import optional,
   shell out as fallback. Fallback usually has no auth in a sandboxed
   container.
2. **Library + clear-error fallback** (the secret-manager path): SDK
   import optional, raise `RuntimeError("install google-cloud-X")` on
   miss. Fails loud at the call site, but the call site only fires
   on rare paths (token rotation), so the error surfaces hours/days
   after the regression actually shipped.
3. **Library + graceful degradation** (the cost dashboard's
   `pipeline.cloud.cost`): SDK import optional, falls back to "feature
   unavailable" with a user-facing reason string. **These ARE safe to
   leave optional** — the cloud image can ship without them and the
   degraded experience is acceptable.

Distinguishing 1+2 (pin in requirements) from 3 (don't pin) is a
deliberate per-package decision, captured in
`tests/test_cloud_runtime_deps.py` as two enumerated sets:
`PRODUCTION_REQUIRED_GCP_PACKAGES` vs `OPTIONAL_GCP_PACKAGES`.

## The test contract

`tests/test_cloud_runtime_deps.py` has **three classes**:

### `ProductionRequiredGcpPackagesTest`

Static sanity check of the allow-list. Every package named in
`PRODUCTION_REQUIRED_GCP_PACKAGES` must:

1. Be pinned in `requirements-control.txt` (regex: top-level entry,
   not a substring of a comment or longer name).
2. Not also appear in `OPTIONAL_GCP_PACKAGES` (a package can't be
   "must install" AND "safe to skip" at the same time).
3. Have a corresponding entry in `GCP_SUBPACKAGE_TO_PIP` (the
   reverse map the inventory test below needs to recognise the
   import).

### `GoogleCloudImportInventoryTest`

Walks every `.py` in `control/`, `web/`, and `pipeline/` (the source
roots the cloud image bakes in via `cloud/web-server/Dockerfile`'s
`COPY` directives). Finds every `from google.cloud import X` and
`import google.cloud.X`. For each subpackage, asserts:

- It's in `GCP_TRANSITIVE_PACKAGES` (api_core, auth, protobuf —
  pulled in transitively, never pinned directly), OR
- It's mapped via `GCP_SUBPACKAGE_TO_PIP` AND the pip name is
  classified as either required or optional.

A NEW `from google.cloud import …` added in code but missed in the
test's allow-list FAILS at pytest time, with a message pointing
the contributor at the file:line of the import and the decision
they need to make (required vs optional).

It also asserts that every classified-required import is actually
pinned in the requirements file (defends against bumping the
allow-list but forgetting the pin).

### `CloudRunDispatchEnvironmentTest`

Drift check. For every package in `PRODUCTION_REQUIRED_GCP_PACKAGES`
that's also pinned in the requirements file, asserts the package is
installable in the current venv via `importlib.util.find_spec`.
Catches the case where the laptop venv falls behind a new pin (next
pytest run will mirror the cloud build's behavior).

**MUST use `find_spec`, not a real `import`.** A real `import`
loads the submodule and binds it as an attribute of the parent
package, which then leaks into downstream tests that monkey-patch
`sys.modules` (those resolve `from google.cloud import X` via the
package attribute, NOT via `sys.modules`). See
`docs/test_isolation.md § "sys.modules + parent-package-attribute
pollution"` for the gory details — this skill caught a real
regression of that exact shape during this very PR.

## How to add a new GCP runtime import

1. Decide: production-required (pin) vs optional (don't).
2. Add `from google.cloud import X` (or `import google.cloud.X`) at
   the call site.
3. Add the pip name to either `PRODUCTION_REQUIRED_GCP_PACKAGES` or
   `OPTIONAL_GCP_PACKAGES` in `tests/test_cloud_runtime_deps.py`,
   with a per-package comment explaining the failure mode.
4. If pinning, also add the pin to `requirements-control.txt` with
   a per-package comment naming the call site and what breaks if
   the pin is removed.
5. If `X` is a new subpackage (not already in
   `GCP_SUBPACKAGE_TO_PIP`), add the mapping.
6. Run `pytest tests/test_cloud_runtime_deps.py -v` — all 14 should
   pass. If they don't, fix the gap before pushing.

The test failure messages tell you exactly which file:line is
unclassified or unpinned, with the grep recipe to reproduce.

## Post-deploy verification

When shipping a fix that touches the chat → render flow (or any
multi-stage user-facing flow), verify the NEXT stage end-to-end
before declaring done. This was the workflow miss on 2026-05-11
first attempt:

1. Snapshot fall-through fix shipped → `/api/jobs/{id}` returned 200
   for the user's job → user could load the page → claimed done.
2. User retried the render → it failed at `stage=dispatch` because
   `google-cloud-run` wasn't pinned (the deeper bug the snapshot
   fix made VISIBLE).
3. User came back angry, surfaced the second failure.

Verification checklist after a deploy that touches `web/server.py`
job routing or `control/core/cloud_run.py`:

```bash
# Recent dispatch errors on the new revision (5-min window)
REV=$(gcloud run services describe ytfactory-web --region=asia-southeast1 \
  --project=ytfactory-prod-v2 --format="value(status.latestReadyRevisionName)")
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=ytfactory-web \
   AND resource.labels.revision_name=\"$REV\" \
   AND (textPayload=~\"dispatch failed\" OR textPayload=~\"trigger_render_job\" \
        OR textPayload=~\"ImportError\" OR severity=ERROR)" \
  --project=ytfactory-prod-v2 --limit=30 --freshness=5m \
  --format='value(timestamp,severity,textPayload)'
```

Empty output is the green light. Any entries → investigate before
declaring done.

## What the test contract does NOT cover

- **Worker-side deps** (the `cloud/render-worker-v2` JOB image).
  Different requirements file, different test coverage gate. If a
  similar dep regression hits the worker, the diagnosis is in
  `feedback_cloudrun_render_worker_progress.md`.
- **Non-google.cloud runtime imports** (e.g. `firebase-admin`,
  `openai`, `httpx`). The pattern generalises but the test today
  only walks `google.cloud.*`. Expand the regex + per-package map
  if the same shape of bug fires for a different SDK.
- **Transitive version conflicts.** This is `cloud_service_dep_playbook.md`'s
  domain (the 6-step rule for adding a new GPU service). Different
  scope: transitive conflicts surface at Cloud Build time, not at
  runtime. The deps-coverage test catches "import not pinned"; the
  playbook catches "pin satisfiable but conflicts with another pin".

## Cross-references

- `tests/test_cloud_runtime_deps.py` — the test contract this doc
  describes.
- `requirements-control.txt` — the pinned set; per-package comments
  name the failure mode if a pin is removed.
- `cloud/web-server/Dockerfile` — the COPY directives that define
  what's "on the cloud-deployed code path".
- `docs/cloud_service_dep_playbook.md` — sibling rule for *adding*
  new GPU services with hostile ML dep trees (different scope: build
  time vs runtime).
- `docs/test_isolation.md § "sys.modules + parent-package-attribute
  pollution"` — why the drift check uses `find_spec` not `import`.
- `docs/jobs_snapshot_unification.md` — the snapshot fix that
  exposed the missing dispatcher pin.
- `~/.claude/projects/.../memory/feedback_cloudrun_dispatch_sdk_required.md`
  — the original symptom + fix.
- `~/.claude/projects/.../memory/feedback_cloud_runtime_deps_invariant.md`
  — terse pointer.

## CLAUDE.md hook

This doc is referenced from the **Cloud-first image generation
migration** and **Cloud-first TTS migration** sections of CLAUDE.md
("Adding a new Cloud Run service") so any contributor reading
those sections sees the test contract before adding a new GCP
client to the slim image.
