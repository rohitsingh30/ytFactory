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

    def test_skips_telemetry_traceback_when_real_traceback_present(self) -> None:
        # The 2026-05-13 b0986504 post-mortem: the long_form
        # subprocess crashed with a real Python exception at line N,
        # then at process exit the OTel Cloud Monitoring exporter
        # ALSO logged a 400 "Points must be written in order" via
        # `logger.error(exc_info=ex)`. The exporter swallows its
        # own exception (returns FAILURE internally), so the
        # telemetry traceback is NOT what killed the subprocess —
        # it's logged noise that happens to be the LAST traceback
        # in the log. Without this filter, operators see "Cloud
        # Monitoring 400" and chase a phantom telemetry bug while
        # the actual render error sits buried above.
        log = (
            "[INFO] starting render\n"
            "Traceback (most recent call last):\n"
            "  File \"/workspace/pipeline/render/long_form.py\", line 1234, in compose\n"
            "    final = _ffmpeg_mux(parts)\n"
            "RuntimeError: this is the REAL crash\n"
            "[INFO] subprocess shutting down\n"
            "Traceback (most recent call last):\n"
            "  File \"/usr/local/lib/python3.12/site-packages/opentelemetry/exporter/cloud_monitoring/__init__.py\", line 429, in export\n"
            "    self._batch_write(all_series)\n"
            "  File \"/usr/local/lib/python3.12/site-packages/google/api_core/grpc_helpers.py\", line 57, in error_remapped_callable\n"
            "    raise exceptions.from_grpc_error(exc) from exc\n"
            "google.api_core.exceptions.InvalidArgument: 400 Points must be written in order.\n"
        )
        out = _video._extract_last_traceback(log)
        self.assertIn("RuntimeError: this is the REAL crash", out)
        self.assertNotIn("Points must be written in order", out)
        self.assertNotIn("opentelemetry/exporter", out)

    def test_falls_back_to_telemetry_traceback_when_only_one_present(self) -> None:
        # If telemetry is the ONLY traceback in the log, surface
        # it anyway — the operator at least sees something, and
        # an all-telemetry log is itself a diagnostic clue (the
        # subprocess died from a non-Python path: signal kill,
        # OOM, or hard exit).
        log = (
            "[INFO] running\n"
            "Traceback (most recent call last):\n"
            "  File \"/usr/local/lib/python3.12/site-packages/opentelemetry/exporter/cloud_monitoring/__init__.py\", line 429, in export\n"
            "    self._batch_write(all_series)\n"
            "google.api_core.exceptions.InvalidArgument: 400 something\n"
        )
        out = _video._extract_last_traceback(log)
        self.assertIn("Traceback", out)
        self.assertIn("opentelemetry/exporter/cloud_monitoring", out)

    def test_recognizes_otel_sdk_export_path_as_telemetry(self) -> None:
        # The SDK export side (BatchSpanProcessor / metric reader
        # flush) lives under opentelemetry/sdk/<signal>/export/ —
        # also non-fatal logged noise.
        log = (
            "Traceback (most recent call last):\n"
            "  File \"/workspace/pipeline/render/long_form.py\", line 50, in main\n"
            "RuntimeError: REAL ERROR\n"
            "Traceback (most recent call last):\n"
            "  File \"/usr/local/lib/python3.12/site-packages/opentelemetry/sdk/metrics/export/__init__.py\", line 200, in _drain\n"
            "google.api_core.exceptions.InvalidArgument: telemetry noise\n"
        )
        out = _video._extract_last_traceback(log)
        self.assertIn("RuntimeError: REAL ERROR", out)
        self.assertNotIn("telemetry noise", out)

    def test_telemetry_classifier_uses_first_file_frame_only(self) -> None:
        # Catch the rubber-duck blind spot: a real render error
        # whose CAUSING frame happens to be deep in
        # `google.api_core` should NOT be misclassified as
        # telemetry. Only the FIRST `File ...` frame (entry point)
        # determines telemetry-vs-real.
        log = (
            "Traceback (most recent call last):\n"
            "  File \"/workspace/pipeline/upload/upload.py\", line 99, in upload\n"
            "    creds.refresh()\n"
            "  File \"/usr/local/lib/python3.12/site-packages/google/api_core/grpc_helpers.py\", line 57, in error_remapped_callable\n"
            "    raise exceptions.from_grpc_error(exc) from exc\n"
            "google.api_core.exceptions.PermissionDenied: 403 Forbidden\n"
        )
        out = _video._extract_last_traceback(log)
        self.assertIn("PermissionDenied", out)
        self.assertIn("pipeline/upload/upload.py", out)


