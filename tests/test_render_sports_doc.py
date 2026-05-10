from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from tests._helpers import PROJECT_ROOT  # noqa: F401
from pipeline.render import sports_doc as sd


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


def _write_big(path: Path, size: int = 120 * 1024) -> Path:
    return _write(path, b"x" * size)


class _FakeRenderPaths:
    def __init__(self, root: Path, slug: str, config: dict):
        self.root = root
        self.config_yaml = root / "config.yaml"
        self.footage_plan = root / "footage_plan"
        self.long_form = root / "long_form"
        self._slug = slug
        root.mkdir(parents=True, exist_ok=True)
        self.config_yaml.write_text(yaml.safe_dump(config))
        for rel in ("narrations", "footage_plan", "cache", "long_form", "branding", "music", "footage/sources"):
            (root / rel).mkdir(parents=True, exist_ok=True)

    def narration_for(self, slug: str) -> Path:
        return self.root / "narrations" / f"{slug}.json"

    def cache_for(self, slug: str) -> Path:
        return self.root / "cache" / slug


def _make_paths(tmp: Path, *, slug: str = "doc", lf: dict | None = None) -> _FakeRenderPaths:
    base_lf = {
        "tts_provider": "cloudrun_chatterbox",
        "tts_voice": "voice",
        "tts_ref_text": "ref",
        "tts_chunk_target_chars": 120,
        "tts_chunk_join_silence_s": 0.2,
        "output_resolution": [1920, 1080],
        "output_fps": 30,
        "captions_enabled": True,
        "watermark": {"enabled": True, "text": "SPORTS"},
        "lower_third": {"enabled": True},
        "chapter_card": {"duration_s": 2.0},
    }
    if lf:
        base_lf.update(lf)
    return _FakeRenderPaths(tmp / "sports", slug, {"long_form_doc": base_lf})


def _write_inputs(paths: _FakeRenderPaths, slug: str = "doc", *, narration: dict | None = None, fp: dict | None = None) -> None:
    narration = narration or {
        "chapters": [{"id": "c1", "title": "One", "narration": "Hello world."}],
        "narrator_tone": "tifo-academic",
        "cold_open": "shocking_stat",
    }
    fp = fp or {"match_footage": [], "talking_heads": [], "archival_footage": [], "b_roll": []}
    paths.narration_for(slug).write_text(json.dumps(narration))
    (paths.footage_plan / f"{slug}.json").write_text(json.dumps(fp))


