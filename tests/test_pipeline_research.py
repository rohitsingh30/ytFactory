"""Tests for pipeline.research — the research-dashboard index builder.

We construct a fixture filesystem under a tempdir that mimics the real
per-channel layout (``<channel>/config.yaml`` + ``<channel>/uploads/``
+ ``<channel>/narrations/`` + ``<channel>/shorts/``) plus a fake
``data/research/youtube/<account>.json`` that stands in for what
``pipeline.youtube_stats.fetch_account`` would have written. We then
point the module's globals at the tempdir and verify the three JSONL
slices come out shaped right.

No network, no subprocesses. Runs in <1s.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401  (sys.path side-effect)

from pipeline import research


# ---- fixture builder ----------------------------------------------------


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _write_text(path: Path, txt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt)


def _build_fixture_tree(root: Path) -> None:
    """Mimics the live ytFactory layout circa 2026-05.

    Two channel dirs (sportstoriesanimated = production,
    historyrecapped = secondary, archived); a YouTube cache for each
    written under ``data/research/youtube/`` describing the videos
    currently live on the channels (more than what we have local
    upload records for, to exercise the LEFT-JOIN path); upload
    records, narrations, mp4s, critique scores, and a couple of
    memory feedback files.
    """
    # ---- channel: sportstoriesanimated ---------------------------------
    chan = root / "sportstoriesanimated"
    (chan / "shorts").mkdir(parents=True)
    (chan / "uploads").mkdir(parents=True)
    (chan / "narrations").mkdir(parents=True)
    (chan / "config.yaml").write_text(
        "name: SportsStoriesAnimated\n"
        "source_adapter: sports_moments_manual\n"
        "upload:\n"
        "  account: sportstoriesanimated\n"
    )
    # Fully-joined: mp4 + narration + upload + critique + analytics.
    slug_full = "aguero-9320"
    vid_full = "ABC123"
    (chan / "shorts" / f"{slug_full}.mp4").write_bytes(b"\x00" * 128)
    _write_json(
        chan / "narrations" / f"{slug_full}.json",
        {
            "slug": slug_full,
            "hook": "Manchester City had not won a league in forty-four years.",
            "narration": "Hello world." * 10,
            "title_options": ["A", "B"],
            "source": "manual:sports_moments",
            "source_url": "https://example/wiki",
            "footage": [{"match_text": "Last kick"}],
        },
    )
    _write_json(
        chan / "uploads" / f"{slug_full}.json",
        {
            "slug": slug_full,
            "video_id": vid_full,
            "url": f"https://youtu.be/{vid_full}",
            "uploaded_at": "2026-05-02T18:00:00+00:00",
            "title": "Aguero 93:20",
            "privacy": "public",
            "account": "sportstoriesanimated",
            "thumbnail_set": False,
        },
    )
    _write_json(
        root / "data" / "critiques" / slug_full / f"{slug_full}.score.json",
        {
            "score": 3,
            "one_line_take": "Cast morphs every beat.",
            "top_issues": [
                "0-3s: empty stadium hook (class-of-bug)",
                "4s: garbled sponsor text (class-of-bug)",
                "9s: lone hand-to-sun (one-off)",
            ],
            "highest_leverage_change": "Lock cast tokens.",
            "system_corrections": [
                {
                    "issue_class": "kit-text-gibberish",
                    "where": "pipeline/images.py",
                    "fix": "Strip sponsor text from prompts.",
                    "principle": "Never ask T2I for typography.",
                },
                {
                    "issue_class": "opposition-cast-missing",
                    "where": "pipeline/cast.py",
                    "fix": "Include opposition kit in dossier.",
                    "principle": "Extend cast tokens to opposition.",
                },
            ],
        },
    )
    (root / "data" / "critiques" / f"{slug_full}.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "data" / "critiques" / f"{slug_full}.md").write_text("# critique\nbody")

    # No-critique render: mp4 + narration + upload but no critique.
    slug_bare = "iniesta-2010"
    vid_bare = "BARE99"
    (chan / "shorts" / f"{slug_bare}.mp4").write_bytes(b"\x00" * 64)
    _write_json(
        chan / "narrations" / f"{slug_bare}.json",
        {
            "slug": slug_bare,
            "hook": "fresh render",
            "narration": "x",
            "title_options": [],
            "source": "manual:sports_moments",
        },
    )
    _write_json(
        chan / "uploads" / f"{slug_bare}.json",
        {
            "slug": slug_bare,
            "video_id": vid_bare,
            "url": f"https://youtu.be/{vid_bare}",
            "uploaded_at": "2026-05-03T18:00:00+00:00",
            "title": "Iniesta 2010 WC",
            "privacy": "public",
            "account": "sportstoriesanimated",
        },
    )

    # ---- channel: historyrecapped --------------------------------------
    chan2 = root / "historyrecapped"
    (chan2 / "shorts").mkdir(parents=True)
    (chan2 / "uploads").mkdir(parents=True)
    (chan2 / "narrations").mkdir(parents=True)
    (chan2 / "config.yaml").write_text(
        "name: History Recapped\n"
        "source_adapter: archive_org\n"
        "upload:\n"
        "  account: historyrecapped\n"
    )
    slug_hist = "midway-1942"
    vid_hist = "HIST01"
    (chan2 / "shorts" / f"{slug_hist}.mp4").write_bytes(b"\x00" * 96)
    _write_json(
        chan2 / "narrations" / f"{slug_hist}.json",
        {
            "slug": slug_hist,
            "hook": "Five minutes that changed the Pacific.",
            "narration": "Midway, June 1942.",
            "source": "archive.org",
        },
    )
    _write_json(
        chan2 / "uploads" / f"{slug_hist}.json",
        {
            "slug": slug_hist,
            "video_id": vid_hist,
            "url": f"https://youtu.be/{vid_hist}",
            "uploaded_at": "2026-05-04T10:00:00+00:00",
            "title": "Midway 1942 — five minutes",
            "privacy": "public",
            "account": "historyrecapped",
        },
    )

    # ---- YouTube caches ------------------------------------------------
    # sportstoriesanimated: 3 videos live on YouTube (one of which has
    # NO local upload record — to exercise the unjoined path).
    _write_json(
        root / "data" / "research" / "youtube" / "sportstoriesanimated.json",
        {
            "account": "sportstoriesanimated",
            "fetched_at": "2026-05-04T12:00:00+00:00",
            "channel": {
                "id": "UC_sports",
                "title": "SportsStoriesAnimated",
                "subscriber_count": 100,
                "view_count": 12345,
                "video_count": 3,
                "hidden_subscribers": False,
                "uploads_playlist": "UU_sports",
            },
            "videos": [
                {
                    "video_id": vid_full,
                    "title": "Aguero 93:20",
                    "published_at": "2026-05-02T18:16:41Z",
                    "channel_id": "UC_sports",
                    "channel_title": "SportsStoriesAnimated",
                    "thumbnail_url": "https://img/abc.jpg",
                    "duration_s": 58,
                    "privacy": "public",
                    "url": f"https://youtu.be/{vid_full}",
                    "view_count": 1234,
                    "like_count": 56,
                    "comment_count": 7,
                    "favorite_count": 0,
                },
                {
                    "video_id": vid_bare,
                    "title": "Iniesta 2010 WC",
                    "published_at": "2026-05-03T18:00:00Z",
                    "channel_id": "UC_sports",
                    "thumbnail_url": "https://img/bare.jpg",
                    "duration_s": 55,
                    "privacy": "public",
                    "url": f"https://youtu.be/{vid_bare}",
                    "view_count": 200,
                    "like_count": 10,
                    "comment_count": 1,
                    "favorite_count": 0,
                },
                {
                    # Lives on YouTube but no local upload record exists.
                    "video_id": "ORPHAN1",
                    "title": "Manually uploaded short",
                    "published_at": "2026-04-30T12:00:00Z",
                    "channel_id": "UC_sports",
                    "thumbnail_url": "https://img/orphan.jpg",
                    "duration_s": 40,
                    "privacy": "public",
                    "url": "https://youtu.be/ORPHAN1",
                    "view_count": 50,
                    "like_count": 1,
                    "comment_count": 0,
                    "favorite_count": 0,
                },
            ],
        },
    )
    # historyrecapped: one video, fully joined.
    _write_json(
        root / "data" / "research" / "youtube" / "historyrecapped.json",
        {
            "account": "historyrecapped",
            "fetched_at": "2026-05-04T12:00:00+00:00",
            "channel": {
                "id": "UC_hist",
                "title": "History Recapped",
                "subscriber_count": 25,
                "view_count": 500,
                "video_count": 1,
                "hidden_subscribers": False,
                "uploads_playlist": "UU_hist",
            },
            "videos": [
                {
                    "video_id": vid_hist,
                    "title": "Midway 1942 — five minutes",
                    "published_at": "2026-05-04T10:00:00Z",
                    "channel_id": "UC_hist",
                    "thumbnail_url": "https://img/hist.jpg",
                    "duration_s": 60,
                    "privacy": "public",
                    "url": f"https://youtu.be/{vid_hist}",
                    "view_count": 80,
                    "like_count": 4,
                    "comment_count": 0,
                    "favorite_count": 0,
                }
            ],
        },
    )

    # ---- memory: one feedback, one project ------------------------------
    mem = root / "memory"
    mem.mkdir(parents=True)
    _write_text(
        mem / "MEMORY.md",
        "- [aita closer](feedback_aita_closer.md) — closer panel rule\n",
    )
    _write_text(
        mem / "feedback_aita_closer.md",
        "---\n"
        "name: AITA closer panel\n"
        "description: LIKE if YTA / COMMENT if NTA\n"
        "type: feedback\n"
        "---\n"
        "Body of the feedback note.\n",
    )
    _write_text(
        mem / "project_two_channels.md",
        "---\n"
        "name: Two production channels\n"
        "description: ships to MyStoriesAnimated + SportsStoriesAnimated\n"
        "type: project\n"
        "---\n"
        "Body.\n",
    )
    _write_text(mem / "feedback_no_frontmatter.md", "just body, no metadata\n")


# ---- patcher ------------------------------------------------------------


class _ResearchPatcher:
    """Context manager that points pipeline.research at a fixture root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._saved: dict[str, Path] = {}

    def __enter__(self) -> "_ResearchPatcher":
        self._saved = {
            "PROJECT_ROOT": research.PROJECT_ROOT,
            "DATA_DIR": research.DATA_DIR,
            "CRITIQUES_DIR": research.CRITIQUES_DIR,
            "RESEARCH_DIR": research.RESEARCH_DIR,
            "YOUTUBE_DIR": research.YOUTUBE_DIR,
            "MEMORY_DIR": research.MEMORY_DIR,
        }
        research.PROJECT_ROOT = self.root
        research.DATA_DIR = self.root / "data"
        research.CRITIQUES_DIR = self.root / "data" / "critiques"
        research.RESEARCH_DIR = self.root / "data" / "research"
        research.YOUTUBE_DIR = self.root / "data" / "research" / "youtube"
        research.MEMORY_DIR = self.root / "memory"
        return self

    def __exit__(self, *exc) -> None:
        for k, v in self._saved.items():
            setattr(research, k, v)


