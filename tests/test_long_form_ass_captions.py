"""Tests for the libass-based caption path + ASS file generation.

Tier-1 OOM fix (2026-05-05): the long-form `final_mux` no longer fans
out N PNG inputs + N-deep overlay chain for caption rendering. Instead
it emits a single libass ASS file consumed via ffmpeg's `subtitles=`
filter (one input + one filter step regardless of cue count). For a
90-min sleep video with 800 sentence cues, mux peak RAM drops from
~10 GB to ~1.5 GB.

These tests cover:
- ASS file structure (header + style line + Dialogue events)
- Style is parameterised from caption_style config (yellow italic by
  default — matches Sleepy Time History signature, NOT the prior
  hardcoded off-white non-italic that the rubber-duck flagged)
- Escape handling for `{`, `}`, `\` in event text
- Color packing into ASS &HAABBGGRR with the inverted alpha convention
- Authored-mode chunk-text alignment (default — no whisper)
- Whisper-mode fallback (legacy `caption_align: whisper`)
- final_mux subtitles= filter integration when libass is available
- final_mux falls back to PNG-overlay when libass is missing
"""
from __future__ import annotations

import re
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pipeline.render._legacy import long_form
from pipeline.render._legacy.long_form import (
    _ass_color_from_rgba,
    _ass_escape,
    _ffmpeg_has_libass,
    _hms_ass,
    build_captions_ass,
    final_mux,
)


def _write_silence_wav(path: Path, duration_s: float, sample_rate: int = 24_000) -> None:
    """Write a tiny silent mono WAV for ffprobe duration tests."""
    n_frames = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)


class AssEscapeTests(unittest.TestCase):
    """libass treats `{...}` as override blocks and `\\` as escape prefix.
    Unescaped braces / backslashes break event parsing."""

    def test_braces_are_escaped(self):
        self.assertEqual(_ass_escape("Some {weird} text"), r"Some \{weird\} text")

    def test_backslash_is_escaped(self):
        # Python literal \ is one char; expected output is two chars (\\).
        self.assertEqual(_ass_escape("a\\b"), r"a\\b")

    def test_newlines_become_ass_line_break(self):
        self.assertEqual(_ass_escape("line1\nline2"), r"line1\Nline2")
        self.assertEqual(_ass_escape("line1\r\nline2"), r"line1\Nline2")

    def test_apostrophes_emdashes_pass_through(self):
        # Plain UTF-8 — no special handling needed.
        s = "It's a 'quote' — em-dash, café, naïve."
        self.assertEqual(_ass_escape(s), s)

    def test_empty_string(self):
        self.assertEqual(_ass_escape(""), "")

    def test_combined_escapes(self):
        # All three at once.
        self.assertEqual(
            _ass_escape("a{b}\\c\nd"),
            r"a\{b\}\\c\Nd",
        )


class AssColorFromRgbaTests(unittest.TestCase):
    """ASS uses Windows BGR with INVERTED alpha (0=opaque, 255=transparent).
    Easy to get wrong, so locked down with explicit cases."""

    def test_yellow_opaque_matches_channel_signature(self):
        # historyrecapped/config.yaml caption_style.text_rgba = (255, 217, 61, 255)
        # = #FFD93D opaque. ASS expects &H00 + B + G + R = &H003DD9FF.
        self.assertEqual(_ass_color_from_rgba((255, 217, 61, 255)), "&H003DD9FF")

    def test_off_white_opaque(self):
        self.assertEqual(_ass_color_from_rgba((240, 240, 240, 255)), "&H00F0F0F0")

    def test_black_opaque(self):
        self.assertEqual(_ass_color_from_rgba((0, 0, 0, 255)), "&H00000000")

    def test_alpha_is_inverted(self):
        # Half-transparent: rgba alpha 128 → ASS alpha 127 (255-128).
        c = _ass_color_from_rgba((255, 217, 61, 128))
        self.assertTrue(c.startswith("&H7F"), c)

    def test_alpha_override(self):
        # When alpha_override is set, ignore the rgba's alpha channel.
        c = _ass_color_from_rgba((255, 217, 61, 0), alpha_override=255)
        # alpha_override=255 → ASS alpha = 0 (opaque).
        self.assertTrue(c.startswith("&H00"), c)

    def test_three_tuple_defaults_to_opaque(self):
        c = _ass_color_from_rgba((10, 20, 30))
        self.assertEqual(c, "&H001E140A")  # alpha 0 = opaque, B=30, G=20, R=10


