"""Tests for pipeline/render/audio/audio_from_fixture.py.

This is the test-only AudioSynthesizer that loads a wav from disk.
We pin its full Protocol contract: returns AudioResult with the right
narration_path, measured (not declared) duration, and a real
voice_fingerprint sidecar.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline.render.audio.audio_from_fixture import AudioFromFixture
from pipeline.render.contracts import AudioResult, AudioSynthesizer
from pipeline.render.spec import build_spec


def _make_wav(path: Path, duration_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


class AudioFromFixtureProtocolTest(unittest.TestCase):
    def test_satisfies_audio_synthesizer_protocol(self):
        # @runtime_checkable Protocol — isinstance works for structural typing.
        self.assertIsInstance(AudioFromFixture(), AudioSynthesizer)


class AudioFromFixtureSynthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-audio-fixture-"))
        self.fixture_wav = self.tmp / "src" / "narration.wav"
        _make_wav(self.fixture_wav, duration_s=2.5)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, **extras):
        return build_spec({
            "channel": "x",
            "channel_overrides": {**extras, "audio_fixture_path": str(self.fixture_wav)},
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_synth_copies_fixture_into_work_dir(self):
        spec = self._spec()
        work_dir = self.tmp / "work"
        result = AudioFromFixture().synth(spec, script={}, work_dir=work_dir)
        self.assertIsInstance(result, AudioResult)
        self.assertEqual(result.narration_path, work_dir / "narration.wav")
        self.assertTrue(result.narration_path.exists())

    def test_synth_returns_measured_duration(self):
        spec = self._spec()
        result = AudioFromFixture().synth(spec, script={}, work_dir=self.tmp / "work")
        # Duration should be ≈ 2.5s (within 0.1s tolerance for ffmpeg
        # rounding). NOT a hardcoded value — the plugin probes the
        # actual file.
        self.assertAlmostEqual(result.duration_s, 2.5, delta=0.1)

    def test_synth_attaches_voice_fingerprint(self):
        spec = self._spec()
        result = AudioFromFixture().synth(spec, script={}, work_dir=self.tmp / "work")
        self.assertIn("tts_provider", result.voice_fingerprint)
        self.assertEqual(result.voice_fingerprint["tts_provider"], "fixture")

    def test_synth_no_chunk_timings(self):
        # Single-pass plugin → no chunk_timings (only chunked TTS sets it).
        spec = self._spec()
        result = AudioFromFixture().synth(spec, script={}, work_dir=self.tmp / "work")
        self.assertIsNone(result.chunk_timings)

    def test_synth_creates_work_dir_if_missing(self):
        spec = self._spec()
        nested_work = self.tmp / "deep" / "nested" / "work"
        result = AudioFromFixture().synth(spec, script={}, work_dir=nested_work)
        self.assertTrue(result.narration_path.parent.exists())


class AudioFromFixtureErrorTest(unittest.TestCase):
    def test_missing_fixture_path_raises(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        with self.assertRaises(ValueError) as ctx:
            AudioFromFixture().synth(spec, script={}, work_dir=Path("/tmp/x"))
        # Error message must point at the spec.extra key so the test
        # author knows what to set.
        self.assertIn("audio_fixture_path", str(ctx.exception))

    def test_nonexistent_fixture_raises_clear_error(self):
        spec = build_spec({
            "channel": "x",
            "channel_overrides": {"audio_fixture_path": "/no/such/path.wav"},
        }, channel_yaml_path=None, variant_yaml_path=None)
        with self.assertRaises(FileNotFoundError) as ctx:
            AudioFromFixture().synth(spec, script={}, work_dir=Path("/tmp/x"))
        self.assertIn("/no/such/path.wav", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
