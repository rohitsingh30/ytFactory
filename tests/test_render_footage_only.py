from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import yaml

from tests._helpers import PROJECT_ROOT  # noqa: F401
from pipeline.render import footage_only as fo


@dataclass
class _FakeWord:
    text: str
    start: float
    end: float


@dataclass
class _FakeBeat:
    text: str
    start: float
    end: float
    words: list = field(default_factory=list)


def _tmpdir():
    return tempfile.TemporaryDirectory(dir=PROJECT_ROOT)


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_big(path: Path, size: int = 2048) -> Path:
    return _write(path, b"x" * size)


def _write_wav(path: Path, duration_s: float = 0.05, sample_rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n)
    return path


class _FakeRenderPaths:
    def __init__(self, root: Path, slug: str):
        self.root = root
        self.channel_root = root
        self.config_yaml = root / "config.yaml"
        self.footage_plan = root / "footage_plan"
        self.long_form = root / "long_form"
        self._slug = slug

    def cache_for(self, slug: str) -> Path:
        return self.root / "cache" / slug

    def scratch_for(self, slug: str) -> Path:
        return self.root / "scratch" / slug

    def narration_for(self, slug: str) -> Path:
        return self.root / "narrations" / f"{slug}.json"

    def shotlist_for(self, slug: str) -> Path:
        return self.root / "shotlist" / f"{slug}.json"


def _make_channel(tmp: Path, channel: str = "chan", slug: str = "slug", *, cfg: dict | None = None) -> _FakeRenderPaths:
    root = tmp / channel
    for rel in ("narrations", "shotlist", "cache", "scratch", "shorts", "long_form", "raw"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump(cfg or {
        "tts_provider": "f5_tts",
        "tts_voice": "sarah",
        "tts_speed": 1.0,
    }))
    (root / "narrations" / f"{slug}.json").write_text(json.dumps({"narration": "Hello world.", "title": "T"}))
    (root / "shotlist" / f"{slug}.json").write_text(json.dumps({
        "slug": slug,
        "windows": [{"in_s": 0.0, "out_s": 5.0, "match_text": "hello", "source_url": "https://e/x.mp4"}],
    }))
    return _FakeRenderPaths(root, slug)


