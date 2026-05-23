"""Lockstep + smoke test for ``cloud/_shared/otel_init.py`` and every
per-service copy.

``cloud/_shared/otel_init.py`` is the canonical Cloud Run OTel boot
helper. ``bash cloud/_shared/sync.sh`` copies it into every
``cloud/<service>/`` directory because each service's container image
is built from its own (per-service or repo-root) Cloud Build context
and can't reach files in ``cloud/_shared/``.

This test exists so that:

1. **Drift between the canonical and any copy is caught at unit-test
   time** — a stale per-service copy ships an out-of-date exporter on
   the next deploy. ``test_every_per_service_otel_init_byte_identical``.
2. **Each copy actually parses + imports** — runs every per-service
   ``otel_init.py`` through ``importlib`` so any syntax error / typo
   lands in pytest output rather than 5 minutes into a Cloud Build.
3. **The Cloud-Run boot path constructs without raising** — calls
   ``init('test-svc')`` against each copy with ``K_SERVICE`` unset
   (so it takes the "off Cloud Run" branch and skips the GCP exporter
   construction that would need ADC). This pins the import-time +
   resource construction code paths.

The per-service directory names are listed explicitly in
``PER_SERVICE_DIRS`` below so the coverage gate's grep-based test
discovery (``scripts/coverage_gate.py::find_related_tests``) can map
each ``cloud/<service>/otel_init.py`` back to this test file.
Without these literal references, the gate's parent-dir-name grep
finds nothing and reports the per-service copies as uncovered. Keep
this list in sync with ``cloud/_shared/sync.sh::SERVICES``.

Per-service dirs covered: ``clone-video-worker``, ``editing-agent``,
``image-z-image-turbo``, ``render-worker-v2``, ``tts-chatterbox``,
``tts-indicf5``, ``web-server``.
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _every_otel_init_path() -> list[Path]:
    cloud_dir = REPO_ROOT / "cloud"
    paths = [cloud_dir / "_shared" / "otel_init.py"]
    for d in sorted(cloud_dir.iterdir()):
        if not d.is_dir() or d.name == "_shared":
            continue
        f = d / "otel_init.py"
        if f.exists():
            paths.append(f)
    return paths


class TestOtelInitLockstep(unittest.TestCase):
    def setUp(self) -> None:
        # Run with K_SERVICE unset so the "off Cloud Run" branch fires
        # — that branch doesn't need ADC and exercises the same import
        # + resource-construction code paths.
        self._snap = dict(os.environ)
        os.environ.pop("K_SERVICE", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._snap)

    def test_every_per_service_otel_init_byte_identical(self) -> None:
        canonical = (REPO_ROOT / "cloud" / "_shared" / "otel_init.py").read_text()
        copies = [p for p in _every_otel_init_path()
                  if p != REPO_ROOT / "cloud" / "_shared" / "otel_init.py"]
        for p in copies:
            with self.subTest(path=str(p)):
                self.assertEqual(
                    p.read_text(), canonical,
                    f"{p} drifted from cloud/_shared/otel_init.py — "
                    f"run `bash cloud/_shared/sync.sh`",
                )

    def test_every_copy_imports_without_error(self) -> None:
        for path in _every_otel_init_path():
            with self.subTest(path=str(path)):
                spec = importlib.util.spec_from_file_location(
                    f"_otel_init_{path.parent.name}", str(path),
                )
                self.assertIsNotNone(spec)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self.assertTrue(hasattr(mod, "init"))
                self.assertTrue(hasattr(mod, "instrument_fastapi"))
                self.assertTrue(hasattr(mod, "instrument_outbound_http"))
                self.assertTrue(hasattr(mod, "attach_traceparent_from_env"))

    def test_init_off_cloud_run_does_not_raise(self) -> None:
        """``init`` is wrapped in try/except (telemetry must NEVER
        block service boot) but we still want to know if its happy
        path raises in our environment. K_SERVICE unset → no GCP
        exporter construction → no ADC needed."""
        for path in _every_otel_init_path():
            with self.subTest(path=str(path)):
                spec = importlib.util.spec_from_file_location(
                    f"_otel_init_smoke_{path.parent.name}", str(path),
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Reset its module-level state so each subtest sees a
                # fresh boot.
                if hasattr(mod, "_STATE"):
                    mod._STATE["initialised"] = False
                # Doesn't return anything; just confirms no exception
                # leaks past the wrapper.
                mod.init(f"smoke-{path.parent.name}")

    def test_audit_q263_cloud_run_json_exporter_imports_cleanly(self) -> None:
        """Audit Q2.63 — pre-fix the test above wrapped `init()` whose
        body itself swallows all exceptions. The test passed even if
        ``import cloud_run_json_exporter`` failed silently inside
        ``init()`` — exactly the regression the lockstep was supposed
        to catch. Now ALSO directly import the sibling JSON exporter
        from the same dir as each otel_init.py copy so a missing /
        rotted helper fails loudly here.
        """
        for init_path in _every_otel_init_path():
            with self.subTest(init_path=str(init_path)):
                exporter_path = init_path.parent / "cloud_run_json_exporter.py"
                self.assertTrue(
                    exporter_path.exists(),
                    f"cloud_run_json_exporter.py missing next to {init_path}; "
                    f"the otel_init.py boot helper imports it via relative "
                    f"name and would silently fall back to ConsoleLogRecordExporter "
                    f"(which Cloud Logging treats as textPayload, breaking "
                    f"jsonPayload-based filters in the dashboard).",
                )
                spec = importlib.util.spec_from_file_location(
                    f"_jsonexp_strict_{init_path.parent.name}",
                    str(exporter_path),
                )
                self.assertIsNotNone(spec)
                mod = importlib.util.module_from_spec(spec)
                # If this raises, the test FAILS — the silent
                # try/except inside otel_init.init() can't hide it
                # because we're invoking the exporter loader directly.
                spec.loader.exec_module(mod)
                # Smoke-check the public surface the otel_init expects.
                self.assertTrue(
                    hasattr(mod, "CloudRunJSONLogExporter")
                    or hasattr(mod, "render_log_record"),
                    f"{exporter_path} is missing both expected symbols",
                )

    def test_attach_traceparent_no_env_is_noop(self) -> None:
        os.environ.pop("YTFACTORY_TRACEPARENT", None)
        for path in _every_otel_init_path():
            spec = importlib.util.spec_from_file_location(
                f"_otel_init_tp_{path.parent.name}", str(path),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Should not raise even when no traceparent env is set.
            mod.attach_traceparent_from_env()

    def test_cloud_monitoring_exporter_uses_unique_identifier(self) -> None:
        """Pin ``add_unique_identifier=True`` on every per-service
        ``CloudMonitoringMetricsExporter(...)`` construction.

        Without this flag, every Cloud Run JOB task / replica / service
        revision writes against the same ``(metric_type, generic_node{
        location:'global', namespace:'', node_id:''})`` tuple. Cloud
        Monitoring rejects the second writer's points with ``400 Points
        must be written in order`` whenever its start_time is older than
        the most recent write — which is *every* fresh JOB execution.

        Pre-fix this rejected ~100% of metric flushes from
        render-worker-v2 AND poisoned ``_format_subprocess_failure``'s
        log-tail extraction (the OTel exporter's stderr trace was the
        last "Python traceback" before exit, masking the real renderer
        error). See the 2026-05-13 cron-failure post-mortem.

        The lockstep test above guarantees per-service copies match
        the canonical, so the canonical is the only file we have to
        grep — but we re-check every copy here so a future drift fix
        that bypasses sync.sh can't silently ship the broken default.
        """
        for path in _every_otel_init_path():
            with self.subTest(path=str(path)):
                src = path.read_text()
                self.assertIn(
                    "CloudMonitoringMetricsExporter(",
                    src,
                    f"{path}: helper no longer constructs "
                    f"CloudMonitoringMetricsExporter — update this test",
                )
                self.assertIn(
                    "add_unique_identifier=True",
                    src,
                    f"{path}: CloudMonitoringMetricsExporter must be "
                    f"constructed with add_unique_identifier=True so "
                    f"per-process metric writers don't collide on the "
                    f"shared (metric_type, generic_node) resource tuple "
                    f"and trigger '400 Points must be written in order'.",
                )

    def test_resource_has_per_process_identity_attrs(self) -> None:
        """Pin the per-process collision-breaker resource attrs.

        ``add_unique_identifier=True`` (above) makes the *exporter*
        emit a unique label per process, but Cloud Monitoring's
        resource-side projection ALSO needs ``service.instance.id``
        (+ ``service.namespace`` + ``cloud.region``) to flip the
        OTel→GCP MonitoredResource mapping from ``generic_node``
        (one bucket region-wide) to ``generic_task`` (one bucket
        per process). Without the resource attrs, the exporter's
        unique-id makes its own writes consistent but does NOT
        prevent the cross-process collision that triggered the
        2026-05-13 b0986504 "Points must be written in order"
        cascade. Belt-and-braces.

        Source-grep rather than runtime call because ``init()``'s
        on-cloud-run branch needs ADC + real GCP exporters; the
        canonical's runtime behaviour is covered by
        ``tests/test_obs_otel_init.py::TestCloudRunIdentityAttrs``.
        """
        required_substrings = (
            "service.instance.id",
            "service.namespace",
            "cloud.region",
            "CLOUD_RUN_EXECUTION",
            "CLOUD_RUN_TASK_INDEX",
            "os.getpid()",
        )
        for path in _every_otel_init_path():
            with self.subTest(path=str(path)):
                src = path.read_text()
                for needle in required_substrings:
                    self.assertIn(
                        needle,
                        src,
                        f"{path}: missing per-process identity attr "
                        f"`{needle}` — the OTel→Cloud Monitoring "
                        f"resource projection will fall back to "
                        f"`generic_node` with all-empty labels and "
                        f"every JOB execution / spawned subprocess "
                        f"will collide on the same time-series "
                        f"bucket. Run `bash cloud/_shared/sync.sh` "
                        f"after editing the canonical to mirror the "
                        f"fix into this copy.",
                    )


if __name__ == "__main__":
    unittest.main()
