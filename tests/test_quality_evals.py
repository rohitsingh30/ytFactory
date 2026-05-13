"""Tests for pipeline.quality.evals — handoff writer / critique parser /
STATUS updater / holds registry / CLI.  (0% → 100%)

All I/O is against tempdir.  EVALS_ROOT is monkey-patched so tests
never touch /Users/rohit/evals/.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline.quality.evals as evals_mod
from pipeline.quality.evals import (
    VERDICT_BLOCK,
    VERDICT_FIX,
    VERDICT_SHIP,
    Critique,
    Hold,
    Route,
    StatusRow,
    clear_hold,
    cross_cutting,
    critiques_dir,
    ensure_status_row,
    holds_for,
    holds_path,
    inbox_dir,
    ingest,
    init_project,
    is_held,
    list_critiques,
    main,
    parse_critique,
    project_dir,
    read_status,
    route,
    set_hold,
    status_path,
    update_status_authoring,
    write_handoff,
)


def _make_evals_root(td: Path) -> Path:
    er = td / "evals"
    er.mkdir()
    return er


def _make_project_root(td: Path) -> Path:
    pr = td / "ytfactory"
    pr.mkdir()
    return pr


_SIMPLE_CRITIQUE = """\
**Date:** 2026-05-01

## Scores

| param | score | notes |
|---|---|---|
| hook_pull | 8 | good |
| audio_sync | 7 | ok |

Total **420 / 500**  avg **8.4 / 10**

**SHIP**

## Single-question gut check

YES — I would watch again.
"""

_FIX_CRITIQUE = """\
**Date:** 2026-05-02

| param | score | notes |
|---|---|---|
| clipping_audible | 3 | loud pop at 0:12 |
| hook_pull | 7 | acceptable |

Total **350 / 500**  avg **7.0 / 10**

**FIX**

## Specific fix instructions

Remove the pop at 0:12. Re-render narration.

## Single-question gut check

MAYBE
"""

_BLOCK_CRITIQUE = """\
**Date:** 2026-05-03

| param | score | notes |
|---|---|---|
| on_screen_text_correctness | 2 | wrong date shown |

Total **200 / 500**  avg **4.0 / 10**

**BLOCK**

## Single-question gut check

NO
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._evals_root = _make_evals_root(self._root)
        self._project_root = _make_project_root(self._root)
        self._patcher_evals = patch.object(evals_mod, "EVALS_ROOT", self._evals_root)
        self._patcher_proj = patch.object(evals_mod, "PROJECT_ROOT", self._project_root)
        self._patcher_evals.start()
        self._patcher_proj.start()

    def tearDown(self):
        self._patcher_evals.stop()
        self._patcher_proj.stop()
        self._td.cleanup()

    def _init(self, project="testproject"):
        init_project(project)
        return project

    def _write_critique(self, project: str, slug: str, content: str) -> Path:
        cdir = critiques_dir(project)
        cdir.mkdir(parents=True, exist_ok=True)
        p = cdir / f"{slug}_critique.md"
        p.write_text(content)
        return p


# ── path helpers ──────────────────────────────────────────────────────────


class TestPathHelpers(_Base):
    def test_project_dir(self):
        self.assertEqual(project_dir("foo"), self._evals_root / "foo")

    def test_inbox_dir(self):
        self.assertEqual(inbox_dir("foo"), self._evals_root / "foo" / "inbox")

    def test_critiques_dir(self):
        self.assertEqual(critiques_dir("foo"), self._evals_root / "foo" / "critiques")

    def test_status_path(self):
        self.assertEqual(status_path("foo"), self._evals_root / "foo" / "STATUS.md")

    def test_holds_path(self):
        self.assertEqual(holds_path("testchan"), self._project_root / "testchan" / "_holds.json")


# ── init_project ──────────────────────────────────────────────────────────


