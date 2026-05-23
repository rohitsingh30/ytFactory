"""Tests for the TTS provider stack in pipeline/audio.py.

Three layers:

1. **Dispatcher** — fast, no model loads. Verifies `synthesize()` routes
   each provider to the right `_synth_*` and surfaces clean errors.

2. **Configuration** — parses every production channel YAML and asserts
   its `tts_provider` / `tts_voice` are coherent.

3. **Live synth smoke** — actually invokes the model. Slow (10s-3min per
   provider, downloads model weights on first run). Auto-skipped when
   the matching lib isn't installed; opt-in via env var so the regular
   test suite stays fast::

       YTFACTORY_TTS_LIVE=1 .venv/bin/python -m unittest \\
           tests.test_audio_tts_providers
"""

from __future__ import annotations

import importlib.util as _u
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.audio import audio


# ---------- helpers --------------------------------------------------------


def _lib_installed(name: str) -> bool:
    return _u.find_spec(name) is not None


_TTS_LIVE = os.environ.get("YTFACTORY_TTS_LIVE") == "1"

_LIVE_TEXT_EN = "This is a brief smoke test of the text to speech path."


def _read_wav_meta(path: Path) -> tuple[float, int]:
    """Return (duration_s, sample_rate) for the WAV at `path`."""
    import soundfile as _sf

    data, sr = _sf.read(path)
    return len(data) / sr, sr


# ---------- 1. Dispatcher --------------------------------------------------


class SynthesizeDispatcherTest(unittest.TestCase):
    """Verify `synthesize()` routes / validates without loading any model."""

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError) as ctx:
            audio.synthesize(
                "x", voice="v", out_path=Path("/tmp/never.wav"),
                provider="not_a_provider",
            )
        msg = str(ctx.exception)
        self.assertIn("unknown TTS provider", msg)
        # Error message must enumerate the legal choices so the operator
        # can fix the YAML without grepping.
        for prov in ("kokoro", "chatterbox", "styletts2"):
            self.assertIn(prov, msg, f"choice list missing {prov!r}")

    def test_kokoro_routes_to_synth_kokoro(self):
        with patch.object(audio, "_synth_kokoro") as mock:
            mock.return_value = Path("/tmp/x.wav")
            audio.synthesize(
                "hello", voice="af_bella",
                out_path=Path("/tmp/x.wav"),
                provider="kokoro",
            )
            mock.assert_called_once()

    def test_chatterbox_routes_to_synth_chatterbox(self):
        with patch.object(audio, "_synth_chatterbox") as mock:
            mock.return_value = Path("/tmp/x.wav")
            audio.synthesize(
                "hello",
                voice="pipeline/voice_refs/sarah.wav",
                out_path=Path("/tmp/x.wav"),
                provider="chatterbox",
            )
            mock.assert_called_once()
            self.assertTrue(
                mock.call_args.kwargs["ref_audio_path"].endswith(
                    "pipeline/voice_refs/sarah.wav"
                ),
                f"got {mock.call_args.kwargs['ref_audio_path']!r}",
            )

    def test_styletts2_routes_to_synth_styletts2(self):
        with patch.object(audio, "_synth_styletts2") as mock:
            mock.return_value = Path("/tmp/x.wav")
            audio.synthesize(
                "hello",
                voice="pipeline/voice_refs/sarah.wav",
                out_path=Path("/tmp/x.wav"),
                provider="styletts2",
            )
            mock.assert_called_once()


# ---------- 2. Channel YAML configuration ---------------------------------


_PRODUCTION_CHANNELS = [
    "historyrecapped",
    "mystoriesanimated",
    "sportsrecapped",
    "hindutavaanimated",
]

_VALID_PROVIDERS = {
    "kokoro", "chatterbox", "styletts2",
    "cloudrun_chatterbox", "cloudrun_indicf5",
}

# Providers that interpret tts_voice as a filesystem path to a ref WAV.
_REF_WAV_PROVIDERS = {"chatterbox", "styletts2"}


