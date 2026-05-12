# Observability runbook

> Cookbook for "debug X using telemetry". Every recipe assumes Cloud
> Trace + Cloud Logging + Cloud Monitoring are receiving data (see
> `docs/telemetry.md` for the architecture). On the laptop in dev mode,
> the same patterns work via the in-memory exporters and the
> `/app/telemetry` dashboard.

## TL;DR — three places to look

1. **`/app/telemetry`** — start here. Sparkline shows whether the
   pipeline is alive; the Errors section shows the most recent
   failures with channel+slug context.
2. **Cloud Trace** — when you have a slug or job_id and want to see
   the full call tree (chat → render-worker → cloud TTS / image →
   upload).
3. **Cloud Logging** — when you want stack traces and surrounding
   prints. Filter by `ytfactory.channel`, `ytfactory.slug`,
   `ytfactory.job_id`, or by event name.

## Recipe: a render is failing — where?

1. Get the slug. Open `/app/telemetry` → Errors section.
2. Click "Cloud Trace" in the Open-in-GCP card with `slug=<slug>`
   filled in.
3. Find the most recent `render.short` (or `.long_form`) span.
4. The first ERROR child span is the failure point. Its
   `exception` event has type + message + stack.

If no `render.short` span exists, the render never started. Check
the chat-request span's child for `trigger_render_job`.

## Recipe: the dashboard shows zero events but the renderer is running

* Confirm the exporter mode: `GET /api/telemetry/init_status`.
  - `{"exporter": "inmemory"}` — fresh laptop without GCP creds; the
    dashboard reads the in-process OTel in-memory exporter directly.
  - `{"exporter": "console"}` — primary exporter prints to stdout AND
    a bounded in-process **shadow log buffer** feeds the dashboard
    (see `docs/telemetry.md` § "In-process shadow log buffer").
  - `{"exporter": "gcp"}` — primary writes to Cloud Trace / Logging /
    Monitoring AND the same in-process shadow buffer feeds the
    dashboard. The dashboard view is **per-process and per-instance**
    — it shows what THIS instance emitted, not the global picture.
    For full-fleet truth, click "Open in GCP → Cloud Logging" with
    your slug filter pre-filled.
  - `{"initialised": false}` — the SDK never booted; something
    silently swallowed an exception in `obs.init()`. Check stderr
    for `"OTel init failed"`.

* Still empty after the above?
  - Check `YTFACTORY_TELEMETRY_BUFFER_DISABLE` is unset (env knob
    that turns off the shadow buffer entirely; dashboard then returns
    `[]` in non-`inmemory` modes).
  - Check `YTFACTORY_TELEMETRY_BUFFER_SIZE` (default 5000) hasn't
    been set absurdly low.
  - Confirm the process emitting events is the same one serving
    `/api/telemetry/*`. Cross-process visibility (e.g. dashboard in
    web-server, events in render-worker JOB) is NOT covered by the
    shadow buffer — use the Cloud Logging deep-link.

## Recipe: cloud TTS is slow — which provider?

* Open `/app/telemetry` → Services section. Look for the
  `cloudrun_*` provider rows; they show p50 / p95 / error% over the
  last hour.
* For deeper analysis, click "Cloud Monitoring" with `channel=`
  filled in. The custom metric
  `ytfactory.stage_duration_ms{event="tts_synth"}` charts per-provider
  histograms.
* For one specific request, find its span in Cloud Trace; inside
  `tts_synth` you'll see the inner outbound HTTP span — its
  duration is the cloud round-trip.

## Recipe: cloud image circuit breaker tripped — why?

* `/app/telemetry` → Errors section. Filter shows
  `image_circuit_breaker_tripped` events with the trip reason in
  metadata.
* Cloud Trace: look at the `image_gen` spans BEFORE the trip.
  `CloudRunUnavailable` raised by `_post_generate` shows up as the
  exception event on those spans. The status code / response body
  is in the span attributes (`http.status_code`,
  `ytfactory.error.response_body`).

## Recipe: which channel had the most errors today?

```
GET /api/telemetry/overview?hours=24
```

The `by_channel` map shows total / success / failed per channel.
Or in Cloud Logging:

```
logName=~"ytfactory" AND severity>=ERROR
| jsonPayload."ytfactory.channel" != ""
| stats count by jsonPayload."ytfactory.channel"
```

## Recipe: an upload is stuck — where in the YouTube chain?

1. Cloud Trace: find the `upload_short` span for the slug.
2. Its child `youtube_upload` span shows whether the actual API call
   started.
3. If you see `oauth_authenticate` as a sibling/child with ERROR
   status and `error_class="RefreshTokenLost"`, the OAuth token was
   evicted — re-run `bash cloud/iam/grant_token_writeback.sh` and
   re-auth from the laptop.

