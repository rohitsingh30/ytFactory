# Cloud-service Dockerfile discipline

> **Two-rule summary:** every `*.py` file under `cloud/<svc>/` must
> appear in a `COPY` line in the matching `cloud/<svc>/Dockerfile`,
> and the guard test
> `tests/cloud/test_dockerfile_copy_completeness.py` enforces that
> at CI time. If the rule slips, the failure mode is either a hard
> ImportError at first request (`render-worker-v2` pattern) or
> **silently disabled telemetry** for months (`_tel_track_io.py` in
> the 3 TTS/ASR services, 2026-05-23).

## The pattern

Every `cloud/<svc>/Dockerfile` in this repo uses an explicit list of
per-file `COPY` instructions, e.g.:

```dockerfile
COPY server.py ./
COPY _tel_track_io.py ./
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./
```

It does **not** use a wholesale `COPY cloud/<svc>/ ./` or `COPY . .`.
This was a deliberate choice — keep image layers minimal, keep the
copy graph explicit at read time.

## The trade-off

The cost of being explicit is that **operator discipline** has to
catch every new file. A `.py` added to the source directory is
invisible to Docker until the Dockerfile is edited. The
build/push/deploy pipeline runs to completion against the stale
Dockerfile and produces an image that, structurally, is missing code
the entrypoint imports.

There is **no automatic check at build time**. The Docker daemon
doesn't know which files the maintainer "meant" to include; it
copies the ones the Dockerfile names.

## The failure mode comes in two flavours

### A) Hard fail (loud, fast)

The missing module is imported unconditionally:

```python
# cloud/render-worker-v2/entrypoint.py
from _stage_envelope import stage_envelope  # type: ignore
```

Result: `ModuleNotFoundError` on every cold start. Worker exits
non-zero in <2 s. Every render fails. Surfaces immediately at first
real render.

This is what bit `cloud/render-worker-v2/writeback.py` (commit
`4597686`) and again `cloud/render-worker-v2/_stage_envelope.py`
(2026-05-23).

### B) Silent fail (quiet, durable)

The missing module is imported via a try/except softener:

```python
# cloud/tts-chatterbox/server.py
try:
    from _tel_track_io import track_io as _tel_track_io
except ImportError:
    _tel_track_io = None

# ... later in a handler:
if _tel_track_io is not None:
    _tel_track_io(input_text=..., output_text=...)
```

Result: the service starts fine. `/readyz` returns 200. The Cloud
Run revision goes healthy. The operator sees a green deploy. But
every call to `_tel_track_io` becomes a no-op, and **every piece of
body-capture telemetry that depended on that helper is silently
discarded.**

This is what hid `_tel_track_io.py` being missing from
`cloud/{tts-chatterbox,tts-indicf5,asr-whisper}/Dockerfile` since
the telemetry phase 4 commit landed. The discovery only happened
because the hard-fail variant (render-worker-v2) surfaced first and
prompted an audit.

## The 2026-05-23 incident

| Step | Time | What happened |
|---|---|---|
| 1 | Phase 0-9 of telemetry committed | New files added to `cloud/render-worker-v2/_stage_envelope.py` and `cloud/{image-z-image-turbo,tts-chatterbox,tts-indicf5,asr-whisper,editing-agent}/_tel_track_io.py`. Image-z-turbo and editing-agent Dockerfiles already had the COPY (added with the source). The other 4 didn't. |
| 2 | `bash cloud/<svc>/deploy.sh` for all 6 services | Builds succeeded. Revisions deployed. `gcloud run services describe` returned `Ready=True` for everything. |
| 3 | Operator declared "all 6 services telemetry-aware ✓" | Based on the service-level health check. **False.** The image-z-turbo + editing-agent revisions were complete; the other 4 were structurally broken. |
| 4 | First real render triggered | Render-worker-v2 died in <2 s on `ModuleNotFoundError: _stage_envelope`. |
| 5 | Audit | Grep across all 6 cloud-service dirs found the silent-fail variant in 3 more services. Body-capture telemetry had been dark for the entire window since deploy. |

**Cost:** ~3 hours of operator time on a "deploy is complete"
declaration that was structurally false.

**Fix:** one-line `COPY` addition to each of the 4 Dockerfiles
+ rebuild + redeploy. The fix was tiny. The cost was the false
confidence in the "service-level deploy = telemetry live" mental
model.

## The two-rule fix

### Rule 1 — Dockerfile parity

For every cloud service:

```bash
# enumerate source files
ls cloud/<svc>/*.py

# verify each appears in the matching Dockerfile's COPY block
grep -E "^COPY .*\\.py" cloud/<svc>/Dockerfile
```

Every entry in the first list must appear in the second. The guard
test (Rule 2) enforces this mechanically.

### Rule 2 — guard test before declaring deploy complete

```bash
.venv/bin/pytest tests/cloud/test_dockerfile_copy_completeness.py -q
```

The test enumerates `cloud/*/Dockerfile`, parses each, and asserts
every `*.py` file in the matching source directory is referenced in
at least one `COPY` line. Failure surfaces the exact missing file
and the file that needs editing.

Run this at three points:

1. **As part of `.venv/bin/pytest tests/ -x -q`** — already in the
   project's standard test loop.
2. **Before every `deploy.sh` run** — operator habit. If the test
   fails, the deploy will produce a broken image.
3. **In CI** — when CI gets wired up.

## When to use the wholesale `COPY cloud/<svc>/ ./` pattern instead

The explicit pattern is the right default. It's appropriate to
switch to `COPY cloud/<svc>/ ./` when:

- The service has 6+ `.py` files (the friction of maintaining the
  explicit list outweighs the layer-size win).
- The directory is well-curated (no large checkpoints, no random
  scratch files, `.dockerignore` is in place).

Switching is a one-time refactor: replace the per-file COPYs with a
single directory COPY, add a `.dockerignore` to exclude what
shouldn't ship (caches, `__pycache__`, `*.pyc`, `_weights/`, etc.).
The guard test (Rule 2) auto-detects this case and skips the check
for that service (any Dockerfile that does `COPY cloud/<svc>/ ./` is
considered self-explanatory).

## See also

- `tests/cloud/test_dockerfile_copy_completeness.py` — the guard test.
- Memory: [[dockerfile-copy-drift]], [[verify-telemetry-with-preflight]].
- `docs/render_telemetry.md` § "What 'telemetry-ready' actually means"
  — the workflow-side correction (rev-healthy ≠ telemetry-live).
- `docs/cost_optimized_deploy.md` § Iron rules — referenced here
  because Dockerfile discipline is in the same family of operator
  guardrails (parity with deploy.sh discipline, region pinning,
  min-instances=0, etc.).
- Commit `4597686` (2026-05-XX) — the first instance of this bug
  class, fixed for `writeback.py` but with no guardrail added.
- Commit (pending, 2026-05-23) — the fix for `_stage_envelope.py`
  and the 3 `_tel_track_io.py` omissions, plus the guard test.
