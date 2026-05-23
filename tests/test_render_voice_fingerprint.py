"""Audit Q2.22 — voice fingerprint sidecar.

Pre-fix only ``shorts.py`` was busting its TTS cache when the
channel YAML's tts_provider/tts_voice changed. long_form +
footage_only + sports_doc all checked ``narr_path.exists()``
and skipped synthesis if true — switching F5 → Chatterbox in
the YAML didn't cause a re-render.

Tests exercise: pipeline/render/shared/voice_fingerprint.py
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.render.shared.voice_fingerprint import (
    compute_fingerprint,
    fingerprint_hash,
    maybe_wipe_stale_chunks,
    needs_resynth,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)


class TestComputeFingerprint(unittest.TestCase):
    def test_empty_cfg_yields_all_None(self):
        fp = compute_fingerprint({})
        self.assertEqual(fp["tts_provider"], None)
        self.assertEqual(fp["tts_voice"], None)
        self.assertEqual(fp["tts_speed"], 1.0)  # default

    def test_provider_voice_speed_extracted(self):
        cfg = {
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "/path/to/clip.wav",
            "tts_speed": 0.85,
            "tts_language": "en",
            "tts_ref_text": "Hello world",
        }
        fp = compute_fingerprint(cfg)
        self.assertEqual(fp["tts_provider"], "cloudrun_chatterbox")
        self.assertEqual(fp["tts_voice"], "/path/to/clip.wav")
        self.assertEqual(fp["tts_speed"], 0.85)
        self.assertEqual(fp["tts_language"], "en")
        self.assertEqual(fp["tts_ref_text"], "Hello world")

    def test_chunk_knobs_included(self):
        cfg = {
            "tts_chunk_join_silence_s": 0.4,
            "tts_chunk_target_chars": 500,
        }
        fp = compute_fingerprint(cfg)
        self.assertEqual(fp["tts_chunk_join_silence_s"], 0.4)
        self.assertEqual(fp["tts_chunk_target_chars"], 500)


class TestFingerprintHash(unittest.TestCase):
    def test_same_input_same_hash(self):
        cfg = {"tts_provider": "cloudrun_chatterbox"}
        self.assertEqual(
            fingerprint_hash(compute_fingerprint(cfg)),
            fingerprint_hash(compute_fingerprint(cfg)),
        )

    def test_different_input_different_hash(self):
        a = fingerprint_hash(compute_fingerprint({"tts_provider": "cloudrun_chatterbox"}))
        b = fingerprint_hash(compute_fingerprint({"tts_provider": "kokoro"}))
        self.assertNotEqual(a, b)

    def test_hash_is_short(self):
        h = fingerprint_hash(compute_fingerprint({}))
        self.assertEqual(len(h), 12)


class TestSidecarRoundTrip(unittest.TestCase):
    def test_sidecar_path_is_sibling(self):
        narr = Path("/tmp/test/narration.wav")
        self.assertEqual(sidecar_path(narr), Path("/tmp/test/narration.voice_fp.json"))

    def test_write_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"fake")
            fp = compute_fingerprint({"tts_provider": "cloudrun_chatterbox", "tts_voice": "v"})
            write_sidecar(narr, fp)
            self.assertEqual(read_sidecar(narr), fp)

    def test_read_sidecar_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            self.assertIsNone(read_sidecar(narr))

    def test_read_sidecar_corrupt_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            sidecar_path(narr).write_text("{not json")
            self.assertIsNone(read_sidecar(narr))


class TestNeedsResynth(unittest.TestCase):
    def test_missing_wav_returns_true(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            resynth, reason = needs_resynth(narr, {})
            self.assertTrue(resynth)
            self.assertIn("missing", reason)

    def test_present_wav_no_sidecar_returns_true(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"fake")
            resynth, reason = needs_resynth(narr, {"tts_provider": "cloudrun_chatterbox"})
            self.assertTrue(resynth)
            self.assertIn("sidecar missing", reason)

    def test_matching_sidecar_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"fake")
            cfg = {"tts_provider": "cloudrun_chatterbox", "tts_voice": "v"}
            write_sidecar(narr, compute_fingerprint(cfg))
            resynth, reason = needs_resynth(narr, cfg)
            self.assertFalse(resynth)
            self.assertIn("unchanged", reason)

    def test_mismatched_sidecar_returns_true(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"fake")
            write_sidecar(narr, compute_fingerprint({"tts_provider": "cloudrun_chatterbox"}))
            resynth, reason = needs_resynth(narr, {"tts_provider": "kokoro"})
            self.assertTrue(resynth)
            self.assertIn("changed", reason)


class TestMaybeWipeStaleChunks(unittest.TestCase):
    """Audit Q2.22 — extracted from sports_doc.main / long_form.main
    so it can be unit-tested without spinning up an entire renderer.
    """

    def test_no_sidecar_no_wipe(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"fake-wav-bytes")
            chunk = Path(td) / "chunk_0001.wav"
            chunk.write_bytes(b"fake-chunk")
            wiped = maybe_wipe_stale_chunks(narr, {"tts_provider": "cloudrun_chatterbox"})
            self.assertFalse(wiped)
            self.assertTrue(narr.exists())
            self.assertTrue(chunk.exists())

    def test_missing_narr_no_wipe(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            # No narr file — even if a sidecar somehow exists, nothing to wipe.
            write_sidecar(narr, compute_fingerprint({"tts_provider": "cloudrun_chatterbox"}))
            wiped = maybe_wipe_stale_chunks(narr, {"tts_provider": "kokoro"})
            self.assertFalse(wiped)

    def test_matching_sidecar_no_wipe(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"fake-wav-bytes")
            chunk = Path(td) / "chunk_0001.wav"
            chunk.write_bytes(b"fake-chunk")
            cfg = {"tts_provider": "cloudrun_chatterbox", "tts_voice": "sarah"}
            write_sidecar(narr, compute_fingerprint(cfg))
            wiped = maybe_wipe_stale_chunks(narr, cfg)
            self.assertFalse(wiped)
            self.assertTrue(narr.exists())
            self.assertTrue(chunk.exists())

    def test_mismatched_sidecar_wipes_narr_and_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narration.wav"
            narr.write_bytes(b"old-voice-wav")
            chunk_a = Path(td) / "chunk_0001.wav"
            chunk_b = Path(td) / "chunk_0002.wav"
            unrelated = Path(td) / "footage.mp4"
            for p, payload in (
                (chunk_a, b"a"),
                (chunk_b, b"b"),
                (unrelated, b"keep me"),
            ):
                p.write_bytes(payload)
            write_sidecar(narr, compute_fingerprint({"tts_provider": "cloudrun_chatterbox"}))
            wiped = maybe_wipe_stale_chunks(narr, {"tts_provider": "kokoro"})
            self.assertTrue(wiped)
            self.assertFalse(narr.exists(), "narration.wav should be wiped")
            self.assertFalse(chunk_a.exists(), "chunk_0001.wav should be wiped")
            self.assertFalse(chunk_b.exists(), "chunk_0002.wav should be wiped")
            self.assertTrue(unrelated.exists(), "non-chunk siblings preserved")


if __name__ == "__main__":
    unittest.main()