## Recipe: the LLM cost looks too high — where?

`tts_synth` / `image_gen` are not LLM calls. The LLM cost lives in
`llm_call` events (one per `cli.call_claude_cli` invocation) with
metadata: `model`, `tier`, `input_tokens`, `output_tokens`,
`backend` (`cli` / `azure_openai` / `anthropic_sdk`).

Cloud Logging query:

```
logName=~"ytfactory" AND jsonPayload.event="llm_call"
| stats sum(jsonPayload."ytfactory.meta.input_tokens") as input,
        sum(jsonPayload."ytfactory.meta.output_tokens") as output
        by jsonPayload."ytfactory.meta.tier"
```

## Recipe: the dashboard's links button is greyed out

`GET /api/telemetry/init_status` reports
`has_gcp_exporter: false`. Two causes:

1. The SDK is in `inmemory` / `console` mode (no GCP exporter
   wired). Set `OTEL_EXPORTER=gcp` + `GOOGLE_CLOUD_PROJECT=...`.
2. The SDK isn't initialised at all. Add an explicit
   `obs.init(service_name=...)` call at startup, or call any
   `obs.timed(...)` to lazy-init.

## Recipe: trace context not flowing chat → render-worker → cloud TTS

Verify each hop:

1. **Chat span exists.** Cloud Trace → find your `POST
   /api/chat/confirm` span.
2. **JOB span links to it.** The `render-worker-v2` JOB's root
   span (named `render.short` / etc.) should appear as a child in
   the same trace. If it doesn't:
   - Check `control/jobs.py::create_job` actually wrote
     `traceparent` to the Firestore doc.
   - Check `cloud/render-worker-v2/entrypoint.py::main` calls
     `attach_traceparent_from_env()`.
   - Confirm `YTFACTORY_TRACEPARENT` was passed via
     `gcloud run jobs execute --update-env-vars`.
3. **Cloud TTS span links to render-worker.** The TTS service's
   server span should be a grandchild. If it's a separate trace,
   the outbound `requests` / `httpx` instrumentation isn't injecting
   `traceparent`. Confirm `obs.instrument_outbound_http()` ran in the
   render-worker process.

## Recipe: tests are erroring with `Cannot call collect on a MetricReader`

The OTel SDK's MeterProvider was shut down (probably by an earlier
test) but a cached histogram still holds a reference. The fix lives
in `pipeline.observability.otel.reset_for_tests` — it clears
`pipeline.observability.telemetry._INSTRUMENTS`. If the warning
spam is back, double-check that conftest's between-test cleanup
calls `obs.reset_for_tests()` and that the `_INSTRUMENTS.clear()`
line is still present in `reset_for_tests`.

## Recipe: render-worker crashes with `AttributeError: 'NonRecordingSpan' object has no attribute 'status'`

Class-of-bug: an OTel span helper inside `pipeline/observability/`
is reading a span attribute that exists only on `RecordingSpan`,
not on `NonRecordingSpan`. The SDK returns NonRecordingSpan during
the brief Cloud Run cold-start window before instrumentation
finishes booting; any `.status` / `.attributes` / `.events` /
`.start_time` / `.end_time` / `.kind` / `.context` / `.parent` /
`.resource` read raises `AttributeError` and crashes the worker
mid-render.

**Pinned fix already in place for `_close_span`** (`commit 2d6333f`,
2026-05-12) — drop the read, call `set_status(Status(StatusCode.ERROR))`
unconditionally on the failure path. NonRecordingSpan's `set_status`
is a no-op; RecordingSpan's is idempotent.

**For NEW span helpers** (anywhere that wraps `tracer().start_span`):

```python
# Bad — works only on RecordingSpan:
if not success and span.status.status_code != StatusCode.ERROR:
    span.set_status(Status(StatusCode.ERROR))

# Good — works on both:
if not success:
    try:
        span.set_status(Status(StatusCode.ERROR))
    except Exception:
        pass  # telemetry must never block the pipeline
```

Use only the methods the OTel `Span` ABC guarantees:
`get_span_context()`, `set_attribute()`, `set_attributes()`,
`set_status()`, `add_event()`, `update_name()`, `is_recording()`,
`record_exception()`, `end()`. Anything else needs
`getattr(span, "attr", None)` defensive read OR a call-pattern only.

The original Cloud Logging trace from the 2026-05-12 incident is
worth keeping for grep:

```
File "pipeline/observability/telemetry.py", line 335, in _close_span
  if not success and span.status.status_code != StatusCode.ERROR:
AttributeError: 'NonRecordingSpan' object has no attribute 'status'
```

See `feedback_otel_nonrecording_span_status_crash.md` for the
generalised rule.

## Recipe: render-worker crashes with `400 Points must be written in order` (Cloud Monitoring)