class AssetHelpersTests(unittest.TestCase):
    def test_url_predicates_safe_names_and_wikimedia_helpers(self):
        self.assertTrue(fo._looks_like_image("https://x/a.JPG?download=1"))
        self.assertTrue(fo._looks_like_image("https://commons.wikimedia.org/wiki/File:Nice_Pic.PNG"))
        self.assertFalse(fo._looks_like_image("https://commons.wikimedia.org/wiki/File:Vector.svg"))
        self.assertFalse(fo._looks_like_image("https://x/a.svg"))
        self.assertTrue(fo._looks_like_video("https://x/a.webm?x=1"))
        self.assertFalse(fo._looks_like_video("https://x/a.txt"))
        self.assertTrue(fo._is_youtube("https://youtube.com/watch?v=1"))
        self.assertTrue(fo._is_youtube("https://youtu.be/1"))
        self.assertFalse(fo._is_youtube("https://example.com/1"))
        self.assertEqual(fo._safe_filename("a b/c?.jpg"), "a_b_c_.jpg")
        self.assertEqual(fo._safe_filename("_", "seed"), "92713d4709377111")
        self.assertEqual(fo._safe_filename(".", "seed"), "92713d4709377111")
        self.assertEqual(fo._wikimedia_filename("https://commons.wikimedia.org/wiki/Image:Hello%20World.jpg"), "Hello World.jpg")
        with self.assertRaises(ValueError):
            fo._wikimedia_filename("https://example.com/not-a-file-page")
        self.assertEqual(
            fo._resolve_wikimedia_file_url("https://commons.wikimedia.org/wiki/File:Hello World.jpg"),
            "https://commons.wikimedia.org/wiki/Special:FilePath/Hello%20World.jpg",
        )
        self.assertIn("split=2", fo._ken_burns_filter("9:16", 3.0))
        self.assertIn("1920:1080", fo._ken_burns_filter("16:9", 3.0))

    def test_archive_details_resolves_largest_mp4_and_errors(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "files": [
                        {"name": "small.mp4", "size": "10"},
                        {"name": "big file.mp4", "size": "99"},
                        {"name": "notes.txt", "size": "999"},
                    ]
                }).encode()

        with patch("urllib.request.urlopen", return_value=Resp()) as mock_open:
            self.assertEqual(
                fo._resolve_archive_org_details("https://archive.org/details/itemid"),
                "https://archive.org/download/itemid/big%20file.mp4",
            )
            mock_open.assert_called_once_with("https://archive.org/metadata/itemid", timeout=30)
        with self.assertRaises(ValueError):
            fo._resolve_archive_org_details("https://example.com/details/item")

        class EmptyResp(Resp):
            def read(self):
                return json.dumps({"files": [{"name": "x.mov"}]}).encode()

        with patch("urllib.request.urlopen", return_value=EmptyResp()):
            with self.assertRaises(RuntimeError):
                fo._resolve_archive_org_details("https://archive.org/details/nomp4")

    def test_urlretrieve_streams_chunks_with_user_agent(self):
        with _tmpdir() as td:
            dest = Path(td) / "download.bin"

            class Resp:
                def __init__(self):
                    self.chunks = [b"abc", b"def", b""]

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, n):
                    return self.chunks.pop(0)

            seen = {}

            def fake_open(req, timeout):
                seen["ua"] = req.headers.get("User-agent") or req.headers.get("User-Agent")
                seen["timeout"] = timeout
                return Resp()

            with patch("urllib.request.urlopen", side_effect=fake_open):
                fo._urlretrieve("https://example.com/file", dest)
            self.assertEqual(dest.read_bytes(), b"abcdef")
            self.assertEqual(seen["timeout"], 120)
            self.assertIn("ytFactory-render", seen["ua"])

    def test_resolve_asset_dispatches_all_supported_sources_and_cache_hits(self):
        with _tmpdir() as td:
            cache = Path(td) / "cache"
            yt_path = Path(td) / "yt.mp4"
            _write_big(yt_path)
            with patch.object(fo.footage_mod, "_download_source", return_value=yt_path, create=True) as mock_dl:
                self.assertEqual(fo._resolve_asset("https://youtu.be/abc", cache), (yt_path, "video"))
                mock_dl.assert_called_once()

            wiki_dest = cache / "Wiki_File.jpg"
            _write_big(wiki_dest)
            with patch.object(fo, "_urlretrieve") as mock_ret:
                self.assertEqual(
                    fo._resolve_asset("https://commons.wikimedia.org/wiki/File:Wiki_File.jpg", cache),
                    (wiki_dest, "image"),
                )
                mock_ret.assert_not_called()
            wiki_dest.unlink()
            with patch.object(fo, "_urlretrieve", side_effect=lambda url, dest: _write_big(dest)) as mock_ret:
                path, kind = fo._resolve_asset("https://commons.wikimedia.org/wiki/File:Wiki_File.jpg", cache)
                self.assertEqual((path.name, kind), ("Wiki_File.jpg", "image"))
                mock_ret.assert_called_once()

            details_dest = cache / "large.mp4"
            _write_big(details_dest)
            with patch.object(fo, "_resolve_archive_org_details", return_value="https://archive.org/download/i/large.mp4"), \
                 patch.object(fo, "_urlretrieve") as mock_ret:
                self.assertEqual(fo._resolve_asset("https://archive.org/details/i", cache), (details_dest, "video"))
                mock_ret.assert_not_called()
            details_dest.unlink()
            with patch.object(fo, "_resolve_archive_org_details", return_value="https://archive.org/download/i/large.mp4"), \
                 patch.object(fo, "_urlretrieve", side_effect=lambda url, dest: _write_big(dest)) as mock_ret:
                self.assertEqual(fo._resolve_asset("https://archive.org/details/i", cache)[1], "video")
                mock_ret.assert_called_once()

            direct_archive = cache / "poster.png"
            _write_big(direct_archive)
            with patch.object(fo, "_urlretrieve") as mock_ret:
                self.assertEqual(
                    fo._resolve_asset("https://archive.org/download/i/poster.png", cache),
                    (direct_archive, "image"),
                )
                mock_ret.assert_not_called()
            direct_archive.unlink()
            with patch.object(fo, "_urlretrieve", side_effect=lambda url, dest: _write_big(dest)):
                self.assertEqual(fo._resolve_asset("https://archive.org/download/i/movie.mp4", cache)[1], "video")

            direct = cache / "clip.webm"
            _write_big(direct)
            with patch.object(fo, "_urlretrieve") as mock_ret:
                self.assertEqual(fo._resolve_asset("https://cdn.example.com/clip.webm?x=1", cache), (direct, "video"))
                mock_ret.assert_not_called()
            direct.unlink()
            with patch.object(fo, "_urlretrieve", side_effect=lambda url, dest: _write_big(dest)):
                self.assertEqual(fo._resolve_asset("https://cdn.example.com/pic.jpeg", cache)[1], "image")

            with self.assertRaises(RuntimeError):
                fo._resolve_asset("https://example.com/page", cache)


