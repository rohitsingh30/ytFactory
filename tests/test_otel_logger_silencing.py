"""Regression tests for the OTel exporter logger silencing.

Tier 0 batch H canary post-mortem (2026-05-14): the OTel cloud_monitoring
exporter logs ERROR-level grpc._InactiveRpcError exceptions to stderr
when Cloud Monitoring rejects a metric flush ("Points must be written
more frequently than the maximum sampling period"). This is recoverable
(next 60s window succeeds, no data lost) but the ERROR records were
poisoning the worker subprocess tail captured by
pipeline.render.video._format_subprocess_failure, masking every real
ffmpeg failure with OTel grpc tracebacks.

Catalogue: OBS-01, OBS-02, TEL-LOG-01 (200 hits/14d on web),
TEL-LOG-07 (78 hits/14d on worker), TEL-LOG-34 (8 hits/14d on
chatterbox), TEL-FS-13 (19 hits/30d on worker).
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_shared_otel_init():
    """Import cloud/_shared/otel_init.py without sys.path pollution.

    The dir contains a hyphen so a regular import can't reach it.
    """
    path = REPO_ROOT / "cloud" / "_shared" / "otel_init.py"
    spec = importlib.util.spec_from_file_location(
        "_shared_otel_init_for_silence_tests",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestNoisyOtelLoggersSilenced(unittest.TestCase):
    """When init() runs on Cloud Run, the noisy OTel exporter loggers
    must be set to CRITICAL so their ERROR-level grpc tracebacks don't
    poison stderr (and the subprocess tail capture)."""

    NOISY_LOGGERS = (
        "opentelemetry.exporter.cloud_monitoring",
        "opentelemetry.sdk.metrics._internal.export",
        "opentelemetry.sdk.trace.export",
    )

    def test_silencing_block_present_in_otel_init_source(self):
        """The silencing block must be in the source, applied during init().

        We don't run init() directly because it requires Cloud Run env +
        ADC. We assert by source-string presence so a refactor can't
        delete the block silently. Pre-fix the block didn't exist and
        every render's stderr was poisoned by ~50-200 OTel ERROR
        tracebacks per run.
        """
        src = (REPO_ROOT / "cloud" / "_shared" / "otel_init.py").read_text()
        for name in self.NOISY_LOGGERS:
            self.assertIn(
                name, src,
                f"otel_init.py must reference {name!r} in the silencing "
                f"block; pre-fix this logger emitted ERROR-level grpc "
                f"tracebacks that poisoned subprocess tail capture",
            )
        self.assertIn(
            "setLevel(logging.CRITICAL)", src,
            "otel_init.py must downgrade the noisy loggers to CRITICAL",
        )


if __name__ == "__main__":
    unittest.main()
