# Cloud Monitoring resource collision — `400 Points must be written in order`

> **Status:** fixed (2026-05-13). Pinned by
> `tests/test_obs_otel_init.py::TestCloudRunIdentityAttrs` (canonical
> runtime), `tests/test_otel_init.py::test_resource_has_per_process_identity_attrs`
> (cloud-side source-grep across all 14 mirrored copies), and
> `tests/test_render_video.py::ExtractLastTracebackTest` (the operator
> error surface).

## What broke

A long-form render under `mystoriesanimated` on 2026-05-13 surfaced
this Firestore error field:

```
RuntimeError: pipeline.render.long_form exited with code 1.
Subprocess error:
Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/opentelemetry/exporter/cloud_monitoring/__init__.py", line 429, in export
    self._batch_write(all_series)
  ...
google.api_core.exceptions.InvalidArgument: 400 One or more TimeSeries
could not be written: timeSeries[0-14] (example
metric.type="workload.googleapis.com/ytfactory.events", ...,
resource.type="generic_node",
resource.labels={"node_id": "", "namespace": "", "location": "global"}):
write for resource failed: Points must be written in order.
```

Two distinct bugs were entangled here:

1. **Cloud Monitoring rejected ~100% of metric flushes** because every
   Cloud Run JOB execution + every spawned `pipeline.render.long_form`
   subprocess wrote against the SAME `(metric_type, generic_node{
   location:'global', namespace:'', node_id:''})` tuple. A new run's
   `start_time` is older than the previous run's most recent point →
   400 every export interval.