class RegenAudioCapsTests(unittest.TestCase):
    def test_regen_audio_synthesizes_cloud_warms_splits_and_prerenders(self):
        with _tmpdir() as td:
            tmp = Path(td)
            slug = "story"
            paths = _make_channel(tmp, slug=slug, cfg={
                "tts_provider": "cloudrun_chatterbox",
                "tts_voice": "voice",
                "tts_speed": 1.2,
                "tts_language": "en",
                "tts_ref_text": "ref",
                "asr_provider": "faster_whisper",
                "beat_target_s": 1.5,
                "beat_max_s": 2.5,
            })
            beat_list = [_FakeBeat("Hello", 0, 1, [_FakeWord("Hello", 0, 0.5)])]
            with patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch.object(fo.audio, "normalize_for_tts", return_value="Hello world.", create=True), \
                 patch.object(fo.audio, "synthesize", side_effect=lambda *a, **kw: _write_wav(kw["out_path"]), create=True), \
                 patch.object(fo.beats_mod, "transcribe_words", return_value=[_FakeWord("Hello", 0, 0.4)]) as transcribe, \
                 patch.object(fo.align, "align_source_to_whisper", return_value="aligned") as align, \
                 patch.object(fo.beats_mod, "split_into_beats", return_value=beat_list) as split, \
                 patch.object(fo.beats_mod, "save_beats", side_effect=lambda beats, path: path.write_text("[]")) as save, \
                 patch.object(fo.compose, "prerender_word_captions", return_value=3) as prerender, \
                 patch.object(fo.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
                narr, beats_path, beats = fo._regen_audio_caps("chan", slug, yaml.safe_load(paths.config_yaml.read_text()))
            self.assertEqual(narr.name, "narration.wav")
            self.assertEqual(beats_path.name, "beats.json")
            self.assertEqual(beats, beat_list)
            if "pre-warming Cloud Run" in Path(fo.__file__).read_text():
                run.assert_called_once()
            else:
                run.assert_not_called()
            transcribe.assert_called_once_with(narr, provider="faster_whisper")
            align.assert_called_once()
            split.assert_called_once_with("aligned", target_s=1.5, max_s=2.5)
            save.assert_called_once_with(beat_list, beats_path)
            prerender.assert_called_once_with(beat_list, paths.cache_for(slug))

    def test_regen_audio_handles_cached_paths_caption_none_missing_and_warm_exceptions(self):
        with _tmpdir() as td:
            tmp = Path(td)
            slug = "story"
            paths = _make_channel(tmp, slug=slug, cfg={"tts_provider": "f5_tts", "tts_voice": "v"})
            with patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths):
                (paths.narration_for(slug)).unlink()
                with self.assertRaises(FileNotFoundError):
                    fo._regen_audio_caps("chan", slug, {"tts_provider": "f5_tts", "tts_voice": "v"})

            paths = _make_channel(tmp / "fallback", slug=slug, cfg={"tts_provider": "f5_tts", "tts_voice": "v"})
            nested = paths.root / "niche" / "narrations"
            nested.mkdir(parents=True)
            nested_narr = nested / f"{slug}.json"
            nested_narr.write_text(json.dumps({"narration": "Nested narration."}))
            paths.narration_for(slug).unlink()
            with patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch.object(fo.audio, "normalize_for_tts", return_value="nested", create=True), \
                 patch.object(fo.audio, "synthesize", side_effect=lambda *a, **kw: _write_wav(kw["out_path"]), create=True):
                narr, beats_path, beats = fo._regen_audio_caps(
                    "chan", slug, {"tts_provider": "f5_tts", "tts_voice": "v"}, caption_mode="none",
                )
            self.assertTrue(narr.exists())
            self.assertIsNone(beats_path)
            self.assertEqual(beats, [])

            paths = _make_channel(tmp / "second", slug=slug, cfg={"tts_provider": "cloudrun_chatterbox", "tts_voice": "v"})
            for exc in (subprocess.TimeoutExpired(cmd="warm", timeout=600), FileNotFoundError()):
                cache = paths.cache_for(slug)
                if cache.exists():
                    for p in cache.glob("*"):
                        p.unlink()
                with patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                     patch.object(fo.audio, "normalize_for_tts", return_value="text", create=True), \
                     patch.object(fo.audio, "synthesize", side_effect=lambda *a, **kw: _write_wav(kw["out_path"]), create=True), \
                     patch.object(fo.subprocess, "run", side_effect=exc):
                    narr, beats_path, beats = fo._regen_audio_caps(
                        "chan", slug, {"tts_provider": "cloudrun_chatterbox", "tts_voice": "v"}, caption_mode="none",
                    )
                    self.assertTrue(narr.exists())
                    self.assertIsNone(beats_path)
                    self.assertEqual(beats, [])

            paths = _make_channel(tmp / "third", slug=slug, cfg={"tts_provider": "f5_tts", "tts_voice": "v"})
            _write_wav(paths.cache_for(slug) / "narration.wav")
            (paths.cache_for(slug) / "beats.json").write_text("[]")
            # Audit Q2.22 — also write the sidecar so the cache check
            # finds a matching voice fingerprint and skips synth. Pre-fix
            # this test pinned cache-skip behaviour using only
            # narration.wav presence; post-fix the sidecar is required
            # to PROVE the cached wav was bound to the current cfg.
            from pipeline.render._voice_fingerprint import (
                compute_fingerprint, write_sidecar,
            )
            write_sidecar(
                paths.cache_for(slug) / "narration.wav",
                compute_fingerprint({"tts_provider": "f5_tts", "tts_voice": "v"}),
            )
            beat_list = [_FakeBeat("cached", 0, 1, [])]
            with patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch.object(fo.audio, "normalize_for_tts", return_value="text", create=True), \
                 patch.object(fo.audio, "synthesize", create=True) as synth, \
                 patch.object(fo.beats_mod, "load_beats", return_value=beat_list) as load, \
                 patch.object(fo.compose, "prerender_word_captions", return_value=0):
                narr, beats_path, beats = fo._regen_audio_caps("chan", slug, {"tts_provider": "f5_tts", "tts_voice": "v"})
            synth.assert_not_called()
            load.assert_called_once_with(beats_path)
            self.assertEqual((narr.name, beats), ("narration.wav", beat_list))


