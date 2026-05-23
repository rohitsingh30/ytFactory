"""Tests that every Python Cloud Run service boots OTel.

Catches the class-of-bug surfaced by audit T1.13: ``otel_init.py``
and ``cloud_run_json_exporter.py`` are synced into every
``cloud/<svc>/`` dir by ``cloud/_shared/sync.sh``, but a service
silently dropping the import means ZERO telemetry from that service
ever reaches Cloud Trace / Cloud Monitoring / Cloud Logging
structured query — a regression that's invisible until someone
notices the dashboard's per-service rollup is empty.

CLAUDE.md "every new Cloud Run service MUST init OTel in its
server.py / entrypoint.py startup".

Scope rule (per CLAUDE.md P9.1): only Python services with a
``server.py`` or ``entrypoint.py`` are auto-covered. Static-asset /
Node.js / one-shot init containers (``cloud/web-next/``,
are explicitly excluded — they have
no Python entry point.
"""
from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUD_DIR = REPO_ROOT / "cloud"


def _python_entrypoints() -> list[Path]:
    """Every cloud/<svc>/server.py + cloud/<svc>/entrypoint.py."""
    out: list[Path] = []
    for svc in sorted(CLOUD_DIR.iterdir()):
        if not svc.is_dir():
            continue
        for name in ("server.py", "entrypoint.py"):
            p = svc / name
            if p.is_file():
                out.append(p)
    return out


class TestEveryServiceInitsOTel(unittest.TestCase):
    def test_at_least_one_service(self) -> None:
        eps = _python_entrypoints()
        self.assertGreaterEqual(len(eps), 5, "expected ≥5 cloud Python services")

    def test_every_python_entrypoint_imports_otel_init(self) -> None:
        # T1.13 regression — pre-fix, 11 of 13 services had otel_init.py
        # COPY'd into the image but never imported, so the dashboard
        # was missing per-service rollups for every TTS + image service.
        for ep in _python_entrypoints():
            with self.subTest(service=ep.relative_to(REPO_ROOT)):
                src = ep.read_text()
                self.assertIn(
                    "from otel_init import",
                    src,
                    f"{ep.relative_to(REPO_ROOT)} does not import otel_init; "
                    "every Python Cloud Run service MUST init OTel "
                    "(see CLAUDE.md 'Telemetry: OTel SDK + Cloud Trace + ...').",
                )
                # The init() call must reference the service name.
                self.assertIn(
                    "_otel_init(",
                    src,
                    f"{ep.relative_to(REPO_ROOT)} imports otel_init but "
                    "never calls _otel_init('<service-name>').",
                )
                # The FastAPI app (if present) must be wrapped.
                if "app = FastAPI" in src or "app=FastAPI" in src:
                    self.assertIn(
                        "_otel_instrument_fastapi(app)",
                        src,
                        f"{ep.relative_to(REPO_ROOT)} has a FastAPI app "
                        "but never calls _otel_instrument_fastapi(app).",
                    )

    def test_every_service_has_otel_init_py_synced(self) -> None:
        # Companion check: the file must be present on disk too. If
        # cloud/_shared/sync.sh stops copying for some reason, the
        # import statement will silently raise + _OTEL_OK becomes False
        # → no telemetry emitted. This catches the upstream sync gap.
        for ep in _python_entrypoints():
            svc_dir = ep.parent
            with self.subTest(service=svc_dir.name):
                self.assertTrue(
                    (svc_dir / "otel_init.py").is_file(),
                    f"{svc_dir.name}/otel_init.py missing; run "
                    "`bash cloud/_shared/sync.sh`.",
                )
                self.assertTrue(
                    (svc_dir / "cloud_run_json_exporter.py").is_file(),
                    f"{svc_dir.name}/cloud_run_json_exporter.py missing; run "
                    "`bash cloud/_shared/sync.sh`.",
                )


if __name__ == "__main__":
    unittest.main()