# ---- tests --------------------------------------------------------------


class BuildVideosTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        _build_fixture_tree(self.tmp)
        self.patcher = _ResearchPatcher(self.tmp)
        self.patcher.__enter__()

    def tearDown(self) -> None:
        self.patcher.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_row_per_youtube_video(self):
        rows = research.build_videos()
        # 3 sports + 1 history = 4 rows
        self.assertEqual(len(rows), 4)
        ids = {r["video_id"] for r in rows}
        self.assertEqual(ids, {"ABC123", "BARE99", "ORPHAN1", "HIST01"})

    def test_full_record_left_joins_local_artefacts(self):
        rows = research.build_videos()
        full = next(r for r in rows if r["video_id"] == "ABC123")
        self.assertEqual(full["slug"], "aguero-9320")
        self.assertEqual(full["channel"], "sportstoriesanimated")
        self.assertEqual(full["account"], "sportstoriesanimated")
        # YouTube fields come straight from the cache.
        self.assertEqual(full["title"], "Aguero 93:20")
        self.assertEqual(full["stats"]["view_count"], 1234)
        # Local artefacts joined by video_id.
        self.assertEqual(full["script"]["hook"][:8], "Manchest")
        self.assertEqual(full["script"]["footage_count"], 1)
        self.assertEqual(full["local"]["mp4_size_bytes"], 128)
        self.assertEqual(full["critique"]["score"], 3)
        self.assertEqual(full["critique"]["class_of_bug_count"], 2)
        self.assertEqual(full["critique"]["one_off_count"], 1)
        self.assertEqual(full["critique"]["system_correction_count"], 2)
        self.assertTrue(full["critique_md_path"].endswith("aguero-9320.md"))

    def test_orphan_youtube_video_has_null_local_block(self):
        rows = research.build_videos()
        orphan = next(r for r in rows if r["video_id"] == "ORPHAN1")
        # The video EXISTS — YouTube knows about it — but local doesn't.
        self.assertEqual(orphan["title"], "Manually uploaded short")
        self.assertEqual(orphan["stats"]["view_count"], 50)
        self.assertIsNone(orphan["slug"])
        self.assertIsNone(orphan["channel"])
        self.assertIsNone(orphan["local"])
        self.assertIsNone(orphan["script"])
        self.assertIsNone(orphan["critique"])

    def test_stats_fetched_at_propagated_from_cache(self):
        rows = research.build_videos()
        for r in rows:
            self.assertTrue(r["stats"]["fetched_at"].startswith("2026-05-04"))

    def test_video_without_critique_has_critique_null(self):
        rows = research.build_videos()
        bare = next(r for r in rows if r["video_id"] == "BARE99")
        self.assertEqual(bare["slug"], "iniesta-2010")
        self.assertIsNone(bare["critique"])
        self.assertEqual(bare["script"]["hook"], "fresh render")
        self.assertEqual(bare["local"]["mp4_size_bytes"], 64)


class BuildChannelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        _build_fixture_tree(self.tmp)
        self.patcher = _ResearchPatcher(self.tmp)
        self.patcher.__enter__()
        self.videos = research.build_videos()

    def tearDown(self) -> None:
        self.patcher.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_row_per_config_yaml(self):
        rows = research.build_channels(self.videos)
        names = {r["channel"] for r in rows}
        self.assertEqual(names, {"sportstoriesanimated", "historyrecapped"})
        # All rows have the new uniform kind.
        self.assertEqual({r["kind"] for r in rows}, {"channel"})

    def test_youtube_meta_propagated(self):
        rows = research.build_channels(self.videos)
        sports = next(r for r in rows if r["channel"] == "sportstoriesanimated")
        self.assertEqual(sports["youtube_channel_id"], "UC_sports")
        self.assertEqual(sports["subscriber_count"], 100)
        self.assertEqual(sports["youtube_video_count"], 3)
        self.assertEqual(sports["youtube_uploads_seen"], 3)

    def test_production_flag_only_for_canonical_recipes(self):
        rows = research.build_channels(self.videos)
        prod = [r for r in rows if r["production"]]
        self.assertEqual({r["channel"] for r in prod}, {"sportstoriesanimated"})

    def test_rollup_only_counts_locally_joined_videos(self):
        rows = research.build_channels(self.videos)
        sports = next(r for r in rows if r["channel"] == "sportstoriesanimated")
        # 2 of the 3 YouTube videos joined to local upload records;
        # the orphan one didn't.
        self.assertEqual(sports["video_count_local"], 2)
        self.assertEqual(sports["locally_rendered_count"], 2)
        # Only one of the local videos has a critique (aguero, score=3).
        self.assertEqual(sports["score_count"], 1)
        self.assertEqual(sports["avg_score"], 3.0)


class BuildLearningsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        _build_fixture_tree(self.tmp)
        self.patcher = _ResearchPatcher(self.tmp)
        self.patcher.__enter__()
        self.videos = research.build_videos()

    def tearDown(self) -> None:
        self.patcher.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_memory_index(self):
        rows = research.build_learnings(self.videos)
        self.assertFalse(any("MEMORY" in (r.get("source_path") or "") for r in rows))

    def test_memory_with_frontmatter_uses_meta(self):
        rows = research.build_learnings(self.videos)
        closer = next(r for r in rows if r["title"] == "AITA closer panel")
        self.assertEqual(closer["source"], "memory")
        self.assertEqual(closer["type"], "feedback")
        self.assertIn("Body of the feedback note", closer["body_excerpt"])

    def test_memory_without_frontmatter_does_not_crash(self):
        rows = research.build_learnings(self.videos)
        titles = {r["title"] for r in rows if r["source"] == "memory"}
        self.assertIn("feedback_no_frontmatter", titles)

    def test_critique_system_corrections_promoted(self):
        rows = research.build_learnings(self.videos)
        crit = [r for r in rows if r["source"] == "critique"]
        self.assertEqual(len(crit), 2)
        classes = {r["title"] for r in crit}
        self.assertEqual(classes, {"kit-text-gibberish", "opposition-cast-missing"})
        for r in crit:
            self.assertEqual(r["channel"], "sportstoriesanimated")
            self.assertEqual(r["related_slugs"], ["aguero-9320"])

    def test_memory_rows_sort_before_critique(self):
        rows = research.build_learnings(self.videos)
        first_critique = next(
            (i for i, r in enumerate(rows) if r["source"] == "critique"), None
        )
        last_memory = max(
            (i for i, r in enumerate(rows) if r["source"] == "memory"), default=-1
        )
        self.assertGreater(first_critique, last_memory)


