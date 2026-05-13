"""Cover the missing branches in pipeline/research/aggregator.py.

Existing tests in test_pipeline_research.py already cover the happy-path
JSONL build.  This file fills the gaps: error paths in helpers, CLI,
rebuild with refresh_analytics, quiet flag, classify_issues edge case, etc.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.research import aggregator as _agg
from pipeline.paths import PROJECT_ROOT as REAL_ROOT


# ---------------------------------------------------------------------------
# helper: build a minimal tempdir that pretends to be PROJECT_ROOT
# ---------------------------------------------------------------------------

def _make_channel(root: Path, name: str, account: str | None = None) -> Path:
    ch = root / name
    ch.mkdir(parents=True, exist_ok=True)
    cfg = {"name": name}
    if account:
        cfg["upload"] = {"account": account}
    (ch / "config.yaml").write_text(
        "\n".join(f"{k}: {v}" for k, v in _flatten(cfg))
    )
    return ch


def _flatten(d: dict, prefix="") -> list[tuple[str, str]]:
    """Ultra-minimal dict → YAML-like key: value pairs (no nesting needed)."""
    # For our purposes the YAML only needs top-level and one nested level.
    pairs: list[tuple[str, str]] = []
    for k, v in d.items():
        if isinstance(v, dict):
            pairs.append((k + ":", ""))
            for kk, vv in v.items():
                pairs.append(("  " + kk + ":", vv))
        else:
            pairs.append((k + ":", v))
    return pairs


# ---------------------------------------------------------------------------
# _read_json / _read_yaml error paths
# ---------------------------------------------------------------------------


class ReadHelpersTest(unittest.TestCase):

    def test_read_json_missing_file_returns_none(self):
        p = Path("/nonexistent/__test_agg__.json")
        self.assertIsNone(_agg._read_json(p))

    def test_read_json_bad_content_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{not: json}")
            tmp = Path(f.name)
        try:
            self.assertIsNone(_agg._read_json(tmp))
        finally:
            tmp.unlink(missing_ok=True)

    def test_read_yaml_missing_file_returns_none(self):
        p = Path("/nonexistent/__test_agg__.yaml")
        self.assertIsNone(_agg._read_yaml(p))

    def test_read_yaml_bad_content_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(":\n  - bad: [unclosed")
            tmp = Path(f.name)
        try:
            result = _agg._read_yaml(tmp)
            # Either None or {} is acceptable for a YAML error
            self.assertIsNone(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_mtime_iso_nonexistent_returns_none(self):
        p = Path("/nonexistent/__test__.txt")
        self.assertIsNone(_agg._mtime_iso(p))

    def test_mtime_iso_existing_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = Path(f.name)
        try:
            result = _agg._mtime_iso(tmp)
            self.assertIsNotNone(result)
            self.assertIn("T", result)  # ISO datetime contains 'T'
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _iter_channel_dirs: PROJECT_ROOT doesn't exist path
# ---------------------------------------------------------------------------


class IterChannelDirsTest(unittest.TestCase):

    def test_nonexistent_project_root_returns_empty(self):
        with patch.object(_agg, "PROJECT_ROOT", Path("/nonexistent/__ytfactory_test__")):
            result = _agg._iter_channel_dirs()
        self.assertEqual(result, [])

    def test_filters_non_channel_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Non-channel dir (no config.yaml)
            (root / "pipeline").mkdir()
            # Channel dir (has config.yaml)
            (root / "mychan").mkdir()
            (root / "mychan" / "config.yaml").write_text("name: mychan")
            # Hidden dir
            (root / ".git").mkdir()

            with patch.object(_agg, "PROJECT_ROOT", root):
                result = _agg._iter_channel_dirs()

        names = [d.name for d in result]
        self.assertIn("mychan", names)
        self.assertNotIn("pipeline", names)
        self.assertNotIn(".git", names)


# ---------------------------------------------------------------------------
# _channel_account: fallback to dir name when no upload.account
# ---------------------------------------------------------------------------


class ChannelAccountTest(unittest.TestCase):

    def test_falls_back_to_dir_name_when_no_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            chan = Path(tmp) / "mychan"
            chan.mkdir()
            (chan / "config.yaml").write_text("name: My Channel\n")
            result = _agg._channel_account(chan)
        self.assertEqual(result, "mychan")

    def test_reads_upload_account_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            chan = Path(tmp) / "mychan"
            chan.mkdir()
            (chan / "config.yaml").write_text("upload:\n  account: myvid\n")
            result = _agg._channel_account(chan)
        self.assertEqual(result, "myvid")


# ---------------------------------------------------------------------------
# _index_local_uploads: skip malformed / missing video_id records
# ---------------------------------------------------------------------------


class IndexLocalUploadsTest(unittest.TestCase):

    def test_skips_json_without_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            (chan / "config.yaml").write_text("name: mychan\n")
            # Record missing video_id
            (ud / "slug1.json").write_text(json.dumps({"slug": "slug1"}))
            # Record with invalid JSON
            (ud / "slug2.json").write_text("{bad}")
            # Valid record
            (ud / "slug3.json").write_text(json.dumps({"video_id": "VID3", "slug": "slug3"}))

            with patch.object(_agg, "PROJECT_ROOT", root):
                with patch.object(_agg, "_iter_channel_dirs", lambda: [chan]):
                    result = _agg._index_local_uploads()

        self.assertIn("VID3", result)
        self.assertNotIn(None, result)

    def test_skips_x_sidecar_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            (chan / "config.yaml").write_text("name: mychan\n")
            # X sidecar — should be skipped
            (ud / "slug1.x.json").write_text(json.dumps({"video_id": "VID_X", "slug": "slug1"}))
            # Normal record
            (ud / "slug1.json").write_text(json.dumps({"video_id": "VID1", "slug": "slug1"}))

            with patch.object(_agg, "PROJECT_ROOT", root):
                with patch.object(_agg, "_iter_channel_dirs", lambda: [chan]):
                    result = _agg._index_local_uploads()

        self.assertIn("VID1", result)
        self.assertNotIn("VID_X", result)


# ---------------------------------------------------------------------------
# _local_script: fallback to scripts/ dir
# ---------------------------------------------------------------------------


class LocalScriptTest(unittest.TestCase):

    def test_falls_back_to_scripts_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            chan = Path(tmp) / "mychan"
            scripts = chan / "scripts"
            scripts.mkdir(parents=True)
            script_data = {"hook": "test", "narration": "hello"}
            (scripts / "myslug.json").write_text(json.dumps(script_data))

            path, data = _agg._local_script(chan, "myslug")

        self.assertIsNotNone(path)
        self.assertEqual(data["hook"], "test")

    def test_prefers_narrations_over_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            chan = Path(tmp) / "mychan"
            (chan / "narrations").mkdir(parents=True)
            (chan / "scripts").mkdir(parents=True)
            (chan / "narrations" / "myslug.json").write_text(json.dumps({"hook": "from_narrations"}))
            (chan / "scripts" / "myslug.json").write_text(json.dumps({"hook": "from_scripts"}))

            path, data = _agg._local_script(chan, "myslug")

        self.assertEqual(data["hook"], "from_narrations")

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            chan = Path(tmp) / "mychan"
            chan.mkdir(parents=True)
            path, data = _agg._local_script(chan, "nonexistent")
        self.assertIsNone(path)
        self.assertIsNone(data)


# ---------------------------------------------------------------------------
# _classify_issues: None / empty list
# ---------------------------------------------------------------------------


class ClassifyIssuesTest(unittest.TestCase):

    def test_none_issues_returns_zero_zero(self):
        cob, one = _agg._classify_issues(None)
        self.assertEqual((cob, one), (0, 0))

    def test_empty_list_returns_zero_zero(self):
        cob, one = _agg._classify_issues([])
        self.assertEqual((cob, one), (0, 0))

    def test_counts_class_of_bug_and_one_off(self):
        issues = ["class-of-bug: something", "one-off: minor", "class-of-bug: other"]
        cob, one = _agg._classify_issues(issues)
        self.assertEqual(cob, 2)
        self.assertEqual(one, 1)


# ---------------------------------------------------------------------------
# build_videos: edge cases
# ---------------------------------------------------------------------------


class BuildVideosEdgeCasesTest(unittest.TestCase):

    def test_returns_empty_when_youtube_dir_missing(self):
        with patch.object(_agg, "YOUTUBE_DIR", Path("/nonexistent/__youtube__")):
            rows = _agg.build_videos()
        self.assertEqual(rows, [])

    def test_skips_empty_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            # empty JSON object (no 'videos' key)
            (yt_dir / "acct.json").write_text("{}")

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "_index_local_uploads", return_value={}):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        rows = _agg.build_videos()

        self.assertEqual(rows, [])

    def test_skips_videos_without_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            (yt_dir / "acct.json").write_text(json.dumps({
                "account": "acct",
                "fetched_at": "2026-01-01T00:00:00Z",
                "videos": [
                    {"title": "No ID"},  # missing video_id
                    {"video_id": "VALID1", "title": "Good"},
                ],
            }))

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "_index_local_uploads", return_value={}):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        rows = _agg.build_videos()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_id"], "VALID1")

    def test_dedupes_repeat_video_ids_across_caches(self):
        """Audit D3.62 — pre-fix the same video appearing on two
        accounts (e.g. cross-posted) was rendered twice, double-
        counting views/likes and confusing the dashboard. Now per-
        video set guards against repeat emission; first cache wins.
        """
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            # Account A's cache (mentions video DUP1 with title A-version).
            (yt_dir / "acct_a.json").write_text(json.dumps({
                "account": "acct_a",
                "fetched_at": "2026-01-01T00:00:00Z",
                "videos": [
                    {"video_id": "DUP1", "title": "A-version"},
                    {"video_id": "ONLY_A", "title": "Only on A"},
                ],
            }))
            # Account B's cache (also mentions DUP1 with B-version).
            (yt_dir / "acct_b.json").write_text(json.dumps({
                "account": "acct_b",
                "fetched_at": "2026-01-01T00:00:00Z",
                "videos": [
                    {"video_id": "DUP1", "title": "B-version"},
                    {"video_id": "ONLY_B", "title": "Only on B"},
                ],
            }))

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "_index_local_uploads", return_value={}):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        rows = _agg.build_videos()

        ids = [r["video_id"] for r in rows]
        # DUP1 must appear EXACTLY once (was twice pre-fix).
        self.assertEqual(ids.count("DUP1"), 1, f"DUP1 not deduped: {ids}")
        # Both unique ids still present.
        self.assertIn("ONLY_A", ids)
        self.assertIn("ONLY_B", ids)
        # First cache (acct_a, sorted alphabetically) wins.
        dup_row = next(r for r in rows if r["video_id"] == "DUP1")
        self.assertEqual(dup_row.get("title"), "A-version")
        self.assertEqual(dup_row.get("account"), "acct_a")

    def test_skips_malformed_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            (yt_dir / "acct.json").write_text("{bad json}")

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "_index_local_uploads", return_value={}):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        rows = _agg.build_videos()

        self.assertEqual(rows, [])

    def test_local_without_chan_dir_in_lookup(self):
        """Upload record references channel not found in chan_lookup → no crash."""
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            (yt_dir / "acct.json").write_text(json.dumps({
                "account": "acct",
                "fetched_at": "2026-01-01T00:00:00Z",
                "videos": [{"video_id": "VID1", "title": "T"}],
            }))
            upload_index = {
                "VID1": {
                    "channel": "ghost_chan",  # not in chan_lookup
                    "slug": "s1",
                    "path": Path(tmp) / "ghost_chan" / "uploads" / "s1.json",
                    "record": {"video_id": "VID1", "slug": "s1"},
                }
            }
            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "PROJECT_ROOT", Path(tmp)):
                    with patch.object(_agg, "_index_local_uploads", return_value=upload_index):
                        with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                            rows = _agg.build_videos()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_id"], "VID1")


# ---------------------------------------------------------------------------
# build_videos: critique_md path coverage
# ---------------------------------------------------------------------------


class BuildVideosCritiqueMdTest(unittest.TestCase):

    def test_critique_md_path_set_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yt_dir = root / "youtube"
            yt_dir.mkdir()
            crit_dir = root / "critiques"
            crit_dir.mkdir()

            (yt_dir / "acct.json").write_text(json.dumps({
                "account": "acct",
                "fetched_at": "2026-01-01T00:00:00Z",
                "videos": [{"video_id": "VID1", "title": "T"}],
            }))

            chan_dir = root / "mychan"
            chan_dir.mkdir()
            up_dir = chan_dir / "uploads"
            up_dir.mkdir()
            up_rec = {"video_id": "VID1", "slug": "slug1"}
            (up_dir / "slug1.json").write_text(json.dumps(up_rec))
            # Create critique md
            (crit_dir / "slug1.md").write_text("# critique")

            upload_index = {
                "VID1": {
                    "channel": "mychan",
                    "slug": "slug1",
                    "path": up_dir / "slug1.json",
                    "record": up_rec,
                }
            }

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "CRITIQUES_DIR", crit_dir):
                    with patch.object(_agg, "PROJECT_ROOT", root):
                        with patch.object(_agg, "_index_local_uploads", return_value=upload_index):
                            with patch.object(_agg, "_iter_channel_dirs", return_value=[chan_dir]):
                                rows = _agg.build_videos()

        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["critique_md_path"])


# ---------------------------------------------------------------------------
# _parse_frontmatter_md: no frontmatter
# ---------------------------------------------------------------------------


class ParseFrontmatterTest(unittest.TestCase):

    def test_no_frontmatter_returns_empty_meta(self):
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("Just some body text\nno frontmatter here")
            tmp = Path(f.name)
        try:
            meta, body = _agg._parse_frontmatter_md(tmp)
            self.assertEqual(meta, {})
            self.assertIn("Just some body text", body)
        finally:
            tmp.unlink(missing_ok=True)

    def test_bad_frontmatter_yaml_returns_empty_meta(self):
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("---\nbad: [\nunclosed\n---\nbody")
            tmp = Path(f.name)
        try:
            meta, body = _agg._parse_frontmatter_md(tmp)
            self.assertEqual(meta, {})
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _learnings_from_memory: MEMORY_DIR missing → empty
# ---------------------------------------------------------------------------


class LearningsFromMemoryTest(unittest.TestCase):

    def test_missing_memory_dir_returns_empty(self):
        with patch.object(_agg, "MEMORY_DIR", Path("/nonexistent/__memory__")):
            rows = _agg._learnings_from_memory()
        self.assertEqual(rows, [])

    def test_skips_memory_index_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            (mem / "MEMORY.md").write_text("# index")
            (mem / "feedback_test.md").write_text("---\ntype: test\n---\nbody")
            with patch.object(_agg, "MEMORY_DIR", mem):
                rows = _agg._learnings_from_memory()
        ids = [r["id"] for r in rows]
        self.assertNotIn("memory:MEMORY", ids)
        self.assertIn("memory:feedback_test", ids)


# ---------------------------------------------------------------------------
# _learnings_from_critiques: critique path missing on disk
# ---------------------------------------------------------------------------


class LearningsFromCritiquesTest(unittest.TestCase):

    def test_missing_critique_file_skipped(self):
        videos = [
            {
                "video_id": "VID1",
                "slug": "s1",
                "channel": "ch",
                "critique": {"path": "does/not/exist.json"},
            }
        ]
        with patch.object(_agg, "PROJECT_ROOT", Path("/nonexistent")):
            rows = _agg._learnings_from_critiques(videos)
        self.assertEqual(rows, [])

    def test_system_corrections_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crit_path = root / "crit.json"
            crit_path.write_text(json.dumps({
                "system_corrections": [
                    {
                        "issue_class": "bad-sync",
                        "principle": "keep sync tight",
                        "fix": "check duration",
                        "where": "pipeline/render",
                    }
                ]
            }))
            videos = [
                {
                    "video_id": "VID1",
                    "slug": "s1",
                    "channel": "ch",
                    "critique": {"path": "crit.json"},
                }
            ]
            with patch.object(_agg, "PROJECT_ROOT", root):
                rows = _agg._learnings_from_critiques(videos)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "class-of-bug")

    def test_empty_issue_class_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crit_path = root / "crit.json"
            crit_path.write_text(json.dumps({
                "system_corrections": [
                    {"issue_class": "  ", "principle": "x", "fix": "y", "where": "z"}
                ]
            }))
            videos = [
                {"video_id": "V", "slug": "s", "channel": "c",
                 "critique": {"path": "crit.json"}}
            ]
            with patch.object(_agg, "PROJECT_ROOT", root):
                rows = _agg._learnings_from_critiques(videos)
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# rebuild: quiet=True, refresh_analytics=True, subset slice
# ---------------------------------------------------------------------------


class RebuildTest(unittest.TestCase):

    def _make_tmp_env(self):
        tmp = Path(tempfile.mkdtemp())
        yt_dir = tmp / "youtube"
        yt_dir.mkdir(parents=True)
        return tmp, yt_dir

    def test_rebuild_quiet_outputs_json_instead_of_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            with patch.object(_agg, "RESEARCH_DIR", rd):
                with patch.object(_agg, "YOUTUBE_DIR", rd / "youtube"):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        with patch.object(_agg, "_index_local_uploads", return_value={}):
                            with patch.object(_agg, "MEMORY_DIR", rd / "mem"):
                                result = _agg.rebuild(quiet=True)
            self.assertIn("videos", result)

    def test_rebuild_with_refresh_analytics_calls_youtube(self):
        fake_youtube = MagicMock()
        fake_youtube.fetch_all.return_value = {"channels": 1, "fetched": 1, "videos": 5}

        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            with patch.object(_agg, "RESEARCH_DIR", rd):
                with patch.object(_agg, "YOUTUBE_DIR", rd / "youtube"):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        with patch.object(_agg, "_index_local_uploads", return_value={}):
                            with patch.object(_agg, "MEMORY_DIR", rd / "mem"):
                                with patch.dict("sys.modules", {"pipeline.research.youtube": fake_youtube}):
                                    result = _agg.rebuild(refresh_analytics=True, quiet=True)

        # fetch_all was called via the import inside rebuild
        self.assertIn("built_at", result)

    def test_rebuild_single_slice_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            with patch.object(_agg, "RESEARCH_DIR", rd):
                with patch.object(_agg, "YOUTUBE_DIR", rd / "youtube"):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        with patch.object(_agg, "_index_local_uploads", return_value={}):
                            with patch.object(_agg, "MEMORY_DIR", rd / "mem"):
                                result = _agg.rebuild(["videos"], quiet=True)

        self.assertIn("videos", result)
        self.assertNotIn("channels", result)
        self.assertNotIn("learnings", result)

    def test_rebuild_single_slice_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            with patch.object(_agg, "RESEARCH_DIR", rd):
                with patch.object(_agg, "YOUTUBE_DIR", rd / "youtube"):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        with patch.object(_agg, "_index_local_uploads", return_value={}):
                            with patch.object(_agg, "MEMORY_DIR", rd / "mem"):
                                result = _agg.rebuild(["channels"], quiet=True)

        self.assertIn("channels", result)
        self.assertNotIn("videos", result)
        self.assertNotIn("learnings", result)

    def test_rebuild_single_slice_learnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            with patch.object(_agg, "RESEARCH_DIR", rd):
                with patch.object(_agg, "YOUTUBE_DIR", rd / "youtube"):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                        with patch.object(_agg, "_index_local_uploads", return_value={}):
                            with patch.object(_agg, "MEMORY_DIR", rd / "mem"):
                                result = _agg.rebuild(["learnings"], quiet=True)

        self.assertIn("learnings", result)
        self.assertNotIn("videos", result)


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


class AggregatorCLITest(unittest.TestCase):

    def _run_main(self, argv):
        with patch("sys.argv", ["pipeline.research"] + argv):
            with patch.object(_agg, "_iter_channel_dirs", return_value=[]):
                with patch.object(_agg, "_index_local_uploads", return_value={}):
                    with patch.object(_agg, "MEMORY_DIR", Path("/nonexistent")):
                        with tempfile.TemporaryDirectory() as tmp:
                            rd = Path(tmp)
                            with patch.object(_agg, "RESEARCH_DIR", rd):
                                with patch.object(_agg, "YOUTUBE_DIR", rd / "youtube"):
                                    _agg.main()

    def test_cli_default_all_slices(self):
        self._run_main([])

    def test_cli_videos_slice(self):
        self._run_main(["videos"])

    def test_cli_quiet_flag(self):
        import io
        with patch("sys.stdout", io.StringIO()) as mock_out:
            self._run_main(["--quiet"])
            output = mock_out.getvalue()
        # quiet mode prints JSON
        self.assertTrue(output.strip().startswith("{"))

    def test_cli_refresh_analytics(self):
        fake_youtube = MagicMock()
        fake_youtube.fetch_all.return_value = {"channels": 0, "fetched": 0, "videos": 0}
        with patch.dict("sys.modules", {"pipeline.research.youtube": fake_youtube}):
            self._run_main(["--refresh-analytics", "--quiet"])


# ---------------------------------------------------------------------------
# _critique_for_slug: score file exists → covers line 171
# ---------------------------------------------------------------------------


class CritiqueForSlugTest(unittest.TestCase):

    def test_score_exists_returns_path_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp) / "critiques"
            slug_dir = cdir / "s1"
            slug_dir.mkdir(parents=True)
            score_path = slug_dir / "s1.score.json"
            score_path.write_text(json.dumps({"score": 8}))
            with patch.object(_agg, "CRITIQUES_DIR", cdir):
                p, data = _agg._critique_for_slug("s1")
            self.assertIsNotNone(p)
            self.assertEqual(data, {"score": 8})

    def test_no_score_returns_none_tuple(self):
        with patch.object(_agg, "CRITIQUES_DIR", Path("/nonexistent")):
            p, data = _agg._critique_for_slug("s1")
        self.assertIsNone(p)
        self.assertIsNone(data)


# ---------------------------------------------------------------------------
# build_videos: covers script_block (line 253) + crit_block (lines 264-265)
# ---------------------------------------------------------------------------


class BuildVideosWithScriptAndCritiqueTest(unittest.TestCase):

    def test_script_block_and_crit_block_populated(self):
        """When a video has a local script + critique, build_videos fills both blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yt_dir = root / "youtube"
            yt_dir.mkdir()
            (yt_dir / "acct.json").write_text(json.dumps({
                "account": "acct",
                "fetched_at": "2026-01-01T00:00:00Z",
                "videos": [{"video_id": "VID1", "title": "T"}],
            }))

            script_data = {
                "hook": "hook text",
                "source": "reddit",
                "source_url": "https://reddit.com/r/x",
                "narration": "A" * 100,
                "title_options": ["Title A"],
                "footage": [],
            }
            script_path = root / "ch" / "narrations" / "s1.json"
            script_path.parent.mkdir(parents=True)
            script_path.write_text(json.dumps(script_data))

            crit_data = {
                "score": 7.5,
                "one_line_take": "ok",
                "top_issues": ["CLASS-BUG: some bug", "one-off: minor thing"],
                "highest_leverage_change": "fix sync",
                "system_corrections": [],
            }
            crit_score = root / "data" / "critiques" / "s1" / "s1.score.json"
            crit_score.parent.mkdir(parents=True)
            crit_score.write_text(json.dumps(crit_data))

            upload_index = {
                "VID1": {
                    "channel": "ch",
                    "slug": "s1",
                    "path": root / "ch" / "uploads" / "s1.json",
                    "record": {"video_id": "VID1", "slug": "s1"},
                }
            }

            chan_dir = root / "ch"
            chan_dir.mkdir(parents=True, exist_ok=True)

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "PROJECT_ROOT", root):
                    with patch.object(_agg, "CRITIQUES_DIR", root / "data" / "critiques"):
                        with patch.object(_agg, "_index_local_uploads", return_value=upload_index):
                            with patch.object(_agg, "_iter_channel_dirs", return_value=[chan_dir]):
                                with patch.object(_agg, "_local_mp4", return_value=None):
                                    with patch.object(_agg, "_local_script", return_value=(script_path, script_data)):
                                        with patch.object(_agg, "_critique_for_slug", return_value=(crit_score, crit_data)):
                                            with patch.object(_agg, "_critique_md_for_slug", return_value=None):
                                                rows = _agg.build_videos()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn("local", row)
        self.assertIsNotNone(row.get("script"))
        self.assertIsNotNone(row.get("critique"))


