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
  - `{"exporter": "inmemory"}` — you're on a fresh laptop without
    GCP creds; the dashboard shows the in-process buffer only.
  - `{"exporter": "console"}` — same, signals are going to stdout
    only.
  - `{"exporter": "gcp"}` — exports are happening; check Cloud
    Trace directly.
  - `{"initialised": false}` — the SDK never booted; something
    silently swallowed an exception in `obs.init()`. Check stderr
    for `"OTel init failed"`.

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

## Recipe: figure out the slowest stage in a render

Cloud Monitoring → Metrics Explorer → custom metric
`ytfactory.stage_duration_ms`. Group by `event` (label). p95 sorted
descending = your hotspots.

For one slug:

```
custom.googleapis.com/opentelemetry/ytfactory.stage_duration_ms
| label.event != ""
| filter label.ytfactory_slug == "aita-001"
```

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