class ChannelTtsConfigTest(unittest.TestCase):
    """Every production channel YAML must declare a coherent TTS config."""

    def _load(self, channel: str) -> dict:
        # Per 2026-05-10 nuclear cleanup, channel render configs live
        # at pipeline/channels/<slug>.yaml (no channel-named dirs at
        # repo root). Source the path from pipeline.channels — never
        # hardcode it here.
        from pipeline.channels import _channel_yaml_path  # noqa: PLC0415
        path = PROJECT_ROOT / _channel_yaml_path(channel)
        self.assertTrue(path.exists(), f"missing config yaml for {channel} at {path}")
        return yaml.safe_load(path.read_text())

    def test_every_channel_has_known_provider(self):
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                cfg = self._load(ch)
                provider = cfg.get("tts_provider")
                self.assertIn(
                    provider, _VALID_PROVIDERS,
                    f"{ch}: tts_provider={provider!r} not in {_VALID_PROVIDERS}",
                )

    def test_ref_wav_channels_point_at_existing_wav(self):
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                cfg = self._load(ch)
                if cfg.get("tts_provider") not in _REF_WAV_PROVIDERS:
                    continue
                ref = cfg.get("tts_voice")
                self.assertIsInstance(ref, str)
                ref_path = PROJECT_ROOT / ref
                self.assertTrue(
                    ref_path.exists(),
                    f"{ch}: tts_voice ref WAV {ref_path} does not exist",
                )
                # Mono 24kHz 5-15s — Chatterbox docs want a short clean clip.
                duration_s, sr = _read_wav_meta(ref_path)
                self.assertGreaterEqual(
                    duration_s, 4.0,
                    f"{ch}: ref clip {duration_s:.2f}s < 5s minimum",
                )
                self.assertLessEqual(
                    duration_s, 16.0,
                    f"{ch}: ref clip {duration_s:.2f}s > 15s maximum",
                )


# ---------- 3. Reference clip integrity -----------------------------------


class VoiceRefClipsTest(unittest.TestCase):
    """The `pipeline/voice_refs/` clips are load-bearing for every clone-
    based provider. A bad ref WAV (wrong sample rate, stereo, > 15s)
    silently degrades cloning quality. Catch problems at test time."""

    REF_DIR = PROJECT_ROOT / "pipeline" / "voice_refs"
    EXPECTED_REFS = ["sarah"]

    def test_all_expected_refs_exist(self):
        for name in self.EXPECTED_REFS:
            with self.subTest(ref=name):
                wav = self.REF_DIR / f"{name}.wav"
                txt = self.REF_DIR / f"{name}.txt"
                self.assertTrue(wav.exists(), f"missing {wav}")
                self.assertTrue(txt.exists(), f"missing {txt}")

    def test_ref_wavs_are_mono_24khz_within_duration(self):
        for name in self.EXPECTED_REFS:
            with self.subTest(ref=name):
                wav = self.REF_DIR / f"{name}.wav"
                duration_s, sr = _read_wav_meta(wav)
                self.assertEqual(sr, 24000,
                                 f"{name}.wav must be 24kHz, got {sr}")
                self.assertGreaterEqual(duration_s, 5.0)
                self.assertLessEqual(duration_s, 15.0)

    def test_ref_transcripts_nonempty_and_short(self):
        for name in self.EXPECTED_REFS:
            with self.subTest(ref=name):
                txt = (self.REF_DIR / f"{name}.txt").read_text().strip()
                self.assertGreater(len(txt), 20)
                # Sanity: the transcript must be short enough that it
                # plausibly matches a 5-15s ref clip (~60 words max).
                self.assertLess(len(txt.split()), 60)


# ---------- 4. Live synth smoke (opt-in, slow, requires libs) -------------


class _LiveSynthBase(unittest.TestCase):
    """Shared assertion: a synth call writes a non-empty WAV with sane sr."""

    def _assert_valid_wav(self, path: Path, *, min_duration_s: float = 0.5,
                          allowed_sample_rates=(16000, 22050, 24000, 32000, 44100, 48000)):
        self.assertTrue(path.exists(), f"synth did not write {path}")
        self.assertGreater(path.stat().st_size, 1024,
                           f"{path} is suspiciously small")
        duration_s, sr = _read_wav_meta(path)
        self.assertGreaterEqual(duration_s, min_duration_s,
                                f"{path} is only {duration_s:.2f}s")
        self.assertIn(sr, allowed_sample_rates,
                      f"{path} sr={sr} not in {allowed_sample_rates}")


@unittest.skipUnless(_TTS_LIVE, "set YTFACTORY_TTS_LIVE=1 to run live synth tests")
class KokoroLiveSynthTest(_LiveSynthBase):
    @unittest.skipUnless(_lib_installed("kokoro_onnx"),
                         "kokoro-onnx not installed")
    def test_kokoro_synth_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "kokoro.wav"
            audio.synthesize(
                _LIVE_TEXT_EN, voice="af_bella",
                out_path=out, speed=1.2, provider="kokoro",
            )
            self._assert_valid_wav(out)


@unittest.skipUnless(_TTS_LIVE, "set YTFACTORY_TTS_LIVE=1 to run live synth tests")
class ChatterboxLiveSynthTest(_LiveSynthBase):
    @unittest.skipUnless(_lib_installed("chatterbox"),
                         "chatterbox-tts not installed")
    def test_chatterbox_synth_with_sarah_ref(self):
        ref_wav = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.wav"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "chatterbox.wav"
            audio.synthesize(
                _LIVE_TEXT_EN, voice=str(ref_wav),
                out_path=out, speed=0.90, provider="chatterbox",
            )
            self._assert_valid_wav(out)


