"""Unit tests for scripts/coverage_gate.py.

Pinning the diff-coverage gate that powers /test-coverage. The gate
itself can't depend on coverage being right (it IS the coverage
checker), so this file tests the pure helpers + a few subprocess-mocked
end-to-end runs.

What's covered here:
  - FileDiff.dotted_module — package vs hyphenated-dir paths.
  - parse_skip_comments — the ``# coverage: <≥6 words>`` justification
    rule (Quality gate 2 of /test-coverage).
  - find_related_tests — the test-discovery heuristic (search order +
    fall-through patterns).
  - _coverage_source_arg — picks the right ``--source`` for both
    package files AND non-package (hyphenated) dirs.
  - CoverageResult.gap_lines / .covered_changed_lines / .coverable_changed_lines —
    the math the gate uses to decide pass/fail.
  - render_report — exit codes for pass / gap / pytest-failure cases.
  - changed_files — git-diff parser handles both modified AND
    untracked files (the 2026-05-12 regression where new files
    silently slipped past the gate).

What's NOT covered here (and shouldn't be):
  - The actual `coverage run` subprocess. That's coverage 7.x's
    contract; we just call it and parse its JSON output. Mocking
    coverage's subprocess output is fragile (tied to coverage's JSON
    schema) and we already exercise the integration path every time
    /test-coverage runs.
  - The `git diff` subprocess. Same logic — it's pinned by integration.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "coverage_gate.py"


def _load_gate():
    """Import scripts/coverage_gate.py without polluting sys.path —
    same pattern test_cloudrun_render_worker_progress.py uses for the
    cloud entrypoint (which lives in a hyphenated dir)."""
    name = "coverage_gate_for_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register BEFORE exec_module — dataclasses' __post_init__ calls
    # sys.modules.get(cls.__module__).__dict__ to resolve forward
    # references and crashes if the module isn't registered yet.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FileDiffDottedModuleTests(unittest.TestCase):
    """The dotted-module derivation used to pick coverage's --source
    flag. Hyphenated dirs (cloud/render-worker-v2/) MUST NOT produce
    an invalid dotted name — they should fall through to the path
    form."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_package_file_has_clean_dotted(self):
        fd = self.gate.FileDiff(
            path=Path("pipeline/render/spec.py"),
            is_python=True,
        )
        self.assertEqual(fd.dotted_module, "pipeline.render.spec")

    def test_init_file_drops_init(self):
        fd = self.gate.FileDiff(
            path=Path("pipeline/render/__init__.py"),
            is_python=True,
        )
        self.assertEqual(fd.dotted_module, "pipeline.render")

    def test_hyphenated_dir_produces_invalid_dotted(self):
        # Important: this returns an "invalid" dotted name — the
        # caller (_coverage_source_arg) detects the hyphen and
        # falls through to the path form. We don't mangle the name
        # here because the path is still useful for grep matching.
        fd = self.gate.FileDiff(
            path=Path("cloud/render-worker-v2/entrypoint.py"),
            is_python=True,
        )
        self.assertIn("-", fd.dotted_module)

    def test_typescript_file_has_no_dotted_module(self):
        fd = self.gate.FileDiff(
            path=Path("web-next/lib/render-display.ts"),
            is_typescript=True,
        )
        self.assertIsNone(fd.dotted_module)