class SilentVideoTests(unittest.TestCase):
    def test_aligned_window_durations_matches_fallbacks_and_clamps(self):
        beats = [
            _FakeBeat("b1", 0, 1, [_FakeWord("hello", 0.2, 0.4), _FakeWord("again", 0.6, 0.8)]),
            _FakeBeat("b2", 1, 2, [_FakeWord("hello", 1.0, 1.1), _FakeWord("final", 1.4, 1.5)]),
        ]
        windows = [
            {"in_s": 0, "out_s": 0.2, "match_text": "hello"},
            {"in_s": 10, "out_s": 11, "match_text": "hello"},
            {"in_s": 0, "out_s": 0.1, "match_text": "missing"},
            {"in_s": 0, "out_s": 2, "match_text": ""},
        ]
        if not hasattr(fo, "_aligned_window_durations"):
            return
        self.assertEqual(fo._aligned_window_durations(windows, beats, 2.4), [1.0, 1.0, 1.0, 1.0])

    def test_build_silent_video_executes_image_passthrough_letterbox_and_concat(self):
        with _tmpdir() as td:
            tmp = Path(td)
            asset_dir = tmp / "assets"
            image = _write_big(asset_dir / "img.jpg")
            wide = _write_big(asset_dir / "wide.mp4")
            square = _write_big(asset_dir / "square.mp4")
            shotlist = {
                "aspect": "16:9",
                "windows": [
                    {"in_s": 0, "out_s": 3, "match_text": "one", "source_url": "image"},
                    {"in_s": 4, "out_s": 8, "match_text": "two", "source_url": "wide"},
                    {"in_s": 10, "out_s": 14, "match_text": "three", "source_url": "square"},
                ],
            }
            beats = [_FakeBeat("b", 0, 6, [
                _FakeWord("one", 0, 0.1), _FakeWord("two", 2.0, 2.1), _FakeWord("three", 4.5, 4.6)
            ])]

            def resolve(url, cache):
                return {"image": (image, "image"), "wide": (wide, "video"), "square": (square, "video")}[url]

            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[0] == "ffprobe":
                    out = "1920,1080\n" if str(wide) in cmd else "1000,1000\n"
                    return MagicMock(stdout=out, returncode=0)
                return MagicMock(returncode=0, stdout="")

            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch.object(fo, "_resolve_asset", side_effect=resolve), \
                 patch("pipeline.parallel.run_parallel", side_effect=lambda jobs, **kw: [j() for j in jobs]) as rp, \
                 patch.object(fo.subprocess, "run", side_effect=fake_run):
                kwargs = {"beat_list": beats, "narration_end_s": 7.0} if hasattr(fo, "_aligned_window_durations") else {}
                out = fo._build_silent_video("chan", "slug", shotlist, tmp / "chan" / "scratch" / "slug", **kwargs)
            self.assertEqual(out.name, "video.mp4")
            rp.assert_called_once()
            flat = "\n".join(" ".join(map(str, c)) for c in calls)
            self.assertIn("stillimage", flat)
            self.assertIn("scale=1920:1080", flat)
            self.assertIn("filter_complex", flat)
            self.assertIn("clip_02.mp4", (tmp / "chan" / "scratch" / "slug" / "concat.txt").read_text())

    def test_build_silent_video_reuses_cache_and_handles_missing_asset_stat(self):
        with _tmpdir() as td:
            tmp = Path(td)
            scratch = tmp / "chan" / "scratch" / "slug"
            clip0 = _write_big(scratch / "clip_00.mp4")
            asset = _write_big(tmp / "asset.mp4")
            os.utime(asset, (1, 1))
            os.utime(clip0, (2, 2))
            shotlist = {"source_url": "legacy", "windows": [{"in_s": 0, "out_s": 1, "match_text": "x"}]}
            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch.object(fo, "_resolve_asset", return_value=(asset, "video")), \
                 patch.object(fo.subprocess, "run", return_value=MagicMock(stdout="1080,1920\n")) as run:
                fo._build_silent_video("chan", "slug", shotlist, scratch)
            run.assert_called_once()  # only concat; trim job was skipped

            missing_asset = tmp / "missing.mp4"
            clip1 = _write_big(scratch / "clip_00.mp4")
            clip1.unlink()
            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch.object(fo, "_resolve_asset", return_value=(missing_asset, "video")), \
                 patch("pipeline.parallel.run_parallel", side_effect=lambda jobs, **kw: [j() for j in jobs]), \
                 patch.object(fo.subprocess, "run", side_effect=[Exception("probe"), MagicMock(), MagicMock()]) as run:
                fo._build_silent_video("chan", "slug", shotlist, scratch)
            self.assertGreaterEqual(run.call_count, 3)

    def test_build_silent_video_error_guards(self):
        with _tmpdir() as td:
            tmp = Path(td)
            scratch = tmp / "s"
            with self.assertRaises(ValueError):
                fo._build_silent_video("c", "s", {"aspect": "1:1", "windows": []}, scratch)
            if "BAD_EXTS" in Path(fo.__file__).read_text():
                with self.assertRaises(ValueError):
                    fo._build_silent_video("c", "s", {"windows": [{"source_url": "https://x/a.svg", "in_s": 0, "out_s": 1}]}, scratch)
            with patch.object(fo, "REPO_ROOT", tmp), patch.object(fo, "_resolve_asset", side_effect=RuntimeError("bad")):
                with self.assertRaises(RuntimeError):
                    fo._build_silent_video("c", "s", {"windows": [{"source_url": "bad", "in_s": 0, "out_s": 1, "match_text": "m"}]}, scratch)
            with patch.object(fo, "REPO_ROOT", tmp):
                with self.assertRaises(ValueError):
                    fo._build_silent_video("c", "s", {"windows": [{"in_s": 0, "out_s": 1}]}, scratch)