Class-of-bug: every Cloud Run JOB execution + every spawned subprocess
writes to the SAME `(metric_type, generic_node{location:'global',
namespace:'', node_id:''})` time-series tuple. A new run's
`start_time` is older than the previous run's last point → Cloud
Monitoring rejects every metric flush with 400. Pre-fix the
exporter's logged exit-time traceback ALSO drowned the real
subprocess crash in `_extract_last_traceback`'s output.

**Pinned fix already in place** (commit landing 2026-05-13) —
`pipeline/observability/otel.py::_cloud_run_identity_attrs` (and the
mirrored `cloud/_shared/otel_init.py` lines 134-157) inject
per-process `service.instance.id` (+ `service.namespace` +
`cloud.region`) so the OTel→GCP MonitoredResource mapping projects to
`generic_task` (per-process bucket) instead of `generic_node`. The
`_extract_last_traceback` helper now classifies tracebacks by entry
frame and prefers the LAST non-telemetry traceback, falling back to
telemetry only when it's the only traceback in the log.

**If you see this AFTER the fix shipped:**

1. Confirm the worker actually re-deployed since the fix:
   ```bash
   gcloud run services describe ytfactory-render-worker-v2 \
     --region asia-southeast1 --project ytfactory-prod-v2 \
     --format='value(metadata.annotations."run.googleapis.com/lastDeployedAt")'
   ```
   Should be on or after the fix's commit date. If older →
   `bash cloud/render-worker-v2/deploy.sh`.

2. Confirm the resource attrs are actually present on the failing
   metric:
   ```bash
   gcloud monitoring time-series list \
     --project ytfactory-prod-v2 \
     --filter='metric.type="workload.googleapis.com/ytfactory.events"' \
     --interval='end-time=now,start-time=-1h' \
     --format='value(resource.type, resource.labels)' | head -5
   ```
   Should print `generic_task` (NOT `generic_node`). If still
   `generic_node` → the worker's `otel_init.py` copy is stale.
   Re-sync via `bash cloud/_shared/sync.sh && bash cloud/render-worker-v2/deploy.sh`.

3. If the surface STILL contains a Cloud Monitoring traceback even
   though there's a real Python traceback in the subprocess log:
   `pipeline/render/video.py::_extract_last_traceback` is mis-
   classifying. Read `tests/test_render_video.py::IsTelemetryTracebackTest`
   to extend the regex (`_TELEMETRY_TRACEBACK_FRAME_RE`).

Full post-mortem: `docs/cloud_monitoring_resource_collision.md`.

## Recipe: figure out the slowest stage in a render

**Fastest path** (added 2026-05-12): open `/app/telemetry` →
**Stage Latency** section. Horizontal bar chart of p50 and p95
duration per render-pipeline stage, sorted by p95 descending — the
slowest stage is at the top. Bars colour-coded: sky for stage
events, violet for full render envelopes, rose if the stage failed
in the window. Tabular detail underneath: count / errors / mean /
p50 / p95 / max / total ms. Backed by
`GET /api/telemetry/stage_latency`. Filtered to a render-pipeline
event whitelist (`tts_synth`, `image_gen`, `llm_call`, `compose`,
`upload_*`, `stage.*`, `render.*`) — bookkeeping events
(`cache_hit`, `cloud.health.sweep`, HTTP auto-spans) are excluded
so the chart isn't dominated by 0-ms noise.

**For one specific render**, open `/app/telemetry` → **Recent
Renders** section. Each row is one render with a horizontal
stacked-bar waterfall (bar width = wall-clock duration vs the
slowest render in the window). Click a row to expand the per-stage
breakdown table with provider + duration + % of render. Backed by
`GET /api/telemetry/renders` — supports `?channel=` filter.

**For longitudinal analysis** (cross-day trend, alerting), use
Cloud Monitoring → Metrics Explorer → custom metric
`ytfactory.stage_duration_ms`. Group by `event` (label). p95 sorted
descending = your hotspots.

For one slug:

```
custom.googleapis.com/opentelemetry/ytfactory.stage_duration_ms
| label.event != ""
| filter label.ytfactory_slug == "aita-001"
```

See `docs/telemetry_dashboard_design.md` for the rule on what
belongs in the dashboard vs Cloud Monitoring.

## Operational SLO suggestions (P9 follow-up)

When ready to wire alerts:

| Metric | Suggested SLO |
|---|---|
| `tts_synth` p95 | < 3s |
| `image_gen` p95 | < 5s |
| `youtube_upload` error rate | < 1% over 24h |
| `image_circuit_breaker_tripped` | 0 over 1h |
| Render-worker JOB success rate | > 95% over 24h |

Author the alert policies in Cloud Monitoring; deep-link from the
new dashboard's `Open in GCP → Monitoring` button.
