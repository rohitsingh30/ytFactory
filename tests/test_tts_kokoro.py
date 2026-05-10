"""Tests for pipeline/tts/kokoro.py — 100 % branch coverage.

Mocks: kokoro_onnx, urllib.request, soundfile, numpy where needed.
No real model loads, no real network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.tts.kokoro as _mod


def _reset_kokoro():
    _mod._KOKORO = None


class TestDownloadIfMissing(unittest.TestCase):
    def test_file_already_exists_skips_download(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.stat") as mock_stat, \
             patch("urllib.request.urlretrieve") as mock_dl:
            mock_stat.return_value.st_size = 1000
            result = _mod._download_if_missing("http://example.com/model.onnx",
                                               Path("/cache/model.onnx"))
            mock_dl.assert_not_called()
        self.assertEqual(result, Path("/cache/model.onnx"))

    def test_file_missing_downloads_and_renames(self):
        dest = Path("/cache/model.onnx")
        tmp = dest.with_suffix(".onnx.part")

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.mkdir"), \
             patch("urllib.request.urlretrieve") as mock_dl, \
             patch.object(Path, "rename") as mock_rename:
            _mod._download_if_missing("http://example.com/model.onnx", dest)
            mock_dl.assert_called_once_with("http://example.com/model.onnx", tmp)
            mock_rename.assert_called_once_with(dest)

    def test_file_size_zero_triggers_download(self):
        """A zero-byte file (incomplete download) should be re-fetched."""
        dest = Path("/cache/model.onnx")
        tmp = dest.with_suffix(".onnx.part")

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.stat") as mock_stat, \
             patch("pathlib.Path.mkdir"), \
             patch("urllib.request.urlretrieve") as mock_dl, \
             patch.object(Path, "rename"):
            mock_stat.return_value.st_size = 0
            _mod._download_if_missing("http://x.com/f", dest)
            mock_dl.assert_called_once()


class TestKokoroModel(unittest.TestCase):
    def setUp(self):
        _reset_kokoro()

    def tearDown(self):
        _reset_kokoro()

    def test_cold_load_creates_instance(self):
        mock_kokoro_cls = MagicMock()
        mock_instance = MagicMock()
        mock_kokoro_cls.return_value = mock_instance

        mock_kokoro_mod = MagicMock()
        mock_kokoro_mod.Kokoro = mock_kokoro_cls

        with patch.dict(sys.modules, {"kokoro_onnx": mock_kokoro_mod}), \
             patch.object(_mod, "_download_if_missing", return_value=Path("/m.onnx")):
            result = _mod._kokoro()

        self.assertIs(result, mock_instance)
        self.assertIs(_mod._KOKORO, mock_instance)

    def test_warm_cache_returns_same_instance(self):
        cached = MagicMock()
        _mod._KOKORO = cached
        result = _mod._kokoro()
        self.assertIs(result, cached)


class TestLangForVoice(unittest.TestCase):
    def test_a_prefix_returns_en_us(self):
        self.assertEqual(_mod.lang_for_voice("af_bella"), "en-us")

    def test_b_prefix_returns_en_gb(self):
        self.assertEqual(_mod.lang_for_voice("bf_alice"), "en-gb")

    def test_h_prefix_returns_hi(self):
        self.assertEqual(_mod.lang_for_voice("hf_alpha"), "hi")

    def test_j_prefix_returns_ja(self):
        self.assertEqual(_mod.lang_for_voice("jm_kenji"), "ja")

    def test_z_prefix_returns_cmn(self):
        self.assertEqual(_mod.lang_for_voice("zf_lin"), "cmn")

    def test_e_prefix_returns_es(self):
        self.assertEqual(_mod.lang_for_voice("ef_diana"), "es")

    def test_f_prefix_returns_fr(self):
        self.assertEqual(_mod.lang_for_voice("ff_elise"), "fr-fr")

    def test_i_prefix_returns_it(self):
        self.assertEqual(_mod.lang_for_voice("if_isabella"), "it")

    def test_p_prefix_returns_pt_br(self):
        self.assertEqual(_mod.lang_for_voice("pf_camila"), "pt-br")

    def test_unknown_prefix_falls_back_to_en_us(self):
        self.assertEqual(_mod.lang_for_voice("xf_unknown"), "en-us")

    def test_empty_string_falls_back(self):
        self.assertEqual(_mod.lang_for_voice(""), "en-us")

    def test_short_string_falls_back(self):
        self.assertEqual(_mod.lang_for_voice("a"), "en-us")
        self.assertEqual(_mod.lang_for_voice("af"), "en-us")


class TestTrimTrailingSilence(unittest.TestCase):
    def test_empty_array_returned_unchanged(self):
        arr = np.array([], dtype=np.float32)
        result = _mod._trim_trailing_silence(arr, sample_rate=24000)
        self.assertEqual(len(result), 0)

    def test_all_silence_returned_unchanged(self):
        arr = np.zeros(240, dtype=np.float32)  # all silence
        result = _mod._trim_trailing_silence(arr, sample_rate=24000)
        np.testing.assert_array_equal(result, arr)

    def test_trailing_silence_trimmed(self):
        # 100 samples of speech + 2000 samples of silence (needs big gap to avoid keep_until hitting end)
        arr = np.zeros(2100, dtype=np.float32)
        arr[:100] = 0.5  # loud part
        result = _mod._trim_trailing_silence(arr, sample_rate=24000, min_keep_s=0.001)
        # Should be shorter than full array (trailing silence trimmed)
        self.assertLess(len(result), len(arr))
        # But not shorter than the speech region + min_keep buffer
        self.assertGreaterEqual(len(result), 100)

    def test_no_trailing_silence_unchanged(self):
        arr = np.ones(100, dtype=np.float32) * 0.5  # all loud
        result = _mod._trim_trailing_silence(arr, sample_rate=24000)
        np.testing.assert_array_equal(result, arr)

    def test_keep_until_beyond_array_length_returns_full(self):
        """When last speech sample + min_keep >= array length, return whole array."""
        arr = np.ones(100, dtype=np.float32) * 0.5
        # All speech → keep_until would be 100 + 1200 which is beyond array
        result = _mod._trim_trailing_silence(arr, sample_rate=24000, min_keep_s=0.05)
        np.testing.assert_array_equal(result, arr)

    def test_int16_input_normalized(self):
        arr = np.zeros(2100, dtype=np.int16)
        arr[:50] = 1000  # loud part
        result = _mod._trim_trailing_silence(arr, sample_rate=24000, min_keep_s=0.001)
        # Should not crash and should be shorter
        self.assertLess(len(result), len(arr))


class TestWordCount(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(_mod._word_count(""), 0)

    def test_whitespace_only(self):
        self.assertEqual(_mod._word_count("   "), 0)

    def test_simple_sentence(self):
        self.assertEqual(_mod._word_count("Hello world"), 2)

    def test_extra_spaces(self):
        self.assertEqual(_mod._word_count("  one  two  three  "), 3)


class TestModulateSentenceSpeed(unittest.TestCase):
    def _call(self, text, *, is_hook=False, is_closer=False,
              prev_words=0, base=1.0, modulation=None):
        return _mod._modulate_sentence_speed(
            text,
            is_hook=is_hook,
            is_closer=is_closer,
            prev_sentence_words=prev_words,
            base_speed=base,
            modulation=modulation,
        )

    def test_hook_slows_down(self):
        speed, tag = self._call("First sentence.", is_hook=True)
        self.assertLess(speed, 1.0)
        self.assertEqual(tag, "hook")

    def test_closer_slows_down(self):
        speed, tag = self._call("Last sentence.", is_closer=True)
        self.assertLess(speed, 1.0)
        self.assertEqual(tag, "closer")

    def test_excited_speeds_up(self):
        speed, tag = self._call("Wow that is amazing!", prev_words=5)
        self.assertGreater(speed, 1.0)
        self.assertEqual(tag, "excited")

    def test_hanging_slows_down(self):
        speed, tag = self._call("And then... silence.", prev_words=5)
        self.assertLess(speed, 1.0)
        self.assertEqual(tag, "hanging")

    def test_thoughtful_slows_for_mid_question(self):
        # Mid-text "?" that does NOT end the sentence
        speed, tag = self._call("Why did it happen? We may never know.", prev_words=5)
        self.assertLess(speed, 1.0)
        self.assertEqual(tag, "thoughtful")

    def test_terminal_question_not_thoughtful(self):
        # Sentence that ends with "?" → not "thoughtful" (it's the terminal mark)
        speed, tag = self._call("Why?", prev_words=5)
        # Should be base (no other rule fires)
        self.assertEqual(tag, "base")

    def test_punch_fragment(self):
        # ≤3 words after ≥6 word setup
        speed, tag = self._call("She lied.", prev_words=7)
        self.assertLess(speed, 1.0)
        self.assertEqual(tag, "punch")

    def test_punch_not_triggered_for_long_sentence(self):
        # >3 words — punch doesn't fire
        speed, tag = self._call("She lied every single time.", prev_words=7)
        # No other rule fires either → base
        self.assertEqual(tag, "base")

    def test_punch_not_triggered_when_prev_short(self):
        # prev_words < punch_min_setup_words (6) — no punch
        speed, tag = self._call("She lied.", prev_words=3)
        self.assertEqual(tag, "base")

    def test_modulation_off(self):
        speed, tag = self._call("text", modulation={"enabled": False})
        self.assertEqual(tag, "off")

    def test_base_case(self):
        speed, tag = self._call("Normal sentence here.", prev_words=5)
        self.assertEqual(tag, "base")
        self.assertAlmostEqual(speed, 1.0)

    def test_custom_modulation_overrides_defaults(self):
        # Use a factor within the [0.75, 1.25] clamp range
        custom = {"hook_speed_factor": 0.80, "enabled": True}
        speed, tag = self._call("First!", is_hook=True, modulation=custom)
        self.assertEqual(tag, "hook")
        self.assertAlmostEqual(speed, 0.80, places=5)

    def test_speed_clamped_to_lower_bound(self):
        """hook_speed_factor * closer_speed_factor can't push below 0.75."""
        # Force a very low factor via custom modulation
        custom = {"hook_speed_factor": 0.50}
        speed, tag = self._call("text", is_hook=True, base=1.0, modulation=custom)
        self.assertGreaterEqual(speed, 0.75)

    def test_speed_clamped_to_upper_bound(self):
        custom = {"excited_speed_factor": 2.0}
        speed, tag = self._call("Amazing!", prev_words=5, modulation=custom)
        self.assertLessEqual(speed, 1.25)