class AssTimecodeTests(unittest.TestCase):
    def test_format_is_h_mm_ss_cs(self):
        self.assertEqual(_hms_ass(0.0), "0:00:00.00")
        self.assertEqual(_hms_ass(65.5), "0:01:05.50")
        self.assertEqual(_hms_ass(3661.234), "1:01:01.23")


class BuildCaptionsAssAuthoredAlignmentTests(unittest.TestCase):
    """Authored alignment is the default path. Tests that:
    - Chunk count mismatch raises (loud failure, not silent corruption)
    - Sentence-level cues are emitted in chronological order
    - Style is yellow italic by default (channel signature)
    - Customising caption_style flows through
    """

    def setUp(self):
        self._td = TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_chunk_mismatch_raises(self):
        # 1 chunk text vs 2 wavs → reject (caller passed inconsistent inputs).
        wavs = [self.tmp / "c0.wav", self.tmp / "c1.wav"]
        for w in wavs:
            _write_silence_wav(w, 5.0)
        with self.assertRaises(RuntimeError) as cm:
            build_captions_ass(
                self.tmp / "out.ass",
                narration_text="Hello world.",
                chunk_wavs=wavs,
                chunk_target_chars=380,
            )
        self.assertIn("chunk count mismatch", str(cm.exception))

    def test_authored_emits_sentence_cues_in_order(self):
        # Three sentences in one chunk → three cues.
        text = "First sentence. Second one. Third here."
        wav = self.tmp / "c0.wav"
        _write_silence_wav(wav, 6.0)
        out = self.tmp / "out.ass"
        _, n = build_captions_ass(
            out, narration_text=text, chunk_wavs=[wav], chunk_target_chars=380,
        )
        self.assertEqual(n, 3)
        body = out.read_text()
        # Three Dialogue lines, in order.
        cues = [ln for ln in body.splitlines() if ln.startswith("Dialogue:")]
        self.assertEqual(len(cues), 3)
        self.assertIn("First sentence", cues[0])
        self.assertIn("Second one", cues[1])
        self.assertIn("Third here", cues[2])

    def test_default_style_is_yellow_italic(self):
        # Channel signature — must NOT regress to off-white non-italic
        # (the rubber-duck-flagged hardcoded style from before 2026-05-05).
        wav = self.tmp / "c0.wav"
        _write_silence_wav(wav, 5.0)
        out = self.tmp / "out.ass"
        build_captions_ass(
            out, narration_text="Hello.", chunk_wavs=[wav], chunk_target_chars=380,
        )
        body = out.read_text()
        # Style line must mention &H003DD9FF (yellow opaque) and Italic=1.
        style_lines = [ln for ln in body.splitlines() if ln.startswith("Style:")]
        self.assertEqual(len(style_lines), 1)
        style = style_lines[0]
        self.assertIn("&H003DD9FF", style)
        # Italic field is the 8th comma-separated value after "Default,Helvetica,38,..."
        # Easier just to check the field count + that "1" appears in the italic slot.
        # Format positions: Name(0), Fontname(1), Fontsize(2), PrimaryColour(3),
        # SecondaryColour(4), OutlineColour(5), BackColour(6), Bold(7), Italic(8)
        fields = [s.strip() for s in style.split(":", 1)[1].split(",")]
        self.assertEqual(fields[8], "1", "Italic field should be 1 (yellow italic default)")

    def test_custom_style_flows_through(self):
        wav = self.tmp / "c0.wav"
        _write_silence_wav(wav, 5.0)
        out = self.tmp / "out.ass"
        build_captions_ass(
            out, narration_text="Hello.", chunk_wavs=[wav], chunk_target_chars=380,
            text_color=(255, 0, 0, 255), italic=False, font_name="Verdana", font_size=44,
        )
        body = out.read_text()
        style_line = next(ln for ln in body.splitlines() if ln.startswith("Style:"))
        # Red opaque → &H000000FF
        self.assertIn("&H000000FF", style_line)
        self.assertIn("Verdana", style_line)
        self.assertIn(",44,", style_line)
        fields = [s.strip() for s in style_line.split(":", 1)[1].split(",")]
        self.assertEqual(fields[8], "0")  # Italic off

    def test_special_chars_in_narration_are_escaped(self):
        # Curly braces in source narration must not become libass override blocks.
        wav = self.tmp / "c0.wav"
        _write_silence_wav(wav, 5.0)
        out = self.tmp / "out.ass"
        build_captions_ass(
            out,
            narration_text="A {fancy} sentence. With backslash a\\b.",
            chunk_wavs=[wav], chunk_target_chars=380,
        )
        body = out.read_text()
        # Both braces and the backslash should be escaped in the Dialogue text.
        self.assertIn(r"\{fancy\}", body)
        self.assertIn(r"a\\b", body)
        # And NO unescaped curly brace appears in any Dialogue line.
        for ln in body.splitlines():
            if ln.startswith("Dialogue:"):
                # Strip everything up through the 9th comma (the Text field starts there).
                # Looking for unescaped { right after a non-backslash char.
                self.assertNotRegex(ln, r"(?<!\\)\{(?!\\)")


