from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import audio_critic as ac


SCRATCH = PROJECT_ROOT / "tests" / ".scratch_audio_critic"
GOOD_CRITIQUE = {
    "score": 8,
    "one_line_take": "clear",
    "top_issues": [],
    "per_finding": [],
    "system_corrections": [{"issue_class": "x", "where": "y", "fix": "z"}],
    "highest_leverage_change": "none",
}


class AudioCriticHelpersTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_silence_gaps_parses_ffmpeg_stderr(self):
        stderr = "silence_start: 0.25\nnoise\nsilence_end: 0.90 | silence_duration: 0.65\nsilence_start: 2\nsilence_end: 3.5 | silence_duration: 1.5"
        with patch.object(ac.subprocess, "run", return_value=SimpleNamespace(stderr=stderr)) as run:
            gaps = ac._silence_gaps(SCRATCH / "a.wav", threshold_db=-35, min_duration_s=0.4)
        self.assertEqual(gaps, [(0.25, 0.9, 0.65), (2.0, 3.5, 1.5)])
        self.assertIn("silencedetect=n=-35dB:d=0.4", run.call_args.args[0])

    def test_audio_duration_uses_probe_fallback(self):
        target = "pipeline.quality.probe.probe_duration_or_none" if "pipeline.quality.probe" in ac._audio_duration.__code__.co_names else "pipeline.probe.probe_duration_or_none"
        with patch(target, return_value=None):
            self.assertEqual(ac._audio_duration(SCRATCH / "a.wav"), 0.0)
        with patch(target, return_value=12.5):
            self.assertEqual(ac._audio_duration(SCRATCH / "a.wav"), 12.5)

    def test_diff_lines_covers_all_opcode_kinds_and_empty(self):
        self.assertIn("perfect match", ac._diff_lines([], []))
        diff = ac._diff_lines(["a", "b", "c"], ["a", "x", "c", "d"])
        self.assertIn("matched words", diff)
        self.assertIn("source:", diff)
        self.assertIn("transcript:", diff)
        self.assertIn("transcript-only", diff)
        self.assertIn("source-only", ac._diff_lines(["a", "b"], ["a"]))

    def test_tokenise_preserves_unicode_and_lowercases(self):
        self.assertEqual(ac._tokenise("Hello, घर! DON'T"), ["hello", "घर", "don't"])

    def test_load_script_text_json_and_plain(self):
        narr = SCRATCH / "narr.json"
        narr.write_text(json.dumps({"narration": "from narration", "text": "fallback"}))
        self.assertEqual(ac._load_script_text(narr), "from narration")
        txt_json = SCRATCH / "text.json"
        txt_json.write_text(json.dumps({"text": "from text"}))
        self.assertEqual(ac._load_script_text(txt_json), "from text")
        plain = SCRATCH / "plain.txt"
        plain.write_text("plain body")
        self.assertEqual(ac._load_script_text(plain), "plain body")


class CritiqueAudioTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.audio = SCRATCH / "narration.wav"
        self.audio.write_bytes(b"RIFF")

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def _patch_metrics(self, llm_return=GOOD_CRITIQUE):
        return patch.multiple(
            ac,
            _audio_duration=unittest.mock.DEFAULT,
            _silence_gaps=unittest.mock.DEFAULT,
        )

    def test_missing_audio_and_empty_asr_raise(self):
        with self.assertRaises(FileNotFoundError):
            ac.critique_audio(audio_path=SCRATCH / "missing.wav", source_script="x")
        with patch.object(ac.asr, "transcribe", return_value={"text": "   "}):
            with self.assertRaises(RuntimeError):
                ac.critique_audio(audio_path=self.audio, source_script="x")

    def test_success_writes_output_and_passes_schema(self):
        out = SCRATCH / "scores" / "audio.json"
        with patch.object(ac.asr, "transcribe", return_value={"text": "Hello world"}) as transcribe, \
             patch.object(ac, "_audio_duration", return_value=2.0), \
             patch.object(ac, "_silence_gaps", return_value=[(0.5, 1.1, 0.6)]), \
             patch.object(ac.llm, "model_for", return_value="opus"), \
             patch.object(ac.llm, "call_claude_cli", return_value=GOOD_CRITIQUE) as call:
            result = ac.critique_audio(audio_path=self.audio, source_script="Hello\n\nworld", out_path=out, asr_provider="fake")
        self.assertEqual(result["score"], 8)
        self.assertTrue(out.exists())
        transcribe.assert_called_once_with(self.audio, provider="fake")
        self.assertIs(call.call_args.kwargs["json_schema"], ac._CRITIC_SCHEMA)
        self.assertEqual(call.call_args.kwargs["model"], "opus")

    def test_non_dict_llm_output_raises(self):
        with patch.object(ac.asr, "transcribe", return_value={"text": "Hello"}), \
             patch.object(ac, "_audio_duration", return_value=1.0), \
             patch.object(ac, "_silence_gaps", return_value=[]), \
             patch.object(ac.llm, "model_for", return_value="opus"), \
             patch.object(ac.llm, "call_claude_cli", return_value=[]):
            with self.assertRaises(ValueError):
                ac.critique_audio(audio_path=self.audio, source_script="Hello")

    def test_main_uses_default_output_and_empty_script_exits(self):
        script = SCRATCH / "script.txt"
        script.write_text("source script")
        with patch.object(sys, "argv", ["audio_critic", str(self.audio), str(script), "--asr-provider", "fake"]), \
             patch.object(ac, "critique_audio", return_value=GOOD_CRITIQUE) as crit:
            ac.main()
        self.assertEqual(crit.call_args.kwargs["out_path"], self.audio.with_suffix(".audio.score.json"))
        self.assertEqual(crit.call_args.kwargs["asr_provider"], "fake")
        empty = SCRATCH / "empty.txt"
        empty.write_text("  ")
        with patch.object(sys, "argv", ["audio_critic", str(self.audio), str(empty)]):
            with self.assertRaises(SystemExit):
                ac.main()


if __name__ == "__main__":
    unittest.main()
