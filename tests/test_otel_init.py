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
``image-flux2-klein``, ``image-hidream``, ``image-qwen``,
``image-z-image-turbo``, ``render-worker-v2``, ``tts-chatterbox``,
``tts-cosyvoice``, ``tts-f5``, ``tts-higgs``, ``tts-indicf5``,
``tts-indicparler``, ``web-server``.
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


if __name__ == "__main__":
    unittest.main()