# ---------- 5. Voice resolver wiring (added 2026-05-15) -------------------


class SynthesizeVoiceResolutionTest(unittest.TestCase):
    """The dispatcher MUST resolve bare voice names (e.g. ``"sarah"``)
    to on-disk WAV paths before handing them to any provider. Pre-2026-05-15
    the wizard form sent ``voice="sarah"`` and chatterbox crashed with
    ``Path("sarah").read_bytes()`` → FileNotFoundError. Surfaced by
    job 215e411b canary."""

    def _capture_synth_kwargs(self, voice: str, ref_audio_text: str | None = None):
        """Drive synthesize() with the impl stubbed; return the kwargs
        the provider actually receives."""
        captured = {}

        def _fake_impl(text, *, voice, out_path, speed, provider,
                        ref_audio_text=None, modulation=None,
                        pronunciation_dict=None, language="en",
                        narration_prosody=None):
            captured["voice"] = voice
            captured["ref_audio_text"] = ref_audio_text
            # Write a token wav so synthesize()'s post-call probe
            # doesn't raise.
            out_path.write_bytes(b"RIFF" + b"\x00" * 40)
            return out_path

        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "test.wav"
            with patch("pipeline.audio._synthesize_impl", side_effect=_fake_impl):
                audio.synthesize(
                    "hello world", voice=voice, out_path=out_path,
                    provider="kokoro", ref_audio_text=ref_audio_text,
                )
        return captured

    def test_bare_name_resolves_to_voice_refs_wav_path(self):
        # "sarah" → pipeline/voice_refs/sarah.wav (real file in repo).
        captured = self._capture_synth_kwargs("sarah")
        self.assertTrue(captured["voice"].endswith("voice_refs/sarah.wav"))

    def test_bare_name_picks_up_transcript_sidecar(self):
        # voice_refs/sarah.txt exists in the repo → resolved transcript
        # should land in ref_audio_text when caller didn't pass one.
        captured = self._capture_synth_kwargs("sarah", ref_audio_text=None)
        self.assertTrue(captured["ref_audio_text"])
        self.assertGreater(len(captured["ref_audio_text"]), 10)

    def test_caller_provided_ref_audio_text_overrides_catalog_transcript(self):
        # /make-* skills sometimes pass per-render transcripts.
        # Resolution must not clobber that.
        captured = self._capture_synth_kwargs(
            "sarah", ref_audio_text="caller-provided override",
        )
        self.assertEqual(captured["ref_audio_text"], "caller-provided override")

    def test_path_style_voice_passes_through_unchanged(self):
        # Already a path → resolver returns it as-is (resolved to abs).
        captured = self._capture_synth_kwargs("pipeline/voice_refs/sarah.wav")
        self.assertTrue(captured["voice"].endswith("voice_refs/sarah.wav"))

    def test_unresolvable_voice_logs_warning_and_propagates_original(self):
        # Voice not in catalog AND not on disk → resolver raises but
        # synthesize() catches it (best-effort) and lets the provider
        # see the original value so it can produce a more specific
        # error than our generic "not found".
        import logging
        with patch.object(audio, "_synthesize_impl") as mock_impl:
            mock_impl.return_value = Path("/tmp/never.wav")
            # Stub the post-call wav probe so it doesn't crash on the fake path.
            def _fake_impl(text, *, voice, out_path, **kw):
                captured.append(voice)
                out_path.write_bytes(b"RIFF" + b"\x00" * 40)
                return out_path
            captured = []
            mock_impl.side_effect = _fake_impl
            with tempfile.TemporaryDirectory() as td:
                out_path = Path(td) / "test.wav"
                with self.assertLogs(level=logging.WARNING) as logs:
                    audio.synthesize(
                        "hello", voice="totally-fake-voice-name",
                        out_path=out_path, provider="kokoro",
                    )
        # Original unresolved value reached the provider.
        self.assertEqual(captured, ["totally-fake-voice-name"])
        # And we logged a warning so operators can see the resolution failure.
        self.assertTrue(
            any("voice resolution failed" in msg for msg in logs.output),
            f"expected resolution-failed warning in logs: {logs.output}",
        )

    def test_empty_voice_passes_through_as_none(self):
        # Description-driven providers accept empty voice.
        captured = self._capture_synth_kwargs("")
        # Resolver returns None for empty input → synthesize keeps the
        # original empty string.
        self.assertEqual(captured["voice"], "")


if __name__ == "__main__":
    unittest.main()
