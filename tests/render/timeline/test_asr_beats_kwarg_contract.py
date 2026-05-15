"""Pin the kwarg contract between asr_beats and split_with_forced_boundaries.

Surfaced by job f1e319a3 canary on 2026-05-15:
``asr_beats.AsrBeats.build()`` called
``split_with_forced_boundaries(words=, forced_lines=)`` but the
function signature is
``split_with_forced_boundaries(words, forced_narration_lines)``. The
``BLE001`` catch swallowed the TypeError and silently fell back to
"one segment per word", which broke captions AND visuals on every
cloud short for an undetermined window.

These tests pin the kwarg name + the regression signal (TypeError
re-raised with a clear log message instead of swallowed).
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401


class SplitWithForcedBoundariesKwargContractTest(unittest.TestCase):
    """Pin the function signature so the asr_beats caller stays in sync."""

    def test_function_accepts_forced_narration_lines_kwarg(self):
        from pipeline.beats import split_with_forced_boundaries
        sig = inspect.signature(split_with_forced_boundaries)
        self.assertIn(
            "forced_narration_lines", sig.parameters,
            "split_with_forced_boundaries must keep the forced_narration_lines "
            "kwarg name — asr_beats and other callers depend on it",
        )
        # And specifically NOT the old wrong name that we shipped.
        self.assertNotIn(
            "forced_lines", sig.parameters,
            "split_with_forced_boundaries must NOT have a forced_lines kwarg "
            "— that was the misspelling that broke captions on f1e319a3",
        )


class AsrBeatsCallsCorrectKwargTest(unittest.TestCase):
    """End-to-end: invoke AsrBeats.build with mocked asr + forced lines
    and assert the call to split_with_forced_boundaries uses the right
    kwarg name. Without this regression test, a single rename of
    forced_lines silently breaks captions on every cloud short."""

    def _build_spec(self):
        spec = MagicMock()
        spec.channel = "test"
        spec.extra = {}
        return spec

    def _build_audio(self, tmp_dir):
        from pipeline.render.contracts import AudioResult
        from pathlib import Path
        wav = Path(tmp_dir) / "narration.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)
        return AudioResult(
            narration_path=wav,
            duration_s=10.0,
        )

    def test_build_passes_forced_narration_lines_kwarg(self):
        import tempfile
        from pipeline.render.timeline.asr_beats import AsrBeats

        captured_kwargs = {}

        def _fake_split(*args, **kwargs):
            captured_kwargs.update(kwargs)
            from pipeline.beats import Beat
            return [Beat(text="hello", start=0.0, end=1.0, words=[])]

        # Cloud path must fail so we drop into the local fallback that
        # calls split_with_forced_boundaries. Then mock transcribe to
        # return word timestamps.
        with tempfile.TemporaryDirectory() as td:
            spec = self._build_spec()
            audio = self._build_audio(td)
            script = {"shots": [{"narration_line": "hello world"}]}

            with patch(
                "pipeline.asr_cloudrun.align_via_cloud",
                side_effect=RuntimeError("cloud unavailable"),
            ), patch(
                "pipeline.asr.transcribe",
                return_value={"segments": [{"words": [
                    {"word": "hello", "start": 0.0, "end": 1.0},
                    {"word": "world", "start": 1.0, "end": 2.0},
                ]}]},
            ), patch(
                "pipeline.beats.split_with_forced_boundaries",
                side_effect=_fake_split,
            ):
                AsrBeats().build(spec, script, audio)

        # The kwarg the caller sends MUST be forced_narration_lines —
        # NOT forced_lines (the bug that broke f1e319a3).
        self.assertIn(
            "forced_narration_lines", captured_kwargs,
            f"asr_beats called split_with_forced_boundaries with kwargs "
            f"{list(captured_kwargs)} — must include forced_narration_lines",
        )
        self.assertNotIn(
            "forced_lines", captured_kwargs,
            "asr_beats must not pass forced_lines (regression: f1e319a3)",
        )


class AsrBeatsTypeErrorReRaisedTest(unittest.TestCase):
    """When split_with_forced_boundaries raises TypeError (kwarg drift),
    asr_beats now re-raises with an ERROR log so the regression doesn't
    silently degrade captions to per-word fallback."""

    def _build_spec(self):
        spec = MagicMock()
        spec.channel = "test"
        spec.extra = {}
        return spec

    def _build_audio(self, tmp_dir):
        from pipeline.render.contracts import AudioResult
        from pathlib import Path
        wav = Path(tmp_dir) / "narration.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)
        return AudioResult(narration_path=wav, duration_s=10.0)

    def test_type_error_propagates_with_log(self):
        import tempfile
        import logging
        from pipeline.render.timeline.asr_beats import AsrBeats

        with tempfile.TemporaryDirectory() as td:
            spec = self._build_spec()
            audio = self._build_audio(td)
            script = {"shots": [{"narration_line": "hello world"}]}

            with patch(
                "pipeline.asr_cloudrun.align_via_cloud",
                side_effect=RuntimeError("cloud unavailable"),
            ), patch(
                "pipeline.asr.transcribe",
                return_value={"segments": [{"words": [
                    {"word": "hello", "start": 0.0, "end": 1.0},
                ]}]},
            ), patch(
                "pipeline.beats.split_with_forced_boundaries",
                side_effect=TypeError("got an unexpected keyword argument"),
            ), self.assertLogs(level=logging.ERROR) as logs:
                with self.assertRaises(TypeError):
                    AsrBeats().build(spec, script, audio)

        # Error log must mention asr_beats so the operator knows where
        # the kwarg drift is.
        self.assertTrue(
            any("asr_beats" in msg for msg in logs.output),
            f"expected asr_beats error log; got: {logs.output}",
        )


if __name__ == "__main__":
    unittest.main()
