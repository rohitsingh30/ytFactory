# Cloud Run JOB subprocess debugging (CLASS-OF-BUG, 2026-05-13)

> **TL;DR** — Three intertwined bugs surfaced from a single render
> failure (`5e37f76b`). Every Cloud Run **JOB** that uses
> `pipeline/observability` and shells out to a subprocess will hit
> all three:
>
> 1. `K_SERVICE` env is **only set in Cloud Run services**, NOT jobs
>    (jobs set `CLOUD_RUN_JOB` + `CLOUD_RUN_EXECUTION` instead).
>    Every K_SERVICE-only check fell through to "console" exporter
>    mode → `ConsoleMetricExporter` dumped JSON to stdout every 60s
>    AND at exit-time metric flush.
> 2. `pipeline/render/video.py::_stream_subprocess` captured Popen
>    stdout to a `/tmp` file inside the container; the only
>    operator surface on failure was "last 25 lines" → drowned by
>    the JSON noise from #1.
> 3. The "last 25 lines" extractor had no smarts — when a 200-line
>    OTel exit dump trailed an actionable Python traceback, the
>    operator saw 100% JSON garbage and zero actionable diagnostic.
>
> All three fixed in commit `b0ce095` (2026-05-13).

## What surfaced this

Render `5e37f76b5cd448a896f5281fd604b5c9` — long-form
"The mystery surrounding Britney Spears" on `mystoriesanimated`.
The subprocess (`python -m pipeline.render.long_form`) ran for
12.5 minutes, then exited code 1. The worker's Firestore error
field showed `pipeline.render.long_form exited with code 1.\nLast
25 log lines:` followed by 25 lines of structured OTel metric
JSON output (no Python traceback). The actual root cause of the
subprocess failure was invisible.

Cloud logs from the parent worker also showed a wall of OTel JSON
emitted by `ConsoleMetricExporter`'s 60-second flush cycle and
exit-time final flush. Both the worker AND the subprocess were
running in "console" exporter mode despite being on Cloud Run.

## Root cause #1 — Cloud Run JOB env signals

`pipeline/observability/exporters.py::resolve_mode()`:

```python
# pre-2026-05-13
if os.environ.get("K_SERVICE"):
    return "gcp"
if os.environ.get("PYTEST_CURRENT_TEST"):
    return "inmemory"
return "console"
```

Cloud Run **services** set `K_SERVICE`, `K_REVISION`, `K_CONFIGURATION`.
Cloud Run **jobs** set `CLOUD_RUN_JOB`, `CLOUD_RUN_EXECUTION`,
`CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_TASK_ATTEMPT`, `CLOUD_RUN_TASK_COUNT`
— and explicitly do NOT set K_SERVICE.

Result: `ytfactory-render-worker-v2` (a JOB) and every long-form
subprocess it spawned resolved to `mode="console"`, which wires up
`ConsoleSpanExporter` + `ConsoleMetricExporter` + `ConsoleLogRecordExporter`.
The metric exporter's `PeriodicExportingMetricReader` flushed every
60 s, dumping the full metric snapshot as multi-line JSON to stdout.
On process exit it flushed once more — that's the dump that
displaced the actionable traceback in the subprocess's
`long_form_renderer.log`.

Same K_SERVICE-only check existed in:

- `pipeline/observability/exporters.py::resolve_mode()` (1 site)
- `pipeline/observability/exporters.py::_build_gcp_log_processor()` (1 site)
- `pipeline/observability/otel.py::_build_resource()` (1 site, `K_SERVICE` fallback chain)
- `pipeline/observability/otel.py::_resolve_env()` (1 site)
- `pipeline/observability/otel.py::_maybe_merge_gcp_resource()` (1 site)
- `cloud/_shared/otel_init.py::init()` (1 site, plus 14 per-service copies)

## Root cause #2 — subprocess stdout never reached cloud logs

`pipeline/render/video.py::_stream_subprocess()` ran:

```python
# pre-2026-05-13
proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    ...
)
with log_path.open("w") as logf:
    for line in proc.stdout:
        logf.write(line); logf.flush()
        if progress_cb:
            _maybe_emit_long_form_progress(line, progress_cb)
```

`subprocess.PIPE` means the parent reads each line; nothing reaches
the parent's own stdout. So none of the subprocess's output reached
Cloud Run's stdout-capture → none of it landed in Cloud Logging.
The only operator surface was `log_path` — a `/tmp` file inside the
container that vanishes the moment the JOB completes.

## Root cause #3 — naive error tail extraction

`render_long_form` on subprocess failure:

```python
# pre-2026-05-13
if rc != 0:
    tail = "\n".join(log_path.read_text().splitlines()[-25:])
    raise RuntimeError(f"... Last 25 log lines:\n{tail}")
```