class ImportEnvTonePreflightTests(unittest.TestCase):
    def test_direction_primitives_success_and_missing_import(self):
        if not hasattr(sd, "_import_direction_primitives"):
            return
        fake = types.ModuleType("pipeline.render.direction_primitives")
        fake.resolve_direction_plan = lambda *a, **kw: []
        fake.render_direction_clips = lambda *a, **kw: []
        with patch.dict(sys.modules, {"pipeline.render.direction_primitives": fake}):
            resolve, render = sd._import_direction_primitives()
            self.assertIs(resolve, fake.resolve_direction_plan)
            self.assertIs(render, fake.render_direction_clips)
        sys.modules.pop("pipeline.render.direction_primitives", None)
        with self.assertRaises(ImportError):
            sd._import_direction_primitives()

    def test_load_env_missing_and_existing_applies_defaults(self):
        with _tmpdir() as td:
            tmp = Path(td)
            sd._load_env(tmp)
            old_a = os.environ.pop("SD_ENV_A", None)
            old_b = os.environ.get("SD_ENV_B")
            os.environ["SD_ENV_B"] = "already"
            try:
                (tmp / ".env").write_text("# no\n\nSD_ENV_A='one'\nSD_ENV_B=two\nBAD\n")
                sd._load_env(tmp)
                self.assertEqual(os.environ["SD_ENV_A"], "one")
                self.assertEqual(os.environ["SD_ENV_B"], "already")
            finally:
                if old_a is None:
                    os.environ.pop("SD_ENV_A", None)
                else:
                    os.environ["SD_ENV_A"] = old_a
                if old_b is None:
                    os.environ.pop("SD_ENV_B", None)
                else:
                    os.environ["SD_ENV_B"] = old_b

    def test_apply_tone_known_unknown_and_render_overrides(self):
        self.assertEqual(sd._apply_tone({}, "serious-doc"), (0.95, 0.92))
        self.assertEqual(sd._apply_tone({"tts_speed": 1.3, "tts_post_atempo": 0.8}, "new"), (1.3, 0.8))
        if hasattr(sd, "apply_render_overrides"):
            lf = {"captions_enabled": True, "chapter_card_enabled": True, "lower_third_enabled": True}
            self.assertIs(sd.apply_render_overrides(lf, {}), lf)
            self.assertTrue(lf["captions_enabled"])
            self.assertIs(sd.apply_render_overrides(lf, {"render_overrides": "nope"}), lf)
            sd.apply_render_overrides(lf, {"render_overrides": {
                "captions_enabled": 0, "chapter_card_enabled": False, "lower_third_enabled": "yes",
            }})
            self.assertEqual(lf, {"captions_enabled": False, "chapter_card_enabled": False, "lower_third_enabled": True})

    def test_cold_open_visual_layer_paths(self):
        if not hasattr(sd, "_check_cold_open_visual_layer"):
            return
        old = os.environ.get("COLD_OPEN_VOID_PREFLIGHT_DISABLE")
        try:
            os.environ["COLD_OPEN_VOID_PREFLIGHT_DISABLE"] = "1"
            sd._check_cold_open_visual_layer({"cold_open": "commentator_clip"}, {}, slug="s")
            os.environ.pop("COLD_OPEN_VOID_PREFLIGHT_DISABLE", None)
            sd._check_cold_open_visual_layer({"cold_open": ""}, {}, slug="s")
            sd._check_cold_open_visual_layer({"cold_open": "shocking_stat"}, {}, slug="s")
            sd._check_cold_open_visual_layer({"cold_open": "dramatic_match_moment"}, {"match_footage": [{"id": "m"}]}, slug="s")
            with self.assertRaises(SystemExit) as cm:
                sd._check_cold_open_visual_layer({"cold_open": "youtuber_take"}, {"talking_heads": []}, slug="sluggy")
            self.assertIn("sluggy", str(cm.exception))
        finally:
            if old is None:
                os.environ.pop("COLD_OPEN_VOID_PREFLIGHT_DISABLE", None)
            else:
                os.environ["COLD_OPEN_VOID_PREFLIGHT_DISABLE"] = old