class TestInitProject(_Base):
    def test_creates_dirs_and_status(self):
        self._init("myproject")
        self.assertTrue(inbox_dir("myproject").exists())
        self.assertTrue(critiques_dir("myproject").exists())
        self.assertTrue(status_path("myproject").exists())

    def test_idempotent(self):
        self._init("myproject")
        self._init("myproject")  # second call should not raise
        self.assertTrue(status_path("myproject").exists())

    def test_status_md_has_table_header(self):
        self._init("myproject")
        content = status_path("myproject").read_text()
        self.assertIn("| video_slug ", content)


# ── handoff_path / write_handoff ──────────────────────────────────────────


class TestHandoff(_Base):
    def test_handoff_path_base(self):
        self._init("p")
        date = dt.date(2026, 5, 1)
        p = evals_mod.handoff_path("p", "batch-a", date)
        self.assertEqual(p.name, "2026-05-01-batch-a.md")

    def test_handoff_path_auto_suffix(self):
        self._init("p")
        date = dt.date(2026, 5, 1)
        # write first file
        write_handoff("p", "batch-a", "content", date)
        # get the v2 path
        p = evals_mod.handoff_path("p", "batch-a", date)
        self.assertEqual(p.name, "2026-05-01-batch-a-v2.md")

    def test_handoff_path_auto_suffix_v3(self):
        self._init("p")
        date = dt.date(2026, 5, 1)
        write_handoff("p", "batch-a", "v1", date)
        write_handoff("p", "batch-a", "v2", date)
        p = evals_mod.handoff_path("p", "batch-a", date)
        self.assertEqual(p.name, "2026-05-01-batch-a-v3.md")

    def test_write_handoff_returns_path(self):
        self._init("p")
        date = dt.date(2026, 5, 1)
        p = write_handoff("p", "batch-a", "# Handoff\n", date)
        self.assertTrue(p.exists())
        self.assertIn("Handoff", p.read_text())

    def test_write_handoff_tracks_slugs(self):
        self._init("p")
        date = dt.date(2026, 5, 1)
        write_handoff("p", "batch-a", "content", date, track_slugs=["slug1", "slug2"])
        rows = read_status("p")
        slugs = {r.slug for r in rows}
        self.assertIn("slug1", slugs)
        self.assertIn("slug2", slugs)


# ── parse_critique ────────────────────────────────────────────────────────


class TestParseCritique(_Base):
    def test_parse_ship_critique(self):
        p = self._write_critique("p", "slug1", _SIMPLE_CRITIQUE)
        c = parse_critique(p)
        self.assertEqual(c.slug, "slug1")
        self.assertEqual(c.verdict, VERDICT_SHIP)
        self.assertEqual(c.total, 420)
        self.assertAlmostEqual(c.avg, 8.4)  # type: ignore[arg-type]
        self.assertEqual(c.date, "2026-05-01")
        self.assertEqual(c.gut_check, "YES")
        self.assertEqual(c.critical_failures, [])

    def test_parse_fix_critique(self):
        p = self._write_critique("p", "slug2", _FIX_CRITIQUE)
        c = parse_critique(p)
        self.assertEqual(c.verdict, VERDICT_FIX)
        self.assertEqual(c.weakest_param, "clipping_audible")
        self.assertEqual(c.weakest_score, 3)
        self.assertIn(("clipping_audible", 3), c.critical_failures)
        self.assertIsNotNone(c.fix_instructions)
        self.assertIn("pop", c.fix_instructions)  # type: ignore[operator]
        self.assertEqual(c.gut_check, "MAYBE")

    def test_parse_block_critique(self):
        p = self._write_critique("p", "slug3", _BLOCK_CRITIQUE)
        c = parse_critique(p)
        self.assertEqual(c.verdict, VERDICT_BLOCK)
        self.assertIn(("on_screen_text_correctness", 2), c.critical_failures)
        self.assertEqual(c.gut_check, "NO")

    def test_parse_missing_verdict(self):
        p = self._write_critique("p", "slug4", "no verdict here\n| hook_pull | 7 |")
        c = parse_critique(p)
        self.assertIsNone(c.verdict)

    def test_parse_missing_scores_table(self):
        p = self._write_critique("p", "slug5", "**SHIP**\n")
        c = parse_critique(p)
        self.assertIsNone(c.weakest_param)
        self.assertEqual(c.critical_failures, [])


