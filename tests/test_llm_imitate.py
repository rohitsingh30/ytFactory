from __future__ import annotations

import json
import shutil
import sys
import types
import unittest
import pipeline.footage as footage_pkg
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import imitate as im


SCRATCH = PROJECT_ROOT / "tests" / ".scratch_imitate"
PROFILE = {
    "niche_match": "unknown",
    "hook_template": "AITA for [x]",
    "structure": "hook → twist",
    "tone": "tense",
    "pacing": "fast",
    "length_target_s": 45,
    "themes": ["family"],
    "viral_hooks": ["secret"],
    "voice_profile_summary": "urgent",
    "suggested_voice": "bad_voice",
    "visual_aesthetic": "cartoon",
}


class ImitateHelperTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_find_yt_dlp_prefers_venv_then_path(self):
        bin_dir = SCRATCH / "bin"
        bin_dir.mkdir()
        (bin_dir / "yt-dlp").write_text("x")
        with patch.object(im.sys, "executable", str(bin_dir / "python")):
            self.assertEqual(im._find_yt_dlp(), str(bin_dir / "yt-dlp"))
        with patch.object(im.sys, "executable", str(SCRATCH / "missing" / "python")), \
             patch.object(im.shutil, "which", return_value="/usr/bin/yt-dlp"):
            self.assertEqual(im._find_yt_dlp(), "/usr/bin/yt-dlp")

    def test_ydl_download_video_success_and_failures(self):
        import inspect
        out_file = SCRATCH / "video.mp4"
        out_file.write_text("mp4")
        if "yt_dlp_cloudrun" in inspect.getsource(im._ydl_download_video):
            fake = types.ModuleType("pipeline.footage.yt_dlp_cloudrun")
            class CloudRunYtDlpFailed(Exception):
                pass
            class CloudRunYtDlpUnavailable(Exception):
                pass
            fake.CloudRunYtDlpFailed = CloudRunYtDlpFailed
            fake.CloudRunYtDlpUnavailable = CloudRunYtDlpUnavailable
            fake.download = lambda *a, **k: out_file
            with patch.dict(sys.modules, {"pipeline.footage.yt_dlp_cloudrun": fake}), \
                 patch.object(footage_pkg, "yt_dlp_cloudrun", fake, create=True):
                self.assertEqual(im._ydl_download_video("abc", SCRATCH / "video"), out_file)
            fake.download = lambda *a, **k: (_ for _ in ()).throw(CloudRunYtDlpFailed("bad"))
            with patch.dict(sys.modules, {"pipeline.footage.yt_dlp_cloudrun": fake}), \
                 patch.object(footage_pkg, "yt_dlp_cloudrun", fake, create=True):
                self.assertIsNone(im._ydl_download_video("abc", SCRATCH / "video2"))
            fake.download = lambda *a, **k: (_ for _ in ()).throw(CloudRunYtDlpUnavailable("down"))
            with patch.dict(sys.modules, {"pipeline.footage.yt_dlp_cloudrun": fake}), \
                 patch.object(footage_pkg, "yt_dlp_cloudrun", fake, create=True):
                self.assertIsNone(im._ydl_download_video("abc", SCRATCH / "video3"))
        else:
            with patch.object(im, "_find_yt_dlp", return_value=None):
                self.assertIsNone(im._ydl_download_video("abc", SCRATCH / "local_none"))
            def fake_run(cmd, check, capture_output):
                dest = SCRATCH / "local_ok" / "abc.mp4"
                dest.write_text("mp4")
                return None
            with patch.object(im, "_find_yt_dlp", return_value="yt-dlp"), patch.object(im.subprocess, "run", side_effect=fake_run):
                self.assertEqual(im._ydl_download_video("abc", SCRATCH / "local_ok").name, "abc.mp4")
            err = im.subprocess.CalledProcessError(1, "yt-dlp", stderr=b"bad")
            with patch.object(im, "_find_yt_dlp", return_value="yt-dlp"), patch.object(im.subprocess, "run", side_effect=err):
                self.assertIsNone(im._ydl_download_video("abc", SCRATCH / "local_fail"))
            with patch.object(im, "_find_yt_dlp", return_value="yt-dlp"), patch.object(im.subprocess, "run", return_value=None):
                self.assertIsNone(im._ydl_download_video("abc", SCRATCH / "local_nomatch"))

    def test_probe_duration_and_sample_frames(self):
        target = "pipeline.quality.probe.probe_duration_or_none" if "pipeline.quality.probe" in im._probe_duration.__code__.co_names else "pipeline.probe.probe_duration_or_none"
        with patch(target, return_value=7.5):
            self.assertEqual(im._probe_duration(SCRATCH / "v.mp4"), 7.5)
        video = SCRATCH / "video.mp4"
        video.write_text("mp4")
        with patch.object(im, "_probe_duration", return_value=None):
            self.assertEqual(im._sample_frames(video, 2, SCRATCH / "frames_none"), [])
        with patch.object(im, "_probe_duration", return_value=10.0), \
             patch.object(im.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            frames = im._sample_frames(video, 2, SCRATCH / "frames")
        self.assertEqual([p.name for p in frames], ["frame_00.jpg", "frame_01.jpg"])
        self.assertEqual(run.call_count, 2)
        existing = SCRATCH / "frames" / "frame_00.jpg"
        existing.write_text("jpg")
        with patch.object(im, "_probe_duration", return_value=10.0), \
             patch.object(im.subprocess, "run", side_effect=RuntimeError("ffmpeg")):
            frames = im._sample_frames(video, 1, SCRATCH / "frames")
        self.assertEqual(frames, [existing])
        with patch.object(im, "_probe_duration", return_value=10.0), \
             patch.object(im.subprocess, "run", side_effect=RuntimeError("ffmpeg")):
            self.assertEqual(im._sample_frames(video, 1, SCRATCH / "newframes"), [])

    def test_analyze_prompt_variants(self):
        no_caps = im._analyze_prompt(title="T", author="A", video_id="v", transcript="", frame_paths=[])
        self.assertIn("no captions available", no_caps)
        long_caps = "x" * 4100
        frame = SCRATCH / "f.jpg"
        prompt = im._analyze_prompt(title="T", author="A", video_id="v", transcript=long_caps, frame_paths=[frame])
        self.assertIn("FRAME FILES", prompt)
        self.assertIn("…", prompt)


class AnalyzeIdeateTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_analyze_success_normalizes_voice_and_writes_profile(self):
        frame = SCRATCH / "frame.jpg"
        frame.write_text("jpg")
        with patch.object(im, "_extract_video_id", return_value="vid123"), \
             patch.object(im, "_captions_via_youtube_transcript_api", return_value="hello\nworld"), \
             patch.object(im, "_fetch_oembed_meta", return_value={"title": "Title", "author_name": "Author"}), \
             patch.object(im, "_ydl_download_video", return_value=SCRATCH / "video.mp4"), \
             patch.object(im, "_sample_frames", return_value=[frame]), \
             patch.object(im.llm, "model_for", return_value="opus"), \
             patch.object(im.llm, "call_claude_cli", return_value=dict(PROFILE)) as call:
            profile = im.analyze("https://youtu.be/vid123", work_root=SCRATCH, n_frames=1)
        self.assertEqual(profile["suggested_voice"], "af_bella")
        self.assertEqual(profile["video_id"], "vid123")
        self.assertEqual(profile["transcript_chars"], len("hello world"))
        self.assertTrue((SCRATCH / "vid123" / "profile.json").exists())
        self.assertEqual(call.call_args.kwargs["allowed_tools"], ["Read"])

    def test_analyze_default_work_root_uses_model_cache_dir(self):
        cache_target = "pipeline.schemas.paths.MODEL_CACHE_DIR" if "pipeline.schemas.paths" in im.analyze.__code__.co_names else "pipeline.paths.MODEL_CACHE_DIR"
        with patch(cache_target, SCRATCH), \
             patch.object(im, "_extract_video_id", return_value="vidcache"), \
             patch.object(im, "_captions_via_youtube_transcript_api", return_value="captions"), \
             patch.object(im, "_fetch_oembed_meta", return_value={}), \
             patch.object(im, "_ydl_download_video", return_value=None), \
             patch.object(im.llm, "model_for", return_value="opus"), \
             patch.object(im.llm, "call_claude_cli", return_value=dict(PROFILE)):
            profile = im.analyze("url")
        self.assertEqual(profile["video_id"], "vidcache")
        self.assertTrue((SCRATCH / "riff" / "vidcache" / "profile.json").exists())

    def test_analyze_without_media_raises_and_non_dict_profile_raises(self):
        with patch.object(im, "_extract_video_id", return_value="vid"), \
             patch.object(im, "_captions_via_youtube_transcript_api", return_value=""), \
             patch.object(im, "_fetch_oembed_meta", return_value={}), \
             patch.object(im, "_ydl_download_video", return_value=None):
            with self.assertRaises(RuntimeError):
                im.analyze("url", work_root=SCRATCH)
        with patch.object(im, "_extract_video_id", return_value="vid"), \
             patch.object(im, "_captions_via_youtube_transcript_api", return_value="captions"), \
             patch.object(im, "_fetch_oembed_meta", return_value={}), \
             patch.object(im, "_ydl_download_video", return_value=None), \
             patch.object(im.llm, "model_for", return_value="opus"), \
             patch.object(im.llm, "call_claude_cli", return_value=[]):
            with self.assertRaises(im.llm.ClaudeCLIError):
                im.analyze("url", work_root=SCRATCH)

    def test_ideate_success_and_failure_paths(self):
        seeds_raw = [
            {"title": "One Good Story", "hook": "Hook", "body": "Body with details"},
            "skip me",
            {"title": "No Body", "body": ""},
        ]
        profile = dict(PROFILE, video_id="vid", url="https://yt", title="Source")
        with patch.object(im.llm, "model_for", return_value="opus"), patch.object(im.llm, "call_claude_cli", return_value=seeds_raw):
            seeds = im.ideate(profile, n=3)
        self.assertEqual(len(seeds), 1)
        self.assertTrue(seeds[0].slug.startswith("riff-vid-1-one-good-story"))
        self.assertIn("Hook", seeds[0].body)
        self.assertEqual(seeds[0].metadata["source_video_id"], "vid")
        with patch.object(im.llm, "model_for", return_value="opus"), patch.object(im.llm, "call_claude_cli", return_value={}):
            with self.assertRaises(im.llm.ClaudeCLIError):
                im.ideate(profile)
        with patch.object(im.llm, "model_for", return_value="opus"), patch.object(im.llm, "call_claude_cli", return_value=[{"title": "x", "body": ""}]):
            with self.assertRaises(im.llm.ClaudeCLIError):
                im.ideate(profile)

    def test_riff_calls_analyze_then_ideate(self):
        with patch.object(im, "analyze", return_value={"p": 1}) as analyze, patch.object(im, "ideate", return_value=["seed"]) as ideate:
            self.assertEqual(im.riff("url", n=4), ({"p": 1}, ["seed"]))
        analyze.assert_called_once_with("url")
        ideate.assert_called_once_with({"p": 1}, n=4)


if __name__ == "__main__":
    unittest.main()
