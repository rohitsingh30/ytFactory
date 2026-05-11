"""P4 — verifies the cloud OTel init helper.

Each Cloud Run service in ``cloud/<service>/`` ships a copy of
:mod:`cloud._shared.otel_init`. We exercise the helper here to confirm:

* :func:`init` boots cleanly on the test laptop (no GCP creds — the
  function should degrade gracefully and not raise).
* The exported helpers (``instrument_fastapi``, ``instrument_outbound_http``,
  ``attach_traceparent_from_env``) are no-ops when called repeatedly
  / without OTel installed.
* The service-side copies are byte-identical to the canonical via
  ``cloud/_shared/sync.sh --check`` (drift detection).

This test does NOT exercise the real GCP exporters — that's reserved
for P8's deploy smoke test against ``ytfactory-prod-v2``.
"""
from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED = REPO_ROOT / "cloud" / "_shared" / "otel_init.py"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(
        f"_otel_init_{abs(hash(str(path)))}",
        str(path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestCanonicalOtelInit(unittest.TestCase):
    def test_canonical_module_loads(self) -> None:
        m = _load_module(SHARED)
        self.assertTrue(hasattr(m, "init"))
        self.assertTrue(hasattr(m, "instrument_fastapi"))
        self.assertTrue(hasattr(m, "instrument_outbound_http"))
        self.assertTrue(hasattr(m, "attach_traceparent_from_env"))

    def test_init_is_idempotent_and_does_not_raise_without_creds(self) -> None:
        m = _load_module(SHARED)
        # Each call resets the per-module state — but for the cloud
        # init it's per-process. We just verify it doesn't raise.
        m.init("test-service")
        m.init("test-service")  # second call is a no-op

    def test_attach_traceparent_no_env_is_safe(self) -> None:
        m = _load_module(SHARED)
        # No YTFACTORY_TRACEPARENT in env → silent no-op, no exception.
        m.attach_traceparent_from_env()

    def test_instrument_outbound_http_idempotent(self) -> None:
        m = _load_module(SHARED)
        m.instrument_outbound_http()
        m.instrument_outbound_http()


class TestServiceCopiesInSync(unittest.TestCase):
    """``cloud/_shared/sync.sh --check`` must report zero drift across
    every cloud service that has a server.py / entrypoint.py."""

    def test_all_service_copies_match_canonical(self) -> None:
        sync_sh = REPO_ROOT / "cloud" / "_shared" / "sync.sh"
        result = subprocess.run(
            ["bash", str(sync_sh), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"sync drift detected:\n{result.stderr}\n{result.stdout}",
        )


if __name__ == "__main__":
    unittest.main()