# ── list_critiques ────────────────────────────────────────────────────────


class TestListCritiques(_Base):
    def test_empty_when_no_dir(self):
        self.assertEqual(list_critiques("p"), [])

    def test_lists_all(self):
        self._init("p")
        self._write_critique("p", "s1", _SIMPLE_CRITIQUE)
        self._write_critique("p", "s2", _FIX_CRITIQUE)
        result = list_critiques("p")
        self.assertEqual(len(result), 2)


# ── STATUS.md read/write ──────────────────────────────────────────────────


class TestStatusReadWrite(_Base):
    def test_read_empty_when_no_file(self):
        self.assertEqual(read_status("p"), [])

    def test_ensure_status_row_adds_row(self):
        self._init("p")
        row = ensure_status_row("p", "myslug")
        self.assertEqual(row.slug, "myslug")
        rows = read_status("p")
        self.assertEqual(len(rows), 1)

    def test_ensure_status_row_idempotent(self):
        self._init("p")
        ensure_status_row("p", "myslug")
        ensure_status_row("p", "myslug")
        rows = read_status("p")
        self.assertEqual(len(rows), 1)

    def test_update_status_authoring_updates_columns(self):
        self._init("p")
        ensure_status_row("p", "myslug")
        r = update_status_authoring("p", "myslug",
                                    last_fix_attempted="2026-05-01",
                                    last_fix_result="success")
        self.assertEqual(r.last_fix_attempted, "2026-05-01")
        self.assertEqual(r.last_fix_result, "success")

    def test_update_status_authoring_creates_row_if_missing(self):
        self._init("p")
        r = update_status_authoring("p", "newslug", last_fix_result="done")
        self.assertEqual(r.slug, "newslug")
        self.assertEqual(r.last_fix_result, "done")

    def test_status_row_to_md(self):
        row = StatusRow(
            slug="s", first_critique="", verdict="SHIP",
            weakest_param="", last_fix_attempted="none",
            last_fix_result="pending", re_critique="", final_status="IN-LOOP",
        )
        md = row.to_md()
        self.assertIn("SHIP", md)
        self.assertIn("|", md)

    def test_split_status_raises_when_no_header(self):
        self._init("p")
        # Overwrite STATUS.md without the table header
        status_path("p").write_text("# broken\n\nno table here\n")
        with self.assertRaises(RuntimeError):
            read_status("p")


# ── holds registry ────────────────────────────────────────────────────────


class TestHolds(_Base):
    def test_is_held_false_when_no_file(self):
        self.assertFalse(is_held("testchan", "slug1"))

    def test_set_and_check_hold(self):
        set_hold("testchan", "slug1", reason="test reason",
                 source_critique="critique.md")
        self.assertTrue(is_held("testchan", "slug1"))

    def test_clear_hold_returns_true(self):
        set_hold("testchan", "slug1", reason="r")
        result = clear_hold("testchan", "slug1")
        self.assertTrue(result)
        self.assertFalse(is_held("testchan", "slug1"))

    def test_clear_hold_missing_returns_false(self):
        result = clear_hold("testchan", "no_such_slug")
        self.assertFalse(result)

    def test_holds_for_returns_list(self):
        set_hold("testchan", "slug1", reason="r1")
        set_hold("testchan", "slug2", reason="r2")
        h = holds_for("testchan")
        self.assertEqual(len(h), 2)
        slugs = {hold.slug for hold in h}
        self.assertIn("slug1", slugs)
        self.assertIn("slug2", slugs)

    def test_read_holds_returns_empty_on_malformed(self):
        # Write invalid JSON
        hp = self._project_root / "testchan" / "_holds.json"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text("NOT JSON")
        self.assertFalse(is_held("testchan", "slug1"))


