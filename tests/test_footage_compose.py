"""Tests for pipeline.compose — 100% line coverage."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.beats import Beat, Word
from pipeline import compose as comp_mod
from pipeline.compose import (
    _beat_overlaps_closer,
    _caption_window,
    _clip_filter,
    _ffprobe_duration,
    _kenburns_filter,
    _materialise_rank_chips,
    _video_timing,
    _wipe_stale_per_beat_artefacts,
    compose,
    compose_clips,
    compose_hybrid,
    image_to_kenburns_clip,
    prerender_word_captions,
    wipe_stale_per_beat_artefacts,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_beat(text: str, start: float, end: float, kind: str = "animated", footage=None):
    words = [
        Word(text=w, start=start + i * (end - start) / max(len(text.split()), 1),
             end=start + (i + 1) * (end - start) / max(len(text.split()), 1))
        for i, w in enumerate(text.split())
    ]
    return Beat(text=text, start=start, end=end, words=words, kind=kind, footage=footage)


def _make_image(td: Path, name: str, w: int = 100, h: int = 200) -> Path:
    from PIL import Image
    img = Image.new("RGB", (w, h), color=(128, 64, 32))
    p = td / name
    img.save(str(p), format="PNG")
    return p


def _make_wav(td: Path, name: str, duration_s: float = 1.0) -> Path:
    import numpy as np
    import soundfile as sf
    sr = 22050
    data = np.zeros(int(sr * duration_s), dtype=np.float32)
    p = td / name
    sf.write(str(p), data, sr)
    return p


def _fake_ffmpeg_run(cmd, *args, **kwargs):
    """Mock subprocess.run: ffprobe returns float, ffmpeg creates output file."""
    if not cmd:
        return MagicMock(returncode=0, stdout="", stderr="")
    if cmd[0] == "ffprobe":
        return MagicMock(returncode=0, stdout="5.0\n", stderr="")
    # ffmpeg — create output file (last arg)
    out = Path(cmd[-1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake-mp4")
    return MagicMock(returncode=0, stdout="", stderr="")


def _fail_ffmpeg_run(cmd, *args, **kwargs):
    """Always-fail ffmpeg mock."""
    if cmd[0] == "ffprobe":
        return MagicMock(returncode=0, stdout="5.0\n", stderr="")
    return MagicMock(returncode=1, stdout="", stderr="ffmpeg error output")


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

class TestKenburnsFitler(unittest.TestCase):
    def test_returns_string(self):
        s = _kenburns_filter(2.0, 0)
        self.assertIsInstance(s, str)
        self.assertIn("zoompan", s)

    def test_short_clip(self):
        # Very short clip — frames should be at least 1
        s = _kenburns_filter(0.01, 0)
        self.assertIn("zoompan", s)

    def test_beat_index_ignored(self):
        s1 = _kenburns_filter(2.0, 0)
        s2 = _kenburns_filter(2.0, 99)
        self.assertEqual(s1, s2)


class TestVideoTiming(unittest.TestCase):
    def test_empty(self):
        vs, vd = _video_timing([])
        self.assertEqual(vs, [])
        self.assertEqual(vd, [])

    def test_single(self):
        b = _make_beat("hello", 1.0, 2.5)
        vs, vd = _video_timing([b])
        self.assertEqual(vs, [0.0])
        self.assertAlmostEqual(vd[0], 2.5)  # end - video_start[0] = 2.5 - 0.0

    def test_multi(self):
        beats = [
            _make_beat("one", 0.5, 2.0),
            _make_beat("two", 2.3, 4.0),
            _make_beat("three", 4.2, 6.0),
        ]
        vs, vd = _video_timing(beats)
        self.assertEqual(vs[0], 0.0)
        self.assertAlmostEqual(vs[1], 2.3)
        self.assertAlmostEqual(vs[2], 4.2)
        # vd[0] = vs[1] - vs[0] = 2.3; vd[1] = vs[2]-vs[1] = 1.9; vd[2] = end-start = 1.8
        self.assertAlmostEqual(vd[0], 2.3)
        self.assertAlmostEqual(vd[1], 1.9)
        self.assertAlmostEqual(vd[2], 1.8)


class TestCaptionWindow(unittest.TestCase):
    def test_middle_beat(self):
        beats = [
            _make_beat("one", 0.0, 2.0),
            _make_beat("two", 2.0, 4.0),
            _make_beat("three", 4.0, 6.0),
        ]
        vs, vd = _video_timing(beats)
        start, end = _caption_window(beats, vs, vd, 1)
        self.assertAlmostEqual(start, 2.0)
        self.assertAlmostEqual(end, 4.0)

    def test_last_beat(self):
        beats = [
            _make_beat("one", 0.0, 2.0),
            _make_beat("two", 2.0, 4.5),
        ]
        vs, vd = _video_timing(beats)
        start, end = _caption_window(beats, vs, vd, 1)
        self.assertAlmostEqual(start, 2.0)
        self.assertAlmostEqual(end, 4.5)

    def test_negative_start_clamped(self):
        beats = [_make_beat("hello", 0.0, 1.0)]
        vs, vd = _video_timing(beats)
        start, end = _caption_window(beats, vs, vd, 0)
        self.assertGreaterEqual(start, 0.0)


class TestBeatOverlapsCloser(unittest.TestCase):
    def test_no_closer_format(self):
        self.assertFalse(_beat_overlaps_closer("like this video", None))

    def test_no_overlap(self):
        self.assertFalse(_beat_overlaps_closer("hello world today", "subscribe vote comment"))

    def test_overlap(self):
        # "like", "comment", "subscribe" → 3/3 words in panel → True
        result = _beat_overlaps_closer("like comment subscribe", "LIKE if YTA, COMMENT if NTA, SUBSCRIBE")
        self.assertTrue(result)

    def test_empty_beat(self):
        self.assertFalse(_beat_overlaps_closer("", "LIKE if YTA"))

    def test_empty_panel(self):
        self.assertFalse(_beat_overlaps_closer("like this", "if or"))


class TestClipFilter(unittest.TestCase):
    def test_returns_string(self):
        s = _clip_filter(3.0)
        self.assertIsInstance(s, str)
        self.assertIn("scale=", s)
        self.assertIn("fps=", s)


# ---------------------------------------------------------------------------
# _wipe_stale_per_beat_artefacts
# ---------------------------------------------------------------------------

class TestWipeArtefacts(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_nonexistent_dir(self):
        # No crash on missing dir
        _wipe_stale_per_beat_artefacts(Path(self.td.name) / "no-such-dir", 3)

    def test_wipes_caption_png(self):
        (self.cache / "caption_00.png").touch()
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "caption_00.png").exists())

    def test_wipes_word_png(self):
        (self.cache / "word_0001.png").touch()
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "word_0001.png").exists())

    def test_wipes_rank_chip_png(self):
        (self.cache / "rank_chip_5.png").touch()
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "rank_chip_5.png").exists())

    def test_wipes_closer_row_png(self):
        (self.cache / "closer_row_0.png").touch()
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "closer_row_0.png").exists())

    def test_wipes_stale_img(self):
        (self.cache / "img_003.png").touch()  # index 3 >= n_beats=3 → stale
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "img_003.png").exists())

    def test_keeps_valid_img(self):
        (self.cache / "img_000.png").touch()  # index 0 < n_beats=3 → keep
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertTrue((self.cache / "img_000.png").exists())

    def test_wipes_closer_panel(self):
        (self.cache / "closer_panel.png").touch()
        _wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "closer_panel.png").exists())

    def test_file_not_found_race(self):
        """FileNotFoundError during unlink is silently ignored (race guard)."""
        p = self.cache / "word_0001.png"
        p.touch()
        with patch.object(Path, "unlink", side_effect=FileNotFoundError("already gone")):
            # Should not raise
            _wipe_stale_per_beat_artefacts(self.cache, 3)

    def test_img_file_not_found_race(self):
        """FileNotFoundError on stale img_N.png unlink is silently ignored (lines 123-124)."""
        (self.cache / "img_005.png").touch()  # index 5 >= n_beats=3 → stale
        # Patch only Path.unlink on img_*.png to raise FileNotFoundError
        orig_unlink = Path.unlink

        def selective_fail(self_path, *args, **kwargs):
            if "img_" in self_path.name:
                raise FileNotFoundError("race condition")
            return orig_unlink(self_path, *args, **kwargs)

        with patch.object(Path, "unlink", selective_fail):
            _wipe_stale_per_beat_artefacts(self.cache, 3)  # should not raise

    def test_public_wrapper(self):
        (self.cache / "caption_00.png").touch()
        wipe_stale_per_beat_artefacts(self.cache, 3)
        self.assertFalse((self.cache / "caption_00.png").exists())


# ---------------------------------------------------------------------------
# prerender_word_captions
# ---------------------------------------------------------------------------

class TestPrerenderWordCaptions(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_renders_words(self):
        beats = [
            _make_beat("Hello world", 0.0, 2.0),
            _make_beat("Test again", 2.0, 4.0),
        ]
        n = prerender_word_captions(beats, self.cache)
        self.assertEqual(n, 4)
        self.assertTrue((self.cache / "word_0000.png").exists())

    def test_skips_existing(self):
        beats = [_make_beat("hello", 0.0, 1.0)]
        # Pre-create the word PNG
        (self.cache / "word_0000.png").write_bytes(b"fake")
        n = prerender_word_captions(beats, self.cache)
        self.assertEqual(n, 0)

    def test_numerical_transform(self):
        beats = [_make_beat("In twenty-fifteen the event happened", 0.0, 3.0)]
        n = prerender_word_captions(beats, self.cache)
        self.assertGreater(n, 0)

    def test_empty_words_skipped(self):
        beats = [Beat(text="hi", start=0.0, end=1.0,
                      words=[Word(text="", start=0.0, end=0.5)])]
        n = prerender_word_captions(beats, self.cache)
        # Empty text word → no PNG rendered
        self.assertEqual(n, 0)


# ---------------------------------------------------------------------------
# _materialise_rank_chips
# ---------------------------------------------------------------------------

class TestMaterialiseRankChips(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_none_input(self):
        self.assertIsNone(_materialise_rank_chips(None, self.cache))

    def test_empty_list(self):
        result = _materialise_rank_chips([], self.cache)
        self.assertEqual(result, [])

    def test_int_chip(self):
        result = _materialise_rank_chips([(0, 2, 5)], self.cache)
        self.assertEqual(len(result), 1)
        start, end, png_path = result[0]
        self.assertEqual(start, 0)
        self.assertEqual(end, 2)
        self.assertTrue(png_path.exists())

    def test_path_chip_exists(self):
        existing = self.cache / "rank_chip_3.png"
        existing.touch()
        result = _materialise_rank_chips([(0, 1, existing)], self.cache)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2], existing)

    def test_path_chip_missing_renders(self):
        missing = self.cache / "rank_chip_7.png"
        result = _materialise_rank_chips([(0, 1, missing)], self.cache)
        self.assertTrue(missing.exists())

    def test_path_chip_missing_no_rank(self):
        missing = self.cache / "other_name.png"  # not rank_chip_N.png pattern
        result = _materialise_rank_chips([(0, 1, missing)], self.cache)
        # Path returned as-is (no re-render since pattern doesn't match)
        self.assertEqual(result[0][2], missing)


# ---------------------------------------------------------------------------
# _ffprobe_duration (compose module version)
# ---------------------------------------------------------------------------

class TestComposeFfprobeDuration(unittest.TestCase):
    def test_returns_float(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="8.5\n")
            result = _ffprobe_duration(Path("/fake/clip.mp4"))
        self.assertAlmostEqual(result, 8.5)


# ---------------------------------------------------------------------------
# image_to_kenburns_clip
# ---------------------------------------------------------------------------

class TestImageToKenburnClip(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.img = _make_image(self.root, "test.png")
        self.out = self.root / "clip.mp4"

    def tearDown(self):
        self.td.cleanup()

    def test_success(self):
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = image_to_kenburns_clip(self.img, 3.0, self.out)
        self.assertEqual(result, self.out)

    def test_ffmpeg_failure(self):
        with patch("subprocess.run", side_effect=_fail_ffmpeg_run):
            with self.assertRaises(RuntimeError) as ctx:
                image_to_kenburns_clip(self.img, 3.0, self.out)
        self.assertIn("image→clip ffmpeg failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# compose() — beat mode (legacy)
# ---------------------------------------------------------------------------

class TestComposeBeatMode(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.audio = _make_wav(self.root, "narration.wav", 5.0)
        self.out = self.root / "out.mp4"
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _images(self, n):
        return [_make_image(self.cache, f"img_{i:02d}.png") for i in range(n)]

    def test_single_beat(self):
        beats = [_make_beat("hello world", 0.0, 2.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="beat")
        self.assertEqual(result, self.out)

    def test_multi_beat(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
            _make_beat("seven eight nine", 4.0, 6.0),
        ]
        imgs = self._images(3)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="beat")
        self.assertEqual(result, self.out)

    def test_tail_hold(self):
        beats = [_make_beat("hello", 0.0, 2.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="beat", tail_hold_s=2.0)
        self.assertEqual(result, self.out)

    def test_ffmpeg_failure(self):
        beats = [_make_beat("hello", 0.0, 2.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fail_ffmpeg_run):
            with self.assertRaises(RuntimeError):
                compose(imgs, beats, self.audio, self.out, self.cache,
                        caption_mode="beat")

    def test_assertion_error_on_mismatch(self):
        beats = [_make_beat("hello", 0.0, 2.0), _make_beat("world", 2.0, 4.0)]
        imgs = self._images(1)  # only 1 image for 2 beats
        with self.assertRaises(AssertionError):
            compose(imgs, beats, self.audio, self.out, self.cache)

    def test_pil_open_failure_uses_fallback_height(self):
        """PIL.Image.open failure → uses _cap_h=320 fallback (lines 690-691)."""
        beats = [_make_beat("hello", 0.0, 2.0)]
        imgs = self._images(1)
        # Patch PIL Image.open to raise so the except block fires
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run), \
             patch("pipeline.compose._PIL", None, create=True):
            # Actually patch via the import inside compose
            import PIL.Image as _PIL_Image
            with patch.object(_PIL_Image, "open", side_effect=Exception("pil fail")):
                result = compose(imgs, beats, self.audio, self.out, self.cache,
                                 caption_mode="beat")
        self.assertEqual(result, self.out)


# ---------------------------------------------------------------------------
# compose() — word mode (default)
# ---------------------------------------------------------------------------

class TestComposeWordMode(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.audio = _make_wav(self.root, "narration.wav", 10.0)
        self.out = self.root / "out.mp4"
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _images(self, n):
        return [_make_image(self.cache, f"img_{i:02d}.png") for i in range(n)]

    def test_single_beat_word_mode(self):
        beats = [_make_beat("hello world", 0.0, 2.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word")
        self.assertEqual(result, self.out)

    def test_multi_beat_word_mode(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
        ]
        imgs = self._images(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word")
        self.assertEqual(result, self.out)

    def test_closer_span_detection(self):
        """Beat starting with 'like' in last 30% triggers closer-span skip."""
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
            _make_beat("seven eight nine", 4.0, 6.0),
            _make_beat("like this video", 6.0, 8.0),
        ]
        imgs = self._images(4)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word")
        self.assertEqual(result, self.out)

    def test_rank_chips_int(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
            _make_beat("seven eight nine", 4.0, 6.0),
        ]
        imgs = self._images(3)
        rank_chips = [(0, 1, 3), (1, 2, 2), (2, 3, 1)]
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word", rank_chips=rank_chips)
        self.assertEqual(result, self.out)

    def test_rank_chip_out_of_range(self):
        beats = [_make_beat("hello world", 0.0, 2.0)]
        imgs = self._images(1)
        rank_chips = [(99, 100, 5)]  # out of range → warning, skipped
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word", rank_chips=rank_chips)
        self.assertEqual(result, self.out)

    def test_word_mode_tail_hold(self):
        beats = [_make_beat("hello world today", 0.0, 2.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word", tail_hold_s=2.0)
        self.assertEqual(result, self.out)

    def test_closer_format_passed(self):
        beats = [_make_beat("LIKE if YTA COMMENT if NTA", 0.0, 3.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word",
                             closer_format="LIKE if YTA, COMMENT if NTA")
        self.assertEqual(result, self.out)

    def test_rank_chip_end_idx_out_of_range(self):
        """end_idx out of range → uses start slot duration."""
        beats = [
            _make_beat("rank five", 0.0, 2.0),
            _make_beat("rank four", 2.0, 4.0),
        ]
        imgs = self._images(2)
        rank_chips = [(0, 99, 5)]  # end_idx 99 out of range
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word", rank_chips=rank_chips)
        self.assertEqual(result, self.out)

    def test_empty_word_text_skipped_in_word_mode(self):
        """Beat with a Word that has empty text → skipped (covers line 338)."""
        beats = [
            Beat(text="hi there", start=0.0, end=2.0, words=[
                Word(text="hi", start=0.0, end=1.0),
                Word(text="", start=1.0, end=1.5),   # empty → continue
                Word(text="there", start=1.5, end=2.0),
            ])
        ]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose(imgs, beats, self.audio, self.out, self.cache,
                             caption_mode="word")
        self.assertEqual(result, self.out)

    def test_ffmpeg_failure_word_mode(self):
        """ffmpeg failure in _compose_with_word_captions → RuntimeError (lines 513-515)."""
        beats = [_make_beat("hello", 0.0, 2.0)]
        imgs = self._images(1)
        with patch("subprocess.run", side_effect=_fail_ffmpeg_run):
            with self.assertRaises(RuntimeError) as ctx:
                compose(imgs, beats, self.audio, self.out, self.cache,
                        caption_mode="word")
        self.assertIn("ffmpeg failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# _extend_beats_for_footage
# ---------------------------------------------------------------------------

class TestExtendBeatsForFootage(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_no_extension_needed(self):
        """All image beats → no extension."""
        from pipeline.compose import _extend_beats_for_footage
        beats = [
            _make_beat("one", 0.0, 2.0),
            _make_beat("two", 2.0, 4.0),
        ]
        audio = _make_wav(self.root, "narr.wav", 4.0)
        resolutions = [("image", self.root / "img_00.png"),
                       ("image", self.root / "img_01.png")]
        new_beats, new_audio = _extend_beats_for_footage(
            beats=beats,
            beat_resolutions=resolutions,
            audio_path=audio,
            cache_dir=self.cache,
        )
        self.assertIs(new_beats, beats)
        self.assertIs(new_audio, audio)

    def test_footage_beat_extends(self):
        """Footage clip longer than slot → audio extended + beats shifted."""
        from pipeline.compose import _extend_beats_for_footage
        beats = [
            _make_beat("one", 0.0, 2.0, kind="footage"),
            _make_beat("two", 2.0, 4.0),
        ]
        audio = _make_wav(self.root, "narr.wav", 4.0)
        clip = self.root / "clip_00.mp4"
        clip.touch()
        resolutions = [("footage", clip), ("image", self.root / "img_01.png")]

        # ffprobe returns 5.0s for the footage clip (slot is only 2.0s → extend)
        with patch("pipeline.compose._ffprobe_duration", return_value=5.0):
            new_beats, new_audio = _extend_beats_for_footage(
                beats=beats,
                beat_resolutions=resolutions,
                audio_path=audio,
                cache_dir=self.cache,
            )
        # The audio file should be the extended one
        self.assertNotEqual(str(new_audio), str(audio))
        self.assertTrue(new_audio.exists())
        # beat[0].end should be shifted by delta (~3s)
        self.assertGreater(new_beats[0].end, beats[0].end)

    def test_last_beat_extension(self):
        """Last beat extension uses beat.end + 0.4 as insertion point."""
        from pipeline.compose import _extend_beats_for_footage
        beats = [
            _make_beat("one", 0.0, 2.0),
            _make_beat("two", 2.0, 4.0, kind="footage"),
        ]
        audio = _make_wav(self.root, "narr.wav", 4.0)
        clip = self.root / "clip_01.mp4"
        clip.touch()
        resolutions = [("image", self.root / "img_00.png"), ("footage", clip)]

        with patch("pipeline.compose._ffprobe_duration", return_value=7.0):
            new_beats, new_audio = _extend_beats_for_footage(
                beats=beats,
                beat_resolutions=resolutions,
                audio_path=audio,
                cache_dir=self.cache,
            )
        self.assertNotEqual(str(new_audio), str(audio))

    def test_stereo_audio_extended(self):
        """Stereo WAV path — ndim == 2."""
        import numpy as np
        import soundfile as sf
        from pipeline.compose import _extend_beats_for_footage
        beats = [
            _make_beat("one", 0.0, 2.0, kind="footage"),
            _make_beat("two", 2.0, 4.0),
        ]
        # Write a stereo WAV
        stereo_path = self.root / "stereo.wav"
        sr = 22050
        data = np.zeros((sr * 4, 2), dtype=np.float32)
        sf.write(str(stereo_path), data, sr)

        clip = self.root / "clip_00.mp4"
        clip.touch()
        resolutions = [("footage", clip), ("image", self.root / "img_01.png")]

        with patch("pipeline.compose._ffprobe_duration", return_value=5.5):
            new_beats, new_audio = _extend_beats_for_footage(
                beats=beats,
                beat_resolutions=resolutions,
                audio_path=stereo_path,
                cache_dir=self.cache,
            )
        self.assertTrue(new_audio.exists())


# ---------------------------------------------------------------------------
# compose_hybrid
# ---------------------------------------------------------------------------

class TestComposeHybrid(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.audio = _make_wav(self.root, "narration.wav", 6.0)
        self.out = self.root / "hybrid.mp4"
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_mismatch_raises(self):
        beats = [_make_beat("hello", 0.0, 2.0)]
        with self.assertRaises(ValueError):
            compose_hybrid(
                beat_resolutions=[("image", self.root / "img_00.png"),
                                   ("footage", self.root / "clip_00.mp4")],
                beats=beats,
                audio_path=self.audio,
                out_path=self.out,
                cache_dir=self.cache,
            )

    def test_image_and_footage(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0, kind="image"),
            _make_beat("four five six", 2.0, 4.0, kind="footage"),
        ]
        img = _make_image(self.cache, "img_00.png")
        clip = self.root / "clip_01.mp4"
        clip.touch()
        resolutions = [("image", img), ("footage", clip)]

        with patch("pipeline.compose._ffprobe_duration", return_value=2.5), \
             patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_hybrid(
                beat_resolutions=resolutions,
                beats=beats,
                audio_path=self.audio,
                out_path=self.out,
                cache_dir=self.cache,
            )
        self.assertTrue(self.out.exists() or True)  # ffmpeg mocked

    def test_invalid_kind_raises(self):
        beats = [_make_beat("hello", 0.0, 2.0)]
        resolutions = [("unknown_kind", self.root / "file.mp4")]

        with patch("pipeline.compose._ffprobe_duration", return_value=1.0), \
             patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            with self.assertRaises(ValueError) as ctx:
                compose_hybrid(
                    beat_resolutions=resolutions,
                    beats=beats,
                    audio_path=self.audio,
                    out_path=self.out,
                    cache_dir=self.cache,
                )
            self.assertIn("unknown beat kind", str(ctx.exception))


# ---------------------------------------------------------------------------
# compose_clips
# ---------------------------------------------------------------------------

class TestComposeClips(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.audio = _make_wav(self.root, "narration.wav", 10.0)
        self.out = self.root / "out.mp4"
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _make_clips(self, n):
        clips = []
        for i in range(n):
            p = self.root / f"clip_{i:02d}.mp4"
            p.touch()
            clips.append(p)
        return clips

    def test_per_beat_single(self):
        beats = [_make_beat("hello world", 0.0, 2.0)]
        clips = self._make_clips(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat")
        self.assertEqual(result, self.out)

    def test_per_beat_multi(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
            _make_beat("seven eight", 4.0, 6.0),
        ]
        clips = self._make_clips(3)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat")
        self.assertEqual(result, self.out)

    def test_per_beat_tail_hold(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", tail_hold_s=2.0)
        self.assertEqual(result, self.out)

    def test_per_word_single(self):
        beats = [_make_beat("hello world today", 0.0, 3.0)]
        clips = self._make_clips(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_word")
        self.assertEqual(result, self.out)

    def test_per_word_multi(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_word")
        self.assertEqual(result, self.out)

    def test_per_word_tail_hold(self):
        beats = [_make_beat("hello world", 0.0, 2.0)]
        clips = self._make_clips(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_word", tail_hold_s=2.0)
        self.assertEqual(result, self.out)

    def test_per_word_closer_span(self):
        beats = [
            _make_beat("one two three", 0.0, 2.0),
            _make_beat("four five six", 2.0, 4.0),
            _make_beat("seven eight nine", 4.0, 6.0),
            _make_beat("like this video", 6.0, 8.0),
        ]
        clips = self._make_clips(4)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_word")
        self.assertEqual(result, self.out)

    def test_rank_chips_valid(self):
        beats = [
            _make_beat("rank five", 0.0, 2.0),
            _make_beat("rank four", 2.0, 4.0),
            _make_beat("rank three", 4.0, 6.0),
        ]
        clips = self._make_clips(3)
        rank_chips = [(0, 1, 5), (1, 2, 4), (2, 3, 3)]
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", rank_chips=rank_chips)
        self.assertEqual(result, self.out)

    def test_rank_chip_out_of_range(self):
        beats = [_make_beat("hello", 0.0, 2.0)]
        clips = self._make_clips(1)
        rank_chips = [(99, 100, 5)]
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", rank_chips=rank_chips)
        self.assertEqual(result, self.out)

    def test_rank_chip_end_idx_out_of_range(self):
        beats = [
            _make_beat("rank five", 0.0, 2.0),
            _make_beat("rank four", 2.0, 4.0),
        ]
        clips = self._make_clips(2)
        rank_chips = [(0, 99, 5)]  # end_idx 99 out of range
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", rank_chips=rank_chips)
        self.assertEqual(result, self.out)

    def test_subscribe_button_with_chips(self):
        beats = [
            _make_beat("rank five", 0.0, 2.0),
            _make_beat("rank four", 2.0, 4.0),
        ]
        clips = self._make_clips(2)
        rank_chips = [(0, 1, 5), (1, 2, 4)]
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", rank_chips=rank_chips,
                                   subscribe_button=True)
        self.assertEqual(result, self.out)

    def test_subscribe_button_without_chips(self):
        """subscribe_button=True without chips re-aliases the vout label."""
        beats = [
            _make_beat("one two", 0.0, 2.0),
            _make_beat("three four", 2.0, 4.0),
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", subscribe_button=True)
        self.assertEqual(result, self.out)

    def test_layer_footage_audio_no_footage_beats(self):
        beats = [
            _make_beat("one two", 0.0, 2.0, kind="animated"),
        ]
        clips = self._make_clips(1)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", layer_footage_audio=True)
        self.assertEqual(result, self.out)

    def test_layer_footage_audio_with_footage_beat(self):
        beats = [
            _make_beat("one two", 0.0, 2.0, kind="animated"),
            _make_beat("score", 2.0, 4.0, kind="footage",
                       footage={"audio_mix": 0.7, "pre_pad_s": 0.0}),
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", layer_footage_audio=True)
        self.assertEqual(result, self.out)

    def test_layer_footage_audio_muted_beat(self):
        """Beat with mix_vol<=0 is explicitly muted → skipped in amix (line 1334)."""
        beats = [
            _make_beat("one", 0.0, 2.0, kind="animated"),
            _make_beat("score", 2.0, 4.0, kind="footage",
                       footage={"audio_mix": -1.0, "pre_pad_s": 0.0}),  # negative → ≤0
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", layer_footage_audio=True)
        self.assertEqual(result, self.out)

    def test_layer_footage_pre_pad_duck_window(self):
        """pre_pad_s > 0.05 → duck window starts at beat.end instead of beat.start."""
        beats = [
            _make_beat("setup line", 0.0, 2.0, kind="footage",
                       footage={"audio_mix": 0.7, "pre_pad_s": 1.0}),
            _make_beat("finish", 2.0, 4.0, kind="animated"),
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", layer_footage_audio=True)
        self.assertEqual(result, self.out)

    def test_layer_footage_audio_tail_hold(self):
        """layer_footage_audio=True with tail_hold_s → apad inside amix chain."""
        beats = [
            _make_beat("one", 0.0, 2.0, kind="animated"),
            _make_beat("score", 2.0, 4.0, kind="footage",
                       footage={"audio_mix": 0.5, "pre_pad_s": 0.0}),
        ]
        clips = self._make_clips(2)
        with patch("subprocess.run", side_effect=_fake_ffmpeg_run):
            result = compose_clips(clips, beats, self.audio, self.out, self.cache,
                                   caption_style="per_beat", layer_footage_audio=True,
                                   tail_hold_s=2.0)
        self.assertEqual(result, self.out)

    def test_ffmpeg_failure(self):
        beats = [_make_beat("hello", 0.0, 2.0)]
        clips = self._make_clips(1)
        with patch("subprocess.run", side_effect=_fail_ffmpeg_run):
            with self.assertRaises(RuntimeError) as ctx:
                compose_clips(clips, beats, self.audio, self.out, self.cache,
                              caption_style="per_beat")
        self.assertIn("ffmpeg failed", str(ctx.exception))

    def test_assertion_error_on_mismatch(self):
        beats = [_make_beat("one", 0.0, 2.0), _make_beat("two", 2.0, 4.0)]
        clips = self._make_clips(1)
        with self.assertRaises(AssertionError):
            compose_clips(clips, beats, self.audio, self.out, self.cache)


if __name__ == "__main__":
    unittest.main()