class AlignmentDownloadPrepTests(unittest.TestCase):
    def test_align_anchors_empty_words_and_matching_warning_paths(self):
        with patch("pipeline.beats.transcribe_words", return_value=[]):
            self.assertEqual(sd._align_anchors_to_narration(Path("n.wav"), ["anything"]), {})

        words = [
            _FakeWord("!!!", 0.0, 0.1),
            _FakeWord("Hello", 0.2, 0.4),
            _FakeWord("wurld", 0.5, 0.7),
            _FakeWord("goal", 1.0, 1.1),
            _FakeWord("machine", 1.2, 1.4),
            _FakeWord("striker", 1.5, 1.6),
            _FakeWord("press", 1.7, 1.8),
        ]
        anchors = ["", "Hello world", "goal machine striker press", "hello missing missing missing missing"]
        with patch("pipeline.beats.transcribe_words", return_value=words), patch("builtins.print") as pr:
            out = sd._align_anchors_to_narration(Path("n.wav"), anchors)
        with patch("pipeline.beats.transcribe_words", return_value=[_FakeWord("alpha", 0.0, 0.1), _FakeWord("beta", 0.2, 0.3)]):
            self.assertEqual(sd._align_anchors_to_narration(Path("n.wav"), ["alpha beta gamma delta"]), {})
        self.assertEqual(out["Hello world"], (0.2, 0.7))
        self.assertEqual(out["goal machine striker press"], (1.0, 1.8))
        self.assertNotIn("hello missing missing missing missing", out)
        self.assertTrue(any("nearest start-token" in str(c) for c in pr.call_args_list))

    def test_slug_download_cache_success_and_cloud_failure(self):
        self.assertEqual(len(sd._slug_from_url("https://example.com/video")), 16)
        with _tmpdir() as td:
            sources = Path(td) / "sources"
            cached = sources / f"{sd._slug_from_url('url')}.mp4"
            _write_big(cached)
            self.assertEqual(sd._download_source("url", sources), cached)

            def fake_run(cmd, check):
                _write_big(Path(cmd[cmd.index("-o") + 1]))
                return MagicMock(returncode=0)

            with patch.object(sd.subprocess, "run", side_effect=fake_run) as run:
                out = sd._download_source("new-url", sources)
            self.assertTrue(out.exists())
            run.assert_called_once()

            with patch.object(sd.subprocess, "run", side_effect=sd.subprocess.CalledProcessError(1, "yt-dlp")):
                with self.assertRaises(sd.subprocess.CalledProcessError):
                    sd._download_source("fail-url", sources)

    def test_prep_footage_clip_cache_and_trim(self):
        with _tmpdir() as td:
            tmp = Path(td)
            entry = {"id": "clip1", "url": "url", "in_s": 1, "out_s": 3}
            cached = tmp / "cache" / "clips" / "clip1.mp4"
            _write_big(cached)
            self.assertEqual(sd._prep_footage_clip(entry, tmp / "sources", tmp / "cache", 1920, 1080, 30, None), cached)
            cached.unlink()
            src = _write_big(tmp / "src.mp4")
            with patch.object(sd, "_download_source", return_value=src) as dl, \
                 patch.object(sd, "_trim_clip_letterbox", side_effect=lambda *a, **kw: _write_big(a[3])) as trim:
                out = sd._prep_footage_clip(entry, tmp / "sources", tmp / "cache", 1920, 1080, 24, "eq")
            self.assertTrue(out.exists())
            dl.assert_called_once_with("url", tmp / "sources")
            trim.assert_called_once_with(src, 1.0, 3.0, out, out_w=1920, out_h=1080, fps=24, grade_filter="eq")


class FakePILTests(unittest.TestCase):
    def _fake_pil_modules(self, *, truetype_raises: bool = False):
        pil = types.ModuleType("PIL")
        image = types.ModuleType("PIL.Image")
        draw_mod = types.ModuleType("PIL.ImageDraw")
        font_mod = types.ModuleType("PIL.ImageFont")

        class FakeImg:
            def __init__(self, size=(1, 1)):
                self.size = size

            def save(self, path):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"png")

        class FakeDraw:
            def __init__(self, img):
                self.img = img

            def textbbox(self, xy, text, font=None):
                return (0, 0, max(1, len(text)) * 10, 20)

            def text(self, *args, **kwargs):
                return None

            def rectangle(self, *args, **kwargs):
                return None

        image.new = MagicMock(side_effect=lambda mode, size, color: FakeImg(size))
        draw_mod.Draw = MagicMock(side_effect=lambda img: FakeDraw(img))
        if truetype_raises:
            font_mod.truetype = MagicMock(side_effect=Exception("bad font"))
        else:
            font_mod.truetype = MagicMock(side_effect=lambda fp, size: {"font": fp, "size": size})
        font_mod.load_default = MagicMock(return_value={"default": True})
        pil.Image = image
        pil.ImageDraw = draw_mod
        pil.ImageFont = font_mod
        return {"PIL": pil, "PIL.Image": image, "PIL.ImageDraw": draw_mod, "PIL.ImageFont": font_mod}, font_mod

    def test_chapter_card_success_and_font_fallback_word_wrap(self):
        with _tmpdir() as td:
            out = Path(td) / "cards" / "card.png"
            mods, font = self._fake_pil_modules(truetype_raises=False)
            with patch.dict(sys.modules, mods), patch("pipeline.render.sports_doc.Path.exists", return_value=True):
                self.assertEqual(sd._render_chapter_card(2, "A long title that wraps over multiple short lines", out, 1920, 1080, (1, 2, 3, 4), (5, 6, 7, 8), 40, 60), out)
            self.assertTrue(out.exists())
            self.assertTrue(font.truetype.called)

            out2 = Path(td) / "cards" / "fallback.png"
            mods, font = self._fake_pil_modules(truetype_raises=True)
            with patch.dict(sys.modules, mods), patch("pipeline.render.sports_doc.Path.exists", return_value=True):
                self.assertEqual(sd._render_chapter_card(3, "Fallback", out2, 100, 80, (1, 2, 3, 4), (5, 6, 7, 8), 10, 12), out2)
            self.assertTrue(font.load_default.called)

    def test_lower_third_with_and_without_handle_and_font_fallback(self):
        with _tmpdir() as td:
            out = Path(td) / "lt" / "one.png"
            mods, font = self._fake_pil_modules(truetype_raises=False)
            with patch.dict(sys.modules, mods), patch("pipeline.render.sports_doc.Path.exists", return_value=True):
                self.assertEqual(sd._render_lower_third("Speaker", "@handle", out, (1, 2, 3, 4), (255, 255, 255, 255), (9, 8, 7, 6), 30, 20), out)
            self.assertTrue(out.exists())
            self.assertTrue(font.truetype.called)

            out2 = Path(td) / "lt" / "two.png"
            mods, font = self._fake_pil_modules(truetype_raises=True)
            with patch.dict(sys.modules, mods), patch("pipeline.render.sports_doc.Path.exists", return_value=True):
                self.assertEqual(sd._render_lower_third("Speaker", "", out2, (1, 2, 3, 4), (255, 255, 255, 255), (9, 8, 7, 6), 30, 20), out2)
            self.assertTrue(font.load_default.called)