2. **The OTel exporter traceback drowned the REAL crash cause** in the
   subprocess error surface. The exporter swallows its own exceptions
   internally (returns `MetricExportResult.FAILURE`, logs via
   `logger.error(exc_info=ex)`), so the telemetry traceback is
   logged-but-non-fatal noise. But because it fires at process exit
   (during the metric reader's flush), it's often the LAST traceback
   in the subprocess log — and `_extract_last_traceback` was picking
   the LAST traceback unconditionally. Operators saw "Cloud Monitoring
   400" and chased a phantom telemetry bug while the actual render
   error sat hundreds of lines higher in the log.

## Why the resource collision happened

The OTel→GCP MonitoredResource mapping
(`opentelemetry-resourcedetector-gcp/_mapping.py`) picks the
projection by inspecting which OTel resource attributes are present:

| Required attrs                                     | Projects to     |
|----------------------------------------------------|-----------------|
| `service.name` + `service.instance.id`             | `generic_task`  |
| `service.name` only (no `service.instance.id`)     | `generic_node`  |
| `cloud.platform=gcp_cloud_run` + Cloud Run metadata| `cloud_run_revision` (Cloud Run *services* only) |

The `GoogleCloudResourceDetector` has no specific support for Cloud
Run JOBS — it sets `service.name` (and `service.version`) but NOT
`service.instance.id`, `service.namespace`, or `cloud.region`. So the
mapper falls through to `generic_node`, and `generic_node`'s field
extraction reads:

```
node_id      ← host.id     || ""
namespace    ← service.namespace  || ""
location     ← cloud.availability_zone || cloud.region || "global"
```

All three default to empty/global → every JOB execution lands in the
same time-series bucket → second writer's start_time is older than
first writer's last point → 400.

`add_unique_identifier=True` on the exporter (added in a prior fix,
2026-05-12) makes the exporter's OWN writes consistent across an
exporter lifetime by appending a per-exporter UUID to each metric's
attributes — but it does NOT change the resource projection, so
cross-process collisions still happen for any process that doesn't
share the same exporter instance (which is every Cloud Run JOB
execution + every subprocess).

## The fix (two layers)

### Layer 1 — per-process resource identity attrs

Inject `service.instance.id` (+ `service.namespace` + `cloud.region`)
when running on Cloud Run, so the mapping flips to `generic_task` with
a unique `task_id` per process:

```python
identity_attrs = {
    SERVICE_NAME: service_name,
    SERVICE_VERSION: version,
    "service.instance.id": "-".join([
        os.environ.get("CLOUD_RUN_EXECUTION") or os.environ.get("K_REVISION") or "unknown",
        os.environ.get("CLOUD_RUN_TASK_INDEX") or "0",
        str(os.getpid()),
    ]),
    "service.namespace": (
        os.environ.get("K_SERVICE")
        or os.environ.get("CLOUD_RUN_JOB")
        or service_name
    ),
    "cloud.region": (
        os.environ.get("GOOGLE_CLOUD_REGION")
        or os.environ.get("CLOUD_RUN_REGION")
        or "asia-southeast1"
    ),
}
resource = base.merge(Resource.create(identity_attrs))
```

**Why we re-assert `SERVICE_NAME` + `SERVICE_VERSION` in the identity
merge:** the GCP detector's `Resource.merge` semantics let detected
attrs override base attrs. Without re-asserting, `service.name` falls
back to the detector's `"unknown_service"` default, which then
propagates as the `generic_task.job` label and breaks per-service
grouping in Cloud Monitoring dashboards.

Files:
- `pipeline/observability/otel.py::_cloud_run_identity_attrs`
  (canonical, laptop + control plane)
- `cloud/_shared/otel_init.py` lines 134-157 (cloud-side mirror, since
  cloud services cannot import `pipeline/`)
- 14 `cloud/<service>/otel_init.py` copies sync'd via
  `bash cloud/_shared/sync.sh`

### Layer 2 — telemetry-traceback noise filter

Even with Layer 1 in place, ANY future telemetry exporter failure
(throttling, IAM glitch, network hiccup) would still drown the real
error if it happened at process exit. So `_extract_last_traceback`
now classifies tracebacks by their entry frame (first `File "..."`
line) and prefers the LAST non-telemetry traceback:

```python
_TELEMETRY_TRACEBACK_FRAME_RE = re.compile(
    r"opentelemetry/(?:exporter|sdk/(?:metrics|_logs|trace)/export)/"
)

def _is_telemetry_traceback(block: list[str]) -> bool:
    """True when the FIRST `File "..."` frame is in OTel exporter / SDK export."""
    for line in block:
        s = line.lstrip()
        if s.startswith("File "):
            return bool(_TELEMETRY_TRACEBACK_FRAME_RE.search(line))
    return False
```

Falls back to the telemetry traceback when it's the ONLY one in the
log — the operator at least sees something, and an all-telemetry log
is itself a diagnostic clue (subprocess died from a non-Python path:
SIGKILL, OOM kill, hard `os._exit`).

The classifier only checks the FIRST file frame because deeper frames
(e.g. `google/api_core/grpc_helpers.py`) appear in BOTH telemetry
tracebacks and real render errors that incidentally call into Google
APIs (token refresh, GCS reads). The entry frame is what reliably
distinguishes "exporter failed during export" from "real code called
into Google APIs and got an error".

File: `pipeline/render/video.py::_extract_last_traceback`,
`_is_telemetry_traceback`.

## How to add a new Cloud Run service

The standing rule "every new Python Cloud Run service MUST boot OTel
via `cloud/_shared/otel_init.py`" (CLAUDE.md → Telemetry section) now
implicitly includes the per-process identity attrs because
`cloud/_shared/otel_init.py` carries them. Just follow the existing
6-step playbook (`docs/cloud_service_dep_playbook.md`):

1. Edit canonical (`cloud/_shared/otel_init.py`) if the rule changes.
2. Run `bash cloud/_shared/sync.sh` to mirror to all per-service copies.
3. Run `bash cloud/_shared/append_otel_deps.sh` for new services.
4. Run `bash cloud/_shared/add_otel_copy.sh` to patch the new
   service's Dockerfile to `COPY otel_init.py ./` AND
   `COPY cloud_run_json_exporter.py ./`.
5. The boot block in `server.py` / `entrypoint.py` stays unchanged.

`tests/test_otel_init.py::test_every_per_service_otel_init_byte_identical`
will fail in CI if you forget step 2.

## Diagnostic quick-reference

| Symptom in subprocess log / Firestore error field | Likely cause | Fix |
|---|---|---|
| `Cloud Monitoring 400 Points must be written in order` PLUS a real Python traceback above it | Pre-2026-05-13 surface bug — telemetry traceback drowned real error | Already fixed (Layer 2) — surface should now show real error |
| Surface contains ONLY a telemetry traceback | Subprocess died from non-Python path (SIGKILL / OOM / hard exit) — telemetry was the only thing that printed | Check Cloud Run logs for OOM kills (`google.cloud.run.execution.terminated_reason`); raise memory limit if needed |
| Surface still contains `generic_node` resource labels | Layer 1 didn't deploy yet — re-deploy the worker after the next OTel canonical change lands | `gcloud run deploy ytfactory-render-worker-v2 ...` |

## Related docs

- `docs/telemetry.md` — top-level telemetry runbook (services, exporters,
  IAM)
- `docs/observability_runbook.md` — day-one debug cookbook
- `docs/cloud_service_dep_playbook.md` — 6-step new-Cloud-Run-service
  rule
- `docs/cloudrun_render_worker.md` — render-worker-v2 specifics
- Memory: `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_cloud_monitoring_resource_collision.md`