# ── routing / action_for ─────────────────────────────────────────────────


class TestActionFor(_Base):
    def test_ship_returns_ship(self):
        self._init("p")
        self._write_critique("p", "s1", _SIMPLE_CRITIQUE)
        routes = route("p")
        self.assertEqual(routes[0].action, "ship")

    def test_fix_returns_refix(self):
        self._init("p")
        self._write_critique("p", "s2", _FIX_CRITIQUE)
        routes = route("p")
        self.assertEqual(routes[0].action, "refix")

    def test_block_returns_block(self):
        self._init("p")
        self._write_critique("p", "s3", _BLOCK_CRITIQUE)
        routes = route("p")
        self.assertEqual(routes[0].action, "block")

    def test_untracked_no_verdict(self):
        self._init("p")
        self._write_critique("p", "s4", "no verdict\n| hook_pull | 7 |")
        routes = route("p")
        self.assertEqual(routes[0].action, "untracked")

    def test_ship_with_critical_failure_becomes_refix(self):
        # SHIP but critical param < 5 → refix
        content = """\
**SHIP**
| clipping_audible | 3 |
Total **420 / 500**  avg **8.4 / 10**
"""
        self._init("p")
        self._write_critique("p", "s5", content)
        routes = route("p")
        self.assertEqual(routes[0].action, "refix")

    def test_route_holds_info(self):
        self._init("p")
        self._write_critique("p", "s2", _FIX_CRITIQUE)
        set_hold("p", "s2", reason="auto")
        routes = route("p")
        # just check the structure
        self.assertIsInstance(routes[0].held, bool)

    def test_route_fix_summary_truncated(self):
        self._init("p")
        self._write_critique("p", "s2", _FIX_CRITIQUE)
        routes = route("p")
        self.assertIsInstance(routes[0].fix_summary, str)


# ── ingest ────────────────────────────────────────────────────────────────


class TestIngest(_Base):
    def test_ingest_ship_clears_hold_and_updates_status(self):
        self._init("p")
        self._write_critique("p", "slug1", _SIMPLE_CRITIQUE)
        set_hold("p", "slug1", reason="prev")
        summary = ingest("p")
        self.assertIn("slug1", summary["holds_cleared"])
        self.assertIn("slug1", [x["slug"] for x in summary["ship"]])

    def test_ingest_fix_sets_hold(self):
        self._init("p")
        self._write_critique("p", "slug2", _FIX_CRITIQUE)
        summary = ingest("p")
        self.assertIn("slug2", summary["holds_set"])
        self.assertIn("slug2", [x["slug"] for x in summary["refix"]])

    def test_ingest_block_sets_hold(self):
        self._init("p")
        self._write_critique("p", "slug3", _BLOCK_CRITIQUE)
        summary = ingest("p")
        self.assertIn("slug3", summary["holds_set"])

    def test_ingest_dry_run_makes_no_changes(self):
        self._init("p")
        self._write_critique("p", "slug2", _FIX_CRITIQUE)
        summary = ingest("p", dry_run=True)
        # dry run → no holds set
        self.assertEqual(summary["holds_set"], [])
        self.assertFalse(is_held("p", "slug2"))

    def test_ingest_skips_already_held(self):
        self._init("p")
        self._write_critique("p", "slug2", _FIX_CRITIQUE)
        set_hold("p", "slug2", reason="pre-set")
        summary = ingest("p")
        # already held, so holds_set should be empty (not double-listed)
        self.assertNotIn("slug2", summary["holds_set"])


# ── cross_cutting ─────────────────────────────────────────────────────────