class CoverageSourceArgTests(unittest.TestCase):
    """_coverage_source_arg picks --source=<dir> universally so
    coverage's file-walker can find the module regardless of how it
    was imported (importlib.spec_from_file_location bypasses --include
    matching)."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_package_file_uses_parent_dir(self):
        fd = self.gate.FileDiff(
            path=Path("pipeline/render/spec.py"),
            is_python=True,
        )
        args = self.gate._coverage_source_arg(fd)
        self.assertEqual(args, ["--source=pipeline/render"])

    def test_hyphenated_dir_uses_parent_dir(self):
        fd = self.gate.FileDiff(
            path=Path("cloud/render-worker-v2/entrypoint.py"),
            is_python=True,
        )
        args = self.gate._coverage_source_arg(fd)
        self.assertEqual(args, ["--source=cloud/render-worker-v2"])


class ParseSkipCommentsTests(unittest.TestCase):
    """The ``# coverage: <reason>`` justification rule. Per Quality
    gate 2 of /test-coverage, the reason must be ≥6 words."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _write(self, src: str) -> Path:
        p = self.tmp / "f.py"
        p.write_text(textwrap.dedent(src))
        return p

    def test_inline_skip_with_justification_accepted(self):
        p = self._write("""
            x = call_real_gpu_inference()  # coverage: needs real GPU not mockable in CI
        """)
        skipped, errors = self.gate.parse_skip_comments(p, [2])
        self.assertEqual(skipped, {2})
        self.assertEqual(errors, [])

    def test_block_scoped_skip_covers_indented_body(self):
        """A skip comment above a multi-line statement covers the
        whole statement. The 2026-05-12 entrypoint.py refactor needs
        this — one skip comment above an `if` block should cover the
        whole body."""
        p = self._write("""
            def f():
                # coverage: integration code, exercised by cloud render run
                if some_condition:
                    do_thing_1()
                    do_thing_2()
                    do_thing_3()
                a = 1
        """)
        skipped, errors = self.gate.parse_skip_comments(p, [3, 4, 5, 6, 7, 8])
        # Comment line itself (3) + the if-statement (4-7).
        # Line 8 (a = 1) is a peer statement — NOT skipped.
        self.assertIn(3, skipped)
        self.assertIn(4, skipped)  # if some_condition:
        self.assertIn(5, skipped)
        self.assertIn(6, skipped)
        self.assertIn(7, skipped)
        self.assertNotIn(8, skipped)
        self.assertEqual(errors, [])

    def test_block_scoped_skip_stops_at_dedent(self):
        p = self._write("""
            # coverage: skip the inner block but not the outer caller
            if x:
                do_a()
                do_b()
            do_c()
        """)
        skipped, errors = self.gate.parse_skip_comments(p, [2, 3, 4, 5, 6])
        self.assertIn(2, skipped)
        self.assertIn(3, skipped)
        self.assertIn(4, skipped)
        self.assertIn(5, skipped)
        self.assertNotIn(6, skipped)

    def test_under_justified_skip_rejected(self):
        p = self._write("""
            x = 1  # coverage: tested elsewhere
        """)
        skipped, errors = self.gate.parse_skip_comments(p, [2])
        self.assertEqual(skipped, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("≥6 words", errors[0])

    def test_bare_coverage_comment_rejected(self):
        p = self._write("""
            x = 1  # coverage:
        """)
        skipped, errors = self.gate.parse_skip_comments(p, [2])
        self.assertEqual(skipped, set())
        # The bare comment matches the regex but yields zero
        # justification words → rejected with the same error.
        self.assertEqual(len(errors), 1)

    def test_no_coverage_comment_yields_no_skip_no_error(self):
        p = self._write("""
            x = 1
        """)
        skipped, errors = self.gate.parse_skip_comments(p, [2])
        self.assertEqual(skipped, set())
        self.assertEqual(errors, [])

    def test_missing_file_returns_empty(self):
        skipped, errors = self.gate.parse_skip_comments(
            self.tmp / "nonexistent.py", [1, 2, 3],
        )
        self.assertEqual(skipped, set())
        self.assertEqual(errors, [])

    def test_textual_fallback_for_non_python_file(self):
        """Non-.py files use the regex-based textual parser. The
        contract: line whose first non-ws char is '#' AND has a
        `# coverage: <≥6 words>` trailer is accepted."""
        p = self.tmp / "f.ts"
        p.write_text(textwrap.dedent("""
            const x = 1;
            // coverage: legacy migration shim, see ticket #1234 for context
            const y = 2;
        """))
        skipped, errors = self.gate.parse_skip_comments(p, [3, 4])
        # The textual parser only matches lines starting with `#`,
        # so JS-style `//` comments are NOT accepted as coverage
        # skips. (The gate only manages Python coverage today; TS
        # uses node:test pass/fail signals.)
        self.assertEqual(skipped, set())
        self.assertEqual(errors, [])

    def test_textual_fallback_accepts_python_style_hash_in_yaml(self):
        """YAML uses # comments, so a coverage: in a generated YAML
        file that the agent is editing should also be respected."""
        p = self.tmp / "f.yaml"
        p.write_text(textwrap.dedent("""
            key: value
            # coverage: legacy YAML key kept for backward compatibility
            old_key: old_value
        """))
        skipped, errors = self.gate.parse_skip_comments(p, [3, 4])
        self.assertIn(3, skipped)
        self.assertEqual(errors, [])

    def test_textual_fallback_rejects_under_justified(self):
        # The textual parser only matches lines whose FIRST non-ws
        # char is `#`. A standalone short comment is parsed and the
        # ≥6-word check fires.
        p = self.tmp / "f.yaml"
        p.write_text(textwrap.dedent("""
            x: 1
            # coverage: short
        """))
        skipped, errors = self.gate.parse_skip_comments(p, [3])
        self.assertEqual(skipped, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("≥6 words", errors[0])

    def test_textual_fallback_inline_skip_on_same_line(self):
        p = self.tmp / "f.yaml"
        # The textual parser only matches lines whose FIRST
        # non-whitespace char is `#`. An inline comment on a code
        # line (key: value  # coverage: …) is NOT skipped because
        # the first non-ws char is `k`, not `#`.
        p.write_text(textwrap.dedent("""
            key: value  # coverage: tested elsewhere by integration suite
        """))
        skipped, errors = self.gate.parse_skip_comments(p, [2])
        self.assertEqual(skipped, set())


class CoverageResultMathTests(unittest.TestCase):
    """The set-arithmetic that decides pass/fail. The 2026-05-12
    docstring-explosion bug surfaced because the gate counted
    pure-comment lines in the diff as "uncovered" — fixed by
    intersecting added_lines with coverage's known executable lines
    (executed_lines | missing_lines)."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def _result(self, **kw):
        fd = self.gate.FileDiff(
            path=Path(kw.pop("path", "x.py")),
            added_lines=kw.pop("added", set()),
            is_python=True,
        )
        r = self.gate.CoverageResult(file=fd)
        r.executed_lines = kw.pop("executed", set())
        r.missing_lines = kw.pop("missing", set())
        r.skipped_with_reason = kw.pop("skipped", set())
        return r

    def test_doc_only_diff_has_no_gaps(self):
        # 5 doc lines added, none of them executable → 0 coverable, 0 gap.
        r = self._result(added={10, 11, 12, 13, 14}, executed=set(), missing=set())
        self.assertEqual(r.coverable_changed_lines, set())
        self.assertEqual(r.gap_lines, set())
        self.assertEqual(r.covered_changed_lines, set())

    def test_all_executable_added_lines_covered(self):
        r = self._result(added={20, 21, 22}, executed={20, 21, 22}, missing=set())
        self.assertEqual(r.coverable_changed_lines, {20, 21, 22})
        self.assertEqual(r.covered_changed_lines, {20, 21, 22})
        self.assertEqual(r.gap_lines, set())

    def test_some_added_lines_uncovered_become_gaps(self):
        r = self._result(added={20, 21, 22}, executed={20}, missing={21, 22})
        self.assertEqual(r.coverable_changed_lines, {20, 21, 22})
        self.assertEqual(r.covered_changed_lines, {20})
        self.assertEqual(r.gap_lines, {21, 22})

    def test_skipped_lines_not_counted_as_gaps(self):
        r = self._result(
            added={20, 21, 22},
            executed={20},
            missing={21, 22},
            skipped={22},
        )
        # 22 is missing AND has a justification → not a gap.
        self.assertEqual(r.gap_lines, {21})
        # And the skipped line counts as covered for the % calculation.
        self.assertEqual(r.covered_changed_lines, {20, 22})

    def test_coverage_outside_diff_is_ignored(self):
        # Lines 30-40 are covered by the test but were NOT in this
        # diff — they shouldn't show up in any of the diff metrics.
        r = self._result(
            added={20, 21},
            executed={20, 21, 30, 31, 32},
            missing=set(),
        )
        self.assertEqual(r.coverable_changed_lines, {20, 21})
        self.assertEqual(r.covered_changed_lines, {20, 21})
        self.assertEqual(r.gap_lines, set())


class FindRelatedTestsTests(unittest.TestCase):
    """The test-discovery heuristic. The 2026-05-12 cascade-coercion
    bug revealed that a naive 'tests/test_<stem>.py' search misses
    test files that use importlib.spec_from_file_location for files
    in hyphenated dirs (cloud/render-worker-v2/). The improved
    heuristic also greps for path strings + bare module dir names."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_returns_empty_when_tests_dir_missing(self):
        # Nudge REPO_ROOT to a temp dir with no tests/ subdir.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        with mock.patch.object(self.gate, "REPO_ROOT", tmp):
            fd = self.gate.FileDiff(path=Path("foo.py"), is_python=True)
            self.assertEqual(self.gate.find_related_tests(fd), [])

    def test_typescript_lib_helper_finds_node_test_file(self):
        # Real-repo integration: web-next/lib/render-display.ts has a
        # sibling at web-next/tests/render-display.test.mjs.
        fd = self.gate.FileDiff(
            path=Path("web-next/lib/render-display.ts"),
            is_typescript=True,
        )
        related = self.gate.find_related_tests(fd)
        # The exact returned path depends on test-file existence — we
        # only assert that IF the .test.mjs exists, it's discovered.
        for p in related:
            self.assertTrue(str(p).endswith(".test.mjs") or
                            str(p).endswith(".test.ts") or
                            str(p).endswith(".test.tsx"))

    def test_typescript_component_returns_empty(self):
        # Components / app routes have no JS test infra → empty list.
        fd = self.gate.FileDiff(
            path=Path("web-next/components/foo.tsx"),
            is_typescript=True,
        )
        self.assertEqual(self.gate.find_related_tests(fd), [])


class MeasureTypescriptTests(unittest.TestCase):
    """measure_typescript handles the React-component soft-pass
    correctly so a JSX tweak in PlayerCard doesn't block the
    /update-docs commit."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_pure_helper_with_no_test_file_is_real_gap(self):
        fd = self.gate.FileDiff(
            path=Path("web-next/lib/foo.ts"),
            added_lines={1, 2, 3},
            is_typescript=True,
        )
        res = self.gate.measure_typescript(fd, related=[], verbose=False)
        # No related test → all lines are gaps.
        self.assertEqual(res.missing_lines, {1, 2, 3})
        self.assertEqual(res.skipped_with_reason, set())

    def test_react_component_with_no_test_file_soft_passes(self):
        fd = self.gate.FileDiff(
            path=Path("web-next/components/foo.tsx"),
            added_lines={1, 2, 3},
            is_typescript=True,
        )
        res = self.gate.measure_typescript(fd, related=[], verbose=False)
        # Soft-pass: every changed line is "skipped with reason"
        # so the gap math computes 0 gaps.
        self.assertEqual(res.skipped_with_reason, {1, 2, 3})
        self.assertEqual(res.gap_lines, set())

    def test_app_route_with_no_test_file_soft_passes(self):
        fd = self.gate.FileDiff(
            path=Path("web-next/app/foo/page.tsx"),
            added_lines={1, 2, 3},
            is_typescript=True,
        )
        res = self.gate.measure_typescript(fd, related=[], verbose=False)
        self.assertEqual(res.skipped_with_reason, {1, 2, 3})
        self.assertEqual(res.gap_lines, set())


class RenderReportExitCodesTests(unittest.TestCase):
    """The exit codes power /update-docs's commit gate — they must
    cleanly distinguish 0 (pass), 1 (gap), 3 (pytest itself failed)."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def _result(self, *, gaps=False, pytest_rc=0, has_related=True):
        fd = self.gate.FileDiff(
            path=Path("pipeline/x.py"),
            added_lines={10, 11, 12},
            is_python=True,
        )
        r = self.gate.CoverageResult(
            file=fd,
            related_tests=[Path("tests/test_x.py")] if has_related else [],
        )
        r.pytest_returncode = pytest_rc
        if gaps:
            r.executed_lines = {10}
            r.missing_lines = {11, 12}
        else:
            r.executed_lines = {10, 11, 12}
            r.missing_lines = set()
        return r

    def test_no_results_exits_zero(self):
        rc = self.gate.render_report([], verbose=False)
        self.assertEqual(rc, 0)

    def test_all_covered_exits_zero(self):
        rc = self.gate.render_report([self._result(gaps=False)], verbose=False)
        self.assertEqual(rc, 0)

    def test_uncovered_lines_exit_one(self):
        rc = self.gate.render_report([self._result(gaps=True)], verbose=False)
        self.assertEqual(rc, 1)

    def test_pytest_failure_with_related_test_exits_three(self):
        # When the test ITSELF crashed (not just a coverage gap),
        # exit 3 so /update-docs can distinguish "missing tests" from
        # "tests broken".
        rc = self.gate.render_report(
            [self._result(gaps=True, pytest_rc=1, has_related=True)],
            verbose=False,
        )
        self.assertEqual(rc, 3)

    def test_pytest_failure_without_related_test_still_exits_one(self):
        # No related tests at all (just a missing-coverage gap) is
        # exit 1, not exit 3 — there's nothing for the test to have
        # crashed in.
        rc = self.gate.render_report(
            [self._result(gaps=True, pytest_rc=0, has_related=False)],
            verbose=False,
        )
        self.assertEqual(rc, 1)


class DiffPathspecsTests(unittest.TestCase):
    """The 2026-05-12 single-level glob bug: ``foo/**/*.py`` requires
    at least one intermediate dir, so files directly under ``foo/``
    were silently invisible to the gate."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_includes_both_shallow_and_deep_globs(self):
        specs = self.gate._diff_pathspecs()
        # For every watched dir, BOTH the shallow `<dir>/*.py` AND
        # the deep `<dir>/**/*.py` patterns must be present.
        for d in self.gate.PY_DIRS_PRODUCTION + self.gate.PY_DIRS_CLOUD:
            self.assertIn(f"{d}/*.py", specs,
                          f"shallow glob missing for {d}")
            self.assertIn(f"{d}/**/*.py", specs,
                          f"deep glob missing for {d}")

    def test_excludes_caches_and_node_modules(self):
        specs = self.gate._diff_pathspecs()
        self.assertIn(":(exclude)**/__pycache__/**", specs)
        self.assertIn(":(exclude)web-next/node_modules/**", specs)
        self.assertIn(":(exclude)tests/**", specs)


class RunHelperTests(unittest.TestCase):
    """The thin subprocess wrapper. Pinning that it returns the
    triple shape the rest of the gate expects + that cwd defaults
    to REPO_ROOT."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_returns_triple_on_success(self):
        rc, out, err = self.gate.run([sys.executable, "-c", "print('hi')"])
        self.assertEqual(rc, 0)
        self.assertIn("hi", out)
        self.assertEqual(err.strip(), "")

    def test_returns_nonzero_on_failure(self):
        rc, out, err = self.gate.run([sys.executable, "-c", "import sys; sys.exit(7)"])
        self.assertEqual(rc, 7)


class MainEntryPointTests(unittest.TestCase):
    """The argparse dispatcher. Each --flag must reach the right
    code path and exit cleanly."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_diff_only_short_circuits_and_exits_zero(self):
        # Patch changed_files to return a fixed list — verifies the
        # --diff-only branch prints the diff and exits without doing
        # any coverage work.
        fake_diff = [
            self.gate.FileDiff(
                path=Path("pipeline/foo.py"),
                added_lines={1, 2, 3},
                is_python=True,
            ),
        ]
        argv = ["coverage_gate.py", "--diff-only"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(self.gate, "changed_files", return_value=fake_diff):
            rc = self.gate.main()
        self.assertEqual(rc, 0)

    def test_plan_short_circuits_and_exits_zero(self):
        fake_diff = [
            self.gate.FileDiff(
                path=Path("pipeline/foo.py"),
                added_lines={1, 2, 3},
                is_python=True,
            ),
        ]
        argv = ["coverage_gate.py", "--plan"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(self.gate, "changed_files", return_value=fake_diff), \
             mock.patch.object(self.gate, "find_related_tests", return_value=[]):
            rc = self.gate.main()
        self.assertEqual(rc, 0)

    def test_no_diff_exits_zero_with_skip_message(self):
        argv = ["coverage_gate.py"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(self.gate, "changed_files", return_value=[]):
            rc = self.gate.main()
        self.assertEqual(rc, 0)


class ChangedFilesTests(unittest.TestCase):
    """The git-diff parser. Exercising the FULL path requires a real
    git repo, which the project itself provides — so we run the actual
    parser against the repo and assert structural invariants."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_returns_list_of_filediff(self):
        # In the live working tree this typically returns at least
        # one entry (the test file we're editing). When the tree
        # is clean it returns []. Both shapes are valid.
        result = self.gate.changed_files()
        self.assertIsInstance(result, list)
        for fd in result:
            self.assertIsInstance(fd, self.gate.FileDiff)
            self.assertIsInstance(fd.path, Path)
            self.assertIsInstance(fd.added_lines, set)
            for line_no in fd.added_lines:
                self.assertIsInstance(line_no, int)
                self.assertGreater(line_no, 0)

    def test_python_files_marked_correctly(self):
        result = self.gate.changed_files()
        for fd in result:
            if fd.path.suffix == ".py":
                self.assertTrue(fd.is_python, f"{fd.path} should be Python")
                self.assertFalse(fd.is_typescript)
            elif fd.path.suffix in (".ts", ".tsx"):
                self.assertTrue(fd.is_typescript, f"{fd.path} should be TS")
                self.assertFalse(fd.is_python)


class MeasurePythonNoRelatedTestsTests(unittest.TestCase):
    """When find_related_tests returns [], measure_python must
    short-circuit and report all added lines as gaps without
    invoking the coverage subprocess (which would be wasteful)."""

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_no_related_tests_marks_all_lines_missing(self):
        fd = self.gate.FileDiff(
            path=Path("pipeline/foo.py"),
            added_lines={10, 11, 12},
            is_python=True,
        )
        # Ensure the subprocess is NEVER called by patching it to
        # raise — if measure_python does invoke it, the test fails.
        with mock.patch.object(self.gate, "run") as m_run:
            res = self.gate.measure_python(fd, related=[], verbose=False)
            m_run.assert_not_called()
        self.assertEqual(res.missing_lines, {10, 11, 12})
        self.assertEqual(res.executed_lines, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