class TimelineAssemblyTests(unittest.TestCase):
    def test_gather_overlays_resolves_chapters_entries_and_fallbacks(self):
        with _tmpdir() as td:
            tmp = Path(td)
            nar_path = tmp / "narration.json"
            fp_path = tmp / "footage.json"
            narration = {
                "chapters": [
                    {"id": "blank", "title": "", "narration": "Blank first. Hidden card."},
                    {"id": "explicit", "title": "Explicit", "start_s": 6, "narration": "Explicit prose."},
                    {"id": "matched", "title": "Matched", "narration": "Matched first. More text."},
                    {"id": "fallback", "title": "Fallback", "narration": "Fallback first. More text."},
                    {"id": "titleonly", "title": "Title Only", "narration": ""},
                ]
            }
            fp = {
                "match_footage": [
                    {"id": "m0", "url": "u", "in_s": 0, "out_s": 2, "at_s": 1},
                    {"id": "m1", "url": "u", "in_s": 2, "out_s": 5, "narration_anchor": "match anchor"},
                    {"id": "m2", "url": "u", "in_s": 0, "out_s": 1, "narration_anchor": "missing anchor"},
                ],
                "talking_heads": [{"id": "t1", "url": "u", "in_s": 1, "out_s": 4, "narration_anchor": "talk anchor"}],
                "archival_footage": [{"id": "a1", "url": "u", "in_s": 0, "out_s": 1, "at_s": 9}],
                "b_roll": [
                    {"id": "b0", "url": "u", "in_s": 0, "out_s": 2},
                    {"id": "b1", "url": "u", "in_s": 0, "out_s": 2, "at_s": 11},
                    {"id": "b2", "url": "u", "in_s": 0, "out_s": 2, "narration_anchor": "b anchor"},
                ],
                "motion_graphics": [{"id": "g", "narration_anchor": "graphic anchor"}],
            }
            nar_path.write_text(json.dumps(narration))
            fp_path.write_text(json.dumps(fp))
            anchor_times = {
                "Matched first": (10.0, 11.0),
                "match anchor": (20.0, 21.0),
                "talk anchor": (30.0, 31.0),
                "b anchor": (40.0, 41.0),
                "graphic anchor": (50.0, 51.0),
            }
            with patch.object(sd, "_align_anchors_to_narration", return_value=anchor_times) as align, \
                 patch.object(sd, "_probe_duration", return_value=100.0), \
                 patch("builtins.print") as pr:
                cards, match, heads, archival, broll = sd._gather_overlays(nar_path, fp_path, tmp / "n.wav", [], 0.2, 120, "text")
            self.assertTrue(align.called)
            self.assertEqual([c["id"] for c in cards], ["explicit", "matched"])
            self.assertEqual(cards[0]["at_s"], 6.0)
            self.assertEqual(cards[1]["at_s"], 8.5)
            self.assertEqual(match[0]["at_s"], 1.0)
            self.assertEqual(match[1]["dur_s"], 3.0)
            self.assertEqual([h["id"] for h in heads], ["t1"])
            self.assertEqual([a["id"] for a in archival], ["a1"])
            self.assertEqual([b["id"] for b in broll], ["b1", "b2"])
            self.assertTrue(any("no anchor match" in str(c) for c in pr.call_args_list))

    def test_gather_overlays_without_anchors_skips_alignment(self):
        with _tmpdir() as td:
            tmp = Path(td)
            nar = tmp / "nar.json"
            fp = tmp / "fp.json"
            nar.write_text(json.dumps({"chapters": [{"id": "c", "title": "T", "start_s": 2, "narration": "Text."}]}))
            fp.write_text(json.dumps({"match_footage": [], "talking_heads": [], "archival_footage": [], "b_roll": []}))
            with patch.object(sd, "_align_anchors_to_narration") as align, patch.object(sd, "_probe_duration", return_value=5.0):
                cards, *_ = sd._gather_overlays(nar, fp, tmp / "n.wav", [], 0.1, 10, "text")
            align.assert_not_called()
            self.assertEqual(cards[0]["at_s"], 2.0)

    def test_build_filler_video_cache_empty_and_cycled_broll(self):
        with _tmpdir() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            out = _write_big(cache / "filler.mp4")
            self.assertEqual(sd._build_filler_video([], 5, cache, 1920, 1080, 30), out)
            out.unlink()
            ffmpeg_calls = []
            with patch.object(sd, "_ffmpeg", side_effect=lambda cmd: ffmpeg_calls.append(cmd) or _write(Path(cmd[-1]), b"v")):
                self.assertEqual(sd._build_filler_video([], 4.2, cache, 1280, 720, 24), out)
            self.assertIn("color=c=0x0a1626", " ".join(ffmpeg_calls[0]))

            out.unlink()
            c1 = _write(tmp / "c1.mp4")
            c2 = _write(tmp / "c2.mp4")
            with patch.object(sd, "_probe_duration", side_effect=lambda p: 1.5 if p == c1 else 2.0), \
                 patch.object(sd, "_ffmpeg", side_effect=lambda cmd: _write(Path(cmd[-1]), b"v")) as ff:
                self.assertEqual(sd._build_filler_video([c1, c2], 5.0, cache, 1920, 1080, 30), out)
            self.assertGreaterEqual(ff.call_count, 2)
            self.assertFalse((cache / "_filler_raw.mp4").exists())

    def test_overlay_clip_on_filler_with_head_tail_and_edge_case(self):
        with _tmpdir() as td:
            tmp = Path(td)
            base = _write(tmp / "base.mp4")
            overlay = _write(tmp / "overlay.mp4")
            cache = tmp / "cache"
            cache.mkdir()
            with patch.object(sd, "_probe_duration", side_effect=lambda p: 2.0 if p == overlay else 10.0), \
                 patch.object(sd, "_ffmpeg", side_effect=lambda cmd: _write(Path(cmd[-1]), b"v")) as ff:
                out = sd._overlay_clip_on_filler(base, overlay, 3.0, cache)
            self.assertTrue(out.exists())
            self.assertFalse((cache / "_h_3.00.mp4").exists())
            self.assertFalse((cache / "_t_3.00.mp4").exists())
            self.assertEqual(ff.call_count, 3)

            with patch.object(sd, "_probe_duration", side_effect=lambda p: 20.0 if p == overlay else 10.0), \
                 patch.object(sd, "_ffmpeg", side_effect=lambda cmd: _write(Path(cmd[-1]), b"v")) as ff:
                out2 = sd._overlay_clip_on_filler(base, overlay, 0.0, cache)
            self.assertTrue(out2.exists())
            self.assertEqual(ff.call_count, 1)