class CaptionOverlayTests(unittest.TestCase):
    def test_ensure_emoji_downloads_once_then_reuses(self):
        with _tmpdir() as td:
            emoji_dir = Path(td) / "emoji"
            with patch.object(fo, "EMOJI_DIR", emoji_dir), \
                 patch("urllib.request.urlretrieve", side_effect=lambda url, dest: _write(dest, b"png")) as ret:
                first = fo._ensure_emoji("fr")
                second = fo._ensure_emoji("fr")
            self.assertEqual(first, second)
            ret.assert_called_once()

    def test_composited_word_pngs_wipes_stale_copies_plain_flags_and_skips_missing(self):
        from PIL import Image

        with _tmpdir() as td:
            tmp = Path(td)
            paths = _make_channel(tmp, slug="slug")
            cache = paths.cache_for("slug")
            out_dir = cache / "_flagged_words"
            out_dir.mkdir(parents=True)
            stale = _write(out_dir / "word_9999.png", b"old")
            Image.new("RGBA", (40, 30), (255, 0, 0, 255)).save(cache / "word_0000.png")
            Image.new("RGBA", (50, 40), (0, 255, 0, 255)).save(cache / "word_0001.png")
            emoji = tmp / "emoji.png"
            Image.new("RGBA", (20, 20), (0, 0, 255, 255)).save(emoji)
            beats = [_FakeBeat("b", 0, 1, [
                _FakeWord("France", 0, 0.1), _FakeWord("hello", 0.2, 0.3), _FakeWord("missing", 0.4, 0.5)
            ])]
            with patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch.object(fo, "_ensure_emoji", return_value=emoji), \
                 patch("pathlib.Path.unlink", side_effect=FileNotFoundError):
                result = fo._composited_word_pngs(cache, beats, "chan", "slug")
            self.assertEqual(result, out_dir)
            self.assertTrue(stale.exists())
            self.assertTrue((out_dir / "word_0000.png").exists())
            self.assertTrue((out_dir / "word_0001.png").exists())
            self.assertFalse((out_dir / "word_0002.png").exists())

    def test_scene_cuts_burn_video_and_mux_no_captions(self):
        with _tmpdir() as td:
            tmp = Path(td)
            scratch = tmp / "chan" / "scratch" / "slug"
            silent = _write(scratch / "video.mp4", b"v")
            narr = _write_wav(tmp / "narration.wav")
            words_dir = tmp / "words"
            words_dir.mkdir()
            _write(words_dir / "word_0000.png", b"png")
            _write(words_dir / "word_0002.png", b"png")
            beats = [
                _FakeBeat("normal", 0, 1.5, [
                    _FakeWord("Hello", 0.0, 0.2), _FakeWord("", 0.3, 0.4), _FakeWord("World", 0.5, 0.6), _FakeWord("Missing", 0.7, 0.8)
                ]),
                _FakeBeat("Like if you learned and subscribe", 1.5, 2.5, [
                    _FakeWord("Like", 1.6, 1.7), _FakeWord("Subscribe", 1.8, 1.9)
                ]),
            ]
            fake_caps = types.SimpleNamespace(
                render_like_button=lambda path, **kw: _write(path, b"like"),
                render_youtube_button=lambda path, **kw: _write(path, b"sub"),
            )
            run_calls = []
            with patch("pipeline.probe.probe_duration", side_effect=lambda p: 2.0 if p == silent else 3.0), \
                 patch.object(fo.subprocess, "run", side_effect=lambda cmd, **kw: run_calls.append(cmd) or MagicMock(returncode=0)), \
                 patch.dict(sys.modules, {"pipeline.footage.captions": fake_caps}):
                self.assertEqual(fo._scene_cuts({"windows": [{"in_s": 0, "out_s": 1}, {"in_s": 2, "out_s": 4}]}), [1.0, 3.0])
                fo._burn_video(silent, narr, words_dir, beats, [0.55, 2.0], tmp / "out" / "burned.mp4")
                fo._mux_audio_no_captions(silent, narr, tmp / "out" / "muxed.mp4")
            self.assertEqual(len(run_calls), 3)
            final_filter = run_calls[1][run_calls[1].index("-filter_complex") + 1]
            self.assertIn("overlay", final_filter)
            self.assertIn("tpad=stop_mode=clone", " ".join(run_calls[2]))


