"""100% line coverage for pipeline/utils/part2_watcher.py.

We pre-stub ``pipeline.rewrite`` (doesn't exist on disk) before
importing part2_watcher, then mock all external I/O (subprocess, upload
module, RenderPaths) so the tests run fully offline.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

# ── pre-stub the missing sibling module before importing part2_watcher ──
_REWRITE_STUB = types.ModuleType("pipeline.rewrite")
_REWRITE_STUB.rewrite_part2 = lambda **kw: {"slug": "test", "beats": []}
_REWRITE_STUB.save_script = lambda script, path: None
sys.modules.setdefault("pipeline.rewrite", _REWRITE_STUB)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import part2_watcher  # noqa: E402

_BASE = Path(__file__).resolve().parent


def _now_plus(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _now_minus(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class _ScratchBase(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        self._patch_root = patch("pipeline.part2_watcher.PROJECT_ROOT", self._scratch)
        self._patch_root.start()

    def tearDown(self) -> None:
        self._patch_root.stop()
        shutil.rmtree(self._scratch, ignore_errors=True)

    def _make_chan(self, name: str) -> Path:
        d = self._scratch / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text("name: test\n")
        return d

    def _make_sidecar(self, chan: str, slug: str, data: dict | None = None) -> Path:
        d = self._scratch / chan / "part2_pending"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{slug}.json"
        base = {
            "slug": slug,
            "account": "acct1",
            "baseline_subs": 1000,
            "threshold_subs_delta": 100,
            "part2_channel": f"{chan}_part2/config.yaml",
            "part1_channel_dir": chan,
        }
        if data:
            base.update(data)
        path.write_text(json.dumps(base))
        return path


# ─────────────────────────────────────────────────────────────────────────────
# _iter_pending_sidecars
# ─────────────────────────────────────────────────────────────────────────────

class TestIterPendingSidecars(_ScratchBase):
    def test_no_channels_returns_empty(self) -> None:
        # scratch dir has no channel dirs
        result = part2_watcher._iter_pending_sidecars()
        self.assertEqual(result, [])

    def test_channel_without_config_yaml_skipped(self) -> None:
        d = self._scratch / "nocfg"
        d.mkdir()
        result = part2_watcher._iter_pending_sidecars()
        self.assertEqual(result, [])

    def test_finds_canonical_sidecars(self) -> None:
        self._make_chan("mychan")
        sc = self._make_sidecar("mychan", "ep01")
        result = part2_watcher._iter_pending_sidecars()
        self.assertIn(sc, result)

    def test_finds_legacy_intermediate_sidecars(self) -> None:
        legacy = self._scratch / "data" / "intermediate" / "oldchan" / "part2_pending"
        legacy.mkdir(parents=True)
        sc = legacy / "old.json"
        sc.write_text(json.dumps({"slug": "old"}))
        result = part2_watcher._iter_pending_sidecars()
        self.assertIn(sc, result)

    def test_no_legacy_dir_is_fine(self) -> None:
        # data/intermediate doesn't exist — should not crash
        result = part2_watcher._iter_pending_sidecars()
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────────────────────────
# _load_sidecar
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadSidecar(unittest.TestCase):
    def test_valid_json_returns_dict(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            p = scratch / "sc.json"
            p.write_text(json.dumps({"slug": "x"}))
            result = part2_watcher._load_sidecar(p)
            self.assertEqual(result, {"slug": "x"})
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_oserror_returns_none(self) -> None:
        with patch("pathlib.Path.read_text", side_effect=OSError("no file")):
            result = part2_watcher._load_sidecar(Path("/fake/path.json"))
        self.assertIsNone(result)

    def test_invalid_json_returns_none(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            p = scratch / "bad.json"
            p.write_text("not json {{{")
            result = part2_watcher._load_sidecar(p)
            self.assertIsNone(result)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# _is_expired
# ─────────────────────────────────────────────────────────────────────────────

class TestIsExpired(unittest.TestCase):
    def test_no_window_expires_at_returns_false(self) -> None:
        self.assertFalse(part2_watcher._is_expired({}))

    def test_future_window_returns_false(self) -> None:
        sc = {"window_expires_at": _now_plus(3600)}
        self.assertFalse(part2_watcher._is_expired(sc))

    def test_past_window_returns_true(self) -> None:
        sc = {"window_expires_at": _now_minus(1)}
        self.assertTrue(part2_watcher._is_expired(sc))

    def test_invalid_iso_format_returns_false(self) -> None:
        sc = {"window_expires_at": "not-a-date"}
        self.assertFalse(part2_watcher._is_expired(sc))


# ─────────────────────────────────────────────────────────────────────────────
# _channel_dir_from_yaml
# ─────────────────────────────────────────────────────────────────────────────

class TestChannelDirFromYaml(unittest.TestCase):
    def test_returns_stem(self) -> None:
        result = part2_watcher._channel_dir_from_yaml(Path("/some/dir/mychannel.yaml"))
        self.assertEqual(result, "mychannel")


# ─────────────────────────────────────────────────────────────────────────────
# _copy_continuity_files
# ─────────────────────────────────────────────────────────────────────────────

class TestCopyContinuityFiles(_ScratchBase):
    def _run(self, src_paths, dst_paths):
        with patch("pipeline.paths.RenderPaths") as MockRP:
            MockRP.from_channel_dir.side_effect = [src_paths, dst_paths]
            with patch("shutil.copy2") as mock_copy:
                part2_watcher._copy_continuity_files("part1chan", "part2chan", "slug")
        return mock_copy

    def _make_src_paths(self, cast_exists: bool, raw_exists: bool):
        m = MagicMock()
        cast_mock = MagicMock()
        cast_mock.exists.return_value = cast_exists
        raw_mock = MagicMock()
        raw_mock.exists.return_value = raw_exists
        m.cast_for.return_value = cast_mock
        m.raw_for.return_value = raw_mock
        return m

    def _make_dst_paths(self, cast_exists: bool, raw_exists: bool):
        m = MagicMock()
        cast_mock = MagicMock()
        cast_mock.exists.return_value = cast_exists
        raw_mock = MagicMock()
        raw_mock.exists.return_value = raw_exists
        m.cast_for.return_value = cast_mock
        m.raw_for.return_value = raw_mock
        return m

    def test_canonical_src_used_and_file_copied(self) -> None:
        src = self._make_src_paths(cast_exists=True, raw_exists=True)
        dst = self._make_dst_paths(cast_exists=False, raw_exists=False)
        mock_copy = self._run(src, dst)
        self.assertEqual(mock_copy.call_count, 2)

    def test_dst_exists_skips_copy(self) -> None:
        src = self._make_src_paths(cast_exists=True, raw_exists=True)
        dst = self._make_dst_paths(cast_exists=True, raw_exists=True)
        mock_copy = self._run(src, dst)
        mock_copy.assert_not_called()

    def test_src_missing_skips_copy(self) -> None:
        src = self._make_src_paths(cast_exists=False, raw_exists=False)
        # Legacy paths also won't exist (scratch has no intermediate/)
        dst = self._make_dst_paths(cast_exists=False, raw_exists=False)
        mock_copy = self._run(src, dst)
        mock_copy.assert_not_called()

    def test_legacy_src_used_when_canonical_missing(self) -> None:
        # Make legacy cast file exist in scratch/data/intermediate/
        legacy_cast = (
            self._scratch / "data" / "intermediate" / "part1chan" / "cast"
        )
        legacy_cast.mkdir(parents=True)
        (legacy_cast / "slug.json").write_text("{}")
        legacy_raw = (
            self._scratch / "data" / "intermediate" / "part1chan" / "raw"
        )
        legacy_raw.mkdir(parents=True)
        (legacy_raw / "slug.json").write_text("{}")

        src = self._make_src_paths(cast_exists=False, raw_exists=False)
        dst = self._make_dst_paths(cast_exists=False, raw_exists=False)
        mock_copy = self._run(src, dst)
        self.assertEqual(mock_copy.call_count, 2)


# ─────────────────────────────────────────────────────────────────────────────
# _produce_and_upload
# ─────────────────────────────────────────────────────────────────────────────

class TestProduceAndUpload(_ScratchBase):
    def _base_sidecar(self) -> dict:
        return {
            "slug": "ep01",
            "part2_channel": "part2chan/config.yaml",
            "part1_channel_dir": "part1chan",
            "raw_story": {"text": "story"},
            "part1_narration": "narration text",
        }

    def _write_part2_yaml(self) -> None:
        d = self._scratch / "part2chan"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text("name: part2\n")

    def test_missing_yaml_returns_false(self) -> None:
        sc_path = self._scratch / "sc.json"
        sc_path.write_text("{}")
        result = part2_watcher._produce_and_upload(sc_path, self._base_sidecar())
        self.assertFalse(result)

    def test_rewrite_failure_returns_false(self) -> None:
        self._write_part2_yaml()
        sc_path = self._scratch / "sc.json"
        sc_path.write_text("{}")
        with patch.object(_REWRITE_STUB, "rewrite_part2", side_effect=RuntimeError("fail")):
            result = part2_watcher._produce_and_upload(sc_path, self._base_sidecar())
        self.assertFalse(result)

    def test_subprocess_exception_returns_false(self) -> None:
        self._write_part2_yaml()
        sc_path = self._scratch / "sc.json"
        sc_path.write_text("{}")
        narr_mock = MagicMock()
        with (
            patch("pipeline.paths.RenderPaths") as MockRP,
            patch("pipeline.part2_watcher._copy_continuity_files"),
            patch("subprocess.run", side_effect=OSError("no exe")),
        ):
            MockRP.from_channel_dir.return_value.narration_for.return_value = narr_mock
            result = part2_watcher._produce_and_upload(sc_path, self._base_sidecar())
        self.assertFalse(result)

    def test_subprocess_nonzero_returns_false(self) -> None:
        self._write_part2_yaml()
        sc_path = self._scratch / "sc.json"
        sc_path.write_text("{}")
        with (
            patch("pipeline.paths.RenderPaths") as MockRP,
            patch("pipeline.part2_watcher._copy_continuity_files"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        ):
            MockRP.from_channel_dir.return_value.narration_for.return_value = MagicMock()
            result = part2_watcher._produce_and_upload(sc_path, self._base_sidecar())
        self.assertFalse(result)

    def test_success_returns_true(self) -> None:
        self._write_part2_yaml()
        sc_path = self._scratch / "sc.json"
        sc_path.write_text("{}")
        with (
            patch("pipeline.paths.RenderPaths") as MockRP,
            patch("pipeline.part2_watcher._copy_continuity_files"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=0)),
        ):
            MockRP.from_channel_dir.return_value.narration_for.return_value = MagicMock()
            result = part2_watcher._produce_and_upload(sc_path, self._base_sidecar())
        self.assertTrue(result)


# ─────────────────────────────────────────────────────────────────────────────
# _tick_for_account
# ─────────────────────────────────────────────────────────────────────────────

class TestTickForAccount(_ScratchBase):
    def _sidecar_data(self, baseline: int = 1000, threshold: int = 100) -> dict:
        return {
            "slug": "ep01",
            "baseline_subs": baseline,
            "threshold_subs_delta": threshold,
            "part2_channel": "ch2/config.yaml",
            "part1_channel_dir": "ch1",
        }

    def test_sub_count_failure_skips_sidecars(self) -> None:
        sc_path = self._scratch / "sc.json"
        sc_path.write_text(json.dumps(self._sidecar_data()))
        with patch(
            "pipeline.upload.get_channel_sub_count",
            side_effect=RuntimeError("no quota"),
        ):
            # Should not raise, just print and return
            part2_watcher._tick_for_account("acct", [(sc_path, self._sidecar_data())])

    def test_delta_below_threshold_prints_waiting(self) -> None:
        sc_path = self._scratch / "sc.json"
        sc_path.write_text(json.dumps(self._sidecar_data(baseline=1000, threshold=100)))
        buf = io.StringIO()
        with (
            patch("pipeline.upload.get_channel_sub_count", return_value=1050),
            patch("sys.stdout", buf),
        ):
            part2_watcher._tick_for_account(
                "acct", [(sc_path, self._sidecar_data(baseline=1000, threshold=100))]
            )
        self.assertIn("waiting", buf.getvalue())

    def test_delta_at_threshold_triggers_and_unlinks(self) -> None:
        sc_path = self._scratch / "ep01.json"
        sc_data = self._sidecar_data(baseline=1000, threshold=100)
        sc_path.write_text(json.dumps(sc_data))
        with (
            patch("pipeline.upload.get_channel_sub_count", return_value=1100),
            patch("pipeline.part2_watcher._produce_and_upload", return_value=True),
        ):
            part2_watcher._tick_for_account("acct", [(sc_path, sc_data)])
        self.assertFalse(sc_path.exists())

    def test_trigger_unlink_failure_is_non_fatal(self) -> None:
        sc_path = self._scratch / "ep01.json"
        sc_data = self._sidecar_data(baseline=1000, threshold=100)
        sc_path.write_text(json.dumps(sc_data))
        buf = io.StringIO()
        with (
            patch("pipeline.upload.get_channel_sub_count", return_value=1100),
            patch("pipeline.part2_watcher._produce_and_upload", return_value=True),
            patch("pathlib.Path.unlink", side_effect=OSError("perm")),
            patch("sys.stdout", buf),
        ):
            part2_watcher._tick_for_account("acct", [(sc_path, sc_data)])
        self.assertIn("unlink failed", buf.getvalue())

    def test_trigger_produce_fails_no_unlink(self) -> None:
        sc_path = self._scratch / "ep01.json"
        sc_data = self._sidecar_data(baseline=1000, threshold=100)
        sc_path.write_text(json.dumps(sc_data))
        with (
            patch("pipeline.upload.get_channel_sub_count", return_value=1100),
            patch("pipeline.part2_watcher._produce_and_upload", return_value=False),
        ):
            part2_watcher._tick_for_account("acct", [(sc_path, sc_data)])
        # Sidecar should still exist (not deleted on failure)
        self.assertTrue(sc_path.exists())

    def test_slug_fallback_to_stem_when_missing(self) -> None:
        """If 'slug' key is absent, fallback to sc_path.stem for logging."""
        sc_path = self._scratch / "my_slug.json"
        sc_data = {"baseline_subs": 1000, "threshold_subs_delta": 100}
        sc_path.write_text(json.dumps(sc_data))
        # delta = 0 < 100, so it just prints "waiting"; no crash on missing slug.
        with patch("pipeline.upload.get_channel_sub_count", return_value=1000):
            part2_watcher._tick_for_account("acct", [(sc_path, sc_data)])


# ─────────────────────────────────────────────────────────────────────────────
# tick
# ─────────────────────────────────────────────────────────────────────────────

class TestTick(_ScratchBase):
    def test_no_sidecars_returns_early(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            part2_watcher.tick()
        self.assertIn("no pending", buf.getvalue())

    def test_expired_sidecar_deleted(self) -> None:
        self._make_chan("ch1")
        sc = self._make_sidecar("ch1", "old", {"window_expires_at": _now_minus(10)})
        part2_watcher.tick()
        self.assertFalse(sc.exists())

    def test_expired_sidecar_unlink_failure_is_non_fatal(self) -> None:
        self._make_chan("ch1")
        sc = self._make_sidecar("ch1", "old", {"window_expires_at": _now_minus(10)})
        buf = io.StringIO()
        with (
            patch("pathlib.Path.unlink", side_effect=OSError("perm")),
            patch("sys.stdout", buf),
        ):
            part2_watcher.tick()
        self.assertIn("unlink failed", buf.getvalue())

    def test_none_sidecar_skipped(self) -> None:
        self._make_chan("ch1")
        self._make_sidecar("ch1", "ep01")
        with (
            patch("pipeline.part2_watcher._load_sidecar", return_value=None),
            patch("pipeline.part2_watcher._tick_for_account") as mock_tick,
        ):
            part2_watcher.tick()
        mock_tick.assert_not_called()

    def test_valid_sidecars_grouped_by_account(self) -> None:
        self._make_chan("ch1")
        self._make_sidecar("ch1", "ep01", {"account": "myacct"})
        with patch("pipeline.part2_watcher._tick_for_account") as mock_tick:
            part2_watcher.tick()
        mock_tick.assert_called_once()
        acct, sc_list = mock_tick.call_args[0]
        self.assertEqual(acct, "myacct")

    def test_no_account_defaults_to_default(self) -> None:
        self._make_chan("ch1")
        self._make_sidecar("ch1", "ep01", {"account": None})
        with patch("pipeline.part2_watcher._tick_for_account") as mock_tick:
            part2_watcher.tick()
        mock_tick.assert_called_once()
        acct, _ = mock_tick.call_args[0]
        self.assertEqual(acct, "default")


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def test_once_flag_calls_tick_and_returns(self) -> None:
        with (
            patch("pipeline.part2_watcher.tick") as mock_tick,
            patch("sys.argv", ["p2w", "--once"]),
        ):
            part2_watcher.main()
        mock_tick.assert_called_once()

    def test_loop_handles_exception_then_keyboard_interrupt(self) -> None:
        side_effects = [Exception("boom"), KeyboardInterrupt()]
        buf = io.StringIO()
        with (
            patch("pipeline.part2_watcher.tick", side_effect=side_effects),
            patch("pipeline.part2_watcher.time.sleep"),
            patch("sys.argv", ["p2w"]),
            patch("sys.stdout", buf),
        ):
            part2_watcher.main()
        output = buf.getvalue()
        self.assertIn("starting", output)
        self.assertIn("interrupted", output)
        self.assertIn("boom", output)


if __name__ == "__main__":
    unittest.main()
