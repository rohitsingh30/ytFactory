"""Tests for CLI entry-point scripts under scripts/.

Mocks all external I/O. Target: 100% line coverage on the 12 scripts.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# cv2 is unavailable; inject before any script that imports it.
_cv2_mock = MagicMock()
_cv2_mock.CascadeClassifier.return_value = MagicMock()
sys.modules.setdefault("cv2", _cv2_mock)


# ---------------------------------------------------------------------------
# 1. scripts/make_shorts.py
# ---------------------------------------------------------------------------
class TestMakeShorts(unittest.TestCase):
    def test_import_and_cli_main(self):
        with patch("pipeline.render.shorts.cli_main") as mock_cli:
            import scripts.make_shorts as ms
            self.assertIs(ms.cli_main, mock_cli)


# ---------------------------------------------------------------------------
# 2. scripts/ops/upload_next.py
# ---------------------------------------------------------------------------
class TestUploadNext(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.ops.upload_next as m
        cls.m = m
        cls.MOD = "scripts.ops.upload_next"

    def _mock_rotation_state(self, **kwargs):
        """Return a properly configured MagicMock for ROTATION_STATE."""
        m = MagicMock(spec=Path)
        for k, v in kwargs.items():
            getattr(m, k).return_value = v
        return m

    def test_load_cursor_missing_file(self):
        rs = self._mock_rotation_state(exists=False)
        with patch(f"{self.MOD}.ROTATION_STATE", rs):
            self.assertEqual(self.m._load_cursor(), 0)

    def test_load_cursor_reads_idx(self):
        rs = self._mock_rotation_state(exists=True, read_text='{"idx": 3}')
        with patch(f"{self.MOD}.ROTATION_STATE", rs):
            self.assertEqual(self.m._load_cursor(), 3)

    def test_load_cursor_corrupt(self):
        rs = MagicMock(spec=Path)
        rs.exists.return_value = True
        rs.read_text.side_effect = ValueError("bad json")
        with patch(f"{self.MOD}.ROTATION_STATE", rs):
            self.assertEqual(self.m._load_cursor(), 0)

    def test_save_cursor(self):
        rs = MagicMock(spec=Path)
        with patch(f"{self.MOD}.ROTATION_STATE", rs):
            self.m._save_cursor(7)
            rs.write_text.assert_called_once_with(json.dumps({"idx": 7}))

    def _mock_channels_root(self, shorts_exists=False, mp4s=None, record_exists=False, held=False):
        """Build a mock CHANNELS_ROOT that yields controlled dirs for _candidates_for_niche."""
        mp4s = mp4s or []
        upload_record = MagicMock(spec=Path)
        upload_record.exists.return_value = record_exists

        uploads_dir = MagicMock(spec=Path)
        uploads_dir.__truediv__ = MagicMock(return_value=upload_record)

        shorts_dir = MagicMock(spec=Path)
        shorts_dir.exists.return_value = shorts_exists
        shorts_dir.glob.return_value = mp4s

        def _niche_div(key):
            return {"shorts": shorts_dir, "uploads": uploads_dir}.get(key, MagicMock())

        niche_dir = MagicMock(spec=Path)
        niche_dir.__truediv__ = MagicMock(side_effect=_niche_div)

        root = MagicMock(spec=Path)
        root.__truediv__ = MagicMock(return_value=niche_dir)
        return root

    def test_candidates_for_niche_no_dir(self):
        root = self._mock_channels_root(shorts_exists=False)
        with patch("scripts.ops.upload_next.CHANNELS_ROOT", root):
            result = self.m._candidates_for_niche("reddit_amitheasshole")
        self.assertEqual(result, [])

    def test_candidates_for_niche_skips_uploaded(self):
        mp4 = MagicMock(spec=Path)
        mp4.stem = "uploaded-slug"
        mp4.stat.return_value.st_mtime = 1000.0
        root = self._mock_channels_root(shorts_exists=True, mp4s=[mp4], record_exists=True)
        with patch("scripts.ops.upload_next.CHANNELS_ROOT", root):
            result = self.m._candidates_for_niche("reddit_amitheasshole")
        self.assertEqual(result, [])

    def test_candidates_for_niche_skips_held(self):
        mp4 = MagicMock(spec=Path)
        mp4.stem = "held-slug"
        mp4.stat.return_value.st_mtime = 1000.0
        root = self._mock_channels_root(shorts_exists=True, mp4s=[mp4], record_exists=False)
        with patch("scripts.ops.upload_next.CHANNELS_ROOT", root), \
             patch.object(self.m, "is_held", return_value=True):
            result = self.m._candidates_for_niche("reddit_amitheasshole")
        self.assertEqual(result, [])

    def test_candidates_for_niche_returns_candidates(self):
        mp4 = MagicMock(spec=Path)
        mp4.stem = "free-slug"
        mp4.stat.return_value.st_mtime = 1000.0
        root = self._mock_channels_root(shorts_exists=True, mp4s=[mp4], record_exists=False)
        with patch("scripts.ops.upload_next.CHANNELS_ROOT", root), \
             patch.object(self.m, "is_held", return_value=False):
            result = self.m._candidates_for_niche("reddit_amitheasshole")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "free-slug")

    def test_upload_one_missing_script(self):
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = "channel_id: UC123\n"
        with patch(f"{self.MOD}.NICHE_TO_YAML", {"reddit_amitheasshole": yaml_path}), \
             patch("yaml.safe_load", return_value={}), \
             patch("pathlib.Path.exists", return_value=False):
            ok = self.m._upload_one("reddit_amitheasshole", "test-slug",
                                    MagicMock(spec=Path), "2025-01-01T00:00:00Z")
        self.assertFalse(ok)

    def test_upload_one_success(self):
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = "channel_id: UC123\n"
        script_path = MagicMock(spec=Path)
        script_path.exists.return_value = True
        script_path.read_text.return_value = json.dumps({"hook": "H", "title_options": ["T"]})
        raw_path = MagicMock(spec=Path)
        raw_path.exists.return_value = False
        rec = {"url": "https://youtu.be/abc"}

        def path_div(self_p, key):
            if "scripts" in str(key):
                return script_path
            return raw_path

        with patch(f"{self.MOD}.NICHE_TO_YAML", {"reddit_amitheasshole": yaml_path}), \
             patch("yaml.safe_load", return_value={"channel_id": "UC123"}), \
             patch("json.loads", return_value={"hook": "H"}), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value='{"hook":"H"}'), \
             patch.object(self.m.up, "upload_short", return_value=rec):
            ok = self.m._upload_one("reddit_amitheasshole", "test-slug",
                                    MagicMock(spec=Path), "2025-01-01T00:00:00Z")
        self.assertTrue(ok)

    def test_upload_one_exception(self):
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = "channel_id: UC123\n"
        with patch(f"{self.MOD}.NICHE_TO_YAML", {"reddit_amitheasshole": yaml_path}), \
             patch("yaml.safe_load", return_value={"channel_id": "UC123"}), \
             patch("json.loads", return_value={"hook": "H"}), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value='{"hook":"H"}'), \
             patch.object(self.m.up, "upload_short", side_effect=Exception("network")):
            ok = self.m._upload_one("reddit_amitheasshole", "test-slug",
                                    MagicMock(spec=Path), "2025-01-01T00:00:00Z")
        self.assertFalse(ok)

    def test_main_dry_run(self):
        with patch.object(self.m, "_load_cursor", return_value=0), \
             patch.object(self.m, "_candidates_for_niche", return_value=[("s1", Path("s1.mp4"))]), \
             patch.object(self.m, "_save_cursor"), \
             patch("sys.argv", ["upload_next", "--dry-run", "--count", "1"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_no_candidates(self):
        with patch.object(self.m, "_load_cursor", return_value=0), \
             patch.object(self.m, "_candidates_for_niche", return_value=[]), \
             patch.object(self.m, "_save_cursor"), \
             patch("sys.argv", ["upload_next", "--count", "1"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_upload_ok(self):
        with patch.object(self.m, "_load_cursor", return_value=0), \
             patch.object(self.m, "_candidates_for_niche", return_value=[("s1", Path("s1.mp4"))]), \
             patch.object(self.m, "_upload_one", return_value=True), \
             patch.object(self.m, "_save_cursor"), \
             patch("sys.argv", ["upload_next", "--count", "1"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_upload_fail(self):
        with patch.object(self.m, "_load_cursor", return_value=0), \
             patch.object(self.m, "_candidates_for_niche", return_value=[("s1", Path("s1.mp4"))]), \
             patch.object(self.m, "_upload_one", return_value=False), \
             patch.object(self.m, "_save_cursor"), \
             patch("sys.argv", ["upload_next", "--count", "1"]):
            result = self.m.main()
        self.assertEqual(result, 1)


# ---------------------------------------------------------------------------
# 3. scripts/ops/bulk_render_queue.py
# ---------------------------------------------------------------------------
class TestBulkRenderQueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.ops.bulk_render_queue as m
        cls.m = m

    def test_yaml_for_script_non_aita(self):
        sp = MagicMock(spec=Path)
        sp.stem = "foo"
        result = self.m._yaml_for_script(sp, "mystoriesanimated/aita_cooking", (30, 30))
        self.assertIn("aita_cooking", result)

    def test_yaml_for_script_aita_animated(self):
        sp = MagicMock(spec=Path)
        sp.stem = "aaa"
        aaa = MagicMock(spec=Path); aaa.stem = "aaa"
        zzz = MagicMock(spec=Path); zzz.stem = "zzz"
        with patch("pathlib.Path.glob", return_value=[aaa, zzz]):
            result = self.m._yaml_for_script(sp, self.m.AITA_NICHE, (1, 30))
        self.assertIn("aita_animated", result)

    def test_yaml_for_script_aita_text(self):
        sp = MagicMock(spec=Path)
        sp.stem = "zzz"
        aaa = MagicMock(spec=Path); aaa.stem = "aaa"
        zzz = MagicMock(spec=Path); zzz.stem = "zzz"
        with patch("pathlib.Path.glob", return_value=[aaa, zzz]):
            result = self.m._yaml_for_script(sp, self.m.AITA_NICHE, (1, 30))
        self.assertIn("aita_text", result)

    def test_render_one_success(self):
        sp = Path("mystoriesanimated/reddit_amitheasshole/scripts/test.json")
        log_p = MagicMock(spec=Path)
        log_file = MagicMock()
        log_p.open.return_value = log_file
        proc = MagicMock()
        proc.returncode = 0
        with patch("subprocess.run", return_value=proc):
            slug, ok, msg = self.m._render_one(sp, "variant.yaml", log_p)
        self.assertEqual(slug, "test")
        self.assertTrue(ok)

    def test_render_one_failure(self):
        sp = Path("mystoriesanimated/reddit_amitheasshole/scripts/test.json")
        log_p = MagicMock(spec=Path)
        log_p.open.return_value = MagicMock()
        proc = MagicMock()
        proc.returncode = 1
        with patch("subprocess.run", return_value=proc):
            slug, ok, msg = self.m._render_one(sp, "variant.yaml", log_p)
        self.assertFalse(ok)

    def test_render_one_timeout(self):
        sp = Path("mystoriesanimated/reddit_amitheasshole/scripts/test.json")
        log_p = MagicMock(spec=Path)
        log_p.open.return_value = MagicMock()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 3600)):
            slug, ok, msg = self.m._render_one(sp, "variant.yaml", log_p)
        self.assertFalse(ok)
        self.assertIn("TIMEOUT", msg)

    def test_render_one_exception(self):
        sp = Path("mystoriesanimated/reddit_amitheasshole/scripts/test.json")
        log_p = MagicMock(spec=Path)
        log_p.open.return_value = MagicMock()
        with patch("subprocess.run", side_effect=OSError("boom")):
            slug, ok, msg = self.m._render_one(sp, "variant.yaml", log_p)
        self.assertFalse(ok)

    def test_enumerate_jobs_no_scripts_dir(self):
        with patch("pathlib.Path.exists", return_value=False):
            jobs = self.m._enumerate_jobs(["mystoriesanimated/aita_cooking"], (30, 30))
        self.assertEqual(jobs, [])

    def test_main_dry_run(self):
        sp = MagicMock(spec=Path)
        sp.stem = "s1"
        sp.relative_to = MagicMock(return_value=Path("s1.json"))
        log_p = MagicMock(spec=Path)
        log_p.parent.mkdir = MagicMock()
        log_p.touch = MagicMock()
        with patch.object(self.m, "_enumerate_jobs", return_value=[(sp, "variant.yaml")]), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.touch"), \
             patch("sys.argv", ["bulk_render_queue",
                                "--channels", "mystoriesanimated/reddit_amitheasshole",
                                "--dry-run", "--log", "bulk_render.log"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_no_jobs(self):
        with patch.object(self.m, "_enumerate_jobs", return_value=[]), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.touch"), \
             patch("sys.argv", ["bulk_render_queue",
                                "--channels", "mystoriesanimated/reddit_amitheasshole",
                                "--log", "bulk_render.log"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def _make_future(self, result):
        import concurrent.futures
        f = concurrent.futures.Future()
        f.set_result(result)
        return f

    def test_main_with_renders(self):
        sp = MagicMock(spec=Path)
        sp.stem = "s1"
        futs_data = [("s1", True, "ok")]

        # Use ThreadPoolExecutor to avoid pickling issues in tests
        import concurrent.futures
        real_futures = [self._make_future(r) for r in futs_data]

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = real_futures[0]

        with patch.object(self.m, "_enumerate_jobs", return_value=[(sp, "variant.yaml")]), \
             patch("scripts.ops.bulk_render_queue.ProcessPoolExecutor", return_value=mock_pool), \
             patch("scripts.ops.bulk_render_queue.as_completed", return_value=real_futures), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.touch"), \
             patch("sys.argv", ["bulk_render_queue",
                                "--channels", "mystoriesanimated/reddit_amitheasshole",
                                "--parallel", "1", "--log", "bulk_render.log"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_with_failure(self):
        sp = MagicMock(spec=Path)
        sp.stem = "s1"
        real_future = self._make_future(("s1", False, "error"))

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = real_future

        with patch.object(self.m, "_enumerate_jobs", return_value=[(sp, "variant.yaml")]), \
             patch("scripts.ops.bulk_render_queue.ProcessPoolExecutor", return_value=mock_pool), \
             patch("scripts.ops.bulk_render_queue.as_completed", return_value=[real_future]), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.touch"), \
             patch("sys.argv", ["bulk_render_queue",
                                "--channels", "mystoriesanimated/reddit_amitheasshole",
                                "--parallel", "1", "--log", "bulk_render.log"]):
            result = self.m.main()
        self.assertEqual(result, 1)


# ---------------------------------------------------------------------------
# 4. scripts/bulk_upload.py
# ---------------------------------------------------------------------------
class TestBulkUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.bulk_upload as m
        cls.m = m

    def test_read_cached_score_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            self.assertIsNone(self.m._read_cached_score("test-slug"))

    def test_read_cached_score_present(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value='{"score": 8}'):
            self.assertEqual(self.m._read_cached_score("test-slug"), 8)

    def test_read_cached_score_corrupt(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="!!!"):
            self.assertIsNone(self.m._read_cached_score("test-slug"))

    def test_discover_candidates_no_inter(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m.discover_candidates(name_filter=None)
        self.assertEqual(result, [])

    def test_discover_candidates_filter_excludes(self):
        inter = MagicMock(spec=Path)
        inter.exists.return_value = True
        inter.iterdir.return_value = []
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m.discover_candidates(name_filter="xyz-no-match")
        self.assertEqual(result, [])

    def test_print_table_empty(self):
        import io
        with patch("sys.stdout", new_callable=io.StringIO):
            self.m.print_table([], min_score=6)

    def test_print_table_with_rows(self):
        import io
        rows = [
            {"slug": "aita-test", "channel_dir": "mystoriesanimated",
             "mp4_size_mb": 3.5, "raw_present": True,
             "uploaded_already": False, "title": "AITA Title", "cached_score": 8},
            {"slug": "tifu-test", "channel_dir": "mystoriesanimated",
             "mp4_size_mb": 2.1, "raw_present": False,
             "uploaded_already": True, "title": "TIFU Title", "cached_score": 4},
            {"slug": "no-score", "channel_dir": "mystoriesanimated",
             "mp4_size_mb": 1.0, "raw_present": False,
             "uploaded_already": False, "title": None, "cached_score": None},
        ]
        with patch("sys.stdout", new_callable=io.StringIO):
            self.m.print_table(rows, min_score=6)

    def test_apply_uploads_no_upload_block(self):
        yaml_path = MagicMock(spec=Path)
        with patch("yaml.safe_load", return_value={"channel_id": "UC123"}):
            with self.assertRaises(SystemExit):
                self.m.apply_uploads([], channel_yaml_path=yaml_path,
                                     privacy="private", sleep_s=0,
                                     skip_critic=True, force_critic=False,
                                     min_score_override=None)

    def test_apply_uploads_nothing_pending(self):
        import io
        yaml_path = MagicMock(spec=Path)
        rows = [{"uploaded_already": True, "slug": "s", "channel_dir": "ch"}]
        with patch("yaml.safe_load", return_value={"upload": {"privacy": "private"}}), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(rows, channel_yaml_path=yaml_path,
                                 privacy="private", sleep_s=0,
                                 skip_critic=True, force_critic=False,
                                 min_score_override=None)

    def test_apply_uploads_success(self):
        import io
        yaml_path = MagicMock(spec=Path)
        rows = [{"uploaded_already": False, "slug": "s1", "channel_dir": "ch",
                 "mp4_path": Path("s1.mp4"), "script": {"hook": "h"}, "raw": None}]
        rec = {"slug": "s1", "url": "https://youtu.be/abc"}
        with patch("yaml.safe_load", return_value={"upload": {"privacy": "private"}}), \
             patch.object(self.m.up, "upload_short", return_value=rec), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(rows, channel_yaml_path=yaml_path,
                                 privacy="private", sleep_s=0,
                                 skip_critic=True, force_critic=False,
                                 min_score_override=None)

    def test_apply_uploads_critic_blocked(self):
        import io
        yaml_path = MagicMock(spec=Path)
        rows = [{"uploaded_already": False, "slug": "s1", "channel_dir": "ch",
                 "mp4_path": Path("s1.mp4"), "script": {"hook": "h"}, "raw": None}]
        with patch("yaml.safe_load", return_value={"upload": {"privacy": "private"}}), \
             patch.object(self.m.up, "upload_short",
                          side_effect=self.m.up.UploadError("critic score too low")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(rows, channel_yaml_path=yaml_path,
                                 privacy="private", sleep_s=0,
                                 skip_critic=False, force_critic=False,
                                 min_score_override=None)

    def test_apply_uploads_general_failure(self):
        import io
        yaml_path = MagicMock(spec=Path)
        rows = [{"uploaded_already": False, "slug": "s1", "channel_dir": "ch",
                 "mp4_path": Path("s1.mp4"), "script": {"hook": "h"}, "raw": None}]
        with patch("yaml.safe_load", return_value={"upload": {"privacy": "private"}}), \
             patch.object(self.m.up, "upload_short", side_effect=Exception("network")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(rows, channel_yaml_path=yaml_path,
                                 privacy="private", sleep_s=0,
                                 skip_critic=True, force_critic=False,
                                 min_score_override=None)

    def test_main_dry_run(self):
        import io
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="upload:\n  min_score: 6\n"), \
             patch("yaml.safe_load", return_value={"upload": {"min_score": 6}}), \
             patch.object(self.m, "discover_candidates", return_value=[]), \
             patch.object(self.m, "print_table"), \
             patch("sys.argv", ["bulk_upload", "--channel", "mystoriesanimated/config.yaml"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()

    def test_main_channel_not_found(self):
        with patch("pathlib.Path.exists", return_value=False), \
             patch("sys.argv", ["bulk_upload", "--channel", "bad/path.yaml"]):
            with self.assertRaises(SystemExit):
                self.m.main()

    def test_main_apply(self):
        import io
        rows = [{"uploaded_already": False, "slug": "s1", "channel_dir": "ch",
                 "mp4_path": Path("s1.mp4"), "script": {"hook": "h"}, "raw": None}]
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="upload:\n  min_score: 6\n"), \
             patch("yaml.safe_load", return_value={"upload": {"min_score": 6}}), \
             patch.object(self.m, "discover_candidates", return_value=rows), \
             patch.object(self.m, "print_table"), \
             patch.object(self.m, "apply_uploads") as mock_apply, \
             patch("sys.argv", ["bulk_upload", "--channel", "mystoriesanimated/config.yaml", "--apply"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()
        mock_apply.assert_called_once()


# ---------------------------------------------------------------------------
# 5. scripts/laptop_cleanup.py
# ---------------------------------------------------------------------------
class TestLaptopCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.laptop_cleanup as m
        cls.m = m

    def test_human_bytes(self):
        self.assertIn("B", self.m._human(500))

    def test_human_kib(self):
        self.assertIn("KiB", self.m._human(2048))

    def test_human_mib(self):
        self.assertIn("MiB", self.m._human(5 * 1024 * 1024))

    def test_human_gib(self):
        self.assertIn("GiB", self.m._human(5 * 1024 ** 3))

    def test_human_tib(self):
        self.assertIn("TiB", self.m._human(5 * 1024 ** 4))

    def test_dir_size_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            self.assertEqual(self.m._dir_size(Path("nonexistent")), 0)

    def test_dir_size_with_files(self):
        with patch("os.walk", return_value=[("/dir", [], ["f1.txt", "f2.txt"])]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=100)):
            size = self.m._dir_size(Path("/dir"))
        self.assertEqual(size, 200)

    def test_dir_size_oserror(self):
        with patch("os.walk", return_value=[("/dir", [], ["f.txt"])]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.stat", side_effect=OSError("no perm")):
            size = self.m._dir_size(Path("/dir"))
        self.assertEqual(size, 0)

    def test_wipe_dir_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m._wipe_dir(Path("/missing"), dry_run=True)
        self.assertEqual(result, 0)

    def test_wipe_dir_dry_run(self):
        p = self.m.DATA / "_jobs"
        with patch("pathlib.Path.exists", return_value=True), \
             patch.object(self.m, "_dir_size", return_value=1024 * 1024):
            result = self.m._wipe_dir(p, dry_run=True)
        self.assertEqual(result, 1024 * 1024)

    def test_wipe_dir_live(self):
        import shutil
        p = self.m.DATA / "_jobs"
        with patch("pathlib.Path.exists", return_value=True), \
             patch.object(self.m, "_dir_size", return_value=512), \
             patch("shutil.rmtree") as mock_rm, \
             patch("pathlib.Path.mkdir"):
            result = self.m._wipe_dir(p, dry_run=False)
        self.assertEqual(result, 512)
        mock_rm.assert_called_once()

    def test_wipe_old_cache_no_cache_dir(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m._wipe_old_cache(dry_run=True, cache_age_h=24.0)
        self.assertEqual(result, 0)

    def test_wipe_old_cache_skips_non_render(self):
        sub = MagicMock(spec=Path)
        sub.is_dir.return_value = True
        sub.name = "model_weights"
        # No img_*.png files → not a per-render slug
        sub.glob.side_effect = lambda p: []
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[sub]):
            result = self.m._wipe_old_cache(dry_run=True, cache_age_h=0.0)
        self.assertEqual(result, 0)

    def test_wipe_old_cache_deletes_old(self):
        sub = MagicMock(spec=Path)
        sub.is_dir.return_value = True
        sub.name = "aita-test-slug"
        sub.glob.side_effect = lambda p: [MagicMock()] if "img_" in p else []
        sub.stat.return_value.st_mtime = 0.0  # very old
        sub.relative_to = MagicMock(return_value=Path("data/cache/aita-test-slug"))
        with patch.object(self.m, "_dir_size", return_value=1024), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[sub]), \
             patch("shutil.rmtree"):
            result = self.m._wipe_old_cache(dry_run=False, cache_age_h=0.001)
        self.assertGreater(result, 0)

    def test_wipe_old_cache_recent_skipped(self):
        import time
        sub = MagicMock(spec=Path)
        sub.is_dir.return_value = True
        sub.name = "recent-slug"
        sub.glob.side_effect = lambda p: [MagicMock()] if "img_" in p else []
        sub.stat.return_value.st_mtime = time.time() + 9999  # future
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[sub]):
            result = self.m._wipe_old_cache(dry_run=True, cache_age_h=24.0)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_no_channel_cache(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m._wipe_render_scratch(dry_run=True, render_age_h=0.0)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_skips_recent(self):
        import time
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "aita-test"
        slug_dir.stat.return_value.st_mtime = time.time() + 9999
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]):
            result = self.m._wipe_render_scratch(dry_run=True, render_age_h=24.0)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_skips_underscore_dirs(self):
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "_special"  # starts with underscore → skip
        slug_dir.stat.return_value.st_mtime = 0.0
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]):
            result = self.m._wipe_render_scratch(dry_run=True, render_age_h=0.001)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_deletes_subdirs(self):
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "aita-test"
        slug_dir.stat.return_value.st_mtime = 0.0

        # Each sub under slug_dir
        sub = MagicMock(spec=Path)
        sub.exists.return_value = True
        sub.relative_to = MagicMock(return_value=Path("ch/cache/aita-test/tts_chunks"))

        # Each scratch file under slug_dir
        fmock = MagicMock(spec=Path)
        fmock.exists.return_value = False

        slug_dir.__truediv__ = MagicMock(side_effect=lambda k: sub if k in self.m.RENDER_SCRATCH_SUBDIRS else fmock)

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]), \
             patch.object(self.m, "_dir_size", return_value=2048), \
             patch("shutil.rmtree"):
            result = self.m._wipe_render_scratch(dry_run=False, render_age_h=0.001)
        self.assertGreater(result, 0)

    def test_wipe_render_scratch_stat_oserror(self):
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "aita-test"
        slug_dir.stat.side_effect = OSError("no stat")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]):
            result = self.m._wipe_render_scratch(dry_run=True, render_age_h=0.0)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_empty_subdir(self):
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "aita-test"
        slug_dir.stat.return_value.st_mtime = 0.0

        sub = MagicMock(spec=Path)
        sub.exists.return_value = True

        fmock = MagicMock(spec=Path)
        fmock.exists.return_value = False

        slug_dir.__truediv__ = MagicMock(side_effect=lambda k: sub if k in self.m.RENDER_SCRATCH_SUBDIRS else fmock)

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]), \
             patch.object(self.m, "_dir_size", return_value=0):
            result = self.m._wipe_render_scratch(dry_run=True, render_age_h=0.001)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_scratch_files(self):
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "aita-test"
        slug_dir.stat.return_value.st_mtime = 0.0

        sub = MagicMock(spec=Path)
        sub.exists.return_value = False  # subdirs don't exist

        fmock = MagicMock(spec=Path)
        fmock.exists.return_value = True
        fmock.stat.return_value.st_size = 512
        fmock.relative_to = MagicMock(return_value=Path("ch/cache/aita-test/_silence.wav"))

        slug_dir.__truediv__ = MagicMock(side_effect=lambda k: sub if k in self.m.RENDER_SCRATCH_SUBDIRS else fmock)

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]), \
             patch("scripts.laptop_cleanup.CHANNEL_DIRS", ("testchan",)), \
             patch.object(self.m, "_dir_size", return_value=0), \
             patch("pathlib.Path.unlink"):
            result = self.m._wipe_render_scratch(dry_run=False, render_age_h=0.001)
        self.assertEqual(result, 512 * len(self.m.RENDER_SCRATCH_FILES))

    def test_main_dry_run(self):
        import io
        with patch("sys.argv", ["laptop_cleanup", "--dry-run"]), \
             patch.object(self.m, "_wipe_dir", return_value=0), \
             patch.object(self.m, "_wipe_old_cache", return_value=0), \
             patch.object(self.m, "_wipe_render_scratch", return_value=0), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_live_keep_shorts(self):
        import io
        with patch("sys.argv", ["laptop_cleanup", "--keep-shorts"]), \
             patch.object(self.m, "_wipe_dir", return_value=100), \
             patch.object(self.m, "_wipe_old_cache", return_value=0), \
             patch.object(self.m, "_wipe_render_scratch", return_value=0), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_skip_render_scratch(self):
        import io
        with patch("sys.argv", ["laptop_cleanup", "--skip-render-scratch"]), \
             patch.object(self.m, "_wipe_dir", return_value=0), \
             patch.object(self.m, "_wipe_old_cache", return_value=0), \
             patch.object(self.m, "_wipe_render_scratch") as mock_scratch, \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        mock_scratch.assert_not_called()
        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# 6. scripts/update_thumbnails.py
# ---------------------------------------------------------------------------
class TestUpdateThumbnails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.update_thumbnails as m
        cls.m = m

    def test_find_records_no_base(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m.find_records(slug_filter=None)
        self.assertEqual(result, [])

    def test_find_records_with_data(self):
        chan_dir = MagicMock(spec=Path)
        chan_dir.is_dir.return_value = True
        chan_dir.name = "mystoriesanimated"
        rec_file = MagicMock(spec=Path)
        rec_file.stem = "aita-slug"
        rec_file.read_text.return_value = json.dumps({
            "slug": "aita-slug", "video_id": "vid123",
            "account": "myaccount", "url": "https://youtu.be/vid123", "title": "My title"})
        chan_dir.glob.return_value = [rec_file]
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[chan_dir]):
            result = self.m.find_records(slug_filter=None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["video_id"], "vid123")

    def test_find_records_slug_filter(self):
        chan_dir = MagicMock(spec=Path)
        chan_dir.is_dir.return_value = True
        chan_dir.name = "mystoriesanimated"
        rec_file = MagicMock(spec=Path)
        rec_file.stem = "aita-slug"
        rec_file.read_text.return_value = json.dumps(
            {"slug": "aita-slug", "video_id": "vid123"})
        chan_dir.glob.return_value = [rec_file]
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[chan_dir]):
            result = self.m.find_records(slug_filter="OTHER-SLUG")
        self.assertEqual(result, [])

    def test_find_records_no_video_id(self):
        chan_dir = MagicMock(spec=Path)
        chan_dir.is_dir.return_value = True
        chan_dir.name = "mystoriesanimated"
        rec_file = MagicMock(spec=Path)
        rec_file.stem = "aita-slug"
        rec_file.read_text.return_value = json.dumps({"slug": "aita-slug"})
        chan_dir.glob.return_value = [rec_file]
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[chan_dir]):
            result = self.m.find_records(slug_filter=None)
        self.assertEqual(result, [])

    def test_find_records_corrupt_json(self):
        chan_dir = MagicMock(spec=Path)
        chan_dir.is_dir.return_value = True
        chan_dir.name = "mystoriesanimated"
        rec_file = MagicMock(spec=Path)
        rec_file.stem = "aita-slug"
        rec_file.read_text.side_effect = OSError("io error")
        chan_dir.glob.return_value = [rec_file]
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[chan_dir]):
            result = self.m.find_records(slug_filter=None)
        self.assertEqual(result, [])

    def test_find_channel_yaml_for_niches(self):
        from pipeline import niches
        with patch.dict(niches.NICHE_CHANNEL,
                        {"test_niche": ("mystoriesanimated",
                                        "mystoriesanimated/variants/aita_animated.yaml")}):
            result = self.m.find_channel_yaml_for("mystoriesanimated")
        self.assertIsNotNone(result)

    def test_find_channel_yaml_for_heuristic_fallback(self):
        c1 = MagicMock(spec=Path)
        c1.stem = "mystoriesanimated"
        with patch("pipeline.niches.NICHE_CHANNEL", {}), \
             patch("pathlib.Path.glob", return_value=[c1]):
            result = self.m.find_channel_yaml_for("mystoriesanimated")
        # Returns c1 (first candidate)
        self.assertIsNotNone(result)

    def test_find_channel_yaml_for_no_candidates(self):
        with patch("pipeline.niches.NICHE_CHANNEL", {}), \
             patch("pathlib.Path.glob", return_value=[]):
            result = self.m.find_channel_yaml_for("unknownchan")
        self.assertIsNone(result)

    def test_find_script_for_present(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value='{"slug": "aita-slug"}'):
            result = self.m.find_script_for("mystoriesanimated", "aita-slug")
        self.assertEqual(result["slug"], "aita-slug")

    def test_find_script_for_corrupt(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="!!!"):
            result = self.m.find_script_for("mystoriesanimated", "aita-slug")
        self.assertEqual(result, {"slug": "aita-slug"})

    def test_find_script_for_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m.find_script_for("mystoriesanimated", "no-slug")
        self.assertEqual(result, {"slug": "no-slug"})

    def test_compose_no_frames(self):
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json")}
        with patch.object(self.m.th, "list_scene_frames", return_value=[]):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=None, apply=False)
        self.assertEqual(status, "skip")

    def test_compose_no_yaml(self):
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json")}
        with patch.object(self.m.th, "list_scene_frames", return_value=[Path("img_00.png")]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=None):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=None, apply=False)
        self.assertEqual(status, "error")
        self.assertIn("YAML", msg)

    def test_compose_thumb_none(self):
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json")}
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = "channel_id: UC123\n"
        with patch.object(self.m.th, "list_scene_frames", return_value=[Path("img_00.png")]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=yaml_path), \
             patch.object(self.m, "find_script_for", return_value={}), \
             patch.object(self.m.th, "auto_thumbnail", return_value=None), \
             patch("yaml.safe_load", return_value={}):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=None, apply=False)
        self.assertEqual(status, "error")

    def test_compose_dry_run(self):
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json")}
        thumb = MagicMock(spec=Path)
        thumb.name = "auto_thumb.jpg"
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = "channel_id: UC123\n"
        with patch.object(self.m.th, "list_scene_frames", return_value=[Path("img_00.png")]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=yaml_path), \
             patch.object(self.m, "find_script_for", return_value={}), \
             patch.object(self.m.th, "auto_thumbnail", return_value=thumb), \
             patch("yaml.safe_load", return_value={}):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=None, apply=False)
        self.assertEqual(status, "dry-run")

    def test_compose_apply_success(self):
        rec_path = MagicMock(spec=Path)
        rec_path.read_text.return_value = '{"video_id": "v"}'
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": rec_path}
        thumb = MagicMock(spec=Path)
        thumb.name = "auto_thumb.jpg"
        thumb.stat.return_value.st_size = 50000
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = "channel_id: UC123\n"
        with patch.object(self.m.th, "list_scene_frames", return_value=[Path("img_00.png")]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=yaml_path), \
             patch.object(self.m, "find_script_for", return_value={}), \
             patch.object(self.m.th, "auto_thumbnail", return_value=thumb), \
             patch.object(self.m.up, "set_thumbnail"), \
             patch("yaml.safe_load", return_value={}), \
             patch("json.loads", return_value={"video_id": "v"}):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override="HEADLINE", style_override=None,
                scene_index_override=0, apply=True)
        self.assertEqual(status, "ok")

    def test_compose_apply_upload_error(self):
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json")}
        thumb = MagicMock(spec=Path)
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = ""
        with patch.object(self.m.th, "list_scene_frames", return_value=[Path("img_00.png")]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=yaml_path), \
             patch.object(self.m, "find_script_for", return_value={}), \
             patch.object(self.m.th, "auto_thumbnail", return_value=thumb), \
             patch.object(self.m.up, "set_thumbnail",
                          side_effect=self.m.up.UploadError("quota")), \
             patch("yaml.safe_load", return_value={}):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=None, apply=True)
        self.assertEqual(status, "error")

    def test_compose_apply_unexpected_error(self):
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json")}
        thumb = MagicMock(spec=Path)
        yaml_path = MagicMock(spec=Path)
        yaml_path.read_text.return_value = ""
        with patch.object(self.m.th, "list_scene_frames", return_value=[Path("img_00.png")]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=yaml_path), \
             patch.object(self.m, "find_script_for", return_value={}), \
             patch.object(self.m.th, "auto_thumbnail", return_value=thumb), \
             patch.object(self.m.up, "set_thumbnail",
                          side_effect=RuntimeError("unexpected")), \
             patch("yaml.safe_load", return_value={}):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=None, apply=True)
        self.assertEqual(status, "error")
        self.assertIn("unexpected", msg)

    def test_main_no_records(self):
        import io
        with patch.object(self.m, "find_records", return_value=[]), \
             patch("sys.argv", ["update_thumbnails"]), \
             patch.object(self.m.th, "_STYLES", {}), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()

    def test_main_dry_run(self):
        import io
        rec = {"slug": "s", "channel_dir": "ch", "video_id": "v",
               "account": "a", "record_path": Path("r.json"),
               "url": "", "title": "T", "current_thumbnail_path": None}
        with patch.object(self.m, "find_records", return_value=[rec]), \
             patch.object(self.m, "compose_and_set_one", return_value=("dry-run", "would set")), \
             patch("sys.argv", ["update_thumbnails"]), \
             patch.object(self.m.th, "_STYLES", {}), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()

    def test_main_apply_multiple(self):
        import io
        recs = [
            {"slug": "s1", "channel_dir": "ch", "video_id": "v1", "account": "a",
             "record_path": Path("r.json"), "url": "", "title": "T1", "current_thumbnail_path": None},
            {"slug": "s2", "channel_dir": "ch", "video_id": "v2", "account": "a",
             "record_path": Path("r.json"), "url": "", "title": "T2", "current_thumbnail_path": None},
        ]
        results = [("ok", "done"), ("error", "fail")]
        with patch.object(self.m, "find_records", return_value=recs), \
             patch.object(self.m, "compose_and_set_one", side_effect=results), \
             patch("sys.argv", ["update_thumbnails", "--apply", "--sleep", "0"]), \
             patch.object(self.m.th, "_STYLES", {}), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()

    def test_main_style_arg(self):
        import io
        with patch.object(self.m, "find_records", return_value=[]), \
             patch.object(self.m.th, "_STYLES", {"aita": {}, "tifu": {}}), \
             patch("sys.argv", ["update_thumbnails", "--style", "aita"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()


# ---------------------------------------------------------------------------
# 7. scripts/sync_upload_records_to_gcs.py
# ---------------------------------------------------------------------------
class TestSyncUploadRecordsToGcs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.sync_upload_records_to_gcs as m
        cls.m = m

    def _make_chan_dir(self, name, rec_files=None, skip_config=False):
        """Build a MagicMock channel dir."""
        chan_dir = MagicMock(spec=Path)
        chan_dir.is_dir.return_value = True
        chan_dir.name = name

        config_yaml = MagicMock(spec=Path)
        config_yaml.exists.return_value = not skip_config

        uploads_dir = MagicMock(spec=Path)
        uploads_dir.exists.return_value = bool(rec_files)
        uploads_dir.rglob.return_value = rec_files or []

        def _div(key):
            if key == "config.yaml":
                return config_yaml
            if key == "uploads":
                return uploads_dir
            return MagicMock(spec=Path)
        chan_dir.__truediv__ = MagicMock(side_effect=_div)
        return chan_dir

    def _make_rec_file(self, video_id="abc123", name="historyrecapped"):
        f = MagicMock(spec=Path)
        f.read_text.return_value = json.dumps({"video_id": video_id})
        f.relative_to = MagicMock(return_value=Path(f"{name}/uploads/s.json"))
        return f

    def test_main_no_channels(self):
        with patch("pathlib.Path.iterdir", return_value=[]), \
             patch("sys.argv", ["sync"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_dry_run(self):
        rec_file = self._make_rec_file()
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[rec_file])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_record_rel_key", return_value="rel/key.json"), \
             patch.object(self.m.storage, "upload_record_uri", return_value="gs://bucket/key.json"), \
             patch.object(self.m.storage, "upload_bytes") as mock_ub, \
             patch("sys.argv", ["sync", "--dry-run"]):
            result = self.m.main()
        mock_ub.assert_not_called()
        self.assertEqual(result, 0)

    def test_main_push(self):
        rec_file = self._make_rec_file()
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[rec_file])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_record_rel_key", return_value="rel/key.json"), \
             patch.object(self.m.storage, "upload_record_uri", return_value="gs://bucket/key.json"), \
             patch.object(self.m.storage, "upload_bytes"), \
             patch("sys.argv", ["sync"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_upload_fail(self):
        rec_file = self._make_rec_file()
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[rec_file])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_record_rel_key", return_value="rel/key.json"), \
             patch.object(self.m.storage, "upload_record_uri", return_value="gs://bucket/key.json"), \
             patch.object(self.m.storage, "upload_bytes", side_effect=Exception("gcs error")), \
             patch("sys.argv", ["sync"]):
            result = self.m.main()
        self.assertEqual(result, 1)

    def test_main_skip_no_video_id(self):
        f = MagicMock(spec=Path)
        f.read_text.return_value = json.dumps({"slug": "no-vid"})
        f.relative_to = MagicMock(return_value=Path("ch/uploads/s.json"))
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[f])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_bytes") as mock_ub, \
             patch("sys.argv", ["sync"]):
            result = self.m.main()
        mock_ub.assert_not_called()
        self.assertEqual(result, 0)

    def test_main_channel_filter_match(self):
        rec_file = self._make_rec_file()
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[rec_file])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_record_rel_key", return_value="k"), \
             patch.object(self.m.storage, "upload_record_uri", return_value="gs://b/k"), \
             patch.object(self.m.storage, "upload_bytes"), \
             patch("sys.argv", ["sync", "--channel", "historyrecapped"]):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_channel_filter_no_match(self):
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[self._make_rec_file()])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_bytes") as mock_ub, \
             patch("sys.argv", ["sync", "--channel", "cosmosdecoded"]):
            result = self.m.main()
        mock_ub.assert_not_called()
        self.assertEqual(result, 0)

    def test_main_corrupt_json(self):
        f = MagicMock(spec=Path)
        f.read_text.side_effect = OSError("io error")
        f.relative_to = MagicMock(return_value=Path("ch/uploads/s.json"))
        chan_dir = self._make_chan_dir("historyrecapped", rec_files=[f])
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_bytes") as mock_ub, \
             patch("sys.argv", ["sync"]):
            result = self.m.main()
        mock_ub.assert_not_called()

    def test_main_no_config_yaml(self):
        chan_dir = self._make_chan_dir("not_a_channel", skip_config=True)
        with patch("pathlib.Path.iterdir", return_value=[chan_dir]), \
             patch.object(self.m.storage, "upload_bytes") as mock_ub, \
             patch("sys.argv", ["sync"]):
            result = self.m.main()
        mock_ub.assert_not_called()


# ---------------------------------------------------------------------------
# 8. scripts/clone_voice.py
# ---------------------------------------------------------------------------
class TestCloneVoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.clone_voice as m
        cls.m = m

    def test_main_calls_clone(self):
        cloned = MagicMock()
        cloned.ref_wav = Path("ref.wav")
        cloned.ref_text = "reference text"
        with patch("pipeline.voice_clone.clone_from_youtube", return_value=cloned) as mock_clone, \
             patch("sys.argv", ["clone_voice",
                                "--url", "https://youtu.be/test",
                                "--channel", "aita_animated",
                                "--slug", "aita-slug",
                                "--start", "5.0",
                                "--duration", "10.0"]):
            self.m.main()
        mock_clone.assert_called_once_with(
            "https://youtu.be/test",
            channel="aita_animated",
            slug="aita-slug",
            start=5.0,
            duration=10.0,
            asr_provider="whisper_mlx",
        )

    def test_main_default_args(self):
        cloned = MagicMock()
        cloned.ref_wav = Path("ref.wav")
        cloned.ref_text = "text"
        with patch("pipeline.voice_clone.clone_from_youtube", return_value=cloned), \
             patch("sys.argv", ["clone_voice",
                                "--url", "https://youtu.be/test",
                                "--channel", "mychan",
                                "--slug", "my-slug"]):
            self.m.main()


# ---------------------------------------------------------------------------
# 9. scripts/pull_backgrounds.py
# ---------------------------------------------------------------------------
class TestPullBackgrounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.pull_backgrounds as m
        cls.m = m

    def test_slugify(self):
        self.assertEqual(self.m._slugify("Hello World!"), "hello-world")

    def test_slugify_empty(self):
        result = self.m._slugify("!!!")
        self.assertEqual(result, "video")

    def test_slugify_long(self):
        result = self.m._slugify("a" * 100)
        self.assertEqual(len(result), 60)

    def test_search_candidates_filters_short(self):
        entries = [
            {"duration": 1200, "view_count": 5000, "id": "abc"},
            {"duration": 100, "view_count": 9999, "id": "def"},
        ]
        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.return_value = {"entries": entries}
        with patch("scripts.pull_backgrounds.YoutubeDL", return_value=mock_ydl):
            result = self.m.search_candidates("cooking", 5, 600)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "abc")

    def test_search_candidates_no_duration(self):
        entries = [{"duration": None, "view_count": 5000, "id": "abc"}]
        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.return_value = {"entries": entries}
        with patch("scripts.pull_backgrounds.YoutubeDL", return_value=mock_ydl):
            result = self.m.search_candidates("cooking", 5, 600)
        self.assertEqual(result, [])

    def test_download_video_cached(self):
        cache_dir = MagicMock(spec=Path)
        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.return_value = {"id": "testid123", "title": "Test"}
        # cache_dir / "testid123.mp4" → MagicMock; .exists() → MagicMock (truthy) → cached
        with patch("scripts.pull_backgrounds.YoutubeDL", return_value=mock_ydl):
            result = self.m.download_video("https://youtu.be/test", cache_dir)
        self.assertIsNotNone(result)

    def test_download_video_fresh(self):
        cache_dir = MagicMock(spec=Path)
        cache_dir.glob.return_value = []
        downloaded = MagicMock(spec=Path)
        downloaded.suffix = ".mp4"
        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.prepare_filename.return_value = "video.mp4"
        with patch("scripts.pull_backgrounds.YoutubeDL", return_value=mock_ydl), \
             patch("pathlib.Path.glob", return_value=[downloaded]):
            result = self.m.download_video("https://youtu.be/test", cache_dir)

    def test_probe_duration(self):
        # probe_duration uses text=True → check_output returns str
        with patch("subprocess.check_output", return_value="123.5\n"):
            result = self.m.probe_duration(Path("video.mp4"))
        self.assertAlmostEqual(result, 123.5)

    def test_probe_duration_error(self):
        with patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.m.probe_duration(Path("video.mp4"))

    def test_slice_to_vertical(self):
        with patch("subprocess.check_call") as mock_cc:
            self.m.slice_to_vertical(Path("src.mp4"), 10.0, 25.0, Path("dest.mp4"))
        mock_cc.assert_called_once()

    def test_has_face_false(self):
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = True
        cap_mock.get.return_value = 30.0
        cap_mock.set = MagicMock()
        # read returns (True, frame), detectMultiScale returns []
        frame = MagicMock()
        cap_mock.read.return_value = (True, frame)
        sys.modules["cv2"].VideoCapture.return_value = cap_mock
        sys.modules["cv2"].CascadeClassifier.return_value.detectMultiScale.return_value = []
        result = self.m.has_face(Path("clip.mp4"))
        self.assertFalse(result)

    def test_has_face_camera_not_opened(self):
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = False
        sys.modules["cv2"].VideoCapture.return_value = cap_mock
        result = self.m.has_face(Path("clip.mp4"))
        self.assertFalse(result)

    def test_pick_clip_starts_normal(self):
        starts = self.m.pick_clip_starts(300.0, 3, 25.0, 15.0, 20.0)
        self.assertLessEqual(len(starts), 3)

    def test_pick_clip_starts_too_short(self):
        starts = self.m.pick_clip_starts(30.0, 5, 25.0, 15.0, 20.0)
        self.assertEqual(len(starts), 0)

    def test_main_url_mode(self):
        src_path = MagicMock(spec=Path)
        src_path.stem = "my-video"
        dest_clip = MagicMock(spec=Path)
        dest_clip.exists.return_value = False
        with patch.object(self.m, "download_video", return_value=src_path), \
             patch.object(self.m, "probe_duration", return_value=300.0), \
             patch.object(self.m, "pick_clip_starts", return_value=[10.0, 50.0]), \
             patch.object(self.m, "slice_to_vertical"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.__truediv__", return_value=dest_clip), \
             patch("sys.argv", ["pull_backgrounds",
                                "--url", "https://youtu.be/test",
                                "--num-clips", "2", "--clip-len", "25"]):
            self.m.main()

    def test_main_search_no_candidates(self):
        with patch.object(self.m, "search_candidates", return_value=[]), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.argv", ["pull_backgrounds",
                                "--query", "cooking no face"]):
            with self.assertRaises(SystemExit) as cm:
                self.m.main()
            self.assertEqual(cm.exception.code, 2)

    def test_main_search_no_clip_starts(self):
        src_path = MagicMock(spec=Path)
        src_path.stem = "cooking-video"
        cand = {"id": "abc123", "url": "https://youtu.be/abc123",
                "title": "Cooking", "view_count": 5000, "duration": 700}
        with patch.object(self.m, "search_candidates", return_value=[cand]), \
             patch.object(self.m, "download_video", return_value=src_path), \
             patch.object(self.m, "probe_duration", return_value=300.0), \
             patch.object(self.m, "pick_clip_starts", return_value=[]), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.argv", ["pull_backgrounds", "--query", "cooking", "--num-clips", "2"]):
            with self.assertRaises(SystemExit) as cm:
                self.m.main()
            self.assertEqual(cm.exception.code, 3)

    def test_main_check_faces_all_rejected(self):
        src_path = MagicMock(spec=Path)
        src_path.stem = "cooking-video"
        dest_clip = MagicMock(spec=Path)
        dest_clip.exists.return_value = False
        with patch.object(self.m, "download_video", return_value=src_path), \
             patch.object(self.m, "probe_duration", return_value=300.0), \
             patch.object(self.m, "pick_clip_starts", return_value=[10.0]), \
             patch.object(self.m, "has_face", return_value=True), \
             patch.object(self.m, "slice_to_vertical"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.__truediv__", return_value=dest_clip), \
             patch("sys.argv", ["pull_backgrounds",
                                "--url", "https://youtu.be/test",
                                "--check-faces", "--max-face-rejects", "1",
                                "--num-clips", "1"]):
            self.m.main()


# ---------------------------------------------------------------------------
# 10. scripts/pull_hindi_voices.py
# ---------------------------------------------------------------------------
class TestPullHindiVoices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.pull_hindi_voices as m
        cls.m = m

    def test_ffmpeg_save_wav(self):
        proc = MagicMock()
        proc.returncode = 0
        with patch("subprocess.run", return_value=proc), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes"), \
             patch("pathlib.Path.unlink"):
            self.m._ffmpeg_save_wav(b"\x00" * 100, Path("out.wav"))

    def test_ffmpeg_save_wav_fail(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "error"
        with patch("subprocess.run", return_value=proc), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes"):
            with self.assertRaises(RuntimeError):
                self.m._ffmpeg_save_wav(b"\x00", Path("out.wav"))

    def test_whisper_transcribe_returns_string(self):
        # whisper is not installed → returns error string, which is fine
        result = self.m._whisper_transcribe(Path("audio.wav"))
        self.assertIsInstance(result, str)

    def test_whisper_transcribe_with_mock(self):
        whisper_mock = MagicMock()
        model_mock = MagicMock()
        model_mock.transcribe.return_value = {"text": "hello world"}
        whisper_mock.load_model.return_value = model_mock
        with patch.dict(sys.modules, {"whisper": whisper_mock}):
            result = self.m._whisper_transcribe(Path("audio.wav"))
        self.assertEqual(result, "hello world")

    def test_find_voice_no_rows(self):
        target = {
            "gender": "Male", "pitch_min": 100, "pitch_max": 160,
            "pitch_std_max": 30, "min_snr": 60,
            "speaking_rate_min": 9.0, "speaking_rate_max": 12.0,
            "age_groups": ["30-45"],
        }
        result = self.m.find_voice_for_target([], target)
        self.assertIsNone(result)

    def test_find_voice_match(self):
        target = {
            "gender": "Male", "pitch_min": 100, "pitch_max": 160,
            "pitch_std_max": 30, "min_snr": 60,
            "speaking_rate_min": 9.0, "speaking_rate_max": 12.0,
            "age_groups": ["30-45"], "preferred_age": "30-45",
        }
        row = {
            "gender": "Male",
            "utterance_pitch_mean": 130.0,
            "utterance_pitch_std": 20.0,
            "snr": 65.0,
            "speaking_rate": 10.0,
            "age_group": "30-45",
            "duration": 10.0,
            "verbatim": "Hello world",
            "audio_bytes": b"\x00" * 100,
            "speaker_id": "spk_001",
            "shard": "test-shard",
        }
        result = self.m.find_voice_for_target([row], target)
        self.assertIsNotNone(result)
        self.assertEqual(result["speaker_id"], "spk_001")

    def test_find_voice_wrong_gender(self):
        target = {
            "gender": "Male", "pitch_min": 100, "pitch_max": 160,
            "pitch_std_max": 30, "min_snr": 60,
            "speaking_rate_min": 9.0, "speaking_rate_max": 12.0,
            "age_groups": ["30-45"], "preferred_age": "30-45",
        }
        row = {
            "gender": "Female",  # wrong
            "utterance_pitch_mean": 130.0, "utterance_pitch_std": 20.0,
            "snr": 65.0, "speaking_rate": 10.0, "age_group": "30-45",
            "duration": 10.0, "verbatim": "Hi", "audio_bytes": b"\x00",
            "speaker_id": "spk_002", "shard": "test",
        }
        result = self.m.find_voice_for_target([row], target)
        self.assertIsNone(result)

    def test_main_mocked(self):
        import io
        hf_mod = types.ModuleType("huggingface_hub")
        hf_mod.hf_hub_download = MagicMock(return_value="fake_path.parquet")

        df_mock = MagicMock()
        df_mock.iterrows.return_value = iter([])

        table_mock = MagicMock()
        table_mock.to_pandas.return_value = df_mock

        pq_mod = types.ModuleType("pyarrow.parquet")
        pq_mod.read_table = MagicMock(return_value=table_mock)

        pa_mod = types.ModuleType("pyarrow")
        pa_mod.parquet = pq_mod

        with patch.dict(sys.modules, {
                "huggingface_hub": hf_mod,
                "pyarrow": pa_mod,
                "pyarrow.parquet": pq_mod,
            }), \
             patch("os.environ.get", return_value="fake_token"), \
             patch.object(self.m, "find_voice_for_target", return_value=None), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()


# ---------------------------------------------------------------------------
# 11. scripts/pull_stories.py
# ---------------------------------------------------------------------------
class TestPullStories(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.pull_stories as m
        cls.m = m

    def test_as_dict_dict(self):
        d = {"slug": "s", "body": "b"}
        result = self.m._as_dict(d)
        self.assertEqual(result["slug"], "s")

    def test_as_dict_dataclass(self):
        from dataclasses import dataclass
        @dataclass
        class Story:
            slug: str
            body: str
        s = Story(slug="s1", body="hello")
        result = self.m._as_dict(s)
        self.assertEqual(result["slug"], "s1")

    def test_load_channel_cfg_unknown(self):
        cfg, threshold = self.m._load_channel_cfg("unknown_channel_xyz")
        self.assertIsInstance(cfg, dict)
        self.assertIsInstance(threshold, float)

    def test_load_channel_cfg_with_yaml(self):
        fake_yaml_path = MagicMock()
        fake_yaml_path.exists.return_value = True
        fake_yaml_path.read_text.return_value = "visualizability_threshold: 0.7\n"
        with patch.dict(self.m._DEFAULT_CHANNEL_YAML, {"test_channel": fake_yaml_path}), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="visualizability_threshold: 0.7\n"):
            cfg, threshold = self.m._load_channel_cfg("test_channel")
        self.assertAlmostEqual(threshold, 0.7)

    def test_seen_post_ids_no_dir(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = self.m._seen_post_ids(Path("nonexistent"))
        self.assertEqual(result, set())

    def test_seen_post_ids_with_data(self):
        raw_path = MagicMock(spec=Path)
        raw_path.read_text.return_value = json.dumps({"metadata": {"post_id": "p123"}})
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.glob", return_value=[raw_path]):
            result = self.m._seen_post_ids(Path("raw"))
        self.assertIn("p123", result)

    def test_seen_post_ids_corrupt(self):
        raw_path = MagicMock(spec=Path)
        raw_path.read_text.side_effect = OSError("io error")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.glob", return_value=[raw_path]):
            result = self.m._seen_post_ids(Path("raw"))
        self.assertEqual(result, set())

    def test_emit_no_stories(self):
        import io
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            self.m._emit([], "test_channel", Path("out"))
        self.assertIn("no stories", mock_out.getvalue())

    def test_emit_all_seen(self):
        import io
        story = {"slug": "s1", "metadata": {"post_id": "p1"}, "body": "test"}
        with patch.object(self.m, "_seen_post_ids", return_value={"p1"}), \
             patch.object(self.m, "_load_channel_cfg", return_value=({}, 0.4)), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m._emit([story], "test_channel", Path("out"), run_llm=False)

    def test_emit_no_llm(self):
        import io
        story = {"slug": "s1", "metadata": {"post_id": "p1"}, "body": "test"}
        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=({}, 0.4)), \
             patch("scripts.pull_stories.save_raw", return_value=Path("raw/s1.json")), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m._emit([story], "test_channel", Path("out"), run_llm=False)

    def test_emit_visualizability_filter(self):
        import io
        story = {"slug": "s1", "metadata": {}, "body": "test", "title": "t"}
        cfg = {"some": "config"}
        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=(cfg, 0.9)), \
             patch.object(self.m.visualizability, "score_visualizability", return_value=(0.1, ["boring"])), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            self.m._emit([story], "test_channel", Path("out"), run_llm=True)
        self.assertIn("filter", mock_out.getvalue())

    def test_emit_no_survivors(self):
        import io
        story = {"slug": "s1", "metadata": {}, "body": "test", "title": "t"}
        cfg = {"some": "config"}
        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=(cfg, 0.9)), \
             patch.object(self.m.visualizability, "score_visualizability", return_value=(0.1, [])), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            self.m._emit([story], "test_channel", Path("out"), run_llm=True)
        self.assertIn("no candidates survived", mock_out.getvalue())

    def test_emit_pick_best(self):
        import io
        story1 = {"slug": "s1", "metadata": {}, "body": "body1", "title": "t1"}
        story2 = {"slug": "s2", "metadata": {}, "body": "body2", "title": "t2"}

        def mock_pick_best(survivor_dicts):
            best = survivor_dicts[0]
            return best, [(best, 0.8, []), (survivor_dicts[1] if len(survivor_dicts) > 1 else best, 0.5, [])]

        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=({}, 0.0)), \
             patch.object(self.m.drama, "pick_best", side_effect=mock_pick_best), \
             patch("scripts.pull_stories.save_raw", return_value=Path("raw/s1.json")), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m._emit([story1, story2], "test_channel", Path("out"),
                         run_llm=False, pick="best")

    def test_emit_with_llm(self):
        import io
        story = {"slug": "s1", "metadata": {}, "body": "test", "title": "t"}
        script_obj = {"hook": "hook text", "beats": []}

        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=({}, 0.0)), \
             patch("scripts.pull_stories.save_raw", return_value=Path("raw/s1.json")), \
             patch("scripts.pull_stories.rewrite.rewrite", return_value=script_obj), \
             patch("scripts.pull_stories.rewrite.save_script"), \
             patch("scripts.pull_stories.cast_mod.author_cast"), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m._emit([story], "test_channel", Path("out"), run_llm=True)

    def test_cmd_reddit(self):
        story = {"slug": "s1", "metadata": {}, "body": "body"}
        args = MagicMock()
        args.subreddit = "AmItheAsshole"
        args.listing = "top"
        args.timeframe = "day"
        args.limit = 5
        args.min_chars = 400
        args.max_chars = 6000
        args.channel = None
        args.no_llm = True
        args.pick = "all"
        with patch.object(self.m.reddit_api, "fetch", return_value=[story]), \
             patch.object(self.m, "_emit") as mock_emit:
            self.m.cmd_reddit(args, Path("out"))
        mock_emit.assert_called_once()

    def test_cmd_wiki(self):
        args = MagicMock()
        args.page = "unusual_deaths"
        args.limit = 5
        args.min_chars = 120
        args.max_chars = 1500
        args.channel = None
        args.no_llm = True
        with patch.object(self.m.wikipedia, "fetch", return_value=[]), \
             patch.object(self.m, "_emit") as mock_emit:
            self.m.cmd_wiki(args, Path("out"))
        mock_emit.assert_called_once()

    def test_cmd_tih(self):
        args = MagicMock()
        args.month = 1
        args.day = 1
        args.feed = "selected"
        args.limit = 5
        args.channel = None
        args.no_llm = True
        with patch.object(self.m.today_in_history, "fetch", return_value=[]), \
             patch.object(self.m, "_emit") as mock_emit:
            self.m.cmd_tih(args, Path("out"))
        mock_emit.assert_called_once()

    def test_cmd_youtube(self):
        args = MagicMock()
        args.url = "https://youtu.be/test"
        args.languages = "en,en-US"
        args.whisper_fallback = False
        args.channel = None
        args.no_llm = True
        with patch.object(self.m.youtube_video, "fetch", return_value=[]), \
             patch.object(self.m, "_emit") as mock_emit:
            self.m.cmd_youtube(args, Path("out"))
        mock_emit.assert_called_once()

    def test_main_reddit(self):
        with patch.object(self.m, "cmd_reddit") as mock_cmd, \
             patch("sys.argv", ["pull_stories", "reddit",
                                "--subreddit", "AmItheAsshole", "--limit", "5"]):
            self.m.main()
        mock_cmd.assert_called_once()

    def test_main_wiki(self):
        with patch.object(self.m, "cmd_wiki") as mock_cmd, \
             patch("sys.argv", ["pull_stories", "wiki"]):
            self.m.main()
        mock_cmd.assert_called_once()

    def test_main_tih(self):
        with patch.object(self.m, "cmd_tih") as mock_cmd, \
             patch("sys.argv", ["pull_stories", "tih"]):
            self.m.main()
        mock_cmd.assert_called_once()

    def test_main_youtube(self):
        with patch.object(self.m, "cmd_youtube") as mock_cmd, \
             patch("sys.argv", ["pull_stories", "youtube", "https://youtu.be/test"]):
            self.m.main()
        mock_cmd.assert_called_once()


# ---------------------------------------------------------------------------
# 12. scripts/setup_x_credentials.py
# ---------------------------------------------------------------------------
class TestSetupXCredentials(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.setup_x_credentials as m
        cls.m = m

    def test_v_consumer_key_valid(self):
        ok, _ = self.m._v_consumer_key("AbCdEfGhIjKlMnOpQrStu12")
        self.assertTrue(ok)

    def test_v_consumer_key_too_short(self):
        ok, _ = self.m._v_consumer_key("short")
        self.assertFalse(ok)

    def test_v_consumer_key_invalid_chars(self):
        ok, _ = self.m._v_consumer_key("Ab-Cd_Ef!GhIjKlMnOpQrS")
        self.assertFalse(ok)

    def test_v_consumer_secret_valid(self):
        ok, _ = self.m._v_consumer_secret("A" * 50)
        self.assertTrue(ok)

    def test_v_consumer_secret_too_short(self):
        ok, _ = self.m._v_consumer_secret("tooshort")
        self.assertFalse(ok)

    def test_v_access_token_valid(self):
        ok, _ = self.m._v_access_token("1234567890-" + "a" * 30)
        self.assertTrue(ok)

    def test_v_access_token_bearer_token(self):
        ok, why = self.m._v_access_token("AAAAAAA" + "x" * 50)
        self.assertFalse(ok)
        self.assertIn("Bearer", why)

    def test_v_access_token_wrong_format(self):
        ok, _ = self.m._v_access_token("notantoken")
        self.assertFalse(ok)

    def test_v_access_token_secret_valid(self):
        ok, _ = self.m._v_access_token_secret("A" * 45)
        self.assertTrue(ok)

    def test_v_access_token_secret_invalid(self):
        ok, _ = self.m._v_access_token_secret("short")
        self.assertFalse(ok)

    def test_prompt_valid_first_try(self):
        with patch("getpass.getpass", return_value="ValidValue123"):
            result = self.m._prompt("Test label", validator=lambda s: (True, ""))
        self.assertEqual(result, "ValidValue123")

    def test_prompt_retry_on_empty(self):
        with patch("getpass.getpass", side_effect=["", "ValidValue123"]):
            result = self.m._prompt("Test label", validator=lambda s: (True, ""))
        self.assertEqual(result, "ValidValue123")

    def test_prompt_retry_on_invalid(self):
        with patch("getpass.getpass", side_effect=["bad", "A" * 25]):
            result = self.m._prompt("CK", validator=self.m._v_consumer_key)
        self.assertEqual(result, "A" * 25)

    def test_main_no_args(self):
        with patch("sys.argv", ["setup_x_credentials"]):
            result = self.m.main()
        self.assertEqual(result, 2)

    def test_main_too_many_args(self):
        with patch("sys.argv", ["setup_x_credentials", "a1", "a2"]):
            result = self.m.main()
        self.assertEqual(result, 2)

    def test_main_invalid_account_name(self):
        with patch("sys.argv", ["setup_x_credentials", "bad@account!"]):
            result = self.m.main()
        self.assertEqual(result, 2)

    def test_main_file_exists_no_overwrite(self):
        import io
        with patch("sys.argv", ["setup_x_credentials", "testaccount"]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.input", return_value="n"), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 1)

    def test_main_success(self):
        import io
        cred_values = [
            "A" * 25,                 # consumer_key
            "B" * 50,                 # consumer_secret
            "123456789-" + "a" * 30,  # access_token
            "C" * 45,                 # access_token_secret
        ]
        with patch("sys.argv", ["setup_x_credentials", "testaccount"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch.object(self.m, "_prompt", side_effect=cred_values), \
             patch("builtins.input", return_value="testaccount"), \
             patch.object(self.m, "_verify_with_x", return_value=(True, "testaccount")), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"), \
             patch("os.chmod"), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_success_handle_mismatch(self):
        import io
        cred_values = [
            "A" * 25, "B" * 50, "123456789-" + "a" * 30, "C" * 45,
        ]
        with patch("sys.argv", ["setup_x_credentials", "testaccount"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch.object(self.m, "_prompt", side_effect=cred_values), \
             patch("builtins.input", return_value="mismatch_handle"), \
             patch.object(self.m, "_verify_with_x", return_value=(True, "real_handle")), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"), \
             patch("os.chmod"), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)

    def test_main_verification_failed(self):
        import io
        cred_values = [
            "A" * 25, "B" * 50, "123456789-" + "a" * 30, "C" * 45,
        ]
        with patch("sys.argv", ["setup_x_credentials", "testaccount"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch.object(self.m, "_prompt", side_effect=cred_values), \
             patch("builtins.input", return_value="testaccount"), \
             patch.object(self.m, "_verify_with_x", return_value=(False, "auth failed")), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 1)

    def test_verify_with_x_success(self):
        import io
        creds = {
            "consumer_key": "A" * 25,
            "consumer_secret": "B" * 50,
            "access_token": "123-" + "a" * 30,
            "access_token_secret": "C" * 45,
        }
        me_mock = MagicMock()
        me_mock.screen_name = "testuser"
        api_mock = MagicMock()
        api_mock.verify_credentials.return_value = me_mock

        client_mock = MagicMock()
        resp_mock = MagicMock()
        resp_mock.data = {"id": "tweet_123"}
        client_mock.create_tweet.return_value = resp_mock
        client_mock.delete_tweet = MagicMock()

        tweepy_mock = MagicMock()
        tweepy_mock.OAuth1UserHandler.return_value = MagicMock()
        tweepy_mock.API.return_value = api_mock
        tweepy_mock.Client.return_value = client_mock
        tweepy_mock.TweepyException = Exception
        tweepy_mock.Forbidden = Exception

        with patch.dict(sys.modules, {"tweepy": tweepy_mock}), \
             patch("sys.stdout", new_callable=io.StringIO):
            ok, handle = self.m._verify_with_x(creds)
        self.assertTrue(ok)
        self.assertEqual(handle, "testuser")

    def test_verify_with_x_no_tweepy(self):
        creds = {"consumer_key": "k", "consumer_secret": "s",
                 "access_token": "t", "access_token_secret": "ts"}
        with patch.dict(sys.modules, {"tweepy": None}):
            ok, msg = self.m._verify_with_x(creds)
        self.assertFalse(ok)



# ---------------------------------------------------------------------------
# Additional tests for coverage of previously missing lines
# ---------------------------------------------------------------------------

class _ExtraTestBulkRenderQueue(unittest.TestCase):
    """Extra coverage tests for bulk_render_queue._enumerate_jobs."""
    @classmethod
    def setUpClass(cls):
        import scripts.ops.bulk_render_queue as m
        cls.m = m

    def _make_root(self, scripts_exists, script_mocks, mp4_exists=False):
        mp4 = MagicMock(spec=Path)
        mp4.exists.return_value = mp4_exists

        shorts_dir = MagicMock(spec=Path)
        shorts_dir.__truediv__ = MagicMock(return_value=mp4)

        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = scripts_exists
        scripts_dir.glob.return_value = script_mocks

        def niche_div(key):
            return {"scripts": scripts_dir, "shorts": shorts_dir}.get(key, MagicMock())

        niche_mock = MagicMock(spec=Path)
        niche_mock.__truediv__ = MagicMock(side_effect=niche_div)

        root = MagicMock(spec=Path)
        root.__truediv__ = MagicMock(return_value=niche_mock)
        return root

    def test_enumerate_jobs_with_scripts(self):
        sp = MagicMock(spec=Path); sp.stem = "story1"
        root = self._make_root(scripts_exists=True, script_mocks=[sp], mp4_exists=False)
        with patch("scripts.ops.bulk_render_queue.PROJECT_ROOT", root):
            jobs = self.m._enumerate_jobs(["mystoriesanimated/aita_cooking"], (30, 30))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][0], sp)

    def test_enumerate_jobs_already_rendered_skips(self):
        sp = MagicMock(spec=Path); sp.stem = "done1"
        root = self._make_root(scripts_exists=True, script_mocks=[sp], mp4_exists=True)
        with patch("scripts.ops.bulk_render_queue.PROJECT_ROOT", root):
            jobs = self.m._enumerate_jobs(["mystoriesanimated/aita_cooking"], (30, 30))
        self.assertEqual(jobs, [])


class _ExtraTestBulkUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.bulk_upload as m
        cls.m = m

    def test_apply_uploads_general_exception(self):
        """Cover the generic except Exception branch (lines 194-196)."""
        import io
        row = {
            "slug": "bad-slug", "channel_dir": "ch", "mp4_path": MagicMock(),
            "script": {}, "raw": None, "raw_present": False,
            "uploaded_already": False, "title": "bad",
            "cached_score": None,
        }
        chan_yaml_path = MagicMock(spec=Path)
        chan_yaml_path.read_text.return_value = "upload:\n  playlist_id: p"
        with patch("scripts.bulk_upload.up.upload_short", side_effect=RuntimeError("boom")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(
                [row], channel_yaml_path=chan_yaml_path, privacy="private",
                sleep_s=0, skip_critic=False, force_critic=False, min_score_override=None,
            )

    def test_apply_uploads_sleep_between_uploads(self):
        """Cover the sleep branch (lines 197-199)."""
        import io
        def make_row(slug, uploaded=False):
            return {
                "slug": slug, "channel_dir": "ch", "mp4_path": MagicMock(),
                "script": {}, "raw": None, "raw_present": False,
                "uploaded_already": uploaded, "title": slug, "cached_score": None,
            }
        chan_yaml_path = MagicMock(spec=Path)
        chan_yaml_path.read_text.return_value = "upload:\n  playlist_id: p"
        with patch("scripts.bulk_upload.up.upload_short", return_value={"slug": "s1", "url": "https://y"}), \
             patch("scripts.bulk_upload.time.sleep") as mock_sleep, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(
                [make_row("s1"), make_row("s2")],
                channel_yaml_path=chan_yaml_path, privacy="private",
                sleep_s=1.0, skip_critic=False, force_critic=False, min_score_override=None,
            )
        mock_sleep.assert_called_once_with(1.0)

    def test_discover_candidates_with_data(self):
        """Cover lines 74-110 of discover_candidates."""
        sp = MagicMock(spec=Path); sp.stem = "my-slug"
        sp.read_text.return_value = '{"hook": "Hello"}'
        mp4 = MagicMock(spec=Path)
        mp4.exists.return_value = True
        mp4.stat.return_value.st_size = 1048576

        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = True
        scripts_dir.glob.return_value = [sp]

        raw_path = MagicMock(spec=Path)
        raw_path.exists.return_value = False

        raw_dir = MagicMock(spec=Path)
        raw_dir.__truediv__ = MagicMock(return_value=raw_path)

        def chan_div(key):
            return {"scripts": scripts_dir, "raw": raw_dir}.get(key, MagicMock())

        chan_dir = MagicMock(spec=Path)
        chan_dir.name = "reddit_amitheasshole"
        chan_dir.__truediv__ = MagicMock(side_effect=chan_div)

        inter = MagicMock(spec=Path)
        inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]

        shorts_dir = MagicMock(spec=Path)
        shorts_dir.__truediv__ = MagicMock(return_value=mp4)

        # PROJECT_ROOT / "data" builds a chain: ROOT → data_mock, data_mock / "intermediate" → inter
        data_mock = MagicMock(spec=Path)
        data_mock.__truediv__ = MagicMock(side_effect=lambda k: inter if k == "intermediate" else shorts_dir)

        mock_root = MagicMock(spec=Path)
        mock_root.__truediv__ = MagicMock(return_value=data_mock)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root), \
             patch("scripts.bulk_upload.up.existing_upload", return_value=False), \
             patch("scripts.bulk_upload._read_cached_score", return_value=80):
            rows = self.m.discover_candidates(name_filter=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slug"], "my-slug")


class _ExtraTestLaptopCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.laptop_cleanup as m
        cls.m = m

    def test_human_pib(self):
        # Each division: GiB→TiB→PiB needs size > 1 PiB
        result = self.m._human(1024 ** 5 + 1)
        self.assertIn("PiB", result)

    def test_wipe_old_cache_skips_non_dir(self):
        """Cover the if not sub.is_dir(): continue branch."""
        file_mock = MagicMock(spec=Path)
        file_mock.is_dir.return_value = False

        with patch("pathlib.Path.exists", return_value=True),              patch("pathlib.Path.iterdir", return_value=[file_mock]):
            result = self.m._wipe_old_cache(dry_run=True, cache_age_h=0.0)
        self.assertEqual(result, 0)

    def test_wipe_render_scratch_file_oserror(self):
        """Cover OSError when reading scratch file stat (lines 182-185 in wipe_render_scratch)."""
        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "aita-test"
        slug_dir.stat.return_value.st_mtime = 0.0  # old enough

        fmock = MagicMock(spec=Path)
        fmock.exists.return_value = True
        fmock.stat.side_effect = OSError("permission denied")

        sub = MagicMock(spec=Path)
        sub.exists.return_value = False

        slug_dir.__truediv__ = MagicMock(
            side_effect=lambda k: sub if k in self.m.RENDER_SCRATCH_SUBDIRS else fmock
        )

        with patch("pathlib.Path.exists", return_value=True),              patch("pathlib.Path.iterdir", return_value=[slug_dir]),              patch("scripts.laptop_cleanup.CHANNEL_DIRS", ("testchan",)),              patch.object(self.m, "_dir_size", return_value=0):
            result = self.m._wipe_render_scratch(dry_run=False, render_age_h=0.001)
        self.assertEqual(result, 0)


class _ExtraTestPullBackgrounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = sys.modules.get("scripts.pull_backgrounds")
        if cls.m is None:
            import scripts.pull_backgrounds as m
            cls.m = m

    def test_download_video_fresh_download(self):
        """Cover lines 72-87: fresh download path."""
        cache_dir = MagicMock(spec=Path)
        out_path = MagicMock(spec=Path)
        out_path.exists.return_value = False  # not cached; not produced after download

        fallback = MagicMock(spec=Path)
        cache_dir.__truediv__ = MagicMock(return_value=out_path)

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.return_value = {"id": "freshid", "title": "Fresh"}

        # out_path.exists() returns False → try glob fallback
        out_path.glob = MagicMock()  # glob on a path isn't the same as cache_dir.glob
        cache_dir.glob.return_value = [fallback]

        with patch("scripts.pull_backgrounds.YoutubeDL", return_value=mock_ydl):
            result = self.m.download_video("https://youtu.be/fresh", cache_dir)
        self.assertEqual(result, fallback)

    def test_has_face_zero_frames(self):
        """Cover lines 134-135: total frame count <= 0."""
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = True
        cap_mock.get.return_value = 0  # CAP_PROP_FRAME_COUNT = 0
        sys.modules["cv2"].VideoCapture.return_value = cap_mock
        result = self.m.has_face(Path("video.mp4"))
        self.assertFalse(result)
        cap_mock.release.assert_called_once()

    def test_has_face_read_fails(self):
        """Cover line 144: cap.read() returns ok=False → continue."""
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = True
        cap_mock.get.return_value = 30  # enough frames
        cap_mock.read.return_value = (False, None)  # read fails
        sys.modules["cv2"].VideoCapture.return_value = cap_mock
        sys.modules["cv2"].CascadeClassifier.return_value = MagicMock()
        result = self.m.has_face(Path("video.mp4"))
        self.assertFalse(result)

    def test_has_face_detected(self):
        """Cover lines 148-149: face detected → found=True, break."""
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = True
        cap_mock.get.return_value = 30
        frame = MagicMock()
        cap_mock.read.return_value = (True, frame)
        cascade = MagicMock()
        cascade.detectMultiScale.return_value = [(10, 10, 50, 50)]  # face found
        cv2_mod = sys.modules["cv2"]
        cv2_mod.VideoCapture.return_value = cap_mock
        cv2_mod.CascadeClassifier.return_value = cascade
        cv2_mod.cvtColor.return_value = frame
        result = self.m.has_face(Path("video.mp4"))
        self.assertTrue(result)

    def test_pick_clip_starts_single(self):
        """Cover line 161: num <= 1 → return center."""
        result = self.m.pick_clip_starts(300.0, 1, 25.0, 30.0, 30.0)
        self.assertEqual(len(result), 1)

    def test_main_face_sliding_retry_runs_out(self):
        """Cover lines 231-237: face check slides forward until runs out of room."""
        src_path = MagicMock(spec=Path); src_path.stem = "test-src"
        dest_clip = MagicMock(spec=Path)
        dest_clip.exists.return_value = False
        dest_clip.unlink = MagicMock()

        with patch.object(self.m, "download_video", return_value=src_path), \
             patch.object(self.m, "probe_duration", return_value=60.0), \
             patch.object(self.m, "pick_clip_starts", return_value=[10.0]), \
             patch.object(self.m, "has_face", return_value=True), \
             patch.object(self.m, "slice_to_vertical"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.write_text"), \
             patch("sys.argv", ["pull_backgrounds",
                                "--url", "https://youtu.be/t",
                                "--check-faces", "--max-face-rejects", "0",
                                "--clip-len", "25", "--num-clips", "1"]):
            # This will print "ran out of room" and stop
            self.m.main()


class _ExtraTestPullHindiVoices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.pull_hindi_voices as m
        cls.m = m

    def _base_target(self):
        return {
            "gender": "Male", "pitch_min": 100, "pitch_max": 160,
            "pitch_std_max": 30, "min_snr": 60,
            "speaking_rate_min": 9.0, "speaking_rate_max": 12.0,
            "age_groups": ["30-45"], "preferred_age": "30-45",
        }

    def _base_row(self):
        return {
            "gender": "Male", "utterance_pitch_mean": 130.0,
            "utterance_pitch_std": 20.0, "snr": 65.0, "speaking_rate": 10.0,
            "age_group": "30-45", "duration": 10.0, "verbatim": "Hello",
            "audio_bytes": b"\x00" * 100, "speaker_id": "spk_001", "shard": "s1",
        }

    def test_find_voice_wrong_age(self):
        """Cover line 123: age_group not in target age_groups."""
        row = self._base_row(); row["age_group"] = "60+"
        result = self.m.find_voice_for_target([row], self._base_target())
        self.assertIsNone(result)

    def test_find_voice_low_snr(self):
        """Cover line 125: snr below min_snr."""
        row = self._base_row(); row["snr"] = 30.0
        result = self.m.find_voice_for_target([row], self._base_target())
        self.assertIsNone(result)

    def test_find_voice_wrong_duration(self):
        """Cover line 127: duration out of range."""
        row = self._base_row(); row["duration"] = 0.1
        result = self.m.find_voice_for_target([row], self._base_target())
        self.assertIsNone(result)

    def test_find_voice_wrong_pitch(self):
        """Cover line 130: pitch out of range."""
        row = self._base_row(); row["utterance_pitch_mean"] = 50.0
        result = self.m.find_voice_for_target([row], self._base_target())
        self.assertIsNone(result)

    def test_find_voice_high_pitch_std(self):
        """Cover line 132: pitch_std too high."""
        row = self._base_row(); row["utterance_pitch_std"] = 200.0
        result = self.m.find_voice_for_target([row], self._base_target())
        self.assertIsNone(result)

    def test_find_voice_wrong_speaking_rate(self):
        """Cover line 135: speaking_rate out of range."""
        row = self._base_row(); row["speaking_rate"] = 20.0
        result = self.m.find_voice_for_target([row], self._base_target())
        self.assertIsNone(result)

    def test_main_processes_rows_and_shard_fail(self):
        """Cover lines 178-195: main() inner loop processes shard rows and handles exceptions."""
        import io
        import types

        hf_mod = types.ModuleType("huggingface_hub")
        hf_mod.hf_hub_download = MagicMock(return_value="fake.parquet")

        row_data = {
            "speaker_id": "spk1", "gender": "Female", "age_group": "30-45",
            "duration": 10.0, "snr": 70.0, "utterance_pitch_mean": 200.0,
            "utterance_pitch_std": 10.0, "speaking_rate": 10.0, "verbatim": "Hi",
            "audio": {"bytes": b"\x00"},
        }
        df_mock = MagicMock()
        df_mock.iterrows.return_value = iter([(0, MagicMock(to_dict=MagicMock(return_value=row_data)))])

        table_ok = MagicMock(); table_ok.to_pandas.return_value = df_mock
        table_fail = MagicMock(); table_fail.to_pandas.side_effect = RuntimeError("oops")

        pq_mod = types.ModuleType("pyarrow.parquet")
        call_count = [0]
        def read_table(path):
            call_count[0] += 1
            return table_fail if call_count[0] == 1 else table_ok
        pq_mod.read_table = read_table

        pa_mod = types.ModuleType("pyarrow")
        pa_mod.parquet = pq_mod

        with patch.dict(sys.modules, {"huggingface_hub": hf_mod, "pyarrow": pa_mod, "pyarrow.parquet": pq_mod}), \
             patch("sys.argv", ["pull_hindi_voices", "--shards", "0", "1", "--dry-run"]), \
             patch.object(self.m, "find_voice_for_target", return_value=None), \
             patch("pathlib.Path.read_text", return_value=""), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()

    def test_main_writes_voice_file(self):
        """Cover lines 216-241: main() writes voice files for matched targets."""
        import io
        import types

        hf_mod = types.ModuleType("huggingface_hub")
        hf_mod.hf_hub_download = MagicMock(return_value="fake.parquet")

        pq_mod = types.ModuleType("pyarrow.parquet")
        table = MagicMock()
        df = MagicMock()
        df.iterrows.return_value = iter([])
        table.to_pandas.return_value = df
        pq_mod.read_table = MagicMock(return_value=table)

        pa_mod = types.ModuleType("pyarrow"); pa_mod.parquet = pq_mod

        pick = {
            "speaker_id": "spk1", "gender": "Male", "age_group": "30-45",
            "duration": 10.0, "snr": 65.0, "utterance_pitch_mean": 130.0,
            "speaking_rate": 10.0, "verbatim": "Hello", "audio_bytes": b"\x00",
            "shard": "s1", "utterance_pitch_std": 20.0, "pitch": 130.0,
        }

        with patch.dict(sys.modules, {"huggingface_hub": hf_mod, "pyarrow": pa_mod, "pyarrow.parquet": pq_mod}), \
             patch("sys.argv", ["pull_hindi_voices", "--shards", "0", "--dry-run"]), \
             patch.object(self.m, "find_voice_for_target", return_value=pick), \
             patch("pathlib.Path.mkdir"), \
             patch.object(self.m, "_ffmpeg_save_wav"), \
             patch("pathlib.Path.write_text"), \
             patch("pathlib.Path.read_text", return_value=""), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()


class _ExtraTestPullStories(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.pull_stories as m
        cls.m = m

    def test_emit_rewrite_fails(self):
        """Cover lines 209-210: rewrite future raises exception."""
        import io
        story = {"slug": "my-slug", "title": "Test", "body": "Body", "metadata": {}}
        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=({}, 0.0)), \
             patch("scripts.pull_stories.save_raw", return_value=Path("raw/s.json")), \
             patch("scripts.pull_stories.rewrite.rewrite", side_effect=RuntimeError("rewrite fail")), \
             patch("scripts.pull_stories.rewrite.save_script"), \
             patch("scripts.pull_stories.cast_mod.author_cast"), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m._emit([story], "test_channel", Path("out"), run_llm=True)

    def test_emit_cast_fails(self):
        """Cover lines 214-215: cast future raises exception."""
        import io
        story = {"slug": "cast-fail", "title": "Fail", "body": "Body", "metadata": {}}
        with patch.object(self.m, "_seen_post_ids", return_value=set()), \
             patch.object(self.m, "_load_channel_cfg", return_value=({}, 0.0)), \
             patch("scripts.pull_stories.save_raw", return_value=Path("raw/s.json")), \
             patch("scripts.pull_stories.rewrite.rewrite", return_value={"hook": "h"}), \
             patch("scripts.pull_stories.rewrite.save_script"), \
             patch("scripts.pull_stories.cast_mod.author_cast", side_effect=RuntimeError("cast fail")), \
             patch("pathlib.Path.mkdir"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m._emit([story], "test_channel", Path("out"), run_llm=True)


class _ExtraTestSetupXCredentials(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.setup_x_credentials as m
        cls.m = m

    def _make_tweepy(self, verify_side_effect=None, create_tweet_side_effect=None,
                     create_tweet_return=None, delete_raises=False):
        """Return a minimal tweepy mock."""
        twmod = MagicMock()
        me = MagicMock()
        me.screen_name = "myhandle"
        api_v1 = MagicMock()

        if verify_side_effect:
            api_v1.verify_credentials.side_effect = verify_side_effect
        else:
            api_v1.verify_credentials.return_value = me

        if create_tweet_side_effect:
            twmod.Client.return_value.create_tweet.side_effect = create_tweet_side_effect
        elif create_tweet_return is not None:
            resp = MagicMock()
            resp.data = {"id": "123"}
            twmod.Client.return_value.create_tweet.return_value = resp
        else:
            resp = MagicMock()
            resp.data = None
            twmod.Client.return_value.create_tweet.return_value = resp

        if delete_raises:
            twmod.Client.return_value.delete_tweet.side_effect = RuntimeError("net")

        twmod.TweepyException = Exception
        twmod.Forbidden = type("Forbidden", (Exception,), {})
        twmod.OAuthHandler.return_value = MagicMock()
        twmod.API.return_value = api_v1
        return twmod

    def test_verify_tweepy_exception_on_verify(self):
        """Cover lines 103-104: verify_credentials raises TweepyException."""
        creds = {"consumer_key": "k"*25, "consumer_secret": "s"*50,
                 "access_token": "12345678901234-" + "a"*30, "access_token_secret": "ts"*25}
        twmod = self._make_tweepy()
        twmod.API.return_value.verify_credentials.side_effect = twmod.TweepyException("bad auth")
        with patch.dict(sys.modules, {"tweepy": twmod}):
            ok, msg = self.m._verify_with_x(creds)
        self.assertFalse(ok)
        self.assertIn("rejected", msg)

    def test_verify_no_screen_name(self):
        """Cover line 107: verify_credentials returns obj with no screen_name."""
        creds = {"consumer_key": "k"*25, "consumer_secret": "s"*50,
                 "access_token": "12345678901234-" + "a"*30, "access_token_secret": "ts"*25}
        twmod = self._make_tweepy()
        me = MagicMock()
        me.screen_name = None
        twmod.API.return_value.verify_credentials.return_value = me
        with patch.dict(sys.modules, {"tweepy": twmod}):
            ok, msg = self.m._verify_with_x(creds)
        self.assertFalse(ok)

    def test_verify_write_forbidden(self):
        """Cover lines 122-129: create_tweet raises Forbidden."""
        creds = {"consumer_key": "k"*25, "consumer_secret": "s"*50,
                 "access_token": "12345678901234-" + "a"*30, "access_token_secret": "ts"*25}
        twmod = self._make_tweepy()
        Forbidden = type("Forbidden", (Exception,), {})
        twmod.Forbidden = Forbidden
        twmod.TweepyException = Exception
        twmod.Client.return_value.create_tweet.side_effect = Forbidden("no write")
        with patch.dict(sys.modules, {"tweepy": twmod}):
            ok, msg = self.m._verify_with_x(creds)
        self.assertFalse(ok)
        self.assertIn("write permission", msg)

    def test_verify_tweepy_exception_on_tweet(self):
        """Cover lines 130-131: create_tweet raises TweepyException."""
        creds = {"consumer_key": "k"*25, "consumer_secret": "s"*50,
                 "access_token": "12345678901234-" + "a"*30, "access_token_secret": "ts"*25}
        twmod = self._make_tweepy()
        twmod.Forbidden = type("Forbidden", (Exception,), {})
        twmod.TweepyException = Exception
        twmod.Client.return_value.create_tweet.side_effect = Exception("unexpected")
        with patch.dict(sys.modules, {"tweepy": twmod}):
            ok, msg = self.m._verify_with_x(creds)
        self.assertFalse(ok)
        self.assertIn("unexpected", msg)

    def test_verify_delete_tweet_raises(self):
        """Cover lines 136-138: delete_tweet raises (just warns)."""
        creds = {"consumer_key": "k"*25, "consumer_secret": "s"*50,
                 "access_token": "12345678901234-" + "a"*30, "access_token_secret": "ts"*25}
        twmod = self._make_tweepy(create_tweet_return=True, delete_raises=True)
        twmod.Forbidden = type("Forbidden", (Exception,), {})
        twmod.TweepyException = Exception
        import io
        with patch.dict(sys.modules, {"tweepy": twmod}), \
             patch("sys.stdout", new_callable=io.StringIO):
            ok, handle = self.m._verify_with_x(creds)
        self.assertTrue(ok)

    def test_main_writes_file_with_chmod(self):
        """Cover lines 195-199: os.chmod after writing credentials."""
        import io
        import getpass as gp
        # Valid credential values that pass validators
        valid_secrets = [
            "k" * 25,              # consumer_key: 20-30 alnum
            "s" * 50,              # consumer_secret: 40-60 alnum
            "12345678901234-" + "a"*30,  # access_token: numeric_id-30chars
            "a" * 45,              # access_token_secret: 40-60 alnum
        ]
        with patch("sys.argv", ["setup_x_credentials.py", "airecap"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("getpass.getpass", side_effect=valid_secrets), \
             patch("builtins.input", return_value="airecap"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"), \
             patch("os.chmod"), \
             patch.object(self.m, "_verify_with_x", return_value=(True, "airecap")), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)


class _ExtraTestSyncUploadRecordsToGcs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.sync_upload_records_to_gcs as m
        cls.m = m

    def test_main_no_uploads_dir(self):
        """Cover line 47: uploads_dir does not exist."""
        import io

        uploads_dir = MagicMock(spec=Path)
        uploads_dir.exists.return_value = False  # no uploads dir

        config_yaml = MagicMock(spec=Path)
        config_yaml.exists.return_value = True

        def chan_div(key):
            return {"config.yaml": config_yaml, "uploads": uploads_dir}.get(key, MagicMock())

        chan_dir = MagicMock(spec=Path)
        chan_dir.__truediv__ = MagicMock(side_effect=chan_div)

        root = MagicMock(spec=Path)
        root.iterdir.return_value = [chan_dir]

        with patch("scripts.sync_upload_records_to_gcs.REPO_ROOT", root),              patch("sys.argv", ["sync_upload_records_to_gcs"]),              patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)


class _ExtraTestUpdateThumbnails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.update_thumbnails as m
        cls.m = m

    def test_find_records_non_dir_skipped(self):
        """Cover line 54: chan_dir is not a directory."""
        file_entry = MagicMock(spec=Path)
        file_entry.is_dir.return_value = False

        base = MagicMock(spec=Path)
        base.exists.return_value = True
        base.iterdir.return_value = [file_entry]

        # find_records uses PROJECT_ROOT / "data" / "uploads" — patch PROJECT_ROOT
        data_mock = MagicMock(spec=Path)
        data_mock.__truediv__ = MagicMock(return_value=base)
        mock_root = MagicMock(spec=Path)
        mock_root.__truediv__ = MagicMock(return_value=data_mock)

        with patch("scripts.update_thumbnails.PROJECT_ROOT", mock_root):
            records = self.m.find_records(slug_filter=None)
        self.assertEqual(records, [])

    def test_find_channel_yaml_exception_fallback(self):
        """Cover lines 91-92: niches lookup raises, falls back to heuristic."""
        # niches is imported locally inside find_channel_yaml_for — inject it
        import types
        fake_niches = types.ModuleType("pipeline.niches")
        fake_niches.NICHE_CHANNEL = {}  # empty → loop body skipped, no exception
        # To actually hit except branch: make NICHE_CHANNEL.items() raise
        bad_nc = MagicMock()
        bad_nc.items.side_effect = Exception("boom")
        fake_niches.NICHE_CHANNEL = bad_nc

        with patch.dict("sys.modules", {"pipeline.niches": fake_niches}), \
             patch("pathlib.Path.glob", return_value=[]):
            result = self.m.find_channel_yaml_for("mystoriesanimated")
        self.assertIsNone(result)

    def test_compose_record_write_error(self):
        """Cover lines 165-166: record write fails (warns but doesn't crash)."""
        import io
        rec = {
            "video_id": "vid123", "channel_dir": "testch",
            "slug": "my-slug", "current_thumbnail_path": None,
            "account": "default", "url": "", "title": "", "record_path": MagicMock(spec=Path),
        }
        rec_path = MagicMock(spec=Path)
        rec_path.name = "my-slug.json"
        rec_path.read_text.return_value = '{"video_id": "vid123"}'
        rec_path.write_text.side_effect = OSError("disk full")
        rec["record_path"] = rec_path

        frames = [MagicMock(spec=Path), MagicMock(spec=Path)]
        thumb = MagicMock(spec=Path)
        thumb.stat.return_value.st_size = 51200

        # up is the upload module: up.set_thumbnail(...)
        with patch.object(self.m.th, "list_scene_frames", return_value=frames), \
             patch.object(self.m.th, "auto_thumbnail", return_value=thumb), \
             patch.object(self.m.up, "set_thumbnail", return_value=None), \
             patch("sys.stdout", new_callable=io.StringIO):
            status, msg = self.m.compose_and_set_one(rec, headline_override=None,
                style_override=None, scene_index_override=0, apply=True)
        # Should complete without raising (write error is caught)
        self.assertIn(status, ("ok", "error"))

    def test_main_skip_status_with_sleep(self):
        """Cover lines 219-222: sleep after apply when status=='skip'."""
        import io
        rec = {"video_id": "vid", "channel_dir": "ch", "slug": "s", "title": "", "url": ""}
        with patch.object(self.m, "find_records", return_value=[rec, rec]), \
             patch.object(self.m, "find_channel_yaml_for", return_value=None), \
             patch.object(self.m, "compose_and_set_one", return_value=("skip", "already set")), \
             patch("scripts.update_thumbnails.time.sleep") as mock_sleep, \
             patch("sys.argv", ["update_thumbnails", "--apply", "--sleep", "0.1"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()
        mock_sleep.assert_called()


# ---------------------------------------------------------------------------
# Additional coverage gap tests
# ---------------------------------------------------------------------------

class _ExtraTestBulkUpload2(unittest.TestCase):
    """Covers discover_candidates branches not hit by the first extra class."""
    @classmethod
    def setUpClass(cls):
        import scripts.bulk_upload as m
        cls.m = m

    def _make_root(self, inter, shorts_dir):
        data_mock = MagicMock(spec=Path)
        data_mock.__truediv__ = MagicMock(
            side_effect=lambda k: inter if k == "intermediate" else shorts_dir)
        mock_root = MagicMock(spec=Path)
        mock_root.__truediv__ = MagicMock(return_value=data_mock)
        return mock_root

    def test_discover_candidates_no_scripts_dir(self):
        """Cover line 77: scripts dir missing → continue."""
        scripts_dir = MagicMock(spec=Path); scripts_dir.exists.return_value = False
        chan_dir = MagicMock(spec=Path); chan_dir.name = "niche_x"
        chan_dir.__truediv__ = MagicMock(return_value=scripts_dir)

        inter = MagicMock(spec=Path); inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]
        shorts_dir = MagicMock(spec=Path)
        mock_root = self._make_root(inter, shorts_dir)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root):
            rows = self.m.discover_candidates(name_filter=None)
        self.assertEqual(rows, [])

    def test_discover_candidates_name_filter_mismatch(self):
        """Cover line 81: name_filter doesn't match → continue."""
        sp = MagicMock(spec=Path); sp.stem = "boring-slug"
        sp.read_text.return_value = '{"hook": "h"}'
        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = True
        scripts_dir.glob.return_value = [sp]

        chan_dir = MagicMock(spec=Path); chan_dir.name = "niche_x"
        chan_dir.__truediv__ = MagicMock(return_value=scripts_dir)

        inter = MagicMock(spec=Path); inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]
        shorts_dir = MagicMock(spec=Path)
        mock_root = self._make_root(inter, shorts_dir)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root):
            rows = self.m.discover_candidates(name_filter="NOPE")
        self.assertEqual(rows, [])

    def test_discover_candidates_no_mp4(self):
        """Cover line 84: mp4 doesn't exist → continue."""
        sp = MagicMock(spec=Path); sp.stem = "my-slug"
        sp.read_text.return_value = '{"hook": "h"}'
        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = True
        scripts_dir.glob.return_value = [sp]

        mp4 = MagicMock(spec=Path); mp4.exists.return_value = False

        chan_dir = MagicMock(spec=Path); chan_dir.name = "niche_x"
        chan_dir.__truediv__ = MagicMock(return_value=scripts_dir)

        inter = MagicMock(spec=Path); inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]
        shorts_dir = MagicMock(spec=Path)
        shorts_dir.__truediv__ = MagicMock(return_value=mp4)
        mock_root = self._make_root(inter, shorts_dir)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root):
            rows = self.m.discover_candidates(name_filter=None)
        self.assertEqual(rows, [])

    def test_discover_candidates_invalid_script_json(self):
        """Cover lines 87-88: JSONDecodeError in script read → continue."""
        import json
        sp = MagicMock(spec=Path); sp.stem = "bad-slug"
        sp.read_text.return_value = 'NOTJSON'

        mp4 = MagicMock(spec=Path); mp4.exists.return_value = True

        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = True
        scripts_dir.glob.return_value = [sp]

        chan_dir = MagicMock(spec=Path); chan_dir.name = "niche_x"
        chan_dir.__truediv__ = MagicMock(return_value=scripts_dir)

        inter = MagicMock(spec=Path); inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]
        shorts_dir = MagicMock(spec=Path)
        shorts_dir.__truediv__ = MagicMock(return_value=mp4)
        mock_root = self._make_root(inter, shorts_dir)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root):
            rows = self.m.discover_candidates(name_filter=None)
        self.assertEqual(rows, [])

    def test_discover_candidates_raw_path_exists(self):
        """Cover lines 92-95: raw_path.exists()=True."""
        import json
        sp = MagicMock(spec=Path); sp.stem = "ok-slug"
        sp.read_text.return_value = '{"hook": "h"}'
        mp4 = MagicMock(spec=Path); mp4.exists.return_value = True
        mp4.stat.return_value.st_size = 2097152

        raw_path = MagicMock(spec=Path)
        raw_path.exists.return_value = True
        raw_path.read_text.return_value = '{"id": "raw1"}'

        raw_dir = MagicMock(spec=Path)
        raw_dir.__truediv__ = MagicMock(return_value=raw_path)

        def chan_div(key):
            if key == "scripts":
                return scripts_dir
            if key == "raw":
                return raw_dir
            return MagicMock()

        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = True
        scripts_dir.glob.return_value = [sp]

        chan_dir = MagicMock(spec=Path); chan_dir.name = "niche_x"
        chan_dir.__truediv__ = MagicMock(side_effect=chan_div)

        inter = MagicMock(spec=Path); inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]
        shorts_dir = MagicMock(spec=Path)
        shorts_dir.__truediv__ = MagicMock(return_value=mp4)
        mock_root = self._make_root(inter, shorts_dir)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root), \
             patch("scripts.bulk_upload.up.existing_upload", return_value=False), \
             patch("scripts.bulk_upload._read_cached_score", return_value=50):
            rows = self.m.discover_candidates(name_filter=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw"], {"id": "raw1"})

    def test_discover_candidates_raw_path_invalid_json(self):
        """Cover lines 94-95: raw_path.read_text() JSONDecodeError → raw=None."""
        sp = MagicMock(spec=Path); sp.stem = "ok-slug"
        sp.read_text.return_value = '{"hook": "h"}'
        mp4 = MagicMock(spec=Path); mp4.exists.return_value = True
        mp4.stat.return_value.st_size = 2097152

        raw_path = MagicMock(spec=Path)
        raw_path.exists.return_value = True
        raw_path.read_text.return_value = 'BADJSON'

        raw_dir = MagicMock(spec=Path)
        raw_dir.__truediv__ = MagicMock(return_value=raw_path)

        scripts_dir = MagicMock(spec=Path)
        scripts_dir.exists.return_value = True
        scripts_dir.glob.return_value = [sp]

        def chan_div(key):
            if key == "scripts": return scripts_dir
            if key == "raw": return raw_dir
            return MagicMock()

        chan_dir = MagicMock(spec=Path); chan_dir.name = "niche_x"
        chan_dir.__truediv__ = MagicMock(side_effect=chan_div)

        inter = MagicMock(spec=Path); inter.exists.return_value = True
        inter.iterdir.return_value = [chan_dir]
        shorts_dir = MagicMock(spec=Path)
        shorts_dir.__truediv__ = MagicMock(return_value=mp4)
        mock_root = self._make_root(inter, shorts_dir)

        with patch("scripts.bulk_upload.PROJECT_ROOT", mock_root), \
             patch("scripts.bulk_upload.up.existing_upload", return_value=False), \
             patch("scripts.bulk_upload._read_cached_score", return_value=50):
            rows = self.m.discover_candidates(name_filter=None)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["raw"])