class TestSplitIntoSentences(unittest.TestCase):
    def test_empty_string(self):
        result = _mod._split_into_sentences("")
        self.assertEqual(result, [])

    def test_whitespace_only(self):
        result = _mod._split_into_sentences("   \n   ")
        self.assertEqual(result, [])

    def test_single_sentence(self):
        result = _mod._split_into_sentences("Hello world.")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], (0, "Hello world."))

    def test_multi_sentence_single_paragraph(self):
        text = "First sentence. Second sentence. Third sentence."
        result = _mod._split_into_sentences(text)
        self.assertEqual(len(result), 3)
        # All in paragraph 0
        for p_idx, _ in result:
            self.assertEqual(p_idx, 0)

    def test_multi_paragraph(self):
        text = "Para one sentence one. Para one sentence two.\n\nPara two sentence one."
        result = _mod._split_into_sentences(text)
        p_indices = [p for p, _ in result]
        # First paragraph → p=0, second → p=1
        self.assertIn(0, p_indices)
        self.assertIn(1, p_indices)

    def test_empty_paragraphs_filtered(self):
        text = "First.\n\n\n\nSecond."
        result = _mod._split_into_sentences(text)
        p_indices = [p for p, _ in result]
        self.assertIn(0, p_indices)
        self.assertIn(1, p_indices)