class MainEntrypointTests(unittest.TestCase):
    def _main_patches(self, paths: _FakeRenderPaths):
        return [
            patch("pipeline.preflight.power_check"),
            patch("pipeline.images_cloudrun.reset_circuit_breaker"),
            patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths),
            patch("pipeline.footage.footage_plan_lint.raise_if_any"),
        ]

    def test_main_guard_errors_for_config_inputs_and_empty_text(self):
        with _tmpdir() as td:
            tmp = Path(td)
            no_lf = _FakeRenderPaths(tmp / "no", "doc", {})
            with patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc"]), \
                 patch.object(sd, "REPO_ROOT", tmp), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=no_lf):
                with self.assertRaises(SystemExit):
                    sd.main()

            paths = _make_paths(tmp / "a")
            with patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc"]), \
                 patch.object(sd, "REPO_ROOT", tmp), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths):
                with self.assertRaises(SystemExit):
                    sd.main()

            paths.narration_for("doc").write_text(json.dumps({"chapters": []}))
            with patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc"]), \
                 patch.object(sd, "REPO_ROOT", tmp), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths):
                with self.assertRaises(SystemExit):
                    sd.main()

            (paths.footage_plan / "doc.json").write_text(json.dumps({}))
            paths.narration_for("doc").write_text(json.dumps({"chapters": []}))
            with patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc"]), \
                 patch.object(sd, "REPO_ROOT", tmp), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.footage.footage_plan_lint.raise_if_any"):
                with self.assertRaises(SystemExit):
                    sd.main()

    def test_main_tts_only_and_align_only(self):
        with _tmpdir() as td:
            tmp = Path(td)
            paths = _make_paths(tmp)
            _write_inputs(paths)
            wav = _write(paths.cache_for("doc") / "narration.wav")
            chunk = _write(paths.cache_for("doc") / "chunk.wav")
            with patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc", "--tts-only"]), \
                 patch.object(sd, "REPO_ROOT", tmp), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.footage.footage_plan_lint.raise_if_any"), \
                 patch.object(sd, "synth_long_narration", return_value=(wav, [chunk])), \
                 patch.object(sd, "_probe_duration", return_value=12.0):
                self.assertEqual(sd.main(), 0)

            with patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc", "--align-only"]), \
                 patch.object(sd, "REPO_ROOT", tmp), \
                 patch("pipeline.preflight.power_check"), \
                 patch("pipeline.images_cloudrun.reset_circuit_breaker"), \
                 patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths), \
                 patch("pipeline.footage.footage_plan_lint.raise_if_any"), \
                 patch.object(sd, "synth_long_narration", return_value=(wav, [chunk])), \
                 patch.object(sd, "_probe_duration", return_value=12.0), \
                 patch.object(sd, "_gather_overlays", return_value=([], [], [], [], [])):
                self.assertEqual(sd.main(), 0)
            self.assertTrue((paths.cache_for("doc") / "timeline.json").exists())

    def test_main_full_pipeline_with_overlays_curated_music_captions_watermark_lower_thirds_and_cleanup(self):
        with _tmpdir() as td:
            tmp = Path(td)
            paths = _make_paths(tmp, lf={"tts_provider": "f5_tts", "visual_grade": {"enabled": True, "filter": "eq"}})
            narration = {
                "chapters": [{"id": "c1", "title": "One", "narration": "Hello world."}],
                "narrator_tone": "intense-podcast",
                "cold_open": "shocking_stat",
            }
            fp = {"match_footage": [], "talking_heads": [], "archival_footage": [], "b_roll": [{"id": "bg", "url": "u", "in_s": 0, "out_s": 1}]}
            _write_inputs(paths, narration=narration, fp=fp)
            _write(paths.root / "music" / "theme.wav")
            cap_dir = paths.cache_for("doc") / "captions"
            _write(cap_dir / "cap_old.png")
            wav = _write(paths.cache_for("doc") / "narration.wav")
            chunk = _write(paths.cache_for("doc") / "chunk.wav")
            clip_counter = {"n": 0}

            def prep(entry, sources_dir, cache_dir, out_w, out_h, fps, grade_filter):
                return _write(cache_dir / "clips" / f"{entry['id']}.mp4")

            def overlay(cur, clip, at_s, composed_dir):
                clip_counter["n"] += 1
                return _write(composed_dir / f"ov{clip_counter['n']}.mp4")

            def ffmpeg(cmd):
                last = Path(cmd[-1])
                if last.suffix:
                    _write(last, b"v" * 2048)

            def chapter_card(**kwargs):
                return _write(kwargs["out_path"], b"png")

            def lower_third(**kwargs):
                return _write(kwargs["out_path"], b"png")

            def caption_builder(**kwargs):
                p = _write(kwargs["out_dir"] / "cap_0000.png", b"png")
                return [(p, 0.0, 2.0)]

            def direction_renderer(meta, cache_dir, out_w, out_h, fps):
                p = _write(cache_dir / "direction.mp4")
                return [(p, {"id": "dir", "at_s": 8.0})]

            gather = (
                [{"id": "cc1", "index": 1, "title": "Chapter", "at_s": 1.0}],
                [{"id": "m1", "url": "u", "in_s": 0, "out_s": 1, "at_s": 2.0, "dur_s": 1.0}],
                [
                    {"id": "t1", "url": "u", "in_s": 0, "out_s": 2, "at_s": 3.0, "dur_s": 2.0, "speaker": "Coach", "speaker_handle": "@c"},
                    {"id": "t2", "url": "u", "in_s": 0, "out_s": 1, "at_s": 4.0, "dur_s": 1.0, "speaker": ""},
                ],
                [{"id": "a1", "url": "u", "in_s": 0, "out_s": 1, "at_s": 5.0, "dur_s": 1.0}],
                [{"id": "b1", "url": "u", "in_s": 0, "out_s": 1, "at_s": 6.0, "dur_s": 1.0}],
            )
            with ExitStack() as stack:
                stack.enter_context(patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc"]))
                stack.enter_context(patch.object(sd, "REPO_ROOT", tmp))
                stack.enter_context(patch("pipeline.preflight.power_check"))
                reset_mlx = stack.enter_context(patch("pipeline.preflight.reset_mlx_state"))
                stack.enter_context(patch("pipeline.images_cloudrun.reset_circuit_breaker"))
                stack.enter_context(patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths))
                stack.enter_context(patch("pipeline.footage.footage_plan_lint.raise_if_any"))
                stack.enter_context(patch.object(sd, "synth_long_narration", return_value=(wav, [chunk])))
                stack.enter_context(patch.object(sd, "_probe_duration", return_value=20.0))
                stack.enter_context(patch.object(sd, "_gather_overlays", return_value=gather))
                prep_mock = stack.enter_context(patch.object(sd, "_prep_footage_clip", side_effect=prep))
                stack.enter_context(patch.object(sd, "_build_filler_video", side_effect=lambda clips, *a: _write(paths.cache_for("doc") / "filler.mp4")))
                ov = stack.enter_context(patch.object(sd, "_overlay_clip_on_filler", side_effect=overlay))
                ff = stack.enter_context(patch.object(sd, "_ffmpeg", side_effect=ffmpeg))
                stack.enter_context(patch.object(sd, "_render_chapter_card", side_effect=chapter_card))
                stack.enter_context(patch.object(sd, "build_caption_pngs_from_chunks", side_effect=caption_builder))
                stack.enter_context(patch.object(sd, "render_watermark_png", side_effect=lambda text, path, **kw: _write(path, b"wm")))
                stack.enter_context(patch.object(sd, "_render_lower_third", side_effect=lower_third))
                stack.enter_context(patch.object(sd.shutil, "copy2", side_effect=lambda src, dst: _write(Path(dst), Path(src).read_bytes())))
                self.assertEqual(sd.main(), 0)
            reset_mlx.assert_called_once_with(drop_f5=True, label="sports-doc stage-1 TTS")
            self.assertGreaterEqual(prep_mock.call_count, 6)
            self.assertGreaterEqual(ov.call_count, 2)
            self.assertTrue((paths.cache_for("doc") / "composed.mp4").exists())
            self.assertFalse((cap_dir / "cap_old.png").exists())
            final_cmd = ff.call_args_list[-1].args[0]
            full_filter = final_cmd[final_cmd.index("-filter_complex") + 1]
            self.assertIn("overlay=x=W-w", full_filter)
            self.assertIn("amix=inputs=2", full_filter)
            self.assertTrue((paths.long_form / "doc.mp4").exists())

    def test_main_full_pipeline_disabled_visual_layers_synth_music_and_cli_main(self):
        with _tmpdir() as td:
            tmp = Path(td)
            paths = _make_paths(tmp, lf={
                "captions_enabled": False,
                "watermark": {"enabled": False},
                "lower_third": {"enabled": False},
            })
            narration = {
                "chapters": [{"id": "c1", "title": "One", "narration": "Solo text."}],
            }
            _write_inputs(paths, narration=narration)
            wav = _write(paths.cache_for("doc") / "narration.wav")
            chunk = _write(paths.cache_for("doc") / "chunk.wav")

            def ffmpeg(cmd):
                _write(Path(cmd[-1]), b"v" * 2048)

            with ExitStack() as stack:
                stack.enter_context(patch.object(sys, "argv", ["sports_doc", "--channel", "sports", "--slug", "doc"]))
                stack.enter_context(patch.object(sd, "REPO_ROOT", tmp))
                stack.enter_context(patch("pipeline.preflight.power_check"))
                stack.enter_context(patch("pipeline.images_cloudrun.reset_circuit_breaker"))
                stack.enter_context(patch("pipeline.paths.RenderPaths.from_channel_dir", return_value=paths))
                stack.enter_context(patch("pipeline.footage.footage_plan_lint.raise_if_any"))
                stack.enter_context(patch.object(sd, "synth_long_narration", return_value=(wav, [chunk])))
                stack.enter_context(patch.object(sd, "_probe_duration", return_value=9.0))
                stack.enter_context(patch.object(sd, "_gather_overlays", return_value=([], [], [], [], [])))
                stack.enter_context(patch.object(sd, "_build_filler_video", side_effect=lambda clips, *a: _write(paths.cache_for("doc") / "filler.mp4")))
                stack.enter_context(patch.object(sd.shutil, "copy2", side_effect=lambda src, dst: _write(Path(dst), Path(src).read_bytes())))
                stack.enter_context(patch.object(sd, "build_music_bed", side_effect=lambda path, dur: _write(path, b"music")))
                ff = stack.enter_context(patch.object(sd, "_ffmpeg", side_effect=ffmpeg))
                caps = stack.enter_context(patch.object(sd, "build_caption_pngs_from_chunks"))
                card = stack.enter_context(patch.object(sd, "_render_chapter_card"))
                lt = stack.enter_context(patch.object(sd, "_render_lower_third"))
                self.assertEqual(sd.main(), 0)
            caps.assert_not_called()
            card.assert_not_called()
            lt.assert_not_called()
            final_cmd = ff.call_args_list[-1].args[0]
            self.assertIn("[1:a]volume", final_cmd[final_cmd.index("-filter_complex") + 1])
            self.assertNotIn("overlay", final_cmd[final_cmd.index("-filter_complex") + 1])

            with patch.object(sd, "main", return_value=7) as main:
                self.assertEqual(sd.cli_main(), 7)
                main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
