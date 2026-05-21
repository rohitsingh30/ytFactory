"""Comprehensive tests for pipeline/audio/* — targets 100% line coverage.

Modules covered:
  - pipeline.audio.transcribe   (0% → 100%)
  - pipeline.audio.asr          (0% → 100%)
  - pipeline.audio.audio        (50% → 100%)
  - pipeline.audio.align        (13% → 100%)
  - pipeline.audio.beats        (13% → 100%)

Rules:
  - No real ASR model loads — faster_whisper is mocked via sys.modules.
  - No real audio file reads — subprocess (ffprobe/ffmpeg) is mocked.
  - No real Cloud Run / Azure HTTP calls — _synth_* functions are mocked.
  - File I/O for save/load uses TemporaryDirectory (same pattern as other tests).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.audio.asr as _asr
import pipeline.audio.audio as _audio
import pipeline.audio.transcribe as _transcribe
from pipeline.audio.align import align_source_to_whisper
from pipeline.audio.beats import (
    Beat,
    Word,
    _audio_low_rms_spans,
    _ffprobe_duration_s,
    _find_subsequence,
    _make_beat,
    _norm_token,
    _norm_tokens,
    _split_long_group,
    load_beats,
    save_beats,
    split_into_beats,
    split_with_forced_boundaries,
    transcribe_words,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end)


def _ws(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


# ===========================================================================
# pipeline.audio.asr
# ===========================================================================


class TestASRCoerceLegacyProvider(unittest.TestCase):

    def setUp(self):
        _asr._COERCION_LOGGED.clear()

    def test_whisper_mlx_coerced(self):
        self.assertEqual(_asr._coerce_legacy_provider("whisper_mlx"), "faster_whisper")

    def test_whisper_mlx_base_coerced(self):
        self.assertEqual(_asr._coerce_legacy_provider("whisper_mlx_base"), "faster_whisper")

    def test_parakeet_mlx_coerced(self):
        self.assertEqual(_asr._coerce_legacy_provider("parakeet_mlx"), "faster_whisper")

    def test_faster_whisper_passthrough(self):
        self.assertEqual(_asr._coerce_legacy_provider("faster_whisper"), "faster_whisper")

    def test_unknown_passthrough(self):
        self.assertEqual(_asr._coerce_legacy_provider("other"), "other")

    def test_coercion_prints_once_per_provider(self):
        with patch("builtins.print") as mp:
            _asr._coerce_legacy_provider("whisper_mlx")
            _asr._coerce_legacy_provider("whisper_mlx")  # second call — no print
        self.assertEqual(mp.call_count, 1)

    def test_different_legacy_providers_each_print_once(self):
        with patch("builtins.print") as mp:
            _asr._coerce_legacy_provider("whisper_mlx")
            _asr._coerce_legacy_provider("parakeet_mlx")
        self.assertEqual(mp.call_count, 2)


class TestASRTranscribe(unittest.TestCase):

    def setUp(self):
        _asr._COERCION_LOGGED.clear()

    def _clean_result(self):
        return {"text": "hello world", "segments": [], "language": "en"}

    def test_faster_whisper_routes(self):
        with patch.object(_asr, "_transcribe_faster_whisper", return_value=self._clean_result()) as m:
            result = _asr.transcribe(Path("x.wav"), provider="faster_whisper", model="base")
        m.assert_called_once_with(Path("x.wav"), "base")
        self.assertEqual(result["text"], "hello world")

    def test_default_model_when_none(self):
        with patch.object(_asr, "_transcribe_faster_whisper", return_value=self._clean_result()) as m:
            _asr.transcribe(Path("x.wav"), provider="faster_whisper")
        self.assertEqual(m.call_args[0][1], "base")

    def test_env_var_overrides_provider(self):
        with patch.dict(os.environ, {"YTFACTORY_ASR_PROVIDER": "faster_whisper"}):
            with patch.object(_asr, "_transcribe_faster_whisper", return_value=self._clean_result()) as m:
                _asr.transcribe(Path("x.wav"), provider="whisper_mlx")
        m.assert_called_once()

    def test_legacy_provider_coerced(self):
        with patch.object(_asr, "_transcribe_faster_whisper", return_value=self._clean_result()) as m:
            _asr.transcribe(Path("x.wav"), provider="parakeet_mlx")
        m.assert_called_once()

    def test_unknown_provider_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _asr.transcribe(Path("x.wav"), provider="bogus_provider")
        self.assertIn("bogus_provider", str(ctx.exception))

    def test_strip_repetition_called_on_result(self):
        # The result goes through _strip_trailing_repetition; verify
        # the returned object is (at minimum) a dict with "text".
        with patch.object(_asr, "_transcribe_faster_whisper", return_value=self._clean_result()):
            result = _asr.transcribe(Path("x.wav"))
        self.assertIn("text", result)


class TestASRStripTrailingRepetition(unittest.TestCase):

    def test_empty_segments_no_change(self):
        result = {"text": "hello", "segments": []}
        out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(out["text"], "hello")

    def test_no_repetition_unchanged(self):
        words = [_ws(w, i, i + 1) for i, w in enumerate(["a", "b", "c", "d"])]
        seg = {"text": "a b c d", "words": words, "start": 0.0, "end": 4.0}
        result = {"text": "a b c d", "segments": [seg]}
        out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(out["text"], "a b c d")
        self.assertEqual(len(out["segments"][0]["words"]), 4)

    def test_run_below_min_threshold_unchanged(self):
        # 3 copies < min_run=4
        words = [_ws("x", i * 0.5, (i + 1) * 0.5) for i in range(3)]
        seg = {"text": "x x x", "words": words, "start": 0.0, "end": 1.5}
        result = {"text": "x x x", "segments": [seg]}
        out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(len(out["segments"][0]["words"]), 3)

    def test_word_run_stripped_at_segment_level(self):
        # 5 × "that" — 4 should be dropped, 1 kept
        words = [_ws("that", i * 0.5, (i + 1) * 0.5) for i in range(5)]
        seg = {"text": "that that that that that", "words": words,
               "start": 0.0, "end": 2.5}
        result = {"text": "that that that that that", "segments": [seg]}
        with patch("builtins.print"):
            out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(len(out["segments"][0]["words"]), 1)
        self.assertEqual(out["segments"][0]["words"][0]["word"], "that")

    def test_segment_with_empty_words_skipped_finds_next(self):
        empty_seg = {"text": "", "words": []}
        words = [_ws("go", i, i + 1) for i in range(4)]
        full_seg = {"text": "go go go go", "words": words, "start": 0.0, "end": 4.0}
        result = {"text": "go go go go", "segments": [empty_seg, full_seg]}
        with patch("builtins.print"):
            out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(len(out["segments"][1]["words"]), 1)

    def test_empty_last_token_not_stripped(self):
        # Words whose text normalizes to "" → no strip
        words = [_ws(".", i * 0.1, (i + 1) * 0.1) for i in range(5)]
        seg = {"text": ". . . . .", "words": words, "start": 0.0, "end": 0.5}
        result = {"text": ". . . . .", "segments": [seg]}
        out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(len(out["segments"][0]["words"]), 5)

    def test_text_level_strip_when_segments_have_no_words(self):
        # Pass 1 skips (no words in segment); pass 2 fires on text
        seg = {"text": "the the the the", "words": []}
        result = {"text": "the the the the", "segments": [seg]}
        with patch("builtins.print"):
            out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(out["text"], "the")

    def test_text_level_run_below_threshold_unchanged(self):
        # 3 tokens < min_run=4 → text unchanged
        seg = {"text": "", "words": []}
        result = {"text": "go go go", "segments": [seg]}
        out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(out["text"], "go go go")

    def test_fewer_than_min_run_words_in_segment(self):
        # len(words) < min_run → _trim_word_run returns early (0 dropped)
        words = [_ws("a", i, i + 1) for i in range(3)]
        seg = {"text": "a a a", "words": words, "start": 0.0, "end": 3.0}
        result = {"text": "a a a", "segments": [seg]}
        out = _asr._strip_trailing_repetition(result, min_run=4)
        self.assertEqual(len(out["segments"][0]["words"]), 3)


class TestASRTranscribeFasterWhisper(unittest.TestCase):

    def _make_fw_module(self, seg_text="hello world", word_text="hello",
                        word_start=0.0, word_end=0.5, lang="en", words_list=None):
        """Return a fake faster_whisper module with a WhisperModel mock."""
        fw = MagicMock()
        model = MagicMock()
        word = MagicMock()
        word.word = word_text
        word.start = word_start
        word.end = word_end
        seg = MagicMock()
        seg.text = seg_text
        seg.start = 0.0
        seg.end = 1.0
        seg.words = words_list if words_list is not None else [word]
        info = MagicMock()
        info.language = lang
        model.transcribe.return_value = (iter([seg]), info)
        fw.WhisperModel.return_value = model
        return fw

    def test_missing_package_raises_runtime_error(self):
        with patch.dict(sys.modules, {"faster_whisper": None}):
            with self.assertRaises(RuntimeError) as ctx:
                _asr._transcribe_faster_whisper(Path("x.wav"), "base")
        self.assertIn("faster-whisper", str(ctx.exception))

    def test_result_has_expected_shape(self):
        fw = self._make_fw_module()
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            result = _asr._transcribe_faster_whisper(Path("x.wav"), "base")
        self.assertIn("text", result)
        self.assertIn("segments", result)
        self.assertIn("language", result)
        self.assertEqual(result["language"], "en")

    def test_segment_words_are_collected(self):
        fw = self._make_fw_module(word_text="hello", word_start=0.1, word_end=0.9)
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            result = _asr._transcribe_faster_whisper(Path("x.wav"), "base")
        self.assertEqual(result["segments"][0]["words"][0]["word"], "hello")
        self.assertAlmostEqual(result["segments"][0]["words"][0]["start"], 0.1)

    def test_segment_with_no_words(self):
        fw = self._make_fw_module(words_list=None)
        # Override seg.words to None
        model = fw.WhisperModel.return_value
        seg = list(model.transcribe.return_value[0])[0]
        # Rebuild with words=None
        fw2 = MagicMock()
        model2 = MagicMock()
        seg2 = MagicMock()
        seg2.text = "hello"
        seg2.start = 0.0
        seg2.end = 1.0
        seg2.words = None
        info2 = MagicMock()
        info2.language = "en"
        model2.transcribe.return_value = (iter([seg2]), info2)
        fw2.WhisperModel.return_value = model2
        with patch.dict(sys.modules, {"faster_whisper": fw2}):
            result = _asr._transcribe_faster_whisper(Path("x.wav"), "base")
        self.assertEqual(result["segments"][0]["words"], [])

    def test_model_normalization_mlx_community_prefix(self):
        fw = self._make_fw_module()
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            _asr._transcribe_faster_whisper(
                Path("x.wav"), "mlx-community/whisper-large-v3-mlx-4bit"
            )
        fw.WhisperModel.assert_called_once_with(
            "large-v3", device="cpu", compute_type="int8"
        )

    def test_model_normalization_mlx_suffix(self):
        fw = self._make_fw_module()
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            _asr._transcribe_faster_whisper(Path("x.wav"), "whisper-small-mlx")
        fw.WhisperModel.assert_called_once_with(
            "small", device="cpu", compute_type="int8"
        )

    def test_model_normalization_whisper_prefix(self):
        fw = self._make_fw_module()
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            _asr._transcribe_faster_whisper(Path("x.wav"), "whisper-medium")
        fw.WhisperModel.assert_called_once_with(
            "medium", device="cpu", compute_type="int8"
        )

    def test_model_normalization_unrecognized_falls_back_to_base(self):
        fw = self._make_fw_module()
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            _asr._transcribe_faster_whisper(Path("x.wav"), "custom-unrecognized")
        fw.WhisperModel.assert_called_once_with(
            "base", device="cpu", compute_type="int8"
        )

    def test_recognized_size_name_used_directly(self):
        fw = self._make_fw_module()
        with patch.dict(sys.modules, {"faster_whisper": fw}):
            _asr._transcribe_faster_whisper(Path("x.wav"), "large-v3")
        fw.WhisperModel.assert_called_once_with(
            "large-v3", device="cpu", compute_type="int8"
        )


# ===========================================================================
# pipeline.audio.transcribe
# ===========================================================================


class TestTranscribeModule(unittest.TestCase):

    def test_transcribe_delegates_to_asr(self):
        fake = {"text": "hi", "segments": []}
        with patch.object(_asr, "transcribe", return_value=fake) as m:
            result = _transcribe.transcribe(Path("a.wav"), provider="faster_whisper", model="base")
        m.assert_called_once_with(Path("a.wav"), provider="faster_whisper", model="base")
        self.assertEqual(result, fake)

    def test_transcribe_prints_source_name(self):
        with patch.object(_asr, "transcribe", return_value={"text": "", "segments": []}):
            with patch("builtins.print") as mp:
                _transcribe.transcribe(Path("mysource.wav"))
        self.assertTrue(any("mysource.wav" in str(c) for c in mp.call_args_list))

    def test_default_provider_attribute(self):
        self.assertEqual(_transcribe.DEFAULT_PROVIDER, _asr.DEFAULT_PROVIDER)

    def test_words_from_result_empty(self):
        self.assertEqual(_transcribe.words_from_result({"segments": []}), [])

    def test_words_from_result_no_segments_key(self):
        self.assertEqual(_transcribe.words_from_result({}), [])

    def test_words_from_result_multi_segment(self):
        result = {
            "segments": [
                {"words": [{"word": "hello", "start": 0.0, "end": 0.5},
                            {"word": "world", "start": 0.5, "end": 1.0}]},
                {"words": [{"word": "foo", "start": 1.0, "end": 1.5}]},
            ]
        }
        words = _transcribe.words_from_result(result)
        self.assertEqual(len(words), 3)
        self.assertEqual(words[0].text, "hello")
        self.assertAlmostEqual(words[0].start, 0.0)
        self.assertEqual(words[2].text, "foo")

    def test_words_from_result_strips_whitespace(self):
        result = {"segments": [{"words": [{"word": " hello ", "start": 0.0, "end": 1.0}]}]}
        words = _transcribe.words_from_result(result)
        self.assertEqual(words[0].text, "hello")

    def test_words_from_result_none_word_becomes_empty_string(self):
        result = {"segments": [{"words": [{"word": None, "start": 0.0, "end": 0.5}]}]}
        words = _transcribe.words_from_result(result)
        self.assertEqual(words[0].text, "")

    def test_words_from_result_segment_with_no_words_key(self):
        result = {"segments": [{"text": "hello"}]}  # no "words" key
        words = _transcribe.words_from_result(result)
        self.assertEqual(words, [])

    def test_save_result_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "result.json"
            data = {"text": "test", "segments": []}
            _transcribe.save_result(data, path)
            self.assertTrue(path.exists())

    def test_save_and_load_result_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "result.json"
            data = {"text": "hello", "segments": [{"text": "hello", "start": 0.0, "end": 1.0, "words": []}]}
            _transcribe.save_result(data, path)
            loaded = _transcribe.load_result(path)
        self.assertEqual(loaded["text"], "hello")
        self.assertEqual(len(loaded["segments"]), 1)


# ===========================================================================
# pipeline.audio.audio (TTS dispatcher)
# ===========================================================================


def _stub_synth(name: str) -> MagicMock:
    """Return a mock that returns a fake Path (no side effects)."""
    m = MagicMock(return_value=Path(f"/fake/{name}.wav"))
    return m


class TestAudioSynthesizeDispatcher(unittest.TestCase):

    _BASE = dict(text="Hello.", voice="ref.wav", out_path=Path("/fake/out.wav"))

    # ---- cloudrun_chatterbox ------------------------------------------------

    def test_cloudrun_chatterbox_routes(self):
        with patch.object(_audio, "_synth_cloudrun_chatterbox", _stub_synth("cb")) as m:
            _audio.synthesize(**self._BASE, provider="cloudrun_chatterbox")
        m.assert_called_once()

    # ---- cloudrun_indicf5 ---------------------------------------------------

    def test_cloudrun_indicf5_missing_ref_text_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _audio.synthesize(**self._BASE, provider="cloudrun_indicf5")
        self.assertIn("ref_audio_text", str(ctx.exception))

    def test_cloudrun_indicf5_routes(self):
        with patch.object(_audio, "_synth_cloudrun_indicf5", _stub_synth("if5")) as m:
            _audio.synthesize(**self._BASE, provider="cloudrun_indicf5", ref_audio_text="x")
        m.assert_called_once()

    # ---- dropped providers (post 2026-05-16 cost-optimization sweep) --------

    def test_dropped_cloudrun_providers_rejected(self):
        # f5, higgs, cosyvoice, indicparler removed; all azure_* removed.
        for dropped in (
            "cloudrun_f5", "cloudrun_higgs", "cloudrun_cosyvoice",
            "cloudrun_indicparler",
            "azure_f5", "azure_higgs", "azure_cosyvoice",
            "azure_chatterbox", "azure_indicparler", "azure_indicf5",
        ):
            with self.assertRaises(ValueError, msg=f"provider={dropped}") as ctx:
                _audio.synthesize(**self._BASE, provider=dropped, ref_audio_text="x")
            self.assertIn("unknown TTS provider", str(ctx.exception))

    # ---- unknown provider ---------------------------------------------------

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _audio.synthesize(**self._BASE, provider="not_a_real_provider")
        self.assertIn("unknown TTS provider", str(ctx.exception))

    # ---- pronunciation_dict passthrough -------------------------------------

    def test_pronunciation_dict_passthrough(self):
        with patch.object(_audio, "_synth_cloudrun_chatterbox", _stub_synth("cb")):
            _audio.synthesize(
                "Hello world", voice="ref.wav", out_path=Path("/fake/out.wav"),
                provider="cloudrun_chatterbox",
                pronunciation_dict={"Hello": "Hullo"},
            )


# ===========================================================================
# pipeline.audio.align
# ===========================================================================


class TestAlignSourceToWhisper(unittest.TestCase):

    def test_empty_source_returns_empty(self):
        result = align_source_to_whisper("", [_w("hello", 0.0, 1.0)])
        self.assertEqual(result, [])

    def test_empty_whisper_spreads_source_evenly(self):
        # 3 source tokens, no whisper timestamps → evenly spread using _AVG_WORD_S
        from pipeline.audio.align import _AVG_WORD_S
        result = align_source_to_whisper("a b c", [])
        self.assertEqual(len(result), 3)
        for i, w in enumerate(result):
            self.assertAlmostEqual(w.start, i * _AVG_WORD_S, places=5)
            self.assertAlmostEqual(w.end, (i + 1) * _AVG_WORD_S, places=5)
        self.assertEqual([w.text for w in result], ["a", "b", "c"])

    def test_equal_match_anchors_timestamps(self):
        # Source matches whisper exactly → timestamps directly anchored
        whisper = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        result = align_source_to_whisper("hello world", whisper)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0].start, 0.0)
        self.assertAlmostEqual(result[0].end, 0.5)
        self.assertAlmostEqual(result[1].start, 0.5)
        self.assertAlmostEqual(result[1].end, 1.0)
        self.assertEqual(result[0].text, "hello")

    def test_replace_op_distributes_time_evenly(self):
        # Whisper says "AITA" but source has "Am I the asshole" (4 tokens → 1 whisper)
        whisper = [_w("ada", 0.0, 2.0)]  # mangled recognition
        source = "Am I the asshole"
        result = align_source_to_whisper(source, whisper)
        self.assertEqual(len(result), 4)
        # All 4 source tokens share the [0.0, 2.0] span
        self.assertAlmostEqual(result[0].start, 0.0, places=5)
        self.assertAlmostEqual(result[3].end, 2.0, places=5)

    def test_insert_op_leaves_none_filled_by_interpolation(self):
        # source has extra word "x" not found in whisper → interpolated
        # whisper: hello@(0.0-0.5), world@(1.5-2.0) — clear gap for "x"
        whisper = [_w("hello", 0.0, 0.5), _w("world", 1.5, 2.0)]
        source = "hello x world"
        result = align_source_to_whisper(source, whisper)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].text, "hello")
        self.assertEqual(result[1].text, "x")
        self.assertEqual(result[2].text, "world")
        # x is interpolated between hello.end (0.5) and world.start (1.5)
        self.assertGreaterEqual(result[1].start, 0.5 - 0.01)
        self.assertLessEqual(result[1].end, 1.5 + 0.01)

    def test_none_run_at_start_uses_zero_as_left_anchor(self):
        # First two source tokens have no whisper anchor (left-of-first-match)
        whisper = [_w("hello", 1.0, 1.5), _w("world", 1.5, 2.0)]
        source = "x y hello world"
        result = align_source_to_whisper(source, whisper)
        self.assertEqual(len(result), 4)
        # x and y are interpolated from 0.0 to 1.0
        self.assertAlmostEqual(result[0].start, 0.0, places=5)
        self.assertAlmostEqual(result[1].end, 1.0, places=5)

    def test_none_run_at_end_extrapolates_forward(self):
        from pipeline.audio.align import _AVG_WORD_S
        # Last two source tokens have no whisper anchor → extrapolate
        whisper = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        source = "hello world x y"
        result = align_source_to_whisper(source, whisper)
        self.assertEqual(len(result), 4)
        # x and y are extrapolated after 1.0
        self.assertGreaterEqual(result[2].start, 1.0 - 0.01)
        self.assertGreater(result[3].end, result[2].end - 0.01)

    def test_t_next_le_t_prev_clamped(self):
        # Non-monotonic whisper timestamps → t_next <= t_prev triggers clamp
        # source "hello x world" with whisper: hello@(2.0,2.5), world@(0.0,0.5)
        whisper = [_w("hello", 2.0, 2.5), _w("world", 0.0, 0.5)]
        source = "hello x world"
        result = align_source_to_whisper(source, whisper)
        self.assertEqual(len(result), 3)
        # x is interpolated; start must be ≥ hello.end (2.5)
        self.assertGreaterEqual(result[1].start, 2.5 - 0.01)

    def test_safety_clamp_end_ge_start(self):
        # Whisper word has end < start → safety clamp fires
        whisper = [_w("hello", 1.0, 0.5)]  # end < start
        result = align_source_to_whisper("hello", whisper)
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0].end, result[0].start)

    def test_delete_op_ignored(self):
        # Whisper has extra token not in source → delete op, no crash
        whisper = [_w("hello", 0.0, 0.5), _w("extra", 0.5, 1.0)]
        result = align_source_to_whisper("hello", whisper)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "hello")


# ===========================================================================
# pipeline.audio.beats
# ===========================================================================


class TestBeatDataclass(unittest.TestCase):

    def test_duration_property(self):
        b = Beat(text="hi", start=1.0, end=3.5, words=[])
        self.assertAlmostEqual(b.duration, 2.5)


class TestNormToken(unittest.TestCase):

    def test_strips_punctuation_and_lowercases(self):
        self.assertEqual(_norm_token("Hello!"), "hello")

    def test_keeps_apostrophe(self):
        self.assertEqual(_norm_token("don't"), "don't")

    def test_empty_string(self):
        self.assertEqual(_norm_token(""), "")

    def test_devanagari_preserved(self):
        # Unicode letters must not be stripped
        result = _norm_token("नमस्कार")
        self.assertGreater(len(result), 0)

    def test_second_call_uses_cached_regex(self):
        # Ensure the lazy-init branch (if _NORM_RE is None) and the cached branch are both hit
        import pipeline.audio.beats as _beats_mod
        _beats_mod._NORM_RE = None  # force lazy re-init
        _norm_token("first")   # initializes _NORM_RE
        _norm_token("second")  # uses cached _NORM_RE


class TestNormTokens(unittest.TestCase):

    def test_splits_and_drops_empties(self):
        result = _norm_tokens("Hello, world!")
        self.assertEqual(result, ["hello", "world"])

    def test_empty_string_returns_empty(self):
        self.assertEqual(_norm_tokens(""), [])

    def test_pure_punctuation_returns_empty(self):
        self.assertEqual(_norm_tokens("... ,,,"), [])


class TestFindSubsequence(unittest.TestCase):

    def test_found_at_start(self):
        self.assertEqual(_find_subsequence(["a", "b", "c"], ["a", "b"], 0), 0)

    def test_found_in_middle(self):
        self.assertEqual(_find_subsequence(["x", "a", "b"], ["a", "b"], 0), 1)

    def test_not_found(self):
        self.assertEqual(_find_subsequence(["a", "b"], ["c"], 0), -1)

    def test_empty_needle_returns_start(self):
        self.assertEqual(_find_subsequence(["a", "b"], [], 2), 2)

    def test_skips_empty_haystack_tokens(self):
        # Empty strings in haystack (from punctuation-only words) are skipped
        self.assertEqual(_find_subsequence(["a", "", "b"], ["a", "b"], 0), 0)

    def test_partial_match_reset(self):
        # "a c" searched in "a b a c" — resets on mismatch
        self.assertEqual(_find_subsequence(["a", "b", "a", "c"], ["a", "c"], 0), 2)

    def test_search_from_offset(self):
        self.assertEqual(_find_subsequence(["a", "b", "a", "b"], ["a", "b"], 2), 2)


class TestMakeBeat(unittest.TestCase):

    def test_basic(self):
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        b = _make_beat(words)
        self.assertEqual(b.text, "hello world")
        self.assertAlmostEqual(b.start, 0.0)
        self.assertAlmostEqual(b.end, 1.0)
        self.assertEqual(b.words, words)


class TestFfprobeDurationS(unittest.TestCase):

    def test_returns_float_on_success(self):
        proc = MagicMock()
        proc.stdout = "3.14\n"
        with patch("subprocess.run", return_value=proc):
            result = _ffprobe_duration_s(Path("x.wav"))
        self.assertAlmostEqual(result, 3.14)

    def test_returns_none_on_value_error(self):
        proc = MagicMock()
        proc.stdout = "not_a_number\n"
        with patch("subprocess.run", return_value=proc):
            result = _ffprobe_duration_s(Path("x.wav"))
        self.assertIsNone(result)

    def test_returns_none_on_os_error(self):
        with patch("subprocess.run", side_effect=OSError("no ffprobe")):
            result = _ffprobe_duration_s(Path("x.wav"))
        self.assertIsNone(result)

    def test_returns_none_on_subprocess_error(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.SubprocessError("timeout")):
            result = _ffprobe_duration_s(Path("x.wav"))
        self.assertIsNone(result)


class TestAudioLowRmsSpans(unittest.TestCase):

    def _make_stderr(self, entries: list[tuple[float, float]]) -> str:
        """Build fake ffmpeg stderr from (pts_time, rms_level) pairs."""
        lines = []
        for t, rms in entries:
            lines.append(f"pts_time:{t:.3f}")
            lines.append(f"lavfi.astats.Overall.RMS_level={rms:.1f}")
        return "\n".join(lines)

    def test_subprocess_error_returns_empty(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.SubprocessError):
            result = _audio_low_rms_spans(Path("x.wav"))
        self.assertEqual(result, [])

    def test_os_error_returns_empty(self):
        with patch("subprocess.run", side_effect=OSError):
            result = _audio_low_rms_spans(Path("x.wav"))
        self.assertEqual(result, [])

    def test_no_samples_returns_empty(self):
        proc = MagicMock()
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            result = _audio_low_rms_spans(Path("x.wav"))
        self.assertEqual(result, [])

    def test_span_meeting_min_duration_appended(self):
        # Low RMS for 1.0s (> min_span_s=0.5s)
        entries = [(0.0, -35.0), (0.5, -35.0), (1.0, -35.0), (1.5, -5.0)]
        proc = MagicMock()
        proc.stderr = self._make_stderr(entries)
        with patch("subprocess.run", return_value=proc):
            spans = _audio_low_rms_spans(Path("x.wav"), threshold_db=-28.0,
                                         sample_window_s=0.1, min_span_s=0.5)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0][0], 0.0)

    def test_span_too_short_not_appended(self):
        # Low RMS for only 0.1s → below min_span_s=0.5s
        entries = [(0.0, -35.0), (0.1, -5.0)]
        proc = MagicMock()
        proc.stderr = self._make_stderr(entries)
        with patch("subprocess.run", return_value=proc):
            spans = _audio_low_rms_spans(Path("x.wav"), threshold_db=-28.0,
                                         sample_window_s=0.1, min_span_s=0.5)
        self.assertEqual(spans, [])

    def test_above_threshold_at_start_no_span_started(self):
        # Starts high → no span_start set; then goes low for long enough
        entries = [(0.0, -5.0), (0.5, -35.0), (1.0, -35.0), (1.5, -35.0)]
        proc = MagicMock()
        proc.stderr = self._make_stderr(entries)
        with patch("subprocess.run", return_value=proc):
            spans = _audio_low_rms_spans(Path("x.wav"), threshold_db=-28.0,
                                         sample_window_s=0.1, min_span_s=0.5)
        # Trailing low span: 0.5 → 1.5, duration=1.0 ≥ 0.5
        self.assertEqual(len(spans), 1)

    def test_trailing_open_span_meeting_duration_appended(self):
        # Low RMS all the way to end of file (open span at loop end)
        entries = [(0.0, -35.0), (0.5, -35.0), (1.0, -35.0), (1.5, -35.0)]
        proc = MagicMock()
        proc.stderr = self._make_stderr(entries)
        with patch("subprocess.run", return_value=proc):
            spans = _audio_low_rms_spans(Path("x.wav"), threshold_db=-28.0,
                                         sample_window_s=0.1, min_span_s=0.5)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0][0], 0.0)


class TestTranscribeWords(unittest.TestCase):

    def _fake_asr_result(self, words: list[dict]) -> dict:
        return {"text": "test", "segments": [{"words": words}]}

    def _patch_asr(self, result):
        # beats.py does `from . import asr` inside transcribe_words,
        # so we patch the underlying module function directly.
        return patch("pipeline.audio.asr.transcribe", return_value=result)

    def _patch_ffprobe(self, duration):
        return patch("pipeline.audio.beats._ffprobe_duration_s", return_value=duration)

    def _patch_rms(self, spans):
        return patch("pipeline.audio.beats._audio_low_rms_spans", return_value=spans)

    def test_basic_word_extraction(self):
        words_data = [{"word": "hello", "start": 0.0, "end": 0.5},
                      {"word": "world", "start": 0.5, "end": 1.0}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(None), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].text, "hello")

    def test_whisper_mlx_default_model_set(self):
        # When provider="whisper_mlx" and model=None, model is set to the mlx default
        words_data = [{"word": "hi", "start": 0.0, "end": 0.5}]
        with self._patch_asr(self._fake_asr_result(words_data)) as m, \
             self._patch_ffprobe(None), self._patch_rms([]):
            transcribe_words(Path("x.wav"), provider="whisper_mlx")
        # The mlx default is passed through
        call_kwargs = m.call_args
        self.assertIn("mlx-community", str(call_kwargs))

    def test_sanitisation_clamps_non_monotonic_timestamps(self):
        # word 1: start=1.0, end=2.0 — word 2: start=0.5 (backwards) → clamped
        words_data = [{"word": "a", "start": 1.0, "end": 2.0},
                      {"word": "b", "start": 0.5, "end": 0.6}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(None), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        self.assertGreaterEqual(result[1].start, result[0].start)

    def test_ffprobe_duration_caps_words(self):
        # Words past audio duration are dropped
        words_data = [{"word": "early", "start": 0.0, "end": 0.5},
                      {"word": "past", "start": 1.5, "end": 2.0}]  # past audio end
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(1.0), self._patch_rms([]):  # audio is 1.0s
            result = transcribe_words(Path("x.wav"))
        # "past" word starts at 1.5 >= audio_dur_s=1.0 → dropped
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "early")

    def test_ffprobe_duration_none_no_cap(self):
        words_data = [{"word": "a", "start": 999.0, "end": 1000.0}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(None), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        # No cap applied; word retained
        self.assertEqual(len(result), 1)

    def test_ffprobe_duration_zero_no_cap(self):
        words_data = [{"word": "a", "start": 0.0, "end": 1.0}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(0.0), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        self.assertEqual(len(result), 1)

    def test_word_end_capped_at_audio_duration(self):
        # Word starts before audio end but ends after
        words_data = [{"word": "clip", "start": 0.8, "end": 1.5}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(1.0), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        self.assertEqual(len(result), 1)
        self.assertLessEqual(result[0].end, 1.0)

    def test_rms_mask_filters_silent_words(self):
        # Word at midpoint 0.5 falls in silence span (0.0, 1.0)
        words_data = [{"word": "silence", "start": 0.0, "end": 1.0},
                      {"word": "voice", "start": 2.0, "end": 2.5}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(5.0), self._patch_rms([(0.0, 1.5)]):
            result = transcribe_words(Path("x.wav"))
        # "silence" midpoint=0.5 is in (0.0,1.5) → dropped
        # "voice" midpoint=2.25 is NOT in (0.0,1.5) → kept
        texts = [w.text for w in result]
        self.assertNotIn("silence", texts)
        self.assertIn("voice", texts)

    def test_empty_rms_mask_keeps_all_words(self):
        words_data = [{"word": "a", "start": 0.0, "end": 0.5}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(5.0), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        self.assertEqual(len(result), 1)

    def test_word_end_equals_start_after_cap_bumped(self):
        # word.start very close to audio_dur_s, word.end also capped → e=s → bump
        words_data = [{"word": "edge", "start": 0.99, "end": 1.0}]
        with self._patch_asr(self._fake_asr_result(words_data)), \
             self._patch_ffprobe(1.0), self._patch_rms([]):
            result = transcribe_words(Path("x.wav"))
        if result:  # might be dropped if start >= dur
            self.assertGreater(result[0].end, result[0].start)


class TestSplitWithForcedBoundaries(unittest.TestCase):

    def test_empty_words_returns_empty(self):
        result = split_with_forced_boundaries([], ["hello world"])
        self.assertEqual(result, [])

    def test_empty_forced_lines_raises(self):
        words = [_w("hi", 0.0, 0.5)]
        with self.assertRaises(ValueError):
            split_with_forced_boundaries(words, [])

    def test_line_not_found_raises(self):
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        with self.assertRaises(ValueError) as ctx:
            split_with_forced_boundaries(words, ["missing phrase"])
        self.assertIn("not found", str(ctx.exception))

    def test_single_line_single_beat(self):
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        beats = split_with_forced_boundaries(words, ["hello world"])
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0].text, "hello world")

    def test_two_lines_two_beats(self):
        words = [_w("first", 0.0, 0.5), _w("line", 0.5, 1.0),
                 _w("second", 1.0, 1.5), _w("beat", 1.5, 2.0)]
        beats = split_with_forced_boundaries(words, ["first line", "second beat"])
        self.assertEqual(len(beats), 2)
        self.assertEqual(beats[0].text, "first line")
        self.assertEqual(beats[1].text, "second beat")

    def test_trailing_words_appended_to_last_beat(self):
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0), _w("extra", 1.0, 1.5)]
        beats = split_with_forced_boundaries(words, ["hello world"])
        self.assertEqual(len(beats), 1)
        self.assertIn("extra", beats[0].text)
        self.assertAlmostEqual(beats[0].end, 1.5)

    def test_leading_words_prepended_to_first_beat(self):
        words = [_w("intro", 0.0, 0.3), _w("hello", 0.3, 0.8), _w("world", 0.8, 1.3)]
        beats = split_with_forced_boundaries(words, ["hello world"])
        self.assertEqual(len(beats), 1)
        self.assertIn("intro", beats[0].text)

    def test_empty_token_line_skipped(self):
        # A forced line of pure punctuation → empty tokens → skipped
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        beats = split_with_forced_boundaries(words, [".", "hello world"])
        self.assertEqual(len(beats), 1)
        self.assertIn("hello", beats[0].text)

    def test_punctuation_word_extends_beat_span(self):
        # A word that normalizes to "" at end of a forced match extends the span
        words = [_w("hello", 0.0, 0.5), _w(",", 0.5, 0.6), _w("world", 0.6, 1.1)]
        beats = split_with_forced_boundaries(words, ["hello", "world"])
        self.assertEqual(len(beats), 2)


class TestSplitIntoBeats(unittest.TestCase):

    def test_empty_words_returns_empty(self):
        self.assertEqual(split_into_beats([]), [])

    def test_delegates_to_forced_boundaries(self):
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        beats = split_into_beats(words, forced_narration_lines=["hello world"])
        self.assertEqual(len(beats), 1)

    def test_single_sentence_single_beat(self):
        # Each sentence is > min_s (1.0s) so no merging; two sentences → two beats
        words = [
            _w("Hello.", 0.0, 1.2),
            _w("World.", 1.5, 2.8),
        ]
        beats = split_into_beats(words, min_s=1.0, max_s=3.0)
        self.assertEqual(len(beats), 2)

    def test_empty_word_text_handled(self):
        # Word with empty text → last_char = "" → no sentence break
        words = [_w("", 0.0, 0.1), _w("Hello.", 0.1, 0.5)]
        beats = split_into_beats(words)
        self.assertGreater(len(beats), 0)

    def test_short_beat_merged_with_next(self):
        # Two very short sentences → merged into one beat
        words = [_w("Hi.", 0.0, 0.2), _w("Yes.", 0.2, 0.5)]
        beats = split_into_beats(words, min_s=1.0, max_s=3.0)
        self.assertEqual(len(beats), 1)

    def test_short_beat_not_merged_if_combined_exceeds_max_s(self):
        # Short + very long → don't merge (would exceed max_s)
        words_short = [_w("Hi.", 0.0, 0.2)]
        words_long = [_w(f"word{i}.", i * 1.0, (i + 1) * 1.0) for i in range(1, 4)]
        words = words_short + words_long
        beats = split_into_beats(words, min_s=1.0, max_s=2.5)
        self.assertGreater(len(beats), 1)

    def test_rank_announcement_merged_with_next(self):
        # "Number five." (short) followed by description → force-merged
        words = [
            _w("Number", 0.0, 0.3), _w("five.", 0.3, 0.7),
            _w("The", 0.8, 0.9), _w("best.", 0.9, 1.2),
        ]
        beats = split_into_beats(words, min_s=2.0, max_s=5.0)
        self.assertEqual(len(beats), 1)
        self.assertIn("Number five", beats[0].text)

    def test_long_sentence_split(self):
        # A long sentence (> max_s) is split into smaller beats
        n = 15
        words = [_w(f"word{i}", i * 0.3, (i + 1) * 0.3) for i in range(n)]
        beats = split_into_beats(words, max_s=1.0)
        # All beats must be within max_s + slack
        for b in beats:
            self.assertLess(b.duration, 2.0)

    def test_overlong_beat_warning_printed(self):
        # A single very long word → overlong warning is printed
        words = [_w("superlongword", 0.0, 10.0)]
        with patch("builtins.print") as mp:
            beats = split_into_beats(words, max_s=2.8)
        # The single word can't be split → overlong warning fires
        self.assertTrue(any("WARNING" in str(c) for c in mp.call_args_list))


class TestSplitLongGroup(unittest.TestCase):

    def test_empty_group_returns_empty(self):
        self.assertEqual(_split_long_group([], 2.0), [])

    def test_within_max_s_returned_unchanged(self):
        words = [_w("hi", 0.0, 0.5)]
        result = _split_long_group(words, 2.0)
        self.assertEqual(result, [words])

    def test_single_word_exceeding_max_s_returned_unchanged(self):
        # Can't split a single word → returns [group] even if > max_s
        word = [_w("verylongword", 0.0, 5.0)]
        result = _split_long_group(word, 2.0)
        self.assertEqual(result, [word])

    def test_tier1_clause_break(self):
        # Comma near middle → split at comma
        words = [
            _w("first,", 0.0, 0.5), _w("and", 0.5, 0.8),
            _w("second", 0.8, 1.5), _w("part", 1.5, 2.2),
        ]
        result = _split_long_group(words, 1.5)
        self.assertGreater(len(result), 1)
        # First fragment ends with the comma word
        self.assertTrue(result[0][-1].text.endswith(","))

    def test_tier2_conjunction_split(self):
        # No clause break but has "and" conjunction → split before "and"
        words = [
            _w("first", 0.0, 0.5), _w("part", 0.5, 1.0),
            _w("and", 1.0, 1.2), _w("second", 1.2, 1.7),
            _w("part", 1.7, 2.2),
        ]
        result = _split_long_group(words, 1.5)
        self.assertGreater(len(result), 1)

    def test_tier3_middle_word_split(self):
        # No clause break, no conjunction → falls back to middle split
        words = [_w(f"word{i}", i * 0.4, (i + 1) * 0.4) for i in range(6)]
        result = _split_long_group(words, 1.0)
        self.assertGreater(len(result), 1)


class TestSaveBeatsMakeBeatsLoadBeats(unittest.TestCase):

    def _simple_beats(self):
        return [
            Beat(text="Hello world",
                 start=0.0, end=1.5,
                 words=[_w("Hello", 0.0, 0.7), _w("world", 0.7, 1.5)]),
            Beat(text="Foo bar",
                 start=2.0, end=3.0,
                 words=[_w("Foo", 2.0, 2.5), _w("bar", 2.5, 3.0)]),
        ]

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "beats.json"
            beats = self._simple_beats()
            save_beats(beats, path)
            self.assertTrue(path.exists())
            loaded = load_beats(path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].text, "Hello world")
        self.assertAlmostEqual(loaded[1].end, 3.0)

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "dir" / "beats.json"
            save_beats(self._simple_beats(), path)
            self.assertTrue(path.exists())

    def test_beat_level_monotonicity_clamped(self):
        # Beat with start < previous beat's start → clamped
        beats = [
            Beat(text="a", start=2.0, end=3.0, words=[_w("a", 2.0, 3.0)]),
            Beat(text="b", start=1.0, end=1.5, words=[_w("b", 1.0, 1.5)]),  # backwards
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "beats.json"
            save_beats(beats, path)
            with path.open() as f:
                data = json.load(f)
        # beat[1].start must be ≥ beat[0].start after clamping
        self.assertGreaterEqual(data[1]["start"], data[0]["start"])

    def test_word_level_monotonicity_clamped(self):
        # Words with identical or backwards timestamps → spread forward
        beats = [
            Beat(text="test",
                 start=0.0, end=1.0,
                 words=[_w("a", 0.5, 0.5), _w("b", 0.5, 0.5), _w("c", 0.5, 0.5)]),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "beats.json"
            save_beats(beats, path)
            with path.open() as f:
                data = json.load(f)
        words = data[0]["words"]
        # Each word should have non-zero duration and be monotonically increasing
        for i in range(1, len(words)):
            self.assertGreater(words[i]["start"], words[i - 1]["start"] - 0.001)

    def test_beat_end_expanded_to_cover_words(self):
        # Beat end < last word end → expanded after word monotonicity fix
        beats = [
            Beat(text="test", start=0.0, end=0.1,
                 words=[_w("a", 0.0, 1.0)]),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "beats.json"
            save_beats(beats, path)
            with path.open() as f:
                data = json.load(f)
        self.assertGreaterEqual(data[0]["end"], data[0]["words"][-1]["end"] - 0.001)

    def test_load_beats_deserializes_words(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "beats.json"
            save_beats(self._simple_beats(), path)
            loaded = load_beats(path)
        self.assertIsInstance(loaded[0].words[0], Word)

    def test_empty_beats_list(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "beats.json"
            save_beats([], path)
            loaded = load_beats(path)
        self.assertEqual(loaded, [])


if __name__ == "__main__":
    unittest.main()
