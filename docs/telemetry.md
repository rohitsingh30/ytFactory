# Telemetry — OTel → Cloud Trace + Cloud Monitoring + Cloud Logging

> Single source of truth for the ytFactory telemetry stack. Every laptop
> pipeline stage, HTTP route, cron job, Cloud Run service, and subprocess
> call flows through this layer.

## TL;DR

| What | Where |
|---|---|
| Observability code | `pipeline/observability/` |
| Legacy back-compat shim (`tlm.track`, `tlm.timed`, …) | `pipeline/telemetry.py` |
| Cloud Run init helper (copied per service) | `cloud/_shared/otel_init.py` |
| Dashboard API (`/api/telemetry/*`) | `control/routes/telemetry_routes.py` |
| Dashboard UI (`/app/telemetry`) | `web-next/app/app/telemetry/` |
| IAM grant script | `cloud/iam/grant_telemetry.sh` |
| Cloud-side redeploy orchestrator | `cloud/_shared/redeploy_for_otel.sh` |
| Per-service file sync | `cloud/_shared/sync.sh` |
| OTel deps appender | `cloud/_shared/append_otel_deps.sh` |
| Dockerfile patcher | `cloud/_shared/add_otel_copy.sh` |

Day-to-day, you almost never touch the SDK directly. Use:

```python
from pipeline import observability as obs

with obs.timed("my_stage", category="image", metadata={"provider": "..."}):
    ...

obs.track("cache_hit", category="cache", metadata={"key": "..."})

with obs.ctx(channel="historyrecapped", slug="aita-001",
             render_kind="short"):
    do_render(...)        # every nested span auto-inherits these attrs
```

## Architecture

### Three signals

OTel separates spans (traces), metrics, and logs. We use all three:

1. **Spans** — every meaningful unit of work opens a span. Spans nest
   to form a trace. Cloud Trace shows the full waterfall: chat
   request → render-worker JOB → cloud TTS / image / upload calls.
2. **Metrics** —
   - `ytfactory.events` (counter): per-event volume.
   - `ytfactory.stage_duration_ms` (histogram): per-event duration.
   Both carry `event` / `category` / `success` attributes so Cloud
   Monitoring can compute p50 / p95 / error-rate per stage.
3. **Logs** — every `obs.timed` / `obs.track` emission also fires an
   OTel log record. In Cloud Run we let stdout JSON flow through the
   platform's log capture; on laptop we use `CloudLoggingLogExporter`
   in `pipeline/observability/gcp_log_bridge.py`.

### Single dispatcher pattern

Most "wrap one entry point gives every variant a span" wins came from
this:

| Layer | Single dispatcher | Where the wrap happens |
|---|---|---|
| TTS | `pipeline.audio.synthesize` | `pipeline/audio/__init__.py` |
| Image | `pipeline.images.generate` | `pipeline/images/images.py` |
| Render | `make_short` / `long_form.main` / `footage_only.render` / `sports_doc.main` | `pipeline/render/*.py` |
| Upload | `upload_short`, `youtube_upload`, `authenticate`, `x_post`, `post_short` | `pipeline/upload/*.py` |
| LLM | `cli.call_claude_cli` (already had `llm_call` event) + `@traced` on each module's entry | `pipeline/llm/*.py` |
| HTTP | `FastAPIInstrumentor.instrument_app(app)` | `web/server.py`, `control/server_dev.py` |

### Per-render context (the key pattern)

`pipeline.observability.context.RenderContext` is held in a
`contextvars.ContextVar`. The renderer pushes once at the entry
point (`obs.render_envelope(channel=..., slug=..., render_kind=...)`),
and every nested `obs.timed` / `obs.track` / `@obs.traced` call
inherits the attributes. Result: in Cloud Trace you can filter for
`ytfactory.channel="historyrecapped" AND ytfactory.slug="aita-001"`
and see one render's full span tree.

### Cross-process trace propagation

| Hop | Mechanism |
|---|---|
| FastAPI → `requests` / `httpx` / `aiohttp` outbound | OTel auto-instrumentation injects `traceparent` HTTP header |
| Laptop → Cloud Run JOB (gcloud / SDK) | `_trace_env_overrides()` in `control/core/cloud_run.py` adds `YTFACTORY_TRACEPARENT` env to the JOB execution; JOB's `entrypoint.py::main` calls `attach_traceparent_from_env()` |
| Chat request → Firestore job doc → render-worker | `control/jobs.py::create_job` stamps `traceparent` onto the job doc; render-worker can read either env or doc |

## Public API