class RebuildAndJsonlRoundtripTest(unittest.TestCase):
    """End-to-end: rebuild() writes the three JSONLs and load_jsonl
    reads them back to identical dicts."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        _build_fixture_tree(self.tmp)
        self.patcher = _ResearchPatcher(self.tmp)
        self.patcher.__enter__()

    def tearDown(self) -> None:
        self.patcher.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_rebuild_writes_three_files(self):
        out = research.rebuild(quiet=True)
        self.assertEqual(out["videos"], 4)
        self.assertEqual(out["channels"], 2)
        self.assertGreaterEqual(out["learnings"], 4)  # 3 memory + 2 critique
        for name, key in [
            ("videos.jsonl", "videos"),
            ("channels.jsonl", "channels"),
            ("learnings.jsonl", "learnings"),
        ]:
            p = research.RESEARCH_DIR / name
            self.assertTrue(p.exists(), f"{name} should exist")
            rows = research.load_jsonl(p)
            self.assertEqual(len(rows), out[key])

    def test_partial_rebuild_only_touches_named_slice(self):
        research.rebuild(quiet=True)
        learnings_path = research.RESEARCH_DIR / "learnings.jsonl"
        original_text = learnings_path.read_text()
        research.rebuild(["videos"], quiet=True)
        self.assertEqual(learnings_path.read_text(), original_text)

    def test_rebuild_does_not_call_youtube_when_refresh_false(self):
        from pipeline import youtube_stats

        original = youtube_stats.fetch_all
        def _boom(*a, **kw):
            raise AssertionError("fetch_all should not be called")
        youtube_stats.fetch_all = _boom
        try:
            research.rebuild(quiet=True)
        finally:
            youtube_stats.fetch_all = original


class LoadJsonlTest(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(research.load_jsonl(Path("/nonexistent.jsonl")), [])

    def test_skips_blank_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"a":1}\n\n  \n{"b":2}\n')
            p = Path(f.name)
        try:
            rows = research.load_jsonl(p)
            self.assertEqual(rows, [{"a": 1}, {"b": 2}])
        finally:
            p.unlink()


if __name__ == "__main__":
    unittest.main()
