"""Tests for ``pipeline.render.video`` subprocess streaming + error
extraction.

Regression-pins the 2026-05-13 5e37f76b post-mortem: the long_form
subprocess died after 12 minutes and the worker's "Last 25 log lines"
error surface was 100% OTel ``ConsoleMetricExporter`` JSON dump,
hiding the actual Python traceback. Three guarantees this file
locks in:

1. ``_stream_subprocess`` tees subprocess output to BOTH the on-disk
   log file AND the parent's stdout (Cloud Run captures parent
   stdout into Cloud Logging — without the tee the subprocess
   output never leaves the container).

2. ``_extract_last_traceback`` finds the LAST Python traceback in
   the log file, not the trailing 25 lines (which can be drowned
   by OTel JSON noise from the subprocess's own exit-time metric
   flush).

3. The renderer surfaces the Python traceback to the worker's
   Firestore error field — operators see "ZeroDivisionError",
   not "}\n  ]\n}\n  ]".
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.render import video as _video


class ExtractLastTracebackTest(unittest.TestCase):
    """Pin the noise-tolerant traceback extractor."""

    def test_returns_empty_on_empty_input(self) -> None:
        self.assertEqual(_video._extract_last_traceback(""), "")

    def test_falls_back_to_last_25_lines_when_no_traceback(self) -> None:
        # No Python traceback in the log — preserve the legacy
        # "tail of the log" surface so we don't lose info entirely.
        log = "\n".join(f"line {i}" for i in range(40))
        out = _video._extract_last_traceback(log)
        self.assertEqual(out.splitlines(), [f"line {i}" for i in range(15, 40)])

    def test_extracts_traceback_from_middle_of_log(self) -> None:
        # Real shape: subprocess hits an exception, then OTel SDK
        # dumps its metric JSON during the BatchSpanProcessor /
        # PeriodicExportingMetricReader shutdown flush. The
        # traceback is BEFORE the JSON noise; tail-25 misses it
        # entirely, but the extractor anchors on the
        # "Traceback (most recent call last):" header and grabs
        # from there to EOF.
        log = (
            "[INFO] starting render\n"
            "[INFO] tts ok\n"
            "Traceback (most recent call last):\n"
            "  File \"/workspace/pipeline/render/long_form.py\", line 1234, in compose\n"
            "    final = _ffmpeg_mux(parts)\n"
            "ZeroDivisionError: division by zero\n"
            # ↓↓↓ OTel metric flush at process exit — would dominate
            # the legacy tail-25 surface.
            "{\n"
            "    \"resource_metrics\": [\n"
        )
        # Pad with 30 lines of OTel JSON noise.
        log += "\n".join("        }," for _ in range(30)) + "\n"
        out = _video._extract_last_traceback(log)
        self.assertIn("Traceback (most recent call last):", out)
        self.assertIn("ZeroDivisionError: division by zero", out)
        # Extractor returns from the LAST Traceback header to EOF
        # (capped) — the JSON noise IS included (so we don't lose
        # the metric data), but the actionable traceback now appears
        # at the START of the surfaced block, not buried below 25
        # lines of garbage.
        self.assertTrue(out.startswith("Traceback"))

    def test_picks_LAST_traceback_when_multiple_present(self) -> None:
        # Some long-form runs swallow an early exception, recover,
        # then crash later. We want the FATAL traceback (the one
        # closest to subprocess exit), not the recovered one.
        log = (
            "Traceback (most recent call last):\n"
            "  File \"x\", line 1, in y\n"
            "ValueError: recovered, kept going\n"
            "[INFO] recovered, continuing\n"
            "[INFO] phase 2\n"
            "Traceback (most recent call last):\n"
            "  File \"a\", line 99, in b\n"
            "RuntimeError: this is the one that killed us\n"
        )
        out = _video._extract_last_traceback(log)
        self.assertIn("RuntimeError: this is the one that killed us", out)
        # The earlier (recovered) traceback should NOT appear.
        self.assertNotIn("ValueError: recovered, kept going", out)

    def test_caps_block_at_max_lines(self) -> None:
        # Even when the last traceback is followed by a 1000-line
        # OTel dump, the surfaced block stays bounded so the
        # Firestore error field doesn't hit the 1MB doc limit.
        header = "Traceback (most recent call last):\n"
        body = "  ".join(["File...\n"] * 5)
        tail = "\n".join("  noise " + str(i) for i in range(500))
        log = header + body + tail
        out = _video._extract_last_traceback(log, max_lines=50)
        self.assertLessEqual(len(out.splitlines()), 50)


class StreamSubprocessTeeTest(unittest.TestCase):
    """Pin the tee-to-parent-stdout behaviour."""

    def test_subprocess_stdout_appears_in_both_log_file_and_parent_stdout(self) -> None:
        # Use a real subprocess (echo a few lines, exit 0) to confirm
        # the tee semantics end-to-end. This catches regressions where
        # someone removes the sys.stdout.write inside the streaming
        # loop "to clean up the noise" — without realising that the
        # noise IS the cloud-logs surface.
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subproc.log"
            cmd = [
                sys.executable, "-c",
                "import sys\n"
                "for i in range(3):\n"
                "    sys.stdout.write(f'line {i}\\n')\n"
                "    sys.stdout.flush()\n",
            ]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = _video._stream_subprocess(
                    cmd, log_path=log_path, progress_cb=None,
                )
            self.assertEqual(rc, 0)
            file_text = log_path.read_text()
            stdout_text = buf.getvalue()
            for i in range(3):
                self.assertIn(f"line {i}", file_text,
                              "subprocess output must reach the on-disk log file")
                self.assertIn(f"line {i}", stdout_text,
                              "subprocess output must also tee to parent stdout "
                              "so Cloud Run captures it into Cloud Logging — "
                              "without this, subprocess errors are invisible "
                              "in cloud-side debugging")

    def test_subprocess_stderr_also_streamed_via_stderr_to_stdout_redirect(self) -> None:
        # Popen is set up with stderr=STDOUT so stderr-only output
        # also reaches both surfaces (the actual Python traceback
        # writes to stderr).
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subproc.log"
            cmd = [
                sys.executable, "-c",
                "import sys\n"
                "sys.stderr.write('this is stderr\\n')\n"
                "sys.stderr.flush()\n",
            ]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = _video._stream_subprocess(
                    cmd, log_path=log_path, progress_cb=None,
                )
            self.assertEqual(rc, 0)
            self.assertIn("this is stderr", log_path.read_text())
            self.assertIn("this is stderr", buf.getvalue())

    def test_progress_callback_still_invoked_on_long_form_markers(self) -> None:
        # The legacy long_form_renderer.log progress regex must keep
        # firing — the tee was added in addition to, not instead of,
        # the per-line progress dispatch.
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))

        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subproc.log"
            cmd = [
                sys.executable, "-c",
                "print('[1/5] chunked TTS')\n"
                "print('[2/5] image_panels')\n",
            ]
            with redirect_stdout(io.StringIO()):
                rc = _video._stream_subprocess(
                    cmd, log_path=log_path, progress_cb=cb,
                )
        self.assertEqual(rc, 0)
        self.assertEqual([s for s, _ in calls], ["tts", "images"])


class RenderLongFormErrorSurfaceTest(unittest.TestCase):
    """Pin that the failure-surface helper produces the right
    operator-actionable message format."""

    def test_format_uses_subprocess_error_header_not_legacy_phrasing(self) -> None:
        # The "Last 25 log lines:" phrasing was the buggy 2026-05-13
        # default — it implied "we can read the log" but the LLM-side
        # operator surface ALSO needs to know it's a subprocess. The
        # new "Subprocess error:" phrasing is unambiguous AND signals
        # the expected payload (Python traceback, not raw tail).
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subproc.log"
            log_path.write_text(
                "Traceback (most recent call last):\n"
                "  File \"x\", line 1, in y\n"
                "RuntimeError: panel render failed: missing image\n"
            )
            msg = _video._format_subprocess_failure(rc=1, log_path=log_path)
        self.assertIn("pipeline.render.long_form exited with code 1.", msg)
        self.assertIn("Subprocess error:", msg)
        self.assertIn("RuntimeError: panel render failed", msg)
        self.assertNotIn("Last 25 log lines:", msg)

    def test_format_handles_missing_log_file_without_crashing(self) -> None:
        # If the subprocess died before the log file got flushed (or
        # the cwd was wiped by the platform), the helper must NOT
        # raise — operators still need the rc + a clear message.
        msg = _video._format_subprocess_failure(
            rc=137, log_path=Path("/nonexistent/path/subproc.log"),
        )
        self.assertIn("exited with code 137", msg)
        # tail is empty when the file is missing, but the structure
        # is preserved so the worker can still pin it to Firestore.
        self.assertIn("Subprocess error:", msg)

    def test_format_skips_otel_json_noise_via_extract_last_traceback(self) -> None:
        # End-to-end fingerprint of the 2026-05-13 5e37f76b failure:
        # log file has a traceback BURIED below 200 lines of OTel
        # ConsoleMetricExporter JSON output. The format helper must
        # surface the traceback at the TOP of the message, not the
        # JSON noise.
        log_text = (
            "[INFO] starting render\n"
            "Traceback (most recent call last):\n"
            "  File \"/workspace/pipeline/render/long_form.py\", line 999, in compose\n"
            "    final = _ffmpeg(parts)\n"
            "OSError: [Errno 28] No space left on device\n"
        )
        log_text += "\n".join("  } ," for _ in range(200)) + "\n"
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subproc.log"
            log_path.write_text(log_text)
            msg = _video._format_subprocess_failure(rc=1, log_path=log_path)
        # Operator should see the OSError without scrolling through
        # 200 lines of JSON garbage. We assert the traceback line
        # appears within the first 5 lines of "Subprocess error:".
        body = msg.split("Subprocess error:\n", 1)[1]
        first_few = "\n".join(body.splitlines()[:5])
        self.assertIn("Traceback (most recent call last):", first_few)
        self.assertIn("OSError:", body)

    def test_format_swallows_read_text_exceptions(self) -> None:
        # If reading the log file raises (rare — disk corruption,
        # permission flip mid-read), the helper must NOT propagate
        # the IO error and mask the original subprocess failure rc.
        # The operator still gets "exited with code <rc>" and an
        # empty Subprocess-error block — better than a chained
        # exception that hides which call site originally failed.
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subproc.log"
            log_path.write_text("real content")
            with patch.object(Path, "read_text", side_effect=OSError("disk gone")):
                msg = _video._format_subprocess_failure(rc=42, log_path=log_path)
        self.assertIn("exited with code 42", msg)
        self.assertIn("Subprocess error:", msg)


if __name__ == "__main__":
    unittest.main()