```python
from pipeline import observability as obs

# 1. Init — idempotent. Auto-resolves exporter from env. You almost
# never need to call this; it auto-runs on first use of timed/track.
obs.init(service_name="ytfactory-laptop")
obs.init_in_memory()                      # tests
obs.reset_for_tests()                     # between tests (autouse if you wire one)

# 2. Single-event emission.
obs.track("event_name", category="cat",
          success=True, duration_ms=100,
          metadata={"channel": "..."})

# 3. Span + duration histogram + log record (most common).
with obs.timed("stage_name", category="render",
               metadata={"provider": "..."}) as t:
    work()
    t.add(metadata={"items": 7})          # add at any time
    if soft_failed:
        t.fail("downstream returned 500") # mark as failed without raising

# 4. Span with a known duration (e.g. the renderer's t0/t1 pattern).
obs.emit_span("stage.tts", duration_ms=2400, success=True,
              metadata={"provider": "cloudrun_chatterbox"})

# 5. Push render context — every nested span inherits the attrs.
with obs.ctx(channel="historyrecapped", slug="aita-001",
             render_kind="short", job_id="...", run_id="..."):
    do_render(...)

# 6. Render envelope = ctx + parent span + auto-detected mode/job/run.
with obs.render_envelope(channel="...", slug="...",
                         render_kind="short") as env:
    do_render(...)
    env.add(metadata={"final_seconds": 58.4})

# 7. @traced decorator for the long tail (one line per function).
@obs.traced("llm.cast.author_cast", category="llm",
            capture=["channel", "slug"])  # auto-captured as ytfactory.meta.*
def author_cast(channel: str, slug: str, ...) -> dict:
    ...

# 8. Record exception against a span.
try:
    work()
except Exception as e:
    obs.record_exception(e, fatal=True)
    raise
```

## Exporter modes

`OTEL_EXPORTER` env var:

| Mode | When | Where signals go |
|---|---|---|
| `gcp` | Cloud Run (auto when `K_SERVICE` set) | Cloud Trace + Cloud Monitoring + Cloud Logging |
| `otlp` | Self-hosted collector (opt-in) | OTLP HTTP collector |
| `console` | Local dev (auto when not pytest, not Cloud Run) | stdout / stderr |
| `inmemory` | Tests (auto when `PYTEST_CURRENT_TEST` set) | OTel in-memory exporters; readable via the dashboard's `read_events()` shim |
| `none` | Performance-critical scripts | nothing |

## How to instrument new code

### A new pipeline stage

```python
from pipeline import observability as _obs

@_obs.traced("my_module.my_function", category="llm",
             capture=["channel", "slug"])
def my_function(text, *, channel, slug):
    ...
```

Or with mid-function metadata:

```python
def my_function(text, *, channel, slug):
    with _obs.timed("my_stage", category="llm",
                    metadata={"channel": channel, "slug": slug}) as t:
        result = do_work()
        t.add(metadata={"output_chars": len(result)})
        return result
```

### A new HTTP route

`FastAPIInstrumentor` already covers every route auto-magically. To
enrich the route span with channel/slug from path/query params, the
existing middleware (`pipeline/observability/http_middleware.py`) does
that — just include the standard names (`channel`, `slug`, `job_id`,
`niche`, `account`).

### A new Cloud Run service

1. Create your `cloud/<service>/server.py` with `FastAPI()`.
2. Run `bash cloud/_shared/sync.sh` to copy `otel_init.py` into the
   new service dir.
3. Add the boot block right around the FastAPI app:

   ```python
   try:
       from otel_init import (
           init as _otel_init,
           instrument_fastapi as _otel_instrument_fastapi,
           instrument_outbound_http as _otel_instrument_outbound,
       )
       _otel_init("my-new-service")
       _otel_instrument_outbound()
       _OTEL_OK = True
   except Exception:
       _OTEL_OK = False

   app = FastAPI(title="my-new-service")
   if _OTEL_OK:
       _otel_instrument_fastapi(app)
   ```

4. Run `bash cloud/_shared/append_otel_deps.sh` to add the OTel pin
   block to your `requirements.txt`.
5. Run `bash cloud/_shared/add_otel_copy.sh` to add `COPY otel_init.py
   ./` to the Dockerfile.
6. Deploy.

### A new Cloud Run JOB (no server)

Same as above, but in `entrypoint.py::main` add:

```python
from otel_init import init, attach_traceparent_from_env
init("my-job-name")
attach_traceparent_from_env()
```

The control plane sets `YTFACTORY_TRACEPARENT` automatically when
spawning the JOB.

## Querying signals

### Cloud Trace