When the subprocess's exit-time OTel metric flush dumps 200+ lines
of JSON below an actionable traceback, the trailing 25 lines are
100% JSON noise and 0% diagnostic.

## Fix shipped (commit `b0ce095`, 2026-05-13)

### Cloud Run env detection

New centralised helper in `pipeline/observability/exporters.py`:

```python
def _on_cloud_run() -> bool:
    """True when running inside Cloud Run — service OR job."""
    return bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("CLOUD_RUN_JOB")
        or os.environ.get("CLOUD_RUN_EXECUTION")
    )
```

Plumbed through every site listed in Root cause #1. All 15
per-service `cloud/<svc>/otel_init.py` copies re-synced via
`bash cloud/_shared/sync.sh`.

### Tee subprocess output to parent stdout

`_stream_subprocess` now also writes each subprocess line to
`sys.stdout`:

```python
for line in proc.stdout:
    logf.write(line); logf.flush()
    sys.stdout.write(line); sys.stdout.flush()  # ← cloud logs see it
    if progress_cb:
        _maybe_emit_long_form_progress(line, progress_cb)
```

Now every line of every subprocess invocation is queryable in Cloud
Logging in real time, not just on failure via the file tail.

### Smart traceback extraction

New helpers in `pipeline/render/video.py`:

- `_TRACEBACK_HEADER_RE` matches `^Traceback \(most recent call last\):\s*$`
- `_extract_last_traceback(log_text, max_lines=80)` walks the log,
  remembers the index of the LAST traceback header, and returns from
  there forward. **HEAD-keeping** cap so the actionable
  `ErrorClass: message` line (which appears in the first few lines
  of any traceback) survives even when followed by hundreds of OTel
  JSON noise lines.
- `_format_subprocess_failure(rc, log_path)` centralises the error
  message shape: `"Subprocess error:"` header (NOT the legacy
  `"Last 25 log lines:"`) + extracted traceback.

The `render_long_form` error surface now reads:

```
pipeline.render.long_form exited with code 1.
Subprocess error:
Traceback (most recent call last):
  File "/workspace/pipeline/render/long_form.py", line 999, in compose
    final = _ffmpeg_mux(parts)
RuntimeError: panel render failed: missing image
{
    "resource_metrics": [
    ...
```

The traceback is at the TOP, JSON noise (if any) trails — operators
see "RuntimeError: panel render failed: missing image" without
scrolling.

## Pin

Tests: `tests/test_render_video.py` (12 cases),
`tests/test_obs_exporters.py::TestResolveMode` (+5 cases),
`tests/test_observability_otel.py` (11 cases). 100% diff coverage.

## Auditable rule for future Cloud Run JOBS

For every new Cloud Run **JOB** (not service) added to `cloud/`:

1. The JOB inherits the same `cloud/<svc>/otel_init.py` boot pattern
   as services. The 2026-05-13 fix covers it — no per-job override
   needed.
2. If the job spawns subprocesses and surfaces their failures to
   operators, use `pipeline/render/video.py::_stream_subprocess`
   (or copy its tee + smart-tail pattern). Do NOT shell out with
   `subprocess.PIPE` and only-on-failure tailing — that's the bug
   class fixed here.
3. The JOB's deploy script MUST source `cloud/_shared/auth_setup.sh`
   (already mandatory per `docs/deploy.md` §"Adding a new
   `cloud/<svc>/deploy.sh`").

## Sweep recipe

```bash
# Find any remaining K_SERVICE-only checks in code:
grep -rn "K_SERVICE" pipeline/ cloud/ scripts/ \
  --include='*.py' --include='*.sh' \
  | grep -v "CLOUD_RUN_JOB\|CLOUD_RUN_EXECUTION"

# Each hit is a candidate for K_SERVICE-or-CLOUD_RUN_JOB normalisation.
# (Some legitimate hits exist — e.g. service-specific pre-warm logic
# that genuinely only applies to services. Audit with eyes, not sed.)

# Find any remaining subprocess.PIPE without tee in render code:
grep -rn "stdout=subprocess.PIPE" pipeline/ cloud/ \
  --include='*.py' \
  | grep -v "subprocess.STDOUT"
```

## See also

- `docs/telemetry.md` — exporter modes table (updated 2026-05-13)
- `docs/cloudrun_render_worker.md` — render worker JOB env contract
  (updated 2026-05-13 with `AZURE_OPENAI_TOKEN_PARAM`)
- `docs/cloud_service_dep_playbook.md` — playbook for adding new
  Cloud Run services (this doc is the JOB-side companion)
- `pipeline/observability/exporters.py::_on_cloud_run` — central helper
- `pipeline/render/video.py::_extract_last_traceback` — diagnostic tail