class IsTelemetryTracebackTest(unittest.TestCase):
    """Direct unit tests on the classifier helper."""

    def test_exporter_path_is_telemetry(self) -> None:
        block = [
            "Traceback (most recent call last):",
            "  File \"/x/opentelemetry/exporter/cloud_monitoring/__init__.py\", line 1, in y",
        ]
        self.assertTrue(_video._is_telemetry_traceback(block))

    def test_sdk_export_path_is_telemetry(self) -> None:
        block = [
            "Traceback (most recent call last):",
            "  File \"/x/opentelemetry/sdk/metrics/export/__init__.py\", line 1, in y",
        ]
        self.assertTrue(_video._is_telemetry_traceback(block))

    def test_render_path_is_not_telemetry(self) -> None:
        block = [
            "Traceback (most recent call last):",
            "  File \"/workspace/pipeline/render/long_form.py\", line 1, in y",
        ]
        self.assertFalse(_video._is_telemetry_traceback(block))

    def test_otel_api_path_is_not_telemetry(self) -> None:
        # `opentelemetry/api/` is the user-facing API surface — if a
        # real render error originates from `obs.timed(...)` failing,
        # we want to see it (it's an actual bug, not exporter noise).
        block = [
            "Traceback (most recent call last):",
            "  File \"/x/opentelemetry/api/metrics/__init__.py\", line 1, in y",
        ]
        self.assertFalse(_video._is_telemetry_traceback(block))

    def test_empty_block_is_not_telemetry(self) -> None:
        self.assertFalse(_video._is_telemetry_traceback([]))

    def test_block_without_file_frame_is_not_telemetry(self) -> None:
        block = ["Traceback (most recent call last):", "RuntimeError: x"]
        self.assertFalse(_video._is_telemetry_traceback(block))


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


class MaybeEmitLongFormProgressDoneMarkersTest(unittest.TestCase):
    """Pin :func:`_maybe_emit_long_form_progress` recognition of the
    new explicit done markers (added 2026-05-13 for stage-overlap).

    The renderer's parallel path emits ``[1/5] tts done X.Ys`` AND
    ``[2/5] video prep done X.Ys`` AFTER the corresponding stage
    finishes. The classifier must forward these as
    ``("<stage>_done", msg)`` so the cloud-worker walker can mark the
    pill done WITHOUT cascading any other pill.

    Backwards-compat: legacy lines like ``[1/5] narration N chunks →
    narration.wav`` continue to be classified as running events on
    the same stage (no false-positive on the standalone ``done``
    token because the legacy line doesn't contain it).
    """

    def test_tts_done_marker_emits_tts_done_event(self) -> None:
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "[1/5] tts done 12.4s — 25 chunks → narration.wav 600.0s",
            cb,
        )
        self.assertEqual(len(calls), 1)
        stage, msg = calls[0]
        self.assertEqual(stage, "tts_done")
        self.assertIn("12.4s", msg)

    def test_video_prep_done_marker_emits_images_done_event(self) -> None:
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "[2/5] video prep done 5.1s (panel_stills)",
            cb,
        )
        self.assertEqual(len(calls), 1)
        stage, msg = calls[0]
        self.assertEqual(stage, "images_done")
        self.assertIn("5.1s", msg)

    def test_video_done_marker_emits_images_done_event(self) -> None:
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "[2/5] video → video.mp4 600.0s",
            cb,
        )
        # No "done" token on this line — it's a running/info marker,
        # not a done marker. The classifier must NOT promote it.
        self.assertEqual(len(calls), 1)
        stage, msg = calls[0]
        self.assertEqual(stage, "images")  # NOT images_done

    def test_legacy_running_lines_still_emit_running_events(self) -> None:
        """Backwards-compat: legacy lines from older renderers that
        don't contain the standalone ``done`` token MUST be classified
        as running events, not done events."""
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "[1/5] chunked TTS via cloudrun_chatterbox", cb,
        )
        _video._maybe_emit_long_form_progress(
            "[1/5] narration 25 chunks → narration.wav 600.0s",
            cb,
        )
        _video._maybe_emit_long_form_progress(
            "[2/5] image_panels: 12 panels → 1920x1080 30fps", cb,
        )
        self.assertEqual([s for s, _ in calls], ["tts", "tts", "images"])

    def test_compose_done_emits_compose_done_event(self) -> None:
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "[5/5] mux done 7.2s", cb,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "compose_done")

    def test_no_match_when_no_bracket_marker(self) -> None:
        """Lines without the ``[N/5]`` shape must NOT trigger any
        callback even if they contain ``done``."""
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "trim done in 1.2s", cb,
        )
        _video._maybe_emit_long_form_progress(
            "[done] beat 3 of 7", cb,
        )
        self.assertEqual(calls, [])

    def test_done_token_must_be_a_word_boundary(self) -> None:
        """``done`` must match as a standalone word — not as a
        substring of ``download`` / ``abandoned`` / etc."""
        calls: list[tuple[str, str]] = []
        def cb(stage: str, msg: str) -> None:
            calls.append((stage, msg))
        _video._maybe_emit_long_form_progress(
            "[2/5] image download in progress…", cb,
        )
        # No "done" word boundary → running event, not done.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "images")


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


