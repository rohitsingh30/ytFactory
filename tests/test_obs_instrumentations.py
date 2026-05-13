"""Tests for :mod:`pipeline.observability.instrumentations`.

Verifies idempotency + that the subprocess wrapper produces a span.
FastAPI instrumentation is exercised in P3 (route-level tests); we
just confirm the helper can be called twice without error here.
"""
from __future__ import annotations

import subprocess
import unittest

from pipeline import observability as obs
from pipeline.observability import instrumentations as inst


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        inst.uninstrument_subprocess()
        obs.reset_for_tests()


class TestSubprocess(_Base):
    def test_subprocess_wrapper_creates_span(self) -> None:
        inst.instrument_subprocess()
        # Run a tiny no-op command.
        subprocess.run(["true"], check=False)
        self.bundle.span_processor.force_flush()
        spans = self.bundle.span_inmemory.get_finished_spans()
        names = [s.name for s in spans]
        # Both the .run() span and the Popen-init span fire.
        self.assertIn("subprocess", names)
        run_span = next(s for s in spans if s.name == "subprocess")
        self.assertEqual(run_span.attributes["ytfactory.cmd_name"], "true")
        self.assertEqual(run_span.attributes["ytfactory.returncode"], 0)

    def test_subprocess_instrument_idempotent(self) -> None:
        inst.instrument_subprocess()
        inst.instrument_subprocess()  # second call is a no-op
        # confirm we still have ONE wrapper, not nested
        subprocess.run(["true"], check=False)
        self.bundle.span_processor.force_flush()
        run_spans = [
            s for s in self.bundle.span_inmemory.get_finished_spans()
            if s.name == "subprocess"
        ]
        self.assertEqual(len(run_spans), 1)

    def test_uninstrument_restores_original(self) -> None:
        inst.instrument_subprocess()
        original = inst._ORIG_RUN
        inst.uninstrument_subprocess()
        # subprocess.run should now be the original
        self.assertIs(subprocess.run, original)


class TestOutboundIdempotent(_Base):
    def test_outbound_http_idempotent(self) -> None:
        inst.instrument_outbound_http()
        inst.instrument_outbound_http()  # second call must not raise

    def test_audit_q268_urllib_instrumentor_attempted(self) -> None:
        """Audit Q2.68 — pre-fix instrument_outbound_http only patched
        requests / httpx / aiohttp. ``pipeline/tts/cloudrun.py`` uses
        ``urllib.request.urlopen`` directly; without an instrumentor
        every TTS Cloud Run call broke the trace-propagation chain.
        Now also patches urllib via OTel's URLLibInstrumentor.

        Test verifies the URLLibInstrumentor import branch is
        attempted (logs a warning if the optional package isn't
        installed; fine in test env). The presence of the import in
        the source is the contract; this test pins it.
        """
        # Source-level pin: the urllib instrumentor import must be
        # in instrument_outbound_http's body.
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "pipeline" / "observability" / "instrumentations.py"
        )
        text = src.read_text()
        self.assertIn(
            "from opentelemetry.instrumentation.urllib import URLLibInstrumentor",
            text,
            "Q2.68 — urllib instrumentor must be wired into "
            "instrument_outbound_http so pipeline/tts/cloudrun.py "
            "urllib calls show up in Cloud Trace.",
        )

    def test_audit_d367_failure_doesnt_block_retry(self) -> None:
        """Audit D3.67 — pre-fix `_STATE['outbound']` was set
        unconditionally at the end, so any partial-failure run
        (some library raised ImportError mid-way) was treated as
        fully-instrumented and never retried. Now, if EVERY library
        fails (success_count == 0), `_STATE['outbound']` stays False
        so a future call can retry once the missing packages are
        installed."""
        import sys as _sys
        from unittest.mock import patch as _patch
        # Reset the flag and force every import to fail by mapping
        # the targeted submodules to ImportError-raising stubs.
        inst._STATE["outbound"] = False

        # Pre-pop any cached imports of the targeted instrumentors so
        # the import statements re-run inside instrument_outbound_http.
        for mod_name in (
            "opentelemetry.instrumentation.requests",
            "opentelemetry.instrumentation.httpx",
            "opentelemetry.instrumentation.aiohttp_client",
            "opentelemetry.instrumentation.urllib",
        ):
            _sys.modules.pop(mod_name, None)
        # Stub each as None so a fresh import raises ImportError per
        # PEP 328 sentinel semantics.
        stubs = {
            "opentelemetry.instrumentation.requests": None,
            "opentelemetry.instrumentation.httpx": None,
            "opentelemetry.instrumentation.aiohttp_client": None,
            "opentelemetry.instrumentation.urllib": None,
        }
        with _patch.object(inst, "init", return_value=None), \
             _patch.dict(_sys.modules, stubs):
            inst.instrument_outbound_http()
        # All-failed → flag must STAY False so future retry is possible.
        self.assertFalse(inst._STATE["outbound"],
                         "outbound flag must stay False when every "
                         "library failed, so retry is possible")


class TestInstallAll(_Base):
    def test_install_all_runs(self) -> None:
        inst.install_all(app=None, with_subprocess=False)


if __name__ == "__main__":
    unittest.main()