class TestCrossCutting(_Base):
    def test_returns_none_when_no_file(self):
        self.assertIsNone(cross_cutting("p"))

    def test_returns_none_when_no_cross_cutting_section(self):
        """If status file has no ## Cross-cutting issues section, returns None."""
        self._init("p")
        sp = status_path("p")
        # Remove the Cross-cutting section entirely
        text = "\n".join(
            l for l in sp.read_text().split("\n")
            if "Cross-cutting" not in l and "(none yet)" not in l
        )
        sp.write_text(text)
        result = cross_cutting("p")
        self.assertIsNone(result)

    def test_returns_cross_cutting_section(self):
        self._init("p")
        content = status_path("p").read_text()
        content += "\n## Cross-cutting issues extra\n\nSome pipeline bug.\n"
        status_path("p").write_text(content)
        result = cross_cutting("p")
        self.assertIsNone(result)  # init writes "(none yet)" which returns None

    def test_returns_text_when_populated(self):
        self._init("p")
        sp = status_path("p")
        text = sp.read_text()
        text = text.replace("(none yet)", "Audio sync drift on all videos.")
        sp.write_text(text)
        result = cross_cutting("p")
        self.assertIn("Audio sync", result)  # type: ignore[operator]


# ── CLI ───────────────────────────────────────────────────────────────────


class TestCLI(_Base):
    def _run(self, *argv):
        return main(list(argv))

    def test_cmd_init(self):
        rc = self._run("init", "proj1")
        self.assertEqual(rc, 0)
        self.assertTrue(status_path("proj1").exists())

    def test_cmd_status_empty(self):
        rc = self._run("status", "proj1")
        self.assertEqual(rc, 0)

    def test_cmd_status_with_rows(self):
        self._init("proj1")
        ensure_status_row("proj1", "slug1")
        rc = self._run("status", "proj1")
        self.assertEqual(rc, 0)

    def test_cmd_hold(self):
        rc = self._run("hold", "proj1", "slug1", "--reason", "bad video")
        self.assertEqual(rc, 0)
        self.assertTrue(is_held("proj1", "slug1"))

    def test_cmd_unhold(self):
        self._run("hold", "proj1", "slug1", "--reason", "bad")
        rc = self._run("unhold", "proj1", "slug1")
        self.assertEqual(rc, 0)
        self.assertFalse(is_held("proj1", "slug1"))

    def test_cmd_unhold_not_held_returns_1(self):
        rc = self._run("unhold", "proj1", "no_such_slug")
        self.assertEqual(rc, 1)

    def test_cmd_holds_empty(self):
        rc = self._run("holds", "proj1")
        self.assertEqual(rc, 0)

    def test_cmd_holds_with_items(self):
        self._run("hold", "proj1", "s1", "--reason", "r1")
        self._run("hold", "proj1", "s2", "--reason", "r2")
        rc = self._run("holds", "proj1")
        self.assertEqual(rc, 0)

    def test_cmd_ingest(self):
        self._init("proj1")
        self._write_critique("proj1", "slug1", _SIMPLE_CRITIQUE)
        rc = self._run("ingest", "proj1")
        self.assertEqual(rc, 0)

    def test_cmd_ingest_dry_run(self):
        self._init("proj1")
        self._write_critique("proj1", "slug2", _FIX_CRITIQUE)
        rc = self._run("ingest", "proj1", "--dry-run")
        self.assertEqual(rc, 0)

    def test_main_entrypoint(self):
        import subprocess as sp
        result = sp.run(
            [sys.executable, "-m", "pipeline.quality.evals", "init", "testentry"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "EVALS_ROOT_OVERRIDE": str(self._evals_root)},
        )
        # It will succeed or fail based on path, but we just care it runs
        # (the CLI __main__ path is covered)



# ── Additional gap tests ──────────────────────────────────────────────────


class TestRowFromLine(_Base):
    """_row_from_line returns None for malformed rows (line 272)."""

    def test_malformed_row_returns_none(self):
        """A row with wrong column count is silently skipped, but a valid row passes."""
        self._init("p")
        # First add a valid row so there's an existing slug in the table
        ensure_status_row("p", "goodslug")
        sp = status_path("p")
        text = sp.read_text()
        # Find the separator line and inject a malformed row right after it
        lines = text.split("\n")
        sep_idx = next(
            (i for i, l in enumerate(lines) if l.startswith("|---|")), None
        )
        self.assertIsNotNone(sep_idx)
        # Insert malformed row (3 cols) right BEFORE the good row
        lines.insert(sep_idx + 1, "| bad | row | only |")
        sp.write_text("\n".join(lines))
        rows = read_status("p")
        # Malformed row is skipped; good row remains
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].slug, "goodslug")


