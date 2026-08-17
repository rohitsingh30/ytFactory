"""Tests for the cloud worker's CTR-optimized thumbnail wiring
(added 2026-05-14 per the 27-render audit's bug B2 — cloud renders
were shipping a plain ffmpeg first-frame as the thumbnail rather
than the ``pipeline.upload.thumbnails.auto_thumbnail`` curiosity-headline
composition that already existed for the laptop path).

Pin:
  * Happy path: auto_thumbnail wins, ffmpeg fallback is NOT called.
  * Auto returns None (no scene frames): ffmpeg fallback IS called.
  * Auto raises: ffmpeg fallback IS called (logged + non-fatal).
  * Missing slug / channel_yaml / script: ffmpeg fallback (no
    auto_thumbnail attempt at all).
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "render_worker_v2_entrypoint_for_thumb_tests",
        ENTRYPOINT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StageRenderThumbnailTests(unittest.TestCase):
    """All four branches of the new thumbnail picker."""

    def setUp(self):
        self.ep = _load_entrypoint()
        self.tmp = tempfile.mkdtemp()
        self.work_dir = Path(self.tmp) / "work"
        self.work_dir.mkdir()
        # Seed a dummy mp4 so the subprocess sees something.
        self.mp4 = self.work_dir / "short.mp4"
        self.mp4.write_bytes(b"fake-mp4")
        # Dummy script + channel YAML so auto_thumbnail has inputs.
        self.script_path = Path(self.tmp) / "script.json"
        self.script_path.write_text(json.dumps({
            "slug": "test-slug",
            "hook": "Test hook for thumbnail",
            "title_options": ["Click bait test"],
        }))
        self.channel_yaml_path = Path(self.tmp) / "channel.yaml"
        self.channel_yaml_path.write_text(
            "channel_name: testchannel\nhandle: '@test'\n"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_job(self, *, with_context: bool = True) -> dict:
        job = {"job_id": "j1"}
        if with_context:
            job["_slug"] = "test-slug"
            job["_script_path"] = str(self.script_path)
            job["_channel_yaml"] = str(self.channel_yaml_path)
        return job

    def test_auto_thumbnail_happy_path_skips_ffmpeg_fallback(self):
        job = self._make_job()
        with mock.patch.object(self.ep, "_run_renderer_via_engines",
                               return_value=self.mp4) as mock_render, \
             mock.patch.object(self.ep, "subprocess") as mock_subp, \
             mock.patch("pipeline.paths.RenderPaths.from_channel_yaml") as mock_rp:
            mock_rp.return_value = mock.MagicMock(
                cache_for=lambda slug: Path(self.tmp) / f"cache_{slug}"
            )

            # auto_thumbnail returns a path AND writes the file → success.
            def fake_auto(*, slug, cache_dir, script, channel_yaml, out_path, **kw):
                Path(out_path).write_bytes(b"\xff\xd8jpeg")  # tiny valid-ish
                return out_path
            with mock.patch("pipeline.upload.thumbnails.auto_thumbnail",
                            side_effect=fake_auto) as mock_auto:
                self.ep._stage_render_real(job, self.work_dir)

        # auto_thumbnail was called with the expected kwargs.
        self.assertEqual(mock_auto.call_count, 1)
        kw = mock_auto.call_args.kwargs
        self.assertEqual(kw["slug"], "test-slug")
        self.assertEqual(kw["script"]["hook"], "Test hook for thumbnail")
        # ffmpeg subprocess fallback NOT called.
        self.assertEqual(mock_subp.run.call_count, 0,
                         "ffmpeg fallback must not run when auto_thumbnail succeeded")

    def test_auto_thumbnail_returns_none_falls_back_to_ffmpeg(self):
        job = self._make_job()
        with mock.patch.object(self.ep, "_run_renderer_via_engines",
                               return_value=self.mp4), \
             mock.patch.object(self.ep, "subprocess") as mock_subp, \
             mock.patch("pipeline.paths.RenderPaths.from_channel_yaml") as mock_rp, \
             mock.patch("pipeline.upload.thumbnails.auto_thumbnail",
                        return_value=None):
            mock_rp.return_value = mock.MagicMock(
                cache_for=lambda slug: Path(self.tmp) / f"cache_{slug}"
            )
            self.ep._stage_render_real(job, self.work_dir)
        # ffmpeg subprocess fallback WAS called.
        self.assertEqual(mock_subp.run.call_count, 1)
        cmd = mock_subp.run.call_args.args[0]
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-frames:v", cmd)

    def test_auto_thumbnail_raises_falls_back_to_ffmpeg(self):
        job = self._make_job()
        with mock.patch.object(self.ep, "_run_renderer_via_engines",
                               return_value=self.mp4), \
             mock.patch.object(self.ep, "subprocess") as mock_subp, \
             mock.patch("pipeline.paths.RenderPaths.from_channel_yaml") as mock_rp, \
             mock.patch("pipeline.upload.thumbnails.auto_thumbnail",
                        side_effect=RuntimeError("PIL crashed")):
            mock_rp.return_value = mock.MagicMock(
                cache_for=lambda slug: Path(self.tmp) / f"cache_{slug}"
            )
            # Must NOT propagate.
            self.ep._stage_render_real(job, self.work_dir)
        # ffmpeg fallback called as a safety net.
        self.assertEqual(mock_subp.run.call_count, 1)

    def test_real_mp4_field_set_on_job(self):
        """Sanity: the existing _real_mp4 binding still happens after
        the new thumbnail logic."""
        job = self._make_job()
        with mock.patch.object(self.ep, "_run_renderer_via_engines",
                               return_value=self.mp4), \
             mock.patch.object(self.ep, "subprocess"), \
             mock.patch("pipeline.paths.RenderPaths.from_channel_yaml") as mock_rp, \
             mock.patch("pipeline.upload.thumbnails.auto_thumbnail", return_value=None):
            mock_rp.return_value = mock.MagicMock(
                cache_for=lambda slug: Path(self.tmp) / f"cache_{slug}"
            )
            self.ep._stage_render_real(job, self.work_dir)
        self.assertEqual(job["_real_mp4"], str(self.mp4))

    def test_missing_context_skips_auto_uses_ffmpeg(self):
        # No slug / script / channel — auto_thumbnail can't run.
        job = self._make_job(with_context=False)
        with mock.patch.object(self.ep, "_run_renderer_via_engines",
                               return_value=self.mp4), \
             mock.patch.object(self.ep, "subprocess") as mock_subp, \
             mock.patch("pipeline.upload.thumbnails.auto_thumbnail") as mock_auto:
            self.ep._stage_render_real(job, self.work_dir)
        # auto_thumbnail not even attempted (context guard short-circuits).
        self.assertEqual(mock_auto.call_count, 0)
        # ffmpeg fallback IS called.
        self.assertEqual(mock_subp.run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