* Console: <https://console.cloud.google.com/traces/list?project=ytfactory-prod-v2>
* CLI:
  ```bash
  gcloud trace list --project=ytfactory-prod-v2 \
      --filter='RootSpanName:"render.short"' --limit=10
  ```
* Filter by attributes (in the UI):
  `ytfactory.channel="historyrecapped" AND ytfactory.slug="aita-001"`

### Cloud Logging

* Console: <https://console.cloud.google.com/logs/query?project=ytfactory-prod-v2>
* Query:
  ```
  logName=~"ytfactory" AND
  jsonPayload."ytfactory.channel"="historyrecapped" AND
  jsonPayload."ytfactory.slug"="aita-001"
  ```
* CLI:
  ```bash
  gcloud logging read 'logName=~"ytfactory"' \
      --project=ytfactory-prod-v2 --limit=20 --format=json
  ```

### Cloud Monitoring

* Console: <https://console.cloud.google.com/monitoring/metrics-explorer?project=ytfactory-prod-v2>
* Custom metrics:
  - `custom.googleapis.com/opentelemetry/ytfactory.events`
  - `custom.googleapis.com/opentelemetry/ytfactory.stage_duration_ms`
* Filter by `event` / `category` / `success` labels.

### One-click from the dashboard

`/app/telemetry` → "Open in GCP" section. Type a channel/slug into
the filter inputs and the three deep-link cards (Trace / Logging /
Monitoring) update with the right filters baked in.

## IAM

Every Cloud Run service runs as `tts-runner@ytfactory-prod-v2.iam.gserviceaccount.com`,
which holds:

* `roles/cloudtrace.agent`
* `roles/monitoring.metricWriter`
* `roles/logging.logWriter`

Granted by `bash cloud/iam/grant_telemetry.sh` (idempotent).

Verify:

```bash
gcloud projects get-iam-policy ytfactory-prod-v2 \
    --filter='bindings.members:serviceAccount:tts-runner@ytfactory-prod-v2.iam.gserviceaccount.com' \
    --format='table(bindings.role)'
```

## Common ops

### Keep `cloud/<service>/otel_init.py` copies in sync

```bash
bash cloud/_shared/sync.sh                # write copies
bash cloud/_shared/sync.sh --check        # CI: drift detection
```

### Re-deploy every service after changing `cloud/_shared/`

```bash
bash cloud/_shared/redeploy_for_otel.sh   # all 13 services in parallel
bash cloud/_shared/redeploy_for_otel.sh tts-chatterbox  # canary one
bash cloud/_shared/redeploy_for_otel.sh --dry-run       # show commands
```

Per-service logs land in `cloud/deploy_logs/<service>.log`.

### Tail a live service's logs

```bash
gcloud run services logs tail ytfactory-tts-chatterbox \
    --project=ytfactory-prod-v2 --region=asia-southeast1
```

### Diagnose a failed render via spans

1. Open `/app/telemetry`, type the channel + slug into the Open-in-GCP
   filter.
2. Click "Cloud Trace" → see the full span tree.
3. Drill into the failed span → "Show logs" inline → exception event +
   stack trace + every neighboring log line are in one place.

## Migration notes (May 2026)

- The legacy JSONL store (`data/telemetry/events-*.jsonl`) is gone.
- The legacy `pipeline.telemetry` API (`tlm.track`, `tlm.timed`,
  `tlm.read_events`, `tlm.percentile`) still works — it's now a thin
  shim over OTel. Existing callers do not need to change.
- The legacy `web/server.py` `/api/telemetry/*` routes were removed
  (`telemetry_overview`, `telemetry_stages`, `telemetry_timeline`,
  `telemetry_llm`, `telemetry_errors`, `telemetry_latency`). The new
  `control/routes/telemetry_routes.py` re-exposes them under the
  same paths.
- Tests that asserted on JSONL files (`tests/test_utils_telemetry.py`)
  were rewritten to assert via the OTel in-memory exporter.

## Operational rules

* **Every new pipeline stage MUST emit a span** via `obs.timed` or
  `@obs.traced`.
* **Every new HTTP route MUST be auto-instrumented** (no per-route
  manual span — `FastAPIInstrumentor` covers it).
* **Every new Cloud Run service MUST init OTel** via
  `cloud/_shared/otel_init.py::init(<service-name>)` in its
  `server.py` startup.
* **Telemetry must never block the pipeline** — every `track` and
  `timed` call swallows exceptions internally; if the SDK fails, the
  render proceeds.

See also:

* `docs/observability_runbook.md` — debug-X-using-telemetry cookbook.
* `pipeline/observability/__init__.py` — full public-API surface.
* `cloud/_shared/otel_init.py` — the canonical Cloud Run boot helper.