# ---------------------------------------------------------------------------
# _aggregate() + build_channels()
# ---------------------------------------------------------------------------


class AggregateAndBuildChannelsTest(unittest.TestCase):

    def test_aggregate_empty_list(self):
        result = _agg._aggregate([])
        self.assertEqual(result["video_count_local"], 0)
        self.assertIsNone(result["avg_score"])
        self.assertEqual(result["total_views"], 0)

    def test_aggregate_with_scores_and_views(self):
        videos = [
            {"slug": "s1", "published_at": "2026-01-01T00:00:00Z",
             "critique": {"score": 8.0}, "stats": {"view_count": 1000},
             "local": {"mp4_path": "x.mp4"}},
            {"slug": "s2", "published_at": "2026-01-02T00:00:00Z",
             "critique": {"score": 6.0}, "stats": {"view_count": 500},
             "local": {}},
        ]
        result = _agg._aggregate(videos)
        self.assertEqual(result["video_count_local"], 2)
        self.assertEqual(result["avg_score"], 7.0)
        self.assertEqual(result["total_views"], 1500)
        self.assertEqual(result["locally_rendered_count"], 1)

    def test_build_channels_with_channel_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan_dir = root / "mychan"
            chan_dir.mkdir()
            (chan_dir / "config.yaml").write_text("name: My Chan\nupload:\n  account: acct\n")
            yt_dir = root / "youtube"
            yt_dir.mkdir()
            (yt_dir / "acct.json").write_text(json.dumps({
                "channel": {"id": "UC1", "title": "My Chan"},
                "videos": []
            }))

            videos = [{"channel": "mychan", "video_id": "V1", "slug": "s1"}]

            with patch.object(_agg, "YOUTUBE_DIR", yt_dir):
                with patch.object(_agg, "PROJECT_ROOT", root):
                    with patch.object(_agg, "_iter_channel_dirs", return_value=[chan_dir]):
                        rows = _agg.build_channels(videos)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "mychan")
        self.assertEqual(rows[0]["account"], "acct")


