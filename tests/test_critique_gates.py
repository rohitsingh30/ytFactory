"""Tests for pipeline/critique/gates.py (2026-05-11).

The gate harness sits between the agent's "I'm done" claim and a
direct-to-main push. A bug here either silently lets a bad change
through (catastrophic) or wrongly rejects a good change (just
annoying). We unit-test every gate against a real ephemeral git
repo so the test surface mirrors production exactly.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from pipeline.critique import gates


# ---------------------------------------------------------------------------
# Helpers — build an ephemeral git repo per test
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t.io", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(repo: Path, *, initial_files: dict[str, str] | None = None) -> None:
    """Init repo + drop initial files + commit so HEAD exists."""
    _git(repo, "init", "-q", "-b", "main")
    if initial_files:
        for rel, body in initial_files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
            _git(repo, "add", rel)
        _git(repo, "commit", "-q", "-m", "init")
    else:
        # Need at least one commit so HEAD exists for diff comparisons.
        (repo / "README.md").write_text("init\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "init")


def _stage_all(repo: Path) -> None:
    """Mirror what the runner does before invoking gates: stage every
    change so brand-new files appear in `git diff --cached HEAD`.

    Required because the gate harness inspects `--cached` diffs by
    default — same view git uses at commit time."""
    _git(repo, "add", "-A")


# ---------------------------------------------------------------------------
# ensure_diff_exists
# ---------------------------------------------------------------------------


class EnsureDiffExistsTests(unittest.TestCase):
    def test_passes_when_py_file_changed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"pipeline/x.py": "x = 1\n"})
            (repo / "pipeline" / "x.py").write_text("x = 1\ny = 2\n")
            _stage_all(repo)
            r = gates.ensure_diff_exists(repo)
        self.assertTrue(r.passed, r.detail)
        self.assertIn("pipeline/x.py", r.metadata["files"])

    def test_fails_on_empty_diff(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            r = gates.ensure_diff_exists(repo)
        self.assertFalse(r.passed)
        self.assertIn("no file changes", r.msg)

    def test_ignores_binary_blobs(self):
        # Add an mp4 — must NOT count as a real diff for this gate.
        # A pipeline-fix critique that only landed a media file is
        # treated as zero useful work.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / "shorts").mkdir()
            (repo / "shorts" / "out.mp4").write_bytes(b"\x00" * 10)
            _stage_all(repo)
            r = gates.ensure_diff_exists(repo)
        self.assertFalse(r.passed)

    def test_unstaged_mode_sees_working_tree_changes(self):
        # Tests can opt out of staging by passing staged=False —
        # useful for quick "does my edit look right" checks before
        # the runner stages.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"pipeline/x.py": "x = 1\n"})
            (repo / "pipeline" / "x.py").write_text("x = 2\n")
            # Deliberately DON'T `git add` — the staged-mode gate would
            # see no change, but staged=False sees the modification.
            r_staged = gates.ensure_diff_exists(repo, staged=True)
            r_unstaged = gates.ensure_diff_exists(repo, staged=False)
        self.assertFalse(r_staged.passed)
        self.assertTrue(r_unstaged.passed)


# ---------------------------------------------------------------------------
# ensure_tests_added
# ---------------------------------------------------------------------------


class EnsureTestsAddedTests(unittest.TestCase):
    def test_passes_when_tests_dir_file_added(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"pipeline/x.py": "x = 1\n"})
            (repo / "pipeline" / "x.py").write_text("x = 2\n")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_x.py").write_text("def test_x(): assert True\n")
            _stage_all(repo)
            r = gates.ensure_tests_added(repo)
        self.assertTrue(r.passed, r.detail)
        self.assertIn("tests/test_x.py", r.metadata["test_files"])

    def test_passes_when_test_named_file_added(self):
        # A file at the repo root named test_*.py also qualifies — we
        # tolerate both layouts because the codebase has historically
        # mixed them (tests/ + scripts/test_*.py).
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"pipeline/x.py": "x = 1\n"})
            (repo / "pipeline" / "x.py").write_text("x = 2\n")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "test_x.py").write_text("def test_x(): assert True\n")
            _stage_all(repo)
            r = gates.ensure_tests_added(repo)
        self.assertTrue(r.passed)
        self.assertEqual(r.metadata["test_files"], ["scripts/test_x.py"])

    def test_fails_when_only_source_changed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"pipeline/x.py": "x = 1\n"})
            (repo / "pipeline" / "x.py").write_text("x = 2\n")
            _stage_all(repo)
            r = gates.ensure_tests_added(repo)
        self.assertFalse(r.passed)
        self.assertIn("Direct-to-main", r.detail)

    def test_test_deletion_alone_does_not_satisfy(self):
        # If the agent ONLY deleted a test, that's insufficient — the
        # gate requires Added or Modified, never plain Delete.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={
                "pipeline/x.py": "x = 1\n",
                "tests/test_x.py": "def test_x(): assert True\n",
            })
            (repo / "pipeline" / "x.py").write_text("x = 2\n")
            (repo / "tests" / "test_x.py").unlink()
            _stage_all(repo)
            r = gates.ensure_tests_added(repo)
        self.assertFalse(r.passed)


# ---------------------------------------------------------------------------
# run_pytest_full
# ---------------------------------------------------------------------------


class RunPytestFullTests(unittest.TestCase):
    def _make_repo_with_test(self, body: str) -> Path:
        # Caller owns the temp dir lifetime; we hand back the path.
        td = tempfile.mkdtemp(prefix="gate-pytest-")
        repo = Path(td)
        _init_repo(repo, initial_files={
            "tests/test_a.py": textwrap.dedent(body),
        })
        return repo

    def test_passes_when_suite_green(self):
        repo = self._make_repo_with_test("""
            def test_ok():
                assert 1 + 1 == 2
        """)
        try:
            r = gates.run_pytest_full(repo, timeout_s=60)
            self.assertTrue(r.passed, r.detail)
            self.assertIn("passed", r.msg)
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)

    def test_fails_when_suite_has_failure(self):
        repo = self._make_repo_with_test("""
            def test_bad():
                assert False, 'intentional'
        """)
        try:
            r = gates.run_pytest_full(repo, timeout_s=60)
            self.assertFalse(r.passed)
            self.assertIn("failed", r.msg)
            # Failing test name surfaced for the agent to act on.
            self.assertTrue(any("test_bad" in f for f in r.metadata["failures"]))
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)

    def test_runs_under_coverage_when_data_file_set(self):
        # When coverage_data_file is supplied, the data file must
        # exist after the run so run_diff_coverage can read it.
        repo = self._make_repo_with_test("""
            def test_ok():
                assert True
        """)
        cov_file = repo / ".coverage.gate"
        try:
            r = gates.run_pytest_full(
                repo,
                timeout_s=60,
                coverage_data_file=str(cov_file),
            )
            self.assertTrue(r.passed, r.detail)
            self.assertTrue(cov_file.exists(),
                            "coverage data file was not produced")
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)


# ---------------------------------------------------------------------------
# run_diff_coverage
# ---------------------------------------------------------------------------


class RunDiffCoverageTests(unittest.TestCase):
    def _build_covered_repo(self) -> Path:
        """Create a repo where the agent added two new lines to mod.py
        and a test that exercises both, so diff-coverage = 100%.
        """
        td = tempfile.mkdtemp(prefix="gate-diffcov-")
        repo = Path(td)
        _init_repo(repo, initial_files={
            "mod.py": textwrap.dedent("""\
                def existing():
                    return 1
            """),
            "tests/test_mod.py": textwrap.dedent("""\
                from mod import existing
                def test_existing():
                    assert existing() == 1
            """),
        })
        # Now apply the agent's change: add two new lines + extend the test.
        (repo / "mod.py").write_text(textwrap.dedent("""\
            def existing():
                return 1

            def added():
                return 42
        """))
        (repo / "tests" / "test_mod.py").write_text(textwrap.dedent("""\
            from mod import existing, added
            def test_existing():
                assert existing() == 1
            def test_added():
                assert added() == 42
        """))
        return repo

    def _build_uncovered_repo(self) -> Path:
        """Same shape but with the new line LEFT uncovered — diff-coverage must fail."""
        td = tempfile.mkdtemp(prefix="gate-diffcov-uncov-")
        repo = Path(td)
        _init_repo(repo, initial_files={
            "mod.py": textwrap.dedent("""\
                def existing():
                    return 1
            """),
            "tests/test_mod.py": textwrap.dedent("""\
                from mod import existing
                def test_existing():
                    assert existing() == 1
            """),
        })
        (repo / "mod.py").write_text(textwrap.dedent("""\
            def existing():
                return 1

            def added():
                return 42
        """))
        # Don't add a test for `added`.
        return repo

    def test_passes_when_every_added_line_is_covered(self):
        repo = self._build_covered_repo()
        cov_file = repo / ".coverage.gate"
        try:
            _stage_all(repo)
            pytest_res = gates.run_pytest_full(
                repo,
                timeout_s=60,
                coverage_data_file=str(cov_file),
            )
            self.assertTrue(pytest_res.passed)
            r = gates.run_diff_coverage(
                repo,
                coverage_data_file=str(cov_file),
            )
            self.assertTrue(r.passed, r.detail)
            self.assertIn("100%", r.msg)
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)

    def test_fails_when_any_added_line_uncovered(self):
        repo = self._build_uncovered_repo()
        cov_file = repo / ".coverage.gate"
        try:
            _stage_all(repo)
            gates.run_pytest_full(
                repo, timeout_s=60, coverage_data_file=str(cov_file),
            )
            r = gates.run_diff_coverage(repo, coverage_data_file=str(cov_file))
            self.assertFalse(r.passed, r.detail)
            self.assertIn("missed", r.msg)
            # The uncovered `return 42` line MUST appear in the missed list.
            missed_files = {f for (f, ln) in r.metadata["missed"]}
            self.assertIn("mod.py", missed_files)
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)

    def test_passes_when_only_comments_changed(self):
        # Edge case: agent changed a docstring / comment; diff exists but
        # there are zero NEW executable lines. Gate should pass with a
        # note rather than divide-by-zero or trip on absent test runs.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={
                "mod.py": "x = 1\n",
                "tests/test_mod.py": "import mod\ndef test_mod(): assert mod.x == 1\n",
            })
            # Comment-only change → no new executable lines.
            (repo / "mod.py").write_text("# new comment\nx = 1\n")
            _stage_all(repo)
            cov_file = repo / ".coverage.gate"
            gates.run_pytest_full(
                repo, timeout_s=60, coverage_data_file=str(cov_file),
            )
            r = gates.run_diff_coverage(repo, coverage_data_file=str(cov_file))
        self.assertTrue(r.passed, r.detail)
        self.assertIn("docs/comments", r.msg)


# ---------------------------------------------------------------------------
# run_all_gates orchestrator — fail-fast contract
# ---------------------------------------------------------------------------


class RunAllGatesTests(unittest.TestCase):
    def test_fail_fast_on_no_diff(self):
        # No changes → diff_exists fails → no other gate runs (we
        # check by ensuring `results` length == 1).
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            report = gates.run_all_gates(repo)
        self.assertFalse(report.all_passed)
        self.assertEqual([r.name for r in report.results], ["diff_exists"])
        self.assertEqual(report.first_failure.name, "diff_exists")

    def test_fail_fast_on_missing_tests(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"pipeline/x.py": "x = 1\n"})
            (repo / "pipeline" / "x.py").write_text("x = 2\n")
            _stage_all(repo)
            report = gates.run_all_gates(repo)
        self.assertFalse(report.all_passed)
        self.assertEqual([r.name for r in report.results],
                         ["diff_exists", "tests_added"])

    def test_full_pass_runs_every_gate(self):
        # Real positive test through the orchestrator — covers the
        # happy path that triggers a push.
        td = tempfile.mkdtemp(prefix="gate-orch-")
        repo = Path(td)
        try:
            _init_repo(repo, initial_files={
                "mod.py": "def f():\n    return 1\n",
                "tests/test_mod.py": "import mod\ndef test_f(): assert mod.f() == 1\n",
            })
            (repo / "mod.py").write_text(
                "def f():\n    return 1\n\n"
                "def g():\n    return 2\n"
            )
            (repo / "tests" / "test_mod.py").write_text(
                "import mod\n"
                "def test_f(): assert mod.f() == 1\n"
                "def test_g(): assert mod.g() == 2\n"
            )
            _stage_all(repo)
            report = gates.run_all_gates(repo, pytest_timeout_s=60)
            self.assertTrue(report.all_passed,
                            "\n".join(f"{r.name}: {r.msg}" for r in report.results))
            self.assertEqual(
                [r.name for r in report.results],
                ["diff_exists", "tests_added", "pytest_full", "diff_coverage"],
            )
            self.assertIsNone(report.first_failure)
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)


# ---------------------------------------------------------------------------
# Lower-level diff parser
# ---------------------------------------------------------------------------


class AddedLinesParserTests(unittest.TestCase):
    """The diff parser is the most error-prone bit (off-by-ones in
    hunk arithmetic, mishandling of -U0). Test it directly so a
    regression here is easy to catch without the full coverage flow.
    """

    def test_added_lines_simple_append(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"x.py": "a = 1\n"})
            (repo / "x.py").write_text("a = 1\nb = 2\nc = 3\n")
            _stage_all(repo)
            added = gates._added_lines_for_file(repo, "x.py")
        # Lines 2 and 3 are the new additions in the AFTER view.
        self.assertEqual(added, [2, 3])

    def test_added_lines_in_middle(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"x.py": "a\nb\nc\n"})
            (repo / "x.py").write_text("a\nNEW1\nNEW2\nb\nc\n")
            _stage_all(repo)
            added = gates._added_lines_for_file(repo, "x.py")
        self.assertEqual(added, [2, 3])

    def test_added_lines_with_replacement(self):
        # Replace b with B,B2,B3 — the AFTER view has B at 2, B2 at 3, B3 at 4.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo, initial_files={"x.py": "a\nb\nc\n"})
            (repo / "x.py").write_text("a\nB\nB2\nB3\nc\n")
            _stage_all(repo)
            added = gates._added_lines_for_file(repo, "x.py")
        self.assertEqual(added, [2, 3, 4])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
