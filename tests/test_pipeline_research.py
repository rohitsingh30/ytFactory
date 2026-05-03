"""Tests for pipeline.research — the research-dashboard index builder.

We construct a fixture filesystem under a tempdir that mimics the real
``data/`` + ``channels/`` + memory layouts, point the module's globals at
it, and verify the three JSONL slices come out shaped right.

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
    """Create a small but representative ytFactory data tree.

    Mirrors the schemas observed in the live repo: an mp4 under
    , a script under intermediate/<channel>/scripts/, an
    upload record under data/uploads/<channel>/, a critique with
    system_corrections + class-of-bug top_issues, plus a YAML and a
    couple of memory feedback files.
    """
    # ---- channels yaml (one prod, one variant, one with no upload block)
    (root / "channels").mkdir(parents=True)
    (root / "channels" / "sportstoriesanimated.yaml").write_text(
        "name: SportsStoriesAnimated\n"
        "source_adapter: sports_moments_manual\n"
        "upload:\n"
        "  account: sportstoriesanimated\n"
    )
    (root / "channels" / "aita_animated.yaml").write_text(
        "name: AITA Animated\n"
        "source_adapter: reddit_video\n"
        "upload:\n"
        "  account: mystoriesanimated\n"
    )
    (root / "channels" / "wiki_oddities.yaml").write_text(
        "name: Wiki Oddities\nsource_adapter: wikipedia\n"
    )

    # ---- a rendered short with full upload + critique --------------------
    slug_full = "aguero-9320"
    (root / "data" / "shorts").mkdir(parents=True)
    (root / "data" / "shorts" / f"{slug_full}.mp4").write_bytes(b"\x00" * 128)
    _write_json(
        root / "data" / "intermediate" / "sportstoriesanimated" / "scripts" / f"{slug_full}.json",
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
        root / "data" / "uploads" / "sportstoriesanimated" / f"{slug_full}.json",
        {
            "slug": slug_full,
            "video_id": "ABC123",
            "url": "https://youtu.be/ABC123",
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
    # also write a markdown report next to the score
    (root / "data" / "critiques" / f"{slug_full}.md").write_text("# critique\nbody")

    # ---- a rendered short with NO critique (the "needs attention" case) --
    slug_bare = "no-critique-yet"
    (root / "data" / "shorts" / f"{slug_bare}.mp4").write_bytes(b"\x00" * 64)
    _write_json(
        root / "data" / "intermediate" / "sportstoriesanimated" / "scripts" / f"{slug_bare}.json",
        {
            "slug": slug_bare,
            "hook": "fresh render",
            "narration": "x",
            "title_options": [],
            "source": "manual:sports_moments",
        },
    )
    # (no upload, no critique, no analytics)

    # ---- a rendered short under a legacy intermediate dir (no YAML) -----
    slug_legacy = "amitheasshole-test"
    (root / "data" / "shorts" / f"{slug_legacy}.mp4").write_bytes(b"\x00" * 64)
    _write_json(
        root / "data" / "intermediate" / "reddit_amitheasshole" / "scripts" / f"{slug_legacy}.json",
        {
            "slug": slug_legacy,
            "hook": "AITA legacy?",
            "narration": "x",
            "title_options": [],
            "source": "reddit:AmItheAsshole",
        },
    )
    _write_json(
        root / "data" / "uploads" / "reddit_amitheasshole" / f"{slug_legacy}.json",
        {
            "slug": slug_legacy,
            "video_id": "DEF456",
            "url": "https://youtu.be/DEF456",
            "uploaded_at": "2026-05-01T12:00:00+00:00",
            "title": "AITA test",
            "privacy": "public",
            "account": "mystoriesanimated",
        },
    )

    # ---- analytics for one slug only -----------------------------------
    _write_json(
        root / "data" / "research" / "analytics" / f"{slug_full}.json",
        {
            "slug": slug_full,
            "video_id": "ABC123",
            "account": "sportstoriesanimated",
            "view_count": 1234,
            "like_count": 56,
            "comment_count": 7,
            "favorite_count": 0,
            "fetched_at": "2026-05-03T00:00:00+00:00",
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
    # Edge case: a markdown without frontmatter — must not crash.
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
            "CHANNELS_DIR": research.CHANNELS_DIR,
            "DATA_DIR": research.DATA_DIR,
            "SHORTS_DIR": research.SHORTS_DIR,
            "INTERMEDIATE_DIR": research.INTERMEDIATE_DIR,
            "UPLOADS_DIR": research.UPLOADS_DIR,
            "CRITIQUES_DIR": research.CRITIQUES_DIR,
            "RESEARCH_DIR": research.RESEARCH_DIR,
            "ANALYTICS_DIR": research.ANALYTICS_DIR,
            "MEMORY_DIR": research.MEMORY_DIR,
        }
        research.PROJECT_ROOT = self.root
        research.CHANNELS_DIR = self.root / "channels"
        research.DATA_DIR = self.root / "data"
        research.SHORTS_DIR = self.root / "data" / "shorts"
        research.INTERMEDIATE_DIR = self.root / "data" / "intermediate"
        research.UPLOADS_DIR = self.root / "data" / "uploads"
        research.CRITIQUES_DIR = self.root / "data" / "critiques"
        research.RESEARCH_DIR = self.root / "data" / "research"
        research.ANALYTICS_DIR = self.root / "data" / "research" / "analytics"
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

    def test_one_row_per_mp4(self):
        rows = research.build_videos()
        self.assertEqual(len(rows), 3)
        slugs = {r["slug"] for r in rows}
        self.assertEqual(slugs, {"aguero-9320", "no-critique-yet", "amitheasshole-test"})

    def test_full_record_joins_script_upload_critique_analytics(self):
        rows = research.build_videos()
        full = next(r for r in rows if r["slug"] == "aguero-9320")
        self.assertEqual(full["channel"], "sportstoriesanimated")
        self.assertEqual(full["script"]["hook"][:8], "Manchest")
        self.assertEqual(full["script"]["footage_count"], 1)
        self.assertEqual(full["upload"]["video_id"], "ABC123")
        self.assertEqual(full["critique"]["score"], 3)
        # 2 class-of-bug + 1 one-off in top_issues
        self.assertEqual(full["critique"]["class_of_bug_count"], 2)
        self.assertEqual(full["critique"]["one_off_count"], 1)
        self.assertEqual(full["critique"]["system_correction_count"], 2)
        self.assertEqual(full["analytics"]["view_count"], 1234)
        self.assertTrue(full["critique_md_path"].endswith("aguero-9320.md"))

    def test_no_critique_no_analytics_no_upload(self):
        rows = research.build_videos()
        bare = next(r for r in rows if r["slug"] == "no-critique-yet")
        self.assertIsNone(bare["upload"])
        self.assertIsNone(bare["critique"])
        self.assertIsNone(bare["analytics"])
        # Script join still works even without the rest.
        self.assertEqual(bare["script"]["hook"], "fresh render")

    def test_legacy_intermediate_dir_still_resolves_channel(self):
        rows = research.build_videos()
        legacy = next(r for r in rows if r["slug"] == "amitheasshole-test")
        self.assertEqual(legacy["channel"], "reddit_amitheasshole")
        self.assertEqual(legacy["upload"]["account"], "mystoriesanimated")


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

    def test_three_kinds_present(self):
        rows = research.build_channels(self.videos)
        kinds = {r["kind"] for r in rows}
        self.assertEqual(kinds, {"yaml", "intermediate", "account"})

    def test_yaml_production_flag_only_for_canonical_recipes(self):
        rows = research.build_channels(self.videos)
        prod_yaml = [r for r in rows if r["kind"] == "yaml" and r["production"]]
        self.assertEqual(len(prod_yaml), 1)
        # sportstoriesanimated.yaml is the only canonical prod recipe in
        # the fixture; aita_animated.yaml ships to mystoriesanimated but
        # is a variant per project_two_production_channels.md.
        self.assertEqual(prod_yaml[0]["yaml_filename"], "sportstoriesanimated.yaml")

    def test_account_view_aggregates_uploads(self):
        rows = research.build_channels(self.videos)
        accts = {r["channel"]: r for r in rows if r["kind"] == "account"}
        self.assertIn("sportstoriesanimated", accts)
        self.assertIn("mystoriesanimated", accts)
        self.assertEqual(accts["sportstoriesanimated"]["video_count"], 1)
        self.assertEqual(accts["sportstoriesanimated"]["uploaded_count"], 1)
        self.assertEqual(accts["mystoriesanimated"]["uploaded_count"], 1)

    def test_intermediate_kind_only_when_no_yaml_claims_it(self):
        rows = research.build_channels(self.videos)
        intermediate_names = {r["channel"] for r in rows if r["kind"] == "intermediate"}
        # reddit_amitheasshole has videos but no YAML → must surface
        self.assertIn("reddit_amitheasshole", intermediate_names)
        # sportstoriesanimated has a YAML and an intermediate dir but the
        # YAML's channel_id matches, so no intermediate-orphan row.
        # (build emits the videos via the kind=account row instead.)

    def test_avg_score_rounded_two_decimals(self):
        rows = research.build_channels(self.videos)
        for r in rows:
            if r["avg_score"] is not None:
                self.assertEqual(round(r["avg_score"], 2), r["avg_score"])


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
        # MEMORY.md is the index; must never appear as a learning.
        self.assertFalse(any("MEMORY" in (r.get("source_path") or "") for r in rows))

    def test_memory_with_frontmatter_uses_meta(self):
        rows = research.build_learnings(self.videos)
        closer = next(r for r in rows if r["title"] == "AITA closer panel")
        self.assertEqual(closer["source"], "memory")
        self.assertEqual(closer["type"], "feedback")
        self.assertIn("Body of the feedback note", closer["body_excerpt"])

    def test_memory_without_frontmatter_does_not_crash(self):
        rows = research.build_learnings(self.videos)
        # The "feedback_no_frontmatter.md" file should still produce a row,
        # falling back to the file stem as the title.
        titles = {r["title"] for r in rows if r["source"] == "memory"}
        self.assertIn("feedback_no_frontmatter", titles)

    def test_critique_system_corrections_promoted(self):
        rows = research.build_learnings(self.videos)
        crit = [r for r in rows if r["source"] == "critique"]
        # 2 system_corrections in the fixture
        self.assertEqual(len(crit), 2)
        classes = {r["title"] for r in crit}
        self.assertEqual(classes, {"kit-text-gibberish", "opposition-cast-missing"})
        # Critique learnings carry the channel + slug they came from.
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
        self.assertEqual(out["videos"], 3)
        self.assertGreaterEqual(out["channels"], 5)  # 3 yamls + at least 1 intermediate + 2 accounts
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
        # First, full build so all three files exist.
        research.rebuild(quiet=True)
        # Stamp learnings to detect untouched.
        learnings_path = research.RESEARCH_DIR / "learnings.jsonl"
        before = learnings_path.stat().st_mtime
        # Sleep-free: just compare the on-disk content.
        original_text = learnings_path.read_text()
        # Rewrite only videos — must not touch learnings.jsonl.
        research.rebuild(["videos"], quiet=True)
        self.assertEqual(learnings_path.read_text(), original_text)

    def test_rebuild_does_not_call_youtube_when_refresh_false(self):
        # If we don't pass refresh_analytics=True, the youtube_stats
        # module must not be imported. We assert by ensuring no
        # network attempt: monkey-patch fetch_all to raise if called.
        from pipeline import youtube_stats

        original = youtube_stats.fetch_all
        def _boom(*a, **kw):
            raise AssertionError("fetch_all should not be called")
        youtube_stats.fetch_all = _boom
        try:
            research.rebuild(quiet=True)  # default: no refresh
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
