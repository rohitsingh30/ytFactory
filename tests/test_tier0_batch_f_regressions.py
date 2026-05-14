"""Regression tests for Tier 0 batch F (worker container correctness).

The three telemetry-confirmed bugs in this batch are ALL already fixed
in code (the telemetry hits were from earlier worker images that hadn't
deployed the fixes yet). These tests pin the current state so a future
edit doesn't reintroduce the regression.

Telemetry catalogue refs:
- TEL-FS-09 (9): worker image missing fastapi import
- TEL-FS-11 (3): NonRecordingSpan .status crash
- TEL-LOG-44 (19): clone-video-worker yt-dlp readonly cookies
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestObservabilityImportsWithoutFastapi(unittest.TestCase):
    """TEL-FS-09 regression — pipeline.observability MUST load even
    when fastapi is not installed (Cloud Run JOB workers don't ship it).

    Pre-fix: a stray `from fastapi import Request` at module level
    in http_middleware.py would crash the worker at import time.
    Post-fix: fastapi is only imported under TYPE_CHECKING (compile-
    time only) and lazily inside install() (which workers never call).
    """

    def test_observability_loads_without_fastapi(self):
        # Simulate Cloud Run JOB worker environment (no fastapi).
        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "fastapi" or name.startswith("fastapi."):
                    raise ImportError(f"{name} blocked for test")
                return None

        # Drop already-imported fastapi from the cache.
        for mod_name in list(sys.modules):
            if mod_name == "fastapi" or mod_name.startswith("fastapi."):
                del sys.modules[mod_name]
        # Drop the observability modules so they re-import.
        for mod_name in list(sys.modules):
            if mod_name == "pipeline.observability" or mod_name.startswith(
                "pipeline.observability."
            ):
                del sys.modules[mod_name]

        blocker = _Block()
        sys.meta_path.insert(0, blocker)
        try:
            # These imports MUST succeed without fastapi.
            import pipeline.observability  # noqa: F401
            import pipeline.observability.http_middleware  # noqa: F401
        finally:
            sys.meta_path.remove(blocker)


class TestNonRecordingSpanStatusGuard(unittest.TestCase):
    """TEL-FS-11 regression — _close_span must not read span.status."""

    def test_telemetry_does_not_read_span_dot_status_in_code(self):
        # The forbidden read is span.status.status_code (or any direct
        # access to span.status). Pre-fix: this AttributeError'd on
        # NonRecordingSpan during the ~1s OTel SDK boot window.
        # Post-fix: _close_span calls set_status() unconditionally,
        # which is a no-op on NonRecordingSpan.
        #
        # The post-mortem comment in the same file mentions the
        # forbidden read by name as the historical explanation, so we
        # check by stripping comment lines before scanning.
        src = (REPO_ROOT / "pipeline/observability/telemetry.py").read_text()
        code_lines = []
        for ln in src.splitlines():
            stripped = ln.strip()
            # Skip pure comment lines + lines inside the """ docstring.
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            code_lines.append(ln)
        code = "\n".join(code_lines)
        self.assertNotIn(
            "span.status.status_code",
            code,
            "regression: re-introducing this read crashes worker on "
            "NonRecordingSpan during SDK boot",
        )
        # Sanity-check the safe replacement is still in place.
        self.assertIn("span.set_status", src)


class TestCloneVideoWorkerCookiesWritable(unittest.TestCase):
    """TEL-LOG-44 regression — yt-dlp cookies must be copied from the
    read-only Secret Manager mount to the writable workspace, AND the
    copy path must be passed to yt-dlp via --cookies."""

    def test_server_copies_cookies_to_writable_path_and_uses_them(self):
        src = (REPO_ROOT / "cloud/clone-video-worker/server.py").read_text()
        # The copy step.
        self.assertIn(
            'cookies_use = target_dir / "cookies.txt"',
            src,
            "regression: cookies must be copied to writable target_dir",
        )
        self.assertIn(
            "cookies_use.write_bytes(Path(cookies_src).read_bytes())",
            src,
        )
        # The actual --cookies flag.
        self.assertIn(
            '"--cookies", str(cookies_use)',
            src,
            "regression: cookies_use must be passed to yt-dlp via --cookies",
        )


if __name__ == "__main__":
    unittest.main()