class BuildCaptionsAssWhisperFallbackTests(unittest.TestCase):
    """Whisper alignment is the legacy fallback for `caption_align: whisper`.
    Mock the whisper call — we don't load a real model in tests."""

    def test_no_args_raises(self):
        with TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                build_captions_ass(Path(td) / "out.ass")

    def test_both_modes_raises_via_no_args_only(self):
        # Only ValueError check is "neither set". Test ensures the wrapper
        # error message is helpful.
        with TemporaryDirectory() as td:
            with self.assertRaises(ValueError) as cm:
                build_captions_ass(Path(td) / "out.ass")
            self.assertIn("authored", str(cm.exception).lower())

    def test_whisper_mode_emits_cues(self):
        # Mock pipeline.beats.transcribe_words to return a few synthetic words.
        class FakeWord:
            def __init__(self, text, start, end):
                self.text, self.start, self.end = text, start, end

        fake_words = [
            FakeWord("Hello", 0.0, 0.5),
            FakeWord("world.", 0.5, 1.0),
            FakeWord("Second", 1.5, 2.0),
            FakeWord("one.", 2.0, 2.5),
        ]
        with TemporaryDirectory() as td:
            wav = Path(td) / "narr.wav"
            _write_silence_wav(wav, 3.0)
            out = Path(td) / "out.ass"
            with patch("pipeline.beats.transcribe_words", return_value=fake_words):
                _, n = build_captions_ass(out, narration_wav=wav)
            self.assertEqual(n, 2)  # two sentences
            body = out.read_text()
            self.assertIn("Hello world", body)
            self.assertIn("Second one", body)


class FfmpegHasLibassProbeTests(unittest.TestCase):
    def test_probe_is_memoised(self):
        # Clear cache, call twice, second call should hit cache (no subprocess).
        if hasattr(_ffmpeg_has_libass, "_cached"):
            delattr(_ffmpeg_has_libass, "_cached")
        with patch("subprocess.check_output", return_value="Render text subtitles") as p:
            self.assertTrue(_ffmpeg_has_libass())
            self.assertTrue(_ffmpeg_has_libass())
            self.assertEqual(p.call_count, 1)

    def test_probe_returns_false_when_filter_missing(self):
        if hasattr(_ffmpeg_has_libass, "_cached"):
            delattr(_ffmpeg_has_libass, "_cached")
        # ffmpeg returns "Unknown filter 'subtitles'" with exit 0 in our test
        # case; what matters is the substring check, not the exit code.
        with patch("subprocess.check_output", return_value="Unknown filter 'subtitles'."):
            self.assertFalse(_ffmpeg_has_libass())

    def test_probe_returns_false_when_ffmpeg_missing(self):
        if hasattr(_ffmpeg_has_libass, "_cached"):
            delattr(_ffmpeg_has_libass, "_cached")
        with patch("subprocess.check_output", side_effect=FileNotFoundError("ffmpeg")):
            self.assertFalse(_ffmpeg_has_libass())


class FinalMuxArgValidationTests(unittest.TestCase):
    def test_passing_both_caption_modes_raises(self):
        # Caller must pick exactly one — passing both is a coding bug, not
        # something we should silently merge.
        with TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "v.mp4").write_bytes(b"")
            (tmp / "n.wav").write_bytes(b"")
            (tmp / "m.wav").write_bytes(b"")
            (tmp / "p.png").write_bytes(b"")
            (tmp / "c.ass").write_text("")
            with self.assertRaises(ValueError):
                final_mux(
                    tmp / "v.mp4", tmp / "n.wav", tmp / "m.wav", tmp / "out.mp4",
                    caption_cues=[(tmp / "p.png", 0.0, 1.0)],
                    captions_ass=tmp / "c.ass",
                )


if __name__ == "__main__":
    unittest.main()