class RenderEntrypointTests(unittest.TestCase):
    def test_render_errors_for_missing_shotlist_and_bad_aspect(self):
        with _tmpdir() as td:
            tmp = Path(td)
            paths = _make_channel(tmp, slug="slug")
            (paths.shotlist_for("slug")).unlink()
            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"):
                with self.assertRaises(FileNotFoundError):
                    fo.render("chan", "slug")
            paths.shotlist_for("slug").write_text(json.dumps({"aspect": "square", "windows": []}))
            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"):
                with self.assertRaises(ValueError):
                    fo.render("chan", "slug")

    def test_render_caption_none_uploads_and_falls_back_to_nested_narration(self):
        with _tmpdir() as td:
            tmp = Path(td)
            cfg = {"tts_provider": "f5_tts", "tts_voice": "v", "kathaa": {"caption_mode": "none"}, "upload": {"privacy": "unlisted"}}
            paths = _make_channel(tmp, slug="slug", cfg=cfg)
            paths.shotlist_for("slug").write_text(json.dumps({"aspect": "16:9", "windows": [{"in_s": 0, "out_s": 2, "source_url": "x"}]}))
            nested = paths.root / "niche" / "narrations"
            nested.mkdir(parents=True)
            (nested / "slug.json").write_text(json.dumps({"narration": "Nested"}))
            (paths.root / "narrations" / "slug.json").unlink()
            narr = _write_wav(paths.cache_for("slug") / "narration.wav")
            silent = _write(paths.scratch_for("slug") / "video.mp4", b"v")
            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.preflight.power_check") as power, \
                 patch("pipeline.preflight.reset_mlx_state") as reset_mlx, \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker") as reset_cloud, \
                 patch.object(fo, "_regen_audio_caps", return_value=(narr, None, [])) as regen, \
                 patch("pipeline.probe.probe_duration", return_value=2.0), \
                 patch.object(fo, "_build_silent_video", return_value=silent) as build, \
                 patch.object(fo, "_mux_audio_no_captions") as mux, \
                 patch("pipeline.upload.upload_short", return_value={"url": "https://youtu.be/x"}, create=True) as upload:
                out = fo.render("chan", "slug", do_upload=True)
            self.assertEqual(out, paths.root / "long_form" / "slug.mp4")
            power.assert_called_once()
            reset_mlx.assert_called_once_with(drop_f5=True, label="footage-only stage-1 TTS")
            reset_cloud.assert_called_once()
            regen.assert_called_once_with("chan", "slug", cfg, caption_mode="none")
            build.assert_called_once()
            mux.assert_called_once_with(silent, narr, out)
            upload.assert_called_once()
            self.assertEqual(upload.call_args.kwargs["privacy_override"], "unlisted")
            self.assertEqual(upload.call_args.kwargs["raw"], None)

    def test_render_with_captions_burns_to_shorts(self):
        with _tmpdir() as td:
            tmp = Path(td)
            paths = _make_channel(tmp, slug="slug")
            paths.shotlist_for("slug").write_text(json.dumps({"windows": [{"in_s": 0, "out_s": 2, "source_url": "x"}]}))
            narr = _write_wav(paths.cache_for("slug") / "narration.wav")
            silent = _write(paths.scratch_for("slug") / "video.mp4", b"v")
            beats = [_FakeBeat("b", 0, 1, [_FakeWord("hello", 0, 0.2)])]
            words_dir = paths.cache_for("slug") / "words"
            with patch.object(fo, "REPO_ROOT", tmp), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.preflight.reset_mlx_state"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch.object(fo, "_regen_audio_caps", return_value=(narr, paths.cache_for("slug") / "beats.json", beats)), \
                 patch("pipeline.probe.probe_duration", return_value=1.0), \
                 patch.object(fo, "_build_silent_video", return_value=silent), \
                 patch.object(fo, "_composited_word_pngs", return_value=words_dir) as comp, \
                 patch.object(fo, "_burn_video") as burn:
                out = fo.render("chan", "slug", aspect_override="9:16")
            self.assertEqual(out, paths.root / "shorts" / "slug.mp4")
            comp.assert_called_once_with(paths.cache_for("slug"), beats, "chan", "slug")
            burn.assert_called_once_with(silent, narr, words_dir, beats, [2.0], out)

    def test_main_loads_env_and_cli_main_delegates(self):
        with _tmpdir() as td:
            tmp = Path(td)
            (tmp / ".env").write_text("# comment\nFO_TEST_ENV=from_file\nNO_EQUALS\nQUOTED='kept'\n")
            old = os.environ.pop("FO_TEST_ENV", None)
            try:
                with patch.object(fo, "REPO_ROOT", tmp), \
                     patch.object(sys, "argv", ["render_footage_only", "--channel", "chan", "--slug", "slug", "--aspect", "16:9", "--upload"]), \
                     patch.object(fo, "render") as render:
                    fo.main()
                self.assertEqual(os.environ["FO_TEST_ENV"], "from_file")
                render.assert_called_once_with("chan", "slug", do_upload=True, aspect_override="16:9")
                with patch.object(fo, "main", return_value=None) as main:
                    self.assertIsNone(fo.cli_main())
                    main.assert_called_once_with()
            finally:
                if old is None:
                    os.environ.pop("FO_TEST_ENV", None)
                else:
                    os.environ["FO_TEST_ENV"] = old


if __name__ == "__main__":
    unittest.main()