class TestSynthKokoro(unittest.TestCase):
    def setUp(self):
        _reset_kokoro()

    def tearDown(self):
        _reset_kokoro()

    def _make_mock_kokoro(self, sample_rate=24000):
        mock_kokoro = MagicMock()
        audio = np.zeros(24000, dtype=np.float32) + 0.1
        mock_kokoro.create.return_value = (audio, sample_rate)
        return mock_kokoro

    def test_single_sentence_fast_path(self):
        """Single sentence → fast path (one create() call at base speed)."""
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/single.wav")

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write") as mock_sf, \
             patch("pathlib.Path.mkdir"):
            _mod._synth_kokoro("Hello world.", voice="af_bella", out_path=out, speed=1.0)

        mock_kokoro.create.assert_called_once()
        call_kwargs = mock_kokoro.create.call_args
        self.assertEqual(call_kwargs[1]["speed"], 1.0)
        mock_sf.assert_called_once()

    def test_single_sentence_returns_path(self):
        out = Path("/fake/out.wav")
        mock_kokoro = self._make_mock_kokoro()

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_kokoro("Hello.", voice="af_bella", out_path=out, speed=1.0)
        self.assertEqual(result, out)

    def test_multi_sentence_calls_create_per_sentence(self):
        """Multi-sentence text → one create() per sentence + silence gaps."""
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/multi.wav")
        text = "First sentence. Second sentence. Third sentence."

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write") as mock_sf, \
             patch("pathlib.Path.mkdir"):
            _mod._synth_kokoro(text, voice="af_bella", out_path=out, speed=1.0)

        self.assertEqual(mock_kokoro.create.call_count, 3)
        mock_sf.assert_called_once()

    def test_empty_text_fallback_to_single_call(self):
        """Empty text → falls through to the empty/single-sentence path."""
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/empty.wav")

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_kokoro("", voice="af_bella", out_path=out, speed=1.0)

        self.assertEqual(result, out)

    def test_lang_inferred_when_not_provided(self):
        """lang=None → inferred from voice prefix."""
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/lang.wav")

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_kokoro("Hello.", voice="hf_alpha", out_path=out, speed=1.0)

        # Verify create was called with lang="hi"
        call_kwargs = mock_kokoro.create.call_args[1]
        self.assertEqual(call_kwargs["lang"], "hi")

    def test_explicit_lang_forwarded(self):
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/lang2.wav")

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_kokoro("Hello.", voice="af_bella", out_path=out,
                               speed=1.0, lang="en-gb")

        call_kwargs = mock_kokoro.create.call_args[1]
        self.assertEqual(call_kwargs["lang"], "en-gb")

    def test_multi_paragraph_uses_paragraph_pause(self):
        """Multi-paragraph text uses _PARAGRAPH_PAUSE_S between paragraphs."""
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/paragraphs.wav")
        text = "Para one. Second sentence.\n\nPara two. Another sentence."

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_kokoro(text, voice="af_bella", out_path=out, speed=1.0)
        self.assertEqual(result, out)

    def test_modulation_applied_in_multi_sentence(self):
        """Custom modulation dict is respected."""
        mock_kokoro = self._make_mock_kokoro()
        out = Path("/fake/mod.wav")
        custom_mod = {
            "hook_speed_factor": 0.80,
            "closer_speed_factor": 0.80,
            "excited_speed_factor": 1.15,
            "hanging_speed_factor": 0.75,
            "thoughtful_speed_factor": 0.88,
            "punch_speed_factor": 0.75,
            "punch_max_words": 3,
            "punch_min_setup_words": 6,
        }
        text = "First hook sentence. Middle normal. Last sentence."

        with patch.object(_mod, "_kokoro", return_value=mock_kokoro), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_kokoro(text, voice="af_bella", out_path=out,
                                         speed=1.0, modulation=custom_mod)
        self.assertEqual(result, out)
        # hook sentence should be slower
        speeds = [call[1]["speed"] for call in mock_kokoro.create.call_args_list]
        self.assertAlmostEqual(speeds[0], 0.80, places=2)


if __name__ == "__main__":
    unittest.main()