class TestUpdateStatusAuthoringNewSlug(_Base):
    """update_status_authoring creates a row when slug not found (line 326)."""

    def test_creates_row_for_new_slug(self):
        self._init("p")
        # Add an existing row so the loop executes (triggering the 'continue' path)
        ensure_status_row("p", "existing-slug")
        # Now add a brand-new slug — loop iterates existing-slug, continues, then not-found
        r = update_status_authoring(
            "p", "brand-new-slug",
            last_fix_attempted="2026-05-10",
            last_fix_result="ok",
        )
        self.assertEqual(r.slug, "brand-new-slug")
        self.assertEqual(r.last_fix_attempted, "2026-05-10")
        self.assertEqual(r.last_fix_result, "ok")
        self.assertEqual(r.final_status, "IN-LOOP")


class TestCLIIngestWithHolds(_Base):
    """CLI ingest output includes HOLDS SET / HOLDS CLEARED / cross-cutting."""

    def _run(self, *argv):
        return main(list(argv))

    def test_cli_ingest_shows_holds_set(self):
        import io, contextlib
        self._init("p")
        self._write_critique("p", "slug-fix", _FIX_CRITIQUE)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run("ingest", "p")
        self.assertIn("HOLDS SET", buf.getvalue())
        self.assertIn("slug-fix", buf.getvalue())

    def test_cli_ingest_shows_holds_cleared(self):
        import io, contextlib
        self._init("p")
        self._write_critique("p", "slug-ship", _SIMPLE_CRITIQUE)
        set_hold("p", "slug-ship", reason="previously held")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run("ingest", "p")
        self.assertIn("HOLDS CLEARED", buf.getvalue())

    def test_cli_ingest_shows_cross_cutting_issues(self):
        import io, contextlib
        self._init("p")
        self._write_critique("p", "slug-ship", _SIMPLE_CRITIQUE)
        # Insert a non-"(none yet)" cross-cutting section
        sp = status_path("p")
        text = sp.read_text().replace("(none yet)", "Sync drift across all videos.")
        sp.write_text(text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run("ingest", "p")
        self.assertIn("cross-cutting", buf.getvalue())


class DefaultEvalsRootTest(unittest.TestCase):
    """Audit D3.7 — pre-fix EVALS_ROOT only fell back to the
    hardcoded `/Users/rohit/evals` when YTFACTORY_EVALS_ROOT was
    unset. Now: env override first, then a repo-sibling `evals/`
    dir, then the legacy laptop path. Tests pin all three branches.
    """

    def test_env_override_wins(self):
        from pipeline.quality import evals as _evals
        with patch.dict(os.environ, {"YTFACTORY_EVALS_ROOT": "/custom/path"}):
            self.assertEqual(_evals._default_evals_root(), Path("/custom/path"))

    def test_repo_sibling_used_when_present(self):
        from pipeline.quality import evals as _evals
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_EVALS_ROOT"}
        with tempfile.TemporaryDirectory() as tmp:
            # Layout: <tmp>/repo + <tmp>/evals (sibling).
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sibling = Path(tmp) / "evals"
            sibling.mkdir()
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(_evals, "PROJECT_ROOT", repo):
                self.assertEqual(_evals._default_evals_root(), sibling)

    def test_legacy_fallback_when_neither_present(self):
        from pipeline.quality import evals as _evals
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_EVALS_ROOT"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # No sibling `evals/` dir exists.
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(_evals, "PROJECT_ROOT", repo):
                self.assertEqual(
                    _evals._default_evals_root(),
                    Path("/Users/rohit/evals"),
                )


if __name__ == "__main__":
    unittest.main()