class RenderViaEnginesDispatchTest(unittest.TestCase):
    """Pin ``render_via_engines`` dispatch + ``progress_cb`` forwarding.

    The cloud worker calls this in-process and depends on:
    - ``pick_engine(spec)`` returning the right engine fn for the kind
    - the engine fn being called with ``progress_cb=...`` so live
      dashboard timeline updates flow through (added 2026-05-14 to
      restore live logs after the engine bigbang regressed them).
    """

    def test_dispatches_short_kind_to_short_engine_with_progress_cb(self):
        from unittest.mock import MagicMock, patch
        # Build a minimal real RenderSpec (not a mock) so engine.pick_engine
        # can read .kind via its enum-equality check.
        from pipeline.render.spec import RenderKind, RenderSpec
        # We don't have a public spec factory that takes raw kwargs, so
        # just construct one with the documented dataclass fields. The
        # engine is mocked so the spec is only inspected for `.kind`.
        spec = MagicMock(spec=RenderSpec)
        spec.kind = RenderKind.SHORT
        spec.channel = "test_channel"
        spec.aspect_ratio = "9:16"
        spec.output_resolution = (1080, 1920)

        captured: dict = {}

        def fake_engine(s, script, work_dir, out_path, *, progress_cb=None):
            captured["called"] = True
            captured["progress_cb"] = progress_cb
            return out_path

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cb = lambda stage, msg: None
            with patch("pipeline.render.engine.pick_engine", return_value=fake_engine):
                result = _video.render_via_engines(
                    spec,
                    script={"narration": "x"},
                    work_dir=tmp_path / "work",
                    out_path=tmp_path / "out.mp4",
                    progress_cb=cb,
                )
        self.assertEqual(result, tmp_path / "out.mp4")
        self.assertTrue(captured["called"])
        self.assertIs(
            captured["progress_cb"], cb,
            "progress_cb MUST be forwarded into the engine — without "
            "it the dashboard timeline freezes during in-process renders",
        )

    def test_progress_cb_optional(self):
        from unittest.mock import MagicMock, patch
        from pipeline.render.spec import RenderKind, RenderSpec
        spec = MagicMock(spec=RenderSpec)
        spec.kind = RenderKind.SHORT
        spec.channel = "test_channel"
        spec.aspect_ratio = "9:16"
        spec.output_resolution = (1080, 1920)

        seen = {}

        def fake_engine(s, script, work_dir, out_path, *, progress_cb=None):
            seen["progress_cb"] = progress_cb
            return out_path

        with TemporaryDirectory() as tmp:
            with patch("pipeline.render.engine.pick_engine", return_value=fake_engine):
                _video.render_via_engines(
                    spec,
                    script={},
                    work_dir=Path(tmp) / "w",
                    out_path=Path(tmp) / "o.mp4",
                )
        self.assertIsNone(seen.get("progress_cb"))


