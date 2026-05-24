"""Regression tests for ASR cloud-server body capture (F34, 2026-05-24).

Background — `cloud/asr-whisper/server.py::_emit_asr_server_telemetry`
pre-fix passed ``input_text=None, output_text=None`` to
``track_io``. The body-capture infra (which redacts secrets and
truncates per ``YTFACTORY_TEL_BODY_MAX_CHARS``) was therefore a no-op
for ASR — Cloud Run operators looking at a suspected ASR
mispronunciation had no quick way to see what whisper actually
decoded; they had to ``gsutil cat`` per-chunk artifacts and read
JSON. Catalogued as F34 in /ai/known-fragility.md.

The fix: pass the joined per-word transcript (capped to 4000 chars
defensively) as ``output_text`` and a short audio descriptor as
``input_text``. The body-capture pipeline then emits the redacted +
hashed preview onto the ``asr.server`` event.

This test verifies the contract at the helper boundary so a
regression (someone reverts to ``output_text=None`` or drops the
transcript build at the call site) trips immediately.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
ASR_SERVER_PATH = REPO_ROOT / "cloud" / "asr-whisper" / "server.py"


def _load_asr_server_module():
    """Load cloud/asr-whisper/server.py without instantiating the
    full FastAPI app — we only need ``_emit_asr_server_telemetry``
    and the surrounding helpers.

    The module imports heavy deps (``faster_whisper``, ``fastapi``)
    at import-time. We stub them so the load is cheap.
    """
    # Stub heavy imports the module pulls in at import-time.
    for name in ("faster_whisper",):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            # ``WhisperModel`` placeholder
            stub.WhisperModel = MagicMock  # type: ignore[attr-defined]
            sys.modules[name] = stub

    spec = importlib.util.spec_from_file_location(
        "asr_whisper_server_for_telemetry_tests",
        ASR_SERVER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        # If FastAPI / other deps are missing, fall back to text-grep
        # contract assertions below — load failures shouldn't make us
        # silently skip the regression coverage.
        raise unittest.SkipTest(
            f"cloud/asr-whisper/server.py module load failed: {exc}"
        ) from exc
    return module


class AsrServerBodyCaptureContract(unittest.TestCase):
    """``_emit_asr_server_telemetry`` MUST pass non-None ``input_text``
    + ``output_text`` to ``track_io`` so the body-capture preview
    actually carries the audio descriptor + whisper transcript.
    """

    def setUp(self) -> None:
        self.module = _load_asr_server_module()

    def test_helper_passes_transcript_to_track_io(self) -> None:
        captured: list[dict] = []

        def _capture(event_name: str, **kwargs):
            captured.append({"event": event_name, **kwargs})

        # Replace the bound _tel_track_io with our capture function.
        self.module._tel_track_io = _capture  # type: ignore[attr-defined]

        # Minimal Request mock: ``.headers.get("traceparent")``.
        request = MagicMock()
        request.headers.get = MagicMock(return_value=None)

        # Minimal AlignRequest-shaped stub.
        req = MagicMock()
        req.mode = "words"
        req.model = "tiny.en"

        self.module._emit_asr_server_telemetry(
            req=req,
            request=request,
            audio_seconds=12.34,
            language="en",
            word_count=42,
            gpu_seconds=0.8,
            success=True,
            transcript_text="hello world this is a test transcript",
        )

        self.assertEqual(len(captured), 1, f"expected one event; got {captured}")
        event = captured[0]
        self.assertEqual(event["event"], "asr.server")
        # The critical bit: input_text + output_text must be non-None.
        self.assertIsNotNone(
            event.get("input_text"),
            "F34 regression: input_text is None; body capture is dead",
        )
        self.assertIsNotNone(
            event.get("output_text"),
            "F34 regression: output_text is None; body capture is dead",
        )
        # Input descriptor describes the audio without dumping it.
        self.assertIn("audio:", event["input_text"])
        self.assertIn("12.34", event["input_text"])
        # Output text is the transcript itself.
        self.assertIn("hello world", event["output_text"])
        self.assertIn("transcript", event["output_text"])

    def test_helper_truncates_long_transcripts(self) -> None:
        captured: list[dict] = []

        def _capture(event_name: str, **kwargs):
            captured.append({"event": event_name, **kwargs})

        self.module._tel_track_io = _capture  # type: ignore[attr-defined]
        request = MagicMock()
        request.headers.get = MagicMock(return_value=None)
        req = MagicMock()
        req.mode = "words"
        req.model = None

        # Build a 10K-char transcript (a 30-min narration easily hits this).
        long_transcript = "the quick brown fox jumps over the lazy dog. " * 250
        self.assertGreater(len(long_transcript), 8000)

        self.module._emit_asr_server_telemetry(
            req=req,
            request=request,
            audio_seconds=1800.0,
            language="en",
            word_count=2500,
            gpu_seconds=42.0,
            success=True,
            transcript_text=long_transcript,
        )

        event = captured[0]
        self.assertLessEqual(
            len(event["output_text"]), 4000,
            "F34: transcript must be pre-truncated to 4000 chars",
        )

    def test_helper_handles_none_transcript(self) -> None:
        """Failure-path emits (success=False) without a transcript;
        helper must not crash."""
        captured: list[dict] = []

        def _capture(event_name: str, **kwargs):
            captured.append({"event": event_name, **kwargs})

        self.module._tel_track_io = _capture  # type: ignore[attr-defined]
        request = MagicMock()
        request.headers.get = MagicMock(return_value=None)
        req = MagicMock()
        req.mode = "anchors"
        req.model = None

        self.module._emit_asr_server_telemetry(
            req=req,
            request=request,
            audio_seconds=0.0,
            language="unknown",
            word_count=0,
            gpu_seconds=0.0,
            success=False,
            error="WhisperModel transcribe failed: CUDA OOM",
            # transcript_text omitted — default None
        )
        self.assertEqual(len(captured), 1)
        event = captured[0]
        # input_text still set (audio descriptor), output_text=None
        # is acceptable on failure because no transcript exists.
        self.assertIsNotNone(event.get("input_text"))
        self.assertIsNone(event.get("output_text"))
        self.assertFalse(event.get("success", True))


class AsrServerCallSiteContract(unittest.TestCase):
    """Source-grep contract: the ``align`` route MUST build a
    transcript from words and pass it to the telemetry helper.

    Skipping the helper-level test alone leaves a gap — someone could
    revert the call site (drop ``transcript_text=...``) and the
    helper test still passes because the helper itself accepts the
    kwarg.
    """

    def test_align_route_passes_transcript_text(self) -> None:
        source = ASR_SERVER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "transcript_text=transcript_text",
            source,
            "F34 regression: align() route must pass transcript_text "
            "into _emit_asr_server_telemetry. Did someone revert the "
            "2026-05-24 fix?",
        )
        # And the transcript must be built from words[].
        self.assertIn(
            'transcript_text = " ".join(',
            source,
            "F34 regression: align() must build transcript_text from "
            "the per-word list. See cloud/asr-whisper/server.py.",
        )


if __name__ == "__main__":
    unittest.main()
