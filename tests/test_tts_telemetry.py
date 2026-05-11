"""Verifies P2b — TTS dispatcher (`pipeline.audio.synthesize`) and the
song lane (`pipeline.tts.song.synth_via_sunoapi`) emit ``tts_synth`` /
``song_synth`` spans, and that the cloud-run fallback path emits a
``tts_fallback`` event with the right success / fail bit.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import audio
from pipeline import observability as obs
from pipeline.tts import cloudrun as tts_cloudrun


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())

    def _events(self):
        from pipeline import telemetry as tlm
        return tlm.read_events()


class TestSynthesizeWraps(_Base):
    """Every TTS provider funnels through ``audio.synthesize`` — wrapping
    that one function gives every provider a ``tts_synth`` span.
    """

    def test_kokoro_route_emits_span(self) -> None:
        called = {}

        def fake_kokoro(text, *, voice, out_path, speed, modulation=None, **kw):
            called["yes"] = True
            return Path(out_path)

        with patch.object(audio, "_synth_kokoro", side_effect=fake_kokoro):
            audio.synthesize(
                "hello world",
                voice="am_eric",
                out_path=Path("/tmp/x.wav"),
                provider="kokoro",
            )
        self.assertTrue(called.get("yes"))

        spans = self._spans()
        names = [s.name for s in spans]
        self.assertIn("tts_synth", names)
        s = next(s for s in spans if s.name == "tts_synth")
        self.assertEqual(s.attributes["ytfactory.meta.provider"], "kokoro")
        self.assertEqual(s.attributes["ytfactory.meta.chars"], 11)
        self.assertEqual(s.attributes["ytfactory.meta.voice"], "am_eric")

    def test_chatterbox_route_emits_span(self) -> None:
        with patch.object(audio, "_synth_chatterbox",
                          side_effect=lambda text, **kw: Path(kw["out_path"])):
            audio.synthesize(
                "x" * 50,
                voice="/refs/sarah.wav",
                out_path=Path("/tmp/y.wav"),
                provider="chatterbox",
            )
        s = next(s for s in self._spans() if s.name == "tts_synth")
        self.assertEqual(s.attributes["ytfactory.meta.provider"], "chatterbox")
        self.assertEqual(s.attributes["ytfactory.meta.chars"], 50)

    def test_unknown_provider_records_failure(self) -> None:
        with self.assertRaises(ValueError):
            audio.synthesize(
                "x", voice="v", out_path=Path("/tmp/q.wav"),
                provider="not_a_real_provider",
            )
        s = next(s for s in self._spans() if s.name == "tts_synth")
        self.assertFalse(s.status.is_ok)


class TestSongSynthSpan(_Base):
    """``synth_via_sunoapi`` emits a ``song_synth`` span. We don't hit
    the real Suno API — patch the impl out.
    """

    def test_song_synth_emits_span(self) -> None:
        from pipeline.tts import song

        with patch.object(song, "_synth_via_sunoapi_impl",
                          side_effect=lambda *a, **kw: Path(a[2])):
            song.synth_via_sunoapi(
                "[Verse 1]\nLa la la",
                "cheerful nursery rhyme, female lead",
                Path("/tmp/song.wav"),
                title="Hathi Raja",
                vocal_gender="f",
                model="V4_5",
            )
        s = next(s for s in self._spans() if s.name == "song_synth")
        self.assertEqual(s.attributes["ytfactory.meta.provider"], "sunoapi")
        self.assertEqual(s.attributes["ytfactory.meta.title"], "Hathi Raja")
        self.assertEqual(s.attributes["ytfactory.meta.model"], "V4_5")


class TestCloudFallbackEvent(_Base):
    """Every cloud-run TTS lane uses ``_local_fallback_or_raise`` for
    its rescue path. That helper now emits a ``tts_fallback`` event so
    the dashboard can split clean cloud success / cloud→local rescues
    / outright failures.
    """

    def test_successful_fallback_emits_success_event(self) -> None:
        err = tts_cloudrun.CloudRunUnavailable("cloud down")
        out = tts_cloudrun._local_fallback_or_raise(
            err, "test_fallback", lambda: Path("/tmp/local.wav"),
        )
        self.assertEqual(out, Path("/tmp/local.wav"))

        events = [e for e in self._events() if e["event"] == "tts_fallback"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["metadata"]["label"], "test_fallback")

    def test_missing_local_dep_emits_failure_event(self) -> None:
        err = tts_cloudrun.CloudRunUnavailable("cloud down")
        with self.assertRaises(tts_cloudrun.CloudRunUnavailable):
            tts_cloudrun._local_fallback_or_raise(
                err, "test_fallback",
                lambda: (_ for _ in ()).throw(ImportError("no torch")),
            )
        events = [e for e in self._events() if e["event"] == "tts_fallback"]
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["success"])
        self.assertIn("no torch", events[0]["metadata"]["venv_missing"])

    def test_disabled_fallback_emits_failure_event(self) -> None:
        import os
        os.environ["CLOUDRUN_TTS_DISABLE_FALLBACK"] = "1"
        try:
            err = tts_cloudrun.CloudRunUnavailable("cloud down")
            with self.assertRaises(tts_cloudrun.CloudRunUnavailable):
                tts_cloudrun._local_fallback_or_raise(
                    err, "canary",
                    lambda: Path("/tmp/never.wav"),
                )
            events = [e for e in self._events() if e["event"] == "tts_fallback"]
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0]["success"])
            self.assertTrue(events[0]["metadata"]["fallback_disabled"])
        finally:
            os.environ.pop("CLOUDRUN_TTS_DISABLE_FALLBACK", None)


if __name__ == "__main__":
    unittest.main()