class RenderLongFormInProcessDispatchTest(unittest.TestCase):
    """Pin the 2026-05-15 cloud-fix: ``render_long_form`` calls
    ``render_via_engines`` IN-PROCESS instead of subprocess-spawning
    ``python -m pipeline.render.long_form`` (a module that no longer
    exists post the 2026-05-14 renderer-consolidation bigbang).

    The pre-fix version had a `cmd = [sys.executable, "-m",
    "pipeline.render.long_form", ...]` block. Every cloud long-form
    render hit::

        /usr/local/bin/python: No module named pipeline.render.long_form
        RuntimeError: pipeline.render.long_form exited with code 1.

    This test hard-fails if the subprocess pattern is reintroduced AND
    asserts the engine receives the legacy long-form dict shape via
    `script=` plus the worker's progress_cb forwarded.
    """

    def test_render_long_form_calls_render_via_engines_in_process(self):
        from unittest.mock import MagicMock, patch
        from pipeline.render.spec import RenderKind, RenderSpec

        spec = MagicMock(spec=RenderSpec)
        spec.kind = RenderKind.LONG_FORM
        spec.channel = "sportsrecapped"
        spec.duration_target_s = 1800
        spec.aspect_ratio = "16:9"
        spec.output_resolution = (1920, 1080)

        # Stub envelope shape.
        env = MagicMock()
        env.slug = "test-slug-abc123"
        env.kind = "long_form"
        env.title_options = ["A", "B"]
        env.long_form = MagicMock()
        env.long_form.sections = [MagicMock(), MagicMock()]
        env.long_form.panels = [MagicMock()]
        env.long_form.hook = "hook text"
        env.to_legacy_long_form_dict = MagicMock(return_value={
            "slug": "test-slug-abc123",
            "narration": "n",
            "sections": [{"id": "s1", "title": "T1", "text": "x"}],
            "panels": [{"scene": "scene-1", "hold_s": 4.0}],
        })
        env.to_dict = MagicMock(return_value={"slug": "test-slug-abc123"})

        captured = {}

        def fake_render_via_engines(s, *, script, work_dir, out_path, progress_cb=None):
            captured["spec"] = s
            captured["script"] = script
            captured["work_dir"] = work_dir
            captured["out_path"] = out_path
            captured["progress_cb"] = progress_cb
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake mp4 bytes")
            return out_path

        cb = lambda stage, msg: None

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = MagicMock()
            paths.narration_for = lambda slug: tmp_path / f"{slug}.json"
            paths.long_form_for = lambda slug: tmp_path / f"{slug}.mp4"
            paths.root = tmp_path

            with patch("pipeline.render.video.rewrite_long_form", return_value=env), \
                 patch("pipeline.render.video._resolve_paths", return_value=paths), \
                 patch("pipeline.render.video._merged_channel_cfg", return_value={}), \
                 patch("pipeline.render.video._raw_story_from_proposal", return_value="raw"), \
                 patch("pipeline.render.video.render_via_engines",
                       side_effect=fake_render_via_engines) as mock_engine, \
                 patch("subprocess.Popen") as mock_popen, \
                 patch("subprocess.run") as mock_run:

                from pipeline.render.video import render_long_form
                mp4 = render_long_form(
                    spec=spec,
                    proposal={},
                    work_dir=tmp_path / "work",
                    job_id="job-xyz",
                    progress_cb=cb,
                )

        # Engine called in-process — NOT subprocess.
        self.assertTrue(mock_engine.called,
            "render_long_form MUST call render_via_engines in-process. "
            "If this fails, the legacy subprocess pattern was re-introduced "
            "and the cloud worker will hit ModuleNotFoundError again.")
        self.assertFalse(mock_popen.called,
            "render_long_form MUST NOT subprocess any renderer. The legacy "
            "`python -m pipeline.render.long_form` module was deleted in the "
            "2026-05-14 bigbang.")
        # subprocess.run is OK to allow other helpers but not for the renderer
        # — assert specifically that no python -m pipeline.render.long_form
        # was invoked.
        for call in mock_run.call_args_list:
            args = call.args[0] if call.args else call.kwargs.get("args", [])
            if isinstance(args, list):
                joined = " ".join(str(a) for a in args)
                self.assertNotIn(
                    "pipeline.render.long_form", joined,
                    "Removed module pipeline.render.long_form must not be "
                    "subprocess-invoked.",
                )

        # Engine received the legacy dict shape (sections + panels).
        self.assertIn("sections", captured["script"])
        self.assertIn("panels", captured["script"])
        # progress_cb forwarded.
        self.assertIs(captured["progress_cb"], cb,
            "progress_cb MUST be forwarded into the engine so the dashboard "
            "timeline keeps receiving live stage events during the in-process "
            "render.")
        # mp4 written at expected path.
        self.assertEqual(mp4, captured["out_path"])

    def test_render_long_form_raises_runtime_error_when_engine_fails(self):
        from unittest.mock import MagicMock, patch
        from pipeline.render.spec import RenderKind, RenderSpec

        spec = MagicMock(spec=RenderSpec)
        spec.kind = RenderKind.LONG_FORM
        spec.channel = "sportsrecapped"
        spec.duration_target_s = 1800

        env = MagicMock()
        env.slug = "boom-slug"
        env.kind = "long_form"
        env.title_options = []
        env.long_form = MagicMock()
        env.long_form.sections = []
        env.long_form.panels = []
        env.long_form.hook = ""
        env.to_legacy_long_form_dict = MagicMock(return_value={"slug": "boom-slug"})
        env.to_dict = MagicMock(return_value={})

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = MagicMock()
            paths.narration_for = lambda slug: tmp_path / f"{slug}.json"
            paths.long_form_for = lambda slug: tmp_path / f"{slug}.mp4"
            paths.root = tmp_path

            with patch("pipeline.render.video.rewrite_long_form", return_value=env), \
                 patch("pipeline.render.video._resolve_paths", return_value=paths), \
                 patch("pipeline.render.video._merged_channel_cfg", return_value={}), \
                 patch("pipeline.render.video._raw_story_from_proposal", return_value="raw"), \
                 patch("pipeline.render.video.render_via_engines",
                       side_effect=ValueError("plugin missing")):

                from pipeline.render.video import render_long_form
                with self.assertRaisesRegex(RuntimeError, "long-form engine render failed"):
                    render_long_form(
                        spec=spec,
                        proposal={},
                        work_dir=tmp_path / "work",
                        job_id="job-boom",
                    )


if __name__ == "__main__":
    unittest.main()
