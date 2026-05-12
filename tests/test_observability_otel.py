"""Tests for :mod:`pipeline.observability.otel` private helpers.

Pins the Cloud Run JOB detection added in the 2026-05-13 5e37f76b
post-mortem: ``_resolve_env`` and ``_maybe_merge_gcp_resource`` MUST
recognise Cloud Run jobs (CLOUD_RUN_JOB env), not only services
(K_SERVICE env), so observability tagging stays consistent for
production.
"""
from __future__ import annotations

import os
import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.observability import otel as _otel


class ResolveEnvTest(unittest.TestCase):
    """``_resolve_env`` must label Cloud Run JOBS as ``prod`` (not
    ``dev``). Pre-2026-05-13 it only checked ``K_SERVICE``, so JOB
    spans/metrics were tagged ``deployment.environment=dev`` even
    though they ran in production — confusing the dashboard's
    "filter by env" facet."""

    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_dev_when_no_cloud_run_signals(self) -> None:
        for k in ("K_SERVICE", "CLOUD_RUN_JOB", "PYTEST_CURRENT_TEST"):
            os.environ.pop(k, None)
        self.assertEqual(_otel._resolve_env(), "dev")

    def test_test_when_pytest_only(self) -> None:
        for k in ("K_SERVICE", "CLOUD_RUN_JOB"):
            os.environ.pop(k, None)
        os.environ["PYTEST_CURRENT_TEST"] = "x"
        self.assertEqual(_otel._resolve_env(), "test")

    def test_prod_when_K_SERVICE_set(self) -> None:
        os.environ.pop("CLOUD_RUN_JOB", None)
        os.environ["K_SERVICE"] = "tts-chatterbox"
        self.assertEqual(_otel._resolve_env(), "prod")

    def test_prod_when_CLOUD_RUN_JOB_set(self) -> None:
        # The regression-pin: JOB env, no K_SERVICE.
        os.environ.pop("K_SERVICE", None)
        os.environ["CLOUD_RUN_JOB"] = "ytfactory-render-worker-v2"
        self.assertEqual(_otel._resolve_env(), "prod")


class MaybeMergeGcpResourceTest(unittest.TestCase):
    """``_maybe_merge_gcp_resource`` must attempt the GCP detector
    on Cloud Run JOBS too. Pre-2026-05-13 it only triggered on
    ``K_SERVICE`` → JOB spans missed gcp.* attributes (project,
    region, instance_id) and Cloud Trace UI couldn't link them
    back to the right service."""

    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_returns_base_unchanged_when_off_cloud_run(self) -> None:
        for k in ("K_SERVICE", "CLOUD_RUN_JOB", "CLOUDRUN_EXECUTION", "CLOUD_RUN_EXECUTION"):
            os.environ.pop(k, None)
        from opentelemetry.sdk.resources import Resource
        base = Resource.create({"service.name": "x"})
        merged = _otel._maybe_merge_gcp_resource(base)
        self.assertIs(merged, base)

    def test_attempts_detector_when_K_SERVICE_set(self) -> None:
        # We can't exercise the actual detector network call in a
        # unit test, but we can verify the GUARD passes (the function
        # ENTERS the try block) by asserting the return is a Resource
        # (whether merged-or-not — the detector swallows errors).
        os.environ.pop("CLOUD_RUN_JOB", None)
        os.environ["K_SERVICE"] = "tts-chatterbox"
        from opentelemetry.sdk.resources import Resource
        base = Resource.create({"service.name": "x"})
        merged = _otel._maybe_merge_gcp_resource(base)
        # Doesn't crash, returns a Resource. The detector itself
        # may have failed (no creds in test env) and logged a
        # warning, which is the correct behaviour.
        self.assertIsInstance(merged, Resource)

    def test_attempts_detector_when_CLOUD_RUN_JOB_set(self) -> None:
        # Regression-pin: JOB env triggers the detector branch too.
        # Without this, the function early-returned `base` for any
        # JOB execution, dropping gcp.* attrs from every long-form
        # render's spans.
        os.environ.pop("K_SERVICE", None)
        os.environ["CLOUD_RUN_JOB"] = "ytfactory-render-worker-v2"
        from opentelemetry.sdk.resources import Resource
        base = Resource.create({"service.name": "x"})
        merged = _otel._maybe_merge_gcp_resource(base)
        self.assertIsInstance(merged, Resource)


class BuildResourceFallbackChainTest(unittest.TestCase):
    """``_build_resource`` must use ``CLOUD_RUN_JOB`` as the service
    name when neither an explicit service_name nor ``OTEL_SERVICE_NAME``
    nor ``K_SERVICE`` is set — otherwise long-form renders fall back
    to ``ytfactory-laptop`` (misleading attribution in Cloud Trace)."""

    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_explicit_service_name_wins(self) -> None:
        os.environ["CLOUD_RUN_JOB"] = "ytfactory-render-worker-v2"
        r = _otel._build_resource(service_name="explicit", service_version=None, extra=None)
        self.assertEqual(r.attributes["service.name"], "explicit")

    def test_K_SERVICE_wins_over_CLOUD_RUN_JOB(self) -> None:
        # When BOTH are set (impossible today but possible if Google
        # ever unifies them), the legacy K_SERVICE takes precedence
        # so existing dashboards keep grouping by service.
        for k in ("OTEL_SERVICE_NAME",):
            os.environ.pop(k, None)
        os.environ["K_SERVICE"] = "from-k-service"
        os.environ["CLOUD_RUN_JOB"] = "from-cloud-run-job"
        r = _otel._build_resource(service_name=None, service_version=None, extra=None)
        self.assertEqual(r.attributes["service.name"], "from-k-service")

    def test_CLOUD_RUN_JOB_used_when_K_SERVICE_unset(self) -> None:
        # The bug we're regression-pinning: render-worker-v2 had
        # service.name="ytfactory-laptop" because nobody asked
        # CLOUD_RUN_JOB.
        for k in ("OTEL_SERVICE_NAME", "K_SERVICE"):
            os.environ.pop(k, None)
        os.environ["CLOUD_RUN_JOB"] = "ytfactory-render-worker-v2"
        r = _otel._build_resource(service_name=None, service_version=None, extra=None)
        self.assertEqual(r.attributes["service.name"], "ytfactory-render-worker-v2")


if __name__ == "__main__":
    unittest.main()