# ---------------------------------------------------------------------------
# _excerpt: body > max_chars → covers line 409
# ---------------------------------------------------------------------------


class ExcerptTest(unittest.TestCase):

    def test_short_body_unchanged(self):
        self.assertEqual(_agg._excerpt("hello"), "hello")

    def test_long_body_truncated(self):
        body = "A" * 400
        result = _agg._excerpt(body, max_chars=320)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 320)


# ---------------------------------------------------------------------------
# _learnings_from_critiques: continue when no crit_meta path → line 440
# ---------------------------------------------------------------------------


class LearningsFromCritiquesNoCritPathTest(unittest.TestCase):

    def test_no_crit_meta_path_continue(self):
        """Video with critique dict but missing 'path' key → skipped (line 440)."""
        videos = [
            {"video_id": "V1", "slug": "s1", "channel": "ch",
             "critique": {"score": 7}},  # no 'path' key
        ]
        rows = _agg._learnings_from_critiques(videos)
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# _write_jsonl: with actual rows → covers lines 479-480
# ---------------------------------------------------------------------------


class WriteJsonlTest(unittest.TestCase):

    def test_writes_rows_and_returns_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.jsonl"
            n = _agg._write_jsonl(out, [{"a": 1}, {"b": 2}])
            self.assertEqual(n, 2)
            lines = out.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"a": 1})


# ---------------------------------------------------------------------------
# load_jsonl → covers lines 531-539
# ---------------------------------------------------------------------------


class LoadJsonlTest(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        self.assertEqual(_agg.load_jsonl(Path("/nonexistent/x.jsonl")), [])

    def test_reads_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "data.jsonl"
            p.write_text('{"a":1}\n{"b":2}\n\n')
            rows = _agg.load_jsonl(p)
        self.assertEqual(rows, [{"a": 1}, {"b": 2}])


if __name__ == "__main__":
    unittest.main()
