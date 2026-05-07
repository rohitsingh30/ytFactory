"""Tests for the TTS provider stack in pipeline/audio.py.

Three layers:

1. **Dispatcher** — fast, no model loads. Verifies `synthesize()` routes
   each provider to the right `_synth_*` and surfaces clean errors.

2. **Configuration** — parses every production channel YAML and asserts
   its `tts_provider` / `tts_voice` are coherent (path exists for clone
   providers; non-empty description for indic_parler).

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

from pipeline import audio


# ---------- helpers --------------------------------------------------------


def _lib_installed(name: str) -> bool:
    return _u.find_spec(name) is not None


_TTS_LIVE = os.environ.get("YTFACTORY_TTS_LIVE") == "1"

_LIVE_TEXT_EN = "This is a brief smoke test of the text to speech path."
_LIVE_TEXT_HI = "नमस्कार। यह एक छोटा परीक्षण है।"


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
        for prov in ("kokoro", "f5_tts", "chatterbox", "styletts2",
                     "indic_parler"):
            self.assertIn(prov, msg, f"choice list missing {prov!r}")

    def test_f5_tts_requires_ref_audio_text(self):
        with self.assertRaises(ValueError) as ctx:
            audio.synthesize(
                "x",
                voice="pipeline/voice_refs/theo.wav",
                out_path=Path("/tmp/never.wav"),
                provider="f5_tts",
                ref_audio_text=None,
            )
        self.assertIn("ref_audio_text", str(ctx.exception))

    def test_kokoro_routes_to_synth_kokoro(self):
        with patch.object(audio, "_synth_kokoro") as mock:
            mock.return_value = Path("/tmp/x.wav")
            audio.synthesize(
                "hello", voice="af_bella",
                out_path=Path("/tmp/x.wav"),
                provider="kokoro",
            )
            mock.assert_called_once()

    def test_f5_tts_routes_to_synth_f5_tts(self):
        with patch.object(audio, "_synth_f5_tts") as mock:
            mock.return_value = Path("/tmp/x.wav")
            audio.synthesize(
                "hello",
                voice="pipeline/voice_refs/theo.wav",
                out_path=Path("/tmp/x.wav"),
                provider="f5_tts",
                ref_audio_text="some transcript",
            )
            mock.assert_called_once()
            kwargs = mock.call_args.kwargs
            self.assertEqual(kwargs["ref_audio_path"], "pipeline/voice_refs/theo.wav")
            self.assertEqual(kwargs["ref_audio_text"], "some transcript")

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
            self.assertEqual(
                mock.call_args.kwargs["ref_audio_path"],
                "pipeline/voice_refs/sarah.wav",
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

    def test_indic_parler_routes_with_voice_as_description(self):
        # indic_parler interprets `voice` as a natural-language
        # description, NOT a path. Make sure the dispatcher passes it
        # through as `description`.
        desc = "Sneha speaks calmly with a moderate pace."
        with patch.object(audio, "_synth_indic_parler") as mock:
            mock.return_value = Path("/tmp/x.wav")
            audio.synthesize(
                "नमस्कार",
                voice=desc,
                out_path=Path("/tmp/x.wav"),
                provider="indic_parler",
            )
            mock.assert_called_once()
            self.assertEqual(mock.call_args.kwargs["description"], desc)

# ---------- 2. Channel YAML configuration ---------------------------------


_PRODUCTION_CHANNELS = [
    "historyrecapped",
    "mystoriesanimated",
    "sportstoriesanimated",
    "hindutavaanimated",
    "airecap",
]

_VALID_PROVIDERS = {
    "kokoro", "f5_tts", "chatterbox", "styletts2", "indic_parler",
    "cloudrun_chatterbox", "cloudrun_f5", "cloudrun_higgs",
    "cloudrun_cosyvoice", "cloudrun_indicparler", "cloudrun_indicf5",
}

# Providers that interpret tts_voice as a filesystem path to a ref WAV.
_REF_WAV_PROVIDERS = {"f5_tts", "chatterbox", "styletts2"}


class ChannelTtsConfigTest(unittest.TestCase):
    """Every production channel YAML must declare a coherent TTS config."""

    def _load(self, channel: str) -> dict:
        path = PROJECT_ROOT / channel / "config.yaml"
        self.assertTrue(path.exists(), f"missing config.yaml for {channel}")
        return yaml.safe_load(path.read_text())

    def test_every_channel_has_known_provider(self):
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                cfg = self._load(ch)
                # rhymetimejunction uses audio_provider: external_song
                # instead of TTS — but it's not in this list, so all
                # listed channels must declare a real tts_provider.
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
                # Mono 24kHz 5-15s — F5/Chatterbox docs both want a
                # short clean clip.
                duration_s, sr = _read_wav_meta(ref_path)
                self.assertGreaterEqual(
                    duration_s, 4.0,
                    f"{ch}: ref clip {duration_s:.2f}s < 5s minimum",
                )
                self.assertLessEqual(
                    duration_s, 16.0,
                    f"{ch}: ref clip {duration_s:.2f}s > 15s maximum",
                )

    def test_f5_tts_channels_have_ref_text(self):
        # F5-TTS requires `tts_ref_text` (transcript of the ref WAV).
        # Without it, the dispatcher will raise at synth time —
        # catch the misconfiguration here instead.
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                cfg = self._load(ch)
                if cfg.get("tts_provider") != "f5_tts":
                    continue
                ref_text = cfg.get("tts_ref_text")
                self.assertIsInstance(ref_text, str)
                self.assertGreater(len(ref_text.strip()), 10,
                                   f"{ch}: tts_ref_text is too short")

    def test_indic_parler_voice_is_description_not_path(self):
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                cfg = self._load(ch)
                if cfg.get("tts_provider") != "indic_parler":
                    continue
                voice = cfg.get("tts_voice")
                self.assertIsInstance(voice, str)
                # A description is multiple words; a path would have a
                # `/` and end in `.wav`. Sanity-check.
                self.assertNotIn(".wav", voice,
                                 f"{ch}: indic_parler voice looks like a path, should be a description")
                self.assertGreater(len(voice.split()), 5,
                                   f"{ch}: indic_parler description is too short")


# ---------- 3. Reference clip integrity -----------------------------------


class VoiceRefClipsTest(unittest.TestCase):
    """The `pipeline/voice_refs/` clips are load-bearing for every clone-
    based provider. A bad ref WAV (wrong sample rate, stereo, > 15s)
    silently degrades cloning quality. Catch problems at test time."""

    REF_DIR = PROJECT_ROOT / "pipeline" / "voice_refs"
    # theo.wav was lost in the 2026-05-04 recovery wipe; sarah.wav is
    # the only F5-TTS ref currently on disk. Channels that previously
    # used Theo (male) now run sarah.wav. Re-add "theo" to this list
    # once a 5-15s 24kHz mono male WAV is dropped at
    # pipeline/voice_refs/theo.wav.
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
class F5TtsLiveSynthTest(_LiveSynthBase):
    @unittest.skipUnless(_lib_installed("f5_tts_mlx"),
                         "f5-tts-mlx not installed")
    def test_f5_tts_synth_with_theo_ref(self):
        ref_wav = PROJECT_ROOT / "pipeline" / "voice_refs" / "theo.wav"
        ref_txt = (PROJECT_ROOT / "pipeline" / "voice_refs" / "theo.txt").read_text().strip()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "f5.wav"
            audio.synthesize(
                _LIVE_TEXT_EN, voice=str(ref_wav),
                out_path=out, speed=0.95, provider="f5_tts",
                ref_audio_text=ref_txt,
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


def _parler_tts_compatible() -> bool:
    """parler-tts is incompatible with transformers >= 4.49 (it depends
    on a removed `PreTrainedConfig` attribute). diffusers 0.38 requires
    transformers >= 4.49 for image-gen, so this venv runs newer
    transformers — parler-tts is permanently broken here."""
    if not _lib_installed("parler_tts"):
        return False
    try:
        from parler_tts import ParlerTTSForConditionalGeneration  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_TTS_LIVE, "set YTFACTORY_TTS_LIVE=1 to run live synth tests")
class IndicParlerLiveSynthTest(_LiveSynthBase):
    """Indic Parler-TTS synth path. NOTE: hindutavaanimated's production
    default is now Kokoro `hf_alpha` (Hindi female) — parler-tts isn't
    used by any channel YAML in this repo because of the transformers
    conflict. This test is kept as a regression check for the wired
    code path; it auto-skips on incompatible venvs."""

    @unittest.skipUnless(_parler_tts_compatible(),
                         "parler-tts incompatible with transformers >= 4.49 "
                         "(set up a separate venv to test)")
    def test_indic_parler_synth_hindi(self):
        description = (
            "Sneha speaks in a calm, gentle, expressive Hindi storytelling "
            "tone with a moderate speed and warm pitch. The recording is of "
            "very high quality, with the speaker voice sounding clear."
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "parler.wav"
            audio.synthesize(
                _LIVE_TEXT_HI, voice=description,
                out_path=out, speed=0.85, provider="indic_parler",
            )
            self._assert_valid_wav(out)


if __name__ == "__main__":
    unittest.main()