class _ExtraTestLaptopCleanup2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.laptop_cleanup as m
        cls.m = m

    def test_wipe_render_scratch_rmdir_oserror(self):
        """Cover lines 182-185: rmdir raises OSError on empty dir (not dry_run)."""
        import io
        sub = MagicMock(spec=Path)
        sub.exists.return_value = True
        sub.rmdir.side_effect = OSError("busy")

        fmock = MagicMock(spec=Path)
        fmock.exists.return_value = False

        slug_dir = MagicMock(spec=Path)
        slug_dir.is_dir.return_value = True
        slug_dir.name = "slug-rmdir-fail"
        slug_dir.stat.return_value.st_mtime = 0.0
        slug_dir.__truediv__ = MagicMock(
            side_effect=lambda k: sub if k in self.m.RENDER_SCRATCH_SUBDIRS else fmock)

        with patch("scripts.laptop_cleanup.CHANNEL_DIRS", ("testchan",)), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", return_value=[slug_dir]), \
             patch.object(self.m, "_dir_size", return_value=0), \
             patch("sys.stdout", new_callable=io.StringIO):
            total = self.m._wipe_render_scratch(dry_run=False, render_age_h=0.001)
        sub.rmdir.assert_called()
        self.assertEqual(total, 0)


class _ExtraTestSetupXCredentials2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.setup_x_credentials as m
        cls.m = m

    def test_main_chmod_config_dir_oserror(self):
        """Cover lines 195-196: os.chmod on CONFIG_DIR raises OSError → caught."""
        import io
        valid_secrets = [
            "k" * 25,
            "s" * 50,
            "12345678901234-" + "a"*30,
            "a" * 45,
        ]
        # First call to os.chmod (CONFIG_DIR) raises, second call (out file) succeeds
        chmod_side_effect = [OSError("eperm"), None]
        with patch("sys.argv", ["setup_x_credentials.py", "airecap"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("getpass.getpass", side_effect=valid_secrets), \
             patch("builtins.input", return_value="airecap"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"), \
             patch("os.chmod", side_effect=chmod_side_effect), \
             patch.object(self.m, "_verify_with_x", return_value=(True, "airecap")), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.main()
        self.assertEqual(result, 0)


class _ExtraTestUpdateThumbnails2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.update_thumbnails as m
        cls.m = m

    def test_find_channel_yaml_niches_exception_fallback(self):
        """Cover lines 91-92: exception in niches lookup → pass → heuristic."""
        from pipeline import niches as real_niches
        bad_nc = MagicMock()
        bad_nc.items.side_effect = Exception("boom niches")
        import io
        with patch.object(real_niches, "NICHE_CHANNEL", bad_nc), \
             patch("pathlib.Path.glob", return_value=[]), \
             patch("sys.stdout", new_callable=io.StringIO):
            result = self.m.find_channel_yaml_for("somechan")
        self.assertIsNone(result)

    def test_compose_record_write_error_covered(self):
        """Cover lines 165-166: record write fails after set_thumbnail succeeds."""
        import io
        rec_path = MagicMock(spec=Path)
        rec_path.name = "my-slug.json"
        rec_path.read_text.return_value = '{"video_id": "vid123"}'
        rec_path.write_text.side_effect = OSError("disk full")

        frames = [MagicMock(spec=Path), MagicMock(spec=Path)]
        thumb = MagicMock(spec=Path)
        thumb.stat.return_value.st_size = 51200
        thumb.name = "auto_thumb.jpg"

        chan_yaml_path = MagicMock(spec=Path)
        chan_yaml_path.read_text.return_value = "{}"

        rec = {
            "video_id": "vid123", "channel_dir": "testch",
            "slug": "my-slug", "current_thumbnail_path": None,
            "account": "default", "url": "", "title": "",
            "record_path": rec_path,
        }

        with patch.object(self.m, "find_channel_yaml_for", return_value=chan_yaml_path), \
             patch.object(self.m, "find_script_for", return_value={"slug": "my-slug"}), \
             patch.object(self.m.th, "list_scene_frames", return_value=frames), \
             patch.object(self.m.th, "auto_thumbnail", return_value=thumb), \
             patch.object(self.m.up, "set_thumbnail", return_value=None), \
             patch("sys.stdout", new_callable=io.StringIO):
            status, msg = self.m.compose_and_set_one(
                rec, headline_override=None, style_override=None,
                scene_index_override=0, apply=True)
        # write_text raised, but function should still return "ok"
        self.assertEqual(status, "ok")


class _ExtraTestPullBackgrounds2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "cv2" not in sys.modules:
            sys.modules["cv2"] = MagicMock()
        import scripts.pull_backgrounds as m
        cls.m = m

    def _make_ydl_mocks(self, meta):
        ydl_probe = MagicMock()
        ydl_probe.__enter__ = MagicMock(return_value=ydl_probe)
        ydl_probe.__exit__ = MagicMock(return_value=False)
        ydl_probe.extract_info.return_value = meta
        ydl_dl = MagicMock()
        ydl_dl.__enter__ = MagicMock(return_value=ydl_dl)
        ydl_dl.__exit__ = MagicMock(return_value=False)
        return ydl_probe, ydl_dl

    def test_download_video_glob_fallback(self):
        """Cover line 85: out_path missing → glob finds an alternate file."""
        url = "https://youtu.be/glbvid"
        cache_dir = MagicMock(spec=Path)

        out_path = MagicMock(spec=Path)
        out_path.exists.return_value = False  # forces glob path

        alt_file = MagicMock(spec=Path)
        cache_dir.glob.return_value = iter([alt_file])
        cache_dir.__truediv__ = MagicMock(return_value=out_path)
        cache_dir.mkdir = MagicMock()

        meta = {"id": "glbvid", "title": "Glob Video", "duration": 300}
        ydl_probe, ydl_dl = self._make_ydl_mocks(meta)

        with patch("scripts.pull_backgrounds.YoutubeDL", side_effect=[ydl_probe, ydl_dl]):
            result = self.m.download_video(url, cache_dir)
        self.assertEqual(result, alt_file)

    def test_download_video_no_file_raises(self):
        """Cover line 86: download produced no file → RuntimeError."""
        url = "https://youtu.be/novid123"
        cache_dir = MagicMock(spec=Path)

        out_path = MagicMock(spec=Path)
        out_path.exists.return_value = False

        cache_dir.glob.return_value = iter([])  # glob finds nothing
        cache_dir.__truediv__ = MagicMock(return_value=out_path)
        cache_dir.mkdir = MagicMock()

        meta = {"id": "novid123", "title": "No File", "duration": 300}
        ydl_probe, ydl_dl = self._make_ydl_mocks(meta)

        with patch("scripts.pull_backgrounds.YoutubeDL", side_effect=[ydl_probe, ydl_dl]):
            with self.assertRaises(RuntimeError):
                self.m.download_video(url, cache_dir)


class _ExtraTestPullHindiVoices2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.pull_hindi_voices as m
        cls.m = m

    def test_main_voice_save_exception(self):
        """Cover lines 239-241: _ffmpeg_save_wav raises → n_fail incremented."""
        import io
        import types

        hf_mod = types.ModuleType("huggingface_hub")
        hf_mod.hf_hub_download = MagicMock(return_value="fake.parquet")

        pq_mod = types.ModuleType("pyarrow.parquet")
        table = MagicMock()
        df = MagicMock()
        df.iterrows.return_value = iter([])
        table.to_pandas.return_value = df
        pq_mod.read_table = MagicMock(return_value=table)

        pa_mod = types.ModuleType("pyarrow"); pa_mod.parquet = pq_mod

        pick = {
            "speaker_id": "spk1", "gender": "Male", "age_group": "30-45",
            "duration": 10.0, "snr": 65.0, "utterance_pitch_mean": 130.0,
            "speaking_rate": 10.0, "verbatim": "Hello", "audio_bytes": b"\x00",
            "shard": "s1", "utterance_pitch_std": 20.0,
        }

        with patch.dict(sys.modules, {"huggingface_hub": hf_mod, "pyarrow": pa_mod, "pyarrow.parquet": pq_mod}), \
             patch("sys.argv", ["pull_hindi_voices"]), \
             patch.object(self.m, "find_voice_for_target", return_value=pick), \
             patch("pathlib.Path.mkdir"), \
             patch.object(self.m, "_ffmpeg_save_wav", side_effect=RuntimeError("wav fail")), \
             patch("pathlib.Path.write_text"), \
             patch("pathlib.Path.read_text", return_value=""), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()


# ---------------------------------------------------------------------------
# Final coverage gap closers
# ---------------------------------------------------------------------------

class _FinalTestBulkUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scripts.bulk_upload as m
        cls.m = m

    def _make_row(self, slug):
        return {
            "slug": slug, "channel_dir": "ch", "mp4_path": MagicMock(),
            "script": {}, "raw": None, "raw_present": False,
            "uploaded_already": False, "title": slug, "cached_score": None,
        }

    def test_apply_uploads_upload_error_not_critic(self):
        """Cover lines 192-193: UploadError without 'critic score' in message."""
        import io
        UploadError = self.m.up.UploadError
        chan_yaml_path = MagicMock(spec=Path)
        chan_yaml_path.read_text.return_value = "upload:\n  playlist_id: p"
        with patch("scripts.bulk_upload.up.upload_short",
                   side_effect=UploadError("quota exceeded")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.apply_uploads(
                [self._make_row("s1")],
                channel_yaml_path=chan_yaml_path, privacy="private",
                sleep_s=0, skip_critic=False, force_critic=False, min_score_override=None,
            )


class _FinalTestPullBackgrounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "cv2" not in sys.modules:
            sys.modules["cv2"] = MagicMock()
        import scripts.pull_backgrounds as m
        cls.m = m

    def test_download_video_exists_after_download(self):
        """Cover line 87: out_path.exists() is True after download → return out_path."""
        url = "https://youtu.be/doneid"
        cache_dir = MagicMock(spec=Path)
        cache_dir.mkdir = MagicMock()

        out_path = MagicMock(spec=Path)
        # First call (line 69): False → proceed to download
        # Second call (line 82): True → return out_path (line 87)
        out_path.exists.side_effect = [False, True]
        cache_dir.__truediv__ = MagicMock(return_value=out_path)

        meta = {"id": "doneid", "title": "Done Video", "duration": 300}
        ydl_probe = MagicMock()
        ydl_probe.__enter__ = MagicMock(return_value=ydl_probe)
        ydl_probe.__exit__ = MagicMock(return_value=False)
        ydl_probe.extract_info.return_value = meta

        ydl_dl = MagicMock()
        ydl_dl.__enter__ = MagicMock(return_value=ydl_dl)
        ydl_dl.__exit__ = MagicMock(return_value=False)

        with patch("scripts.pull_backgrounds.YoutubeDL", side_effect=[ydl_probe, ydl_dl]):
            result = self.m.download_video(url, cache_dir)
        self.assertEqual(result, out_path)

    def test_main_face_sliding_out_of_room(self):
        """Cover lines 231-237: face detected, sliding forward runs out of room."""
        import io

        src_path = MagicMock(spec=Path); src_path.stem = "test-src"
        dest_clip = MagicMock(spec=Path)
        dest_clip.unlink = MagicMock()

        with patch.object(self.m, "download_video", return_value=src_path), \
             patch.object(self.m, "probe_duration", return_value=80.0), \
             patch.object(self.m, "pick_clip_starts", return_value=[10.0]), \
             patch.object(self.m, "has_face", return_value=True), \
             patch.object(self.m, "slice_to_vertical"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.write_text"), \
             patch("sys.argv", ["pull_backgrounds",
                                "--url", "https://youtu.be/t",
                                "--check-faces",
                                "--max-face-rejects", "10",
                                "--clip-len", "25",
                                "--tail-skip", "10",
                                "--num-clips", "1"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.m.main()


if __name__ == "__main__":
    unittest.main()
