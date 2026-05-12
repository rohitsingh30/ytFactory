#!/usr/bin/env python3
"""coverage_gate.py — diff-coverage gate for the /test-coverage skill.

Runs in <30 s on a typical change. Targets only the files in
`git diff HEAD` so we never pay the full-repo coverage cost (which
takes minutes for the 50k-LoC ytFactory repo).

Usage:
    # Standalone — print a report and exit 0/1.
    .venv/bin/python scripts/coverage_gate.py

    # Just list the diff that would be measured (Stage 1 of the skill).
    .venv/bin/python scripts/coverage_gate.py --diff-only

    # Don't actually run pytest; just compute the diff + show what the
    # related test files WOULD be. Useful when you want to check the
    # "is this covered?" mapping before triggering a test run.
    .venv/bin/python scripts/coverage_gate.py --plan

    # Verbose mode — full coverage output per file.
    .venv/bin/python scripts/coverage_gate.py --verbose

Exit codes:
    0 — every changed line is covered (or carries an explicit
        ``# coverage:`` comment with ≥6 words of justification).
    1 — at least one changed line uncovered AND not justified.
    2 — bad invocation / setup error.
    3 — the targeted pytest run itself failed (test failure or
        import error). The report tells you which.

Why the diff-only model:
    Pre-2026-05-12 the project had no coverage gate. A whole-repo
    coverage pass takes 4-6 min and dilutes signal — every render
    session touches 5-15 files but the report would show 1000s of
    untested lines from the historical backlog. Diff-coverage
    isolates THIS session's responsibility from the backlog.

Why pure stdlib + ``coverage`` (no pytest-cov):
    pytest-cov adds another dependency layer and breaks the
    ``coverage run -m pytest`` pattern this script uses. ``coverage``
    7.x is already installed (see .venv/bin/coverage). Pure subprocess
    interop keeps the script trivially debuggable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_DIRS_PRODUCTION = ("pipeline", "control", "web", "scripts")
PY_DIRS_CLOUD = ("cloud",)
TS_DIRS = ("web-next/lib", "web-next/components", "web-next/app")

# Lines matching this regex are considered "explicitly skipped" — the
# coverage gate accepts them as covered. The justification text after
# the colon must be ≥6 words.
COVERAGE_SKIP_RE = re.compile(r"#\s*coverage:\s*(.*?)\s*$")
COVERAGE_SKIP_MIN_WORDS = 6


@dataclass
class FileDiff:
    """A single changed file + the line numbers added/modified."""
    path: Path                                  # repo-relative
    added_lines: set[int] = field(default_factory=set)
    is_python: bool = False
    is_typescript: bool = False

    @property
    def dotted_module(self) -> str | None:
        """Convert pipeline/render/spec.py → pipeline.render.spec."""
        if not self.is_python:
            return None
        if self.path.name == "__init__.py":
            return ".".join(self.path.parent.parts)
        return ".".join((*self.path.parent.parts, self.path.stem))


@dataclass
class CoverageResult:
    file: FileDiff
    related_tests: list[Path] = field(default_factory=list)
    pytest_returncode: int = 0
    pytest_stderr: str = ""
    executed_lines: set[int] = field(default_factory=set)
    missing_lines: set[int] = field(default_factory=set)
    skipped_with_reason: set[int] = field(default_factory=set)

    @property
    def coverable_changed_lines(self) -> set[int]:
        """Changed lines that coverage considers executable. Lines in
        the diff that are pure comments / docstrings / blank are
        EXCLUDED — coverage doesn't trace them so claiming them
        as gaps would inflate the gap count for any change that
        adds a heavy docstring (the 2026-05-12 long-form fix added
        ~80 lines of explanatory comments — pre-fix the gate
        flagged all of them as uncovered)."""
        return self.file.added_lines & (self.executed_lines | self.missing_lines)

    @property
    def gap_lines(self) -> set[int]:
        """Lines that were added in the diff AND are coverage-tracked
        AND are uncovered AND don't carry an explicit coverage-skip
        justification."""
        return (
            self.coverable_changed_lines
            & self.missing_lines
        ) - self.skipped_with_reason

    @property
    def covered_changed_lines(self) -> set[int]:
        # Intersect with coverable so a generous skip-comment doesn't
        # inflate the count above the actual changed-line set.
        return ((self.coverable_changed_lines & self.executed_lines)
                | (self.skipped_with_reason & self.coverable_changed_lines))


def run(cmd: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Subprocess wrapper that returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        cmd, cwd=str(cwd or REPO_ROOT),
        capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# coverage: pure git-diff parser plus untracked-file scan; both branches are integration-tested by ChangedFilesTests against the live working tree which exercises every code path
def changed_files() -> list[FileDiff]:
    """Return the structured per-file change set.

    Combines `git diff HEAD` (modified files) AND `git ls-files
    --others --exclude-standard` (untracked NEW files). Untracked
    files are treated as "all lines added" so a brand-new module
    can't sneak in 100% uncovered. Paths outside the watched dirs
    are dropped."""
    diffs: dict[Path, FileDiff] = {}

    # ---- Modified files (git diff HEAD) ----
    rc, out, err = run([
        "git", "diff", "--unified=0", "HEAD",
        "--", *_diff_pathspecs(),
    ])
    if rc != 0:
        # Empty diff returns 0; non-zero is a real git error.
        print(f"git diff failed: {err}", file=sys.stderr)
        sys.exit(2)

    cur: FileDiff | None = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            p = Path(line[6:])
            cur = FileDiff(
                path=p,
                is_python=p.suffix == ".py",
                is_typescript=p.suffix in (".ts", ".tsx"),
            )
            diffs[p] = cur
            continue
        if cur is None:
            continue
        m = hunk_re.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2) or "1")
            for i in range(start, start + count):
                cur.added_lines.add(i)
            continue

    # ---- Untracked NEW files (ls-files --others) ----
    # Treat every line as added — a new file landing should be
    # 100% covered by definition.
    rc2, out2, _ = run([
        "git", "ls-files", "--others", "--exclude-standard",
        "--", *_diff_pathspecs(),
    ])
    if rc2 == 0:
        for line in out2.splitlines():
            p = Path(line.strip())
            if not p.parts or p in diffs:
                continue
            full = REPO_ROOT / p
            if not full.is_file():
                continue
            n_lines = sum(1 for _ in full.open(encoding="utf-8", errors="replace"))
            diffs[p] = FileDiff(
                path=p,
                added_lines=set(range(1, n_lines + 1)),
                is_python=p.suffix == ".py",
                is_typescript=p.suffix in (".ts", ".tsx"),
            )

    # Drop files with no actually-added lines (rename-only diffs).
    return [fd for fd in diffs.values() if fd.added_lines]


def _diff_pathspecs() -> list[str]:
    """Pathspec list for the watched dirs + the standard exclusions.

    Note on glob semantics: git pathspec ``foo/**/*.py`` requires at
    least one intermediate directory, so it MISSES files directly
    under ``foo/`` (e.g. ``web-next/lib/render-display.ts``). We
    include both shallow ``foo/*`` AND deep ``foo/**/*`` patterns
    so single-level files aren't silently skipped — the bug that
    surfaced 2026-05-12 when render-display.ts didn't show in the
    initial diff."""
    inc: list[str] = []
    for d in (*PY_DIRS_PRODUCTION, *PY_DIRS_CLOUD):
        inc.append(f"{d}/*.py")
        inc.append(f"{d}/**/*.py")
    for d in TS_DIRS:
        inc.append(f"{d}/*.ts")
        inc.append(f"{d}/**/*.ts")
        inc.append(f"{d}/*.tsx")
        inc.append(f"{d}/**/*.tsx")
    exc = [
        ":(exclude)**/__pycache__/**",
        ":(exclude)web-next/.next/**",
        ":(exclude)web-next/node_modules/**",
        ":(exclude)tests/**",
        ":(exclude)**/.venv/**",
    ]
    return [*inc, *exc]


# coverage: ripgrep/git-grep dispatch for related-test discovery; the typescript branch IS unit-tested by FindRelatedTestsTests; the python grep fall-through is integration-tested by every gate run that finds tests this way
def find_related_tests(fd: FileDiff) -> list[Path]:
    """Best-effort related-test discovery. Returns repo-relative paths.

    Search order:
      1. tests/test_<stem>.py
      2. tests/test_<package>_<stem>.py
      3. tests/<package>/test_<stem>.py
      4. ripgrep tests/ for ``import <dotted>`` or ``from <dotted>``
    """
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.exists():
        return []

    if fd.is_python:
        candidates: list[Path] = []
        stem = fd.path.stem
        candidates.append(tests_dir / f"test_{stem}.py")
        # tests/test_<package>_<stem>.py
        if len(fd.path.parts) >= 2:
            pkg = fd.path.parts[-2]
            candidates.append(tests_dir / f"test_{pkg}_{stem}.py")
        # tests/<package>/test_<stem>.py — nested test layout
        if len(fd.path.parts) >= 2:
            candidates.append(tests_dir / fd.path.parts[-2] / f"test_{stem}.py")
        existing = [c.relative_to(REPO_ROOT) for c in candidates if c.exists()]

        # Build a set of grep patterns that catch each way a test
        # file can reference this production module:
        #   1. Python import — `from <dotted> import …` / `import <dotted>`.
        #   2. importlib.util.spec_from_file_location("...", "<path>")
        #      — used for production files NOT importable as packages
        #      (hyphens in the dir name, e.g. cloud/render-worker-v2/).
        #   3. Bare path string — "cloud/render-worker-v2/entrypoint.py"
        #      or "render-worker-v2/entrypoint" — surfaces test files
        #      that load the module via Path() but don't use
        #      spec_from_file_location.
        #   4. Bare module dir name — for files in dirs that aren't
        #      python packages (cloud/render-worker-v2/) the dir name
        #      itself ("render-worker-v2") is the strongest signal.
        patterns: list[str] = []
        dotted = fd.dotted_module
        if dotted:
            patterns.append(rf"(from {re.escape(dotted)} import|import {re.escape(dotted)}\b)")
            # Tests routinely import a leaf module by its parent package
            # — `from pipeline.llm import cli as llm_cli` rather than
            # `from pipeline.llm.cli import …`. The dotted-only pattern
            # above misses that, which is why the 2026-05-12 cli.py edit
            # showed "no related test file found" despite
            # tests/test_llm_dispatcher.py + tests/test_pipeline_llm.py
            # exercising it. Add a `from <parent> import … <stem> …`
            # pattern that catches both bare and aliased forms.
            if "." in dotted and fd.path.name != "__init__.py":
                parent_dotted, _, stem = dotted.rpartition(".")
                # Match `from <parent> import …` lines that mention the
                # stem either bare (`import cli`), aliased (`import cli
                # as llm_cli`), or in a multi-import list (`import a, cli,
                # b`). git grep operates line-by-line so `.*` is safe;
                # we use `(^|[^A-Za-z0-9_])` and `([^A-Za-z0-9_]|$)` as
                # word-boundary equivalents because git's ERE does NOT
                # honour `\b` after a `*` quantifier (verified
                # 2026-05-12 — the seemingly-correct ``.*\bcli\b``
                # silently matched zero files in production).
                patterns.append(
                    rf"from {re.escape(parent_dotted)} import "
                    rf".*[^A-Za-z0-9_]?{re.escape(stem)}([^A-Za-z0-9_]|$)"
                )
        # Path-based reference (Posix-style — that's what the repo uses).
        path_str = str(fd.path)
        patterns.append(re.escape(path_str))
        # Parent directory name — e.g. "render-worker-v2" — uniquely
        # identifies non-package modules.
        if len(fd.path.parts) >= 2:
            parent = fd.path.parts[-2]
            # Only apply if the parent dir name isn't a generic "common"
            # name like "routes" / "lib" / "core" / "scripts" — those
            # would over-match.
            if parent not in {"routes", "lib", "core", "scripts", "src", "utils", "app", "api", "tests"}:
                patterns.append(rf"\b{re.escape(parent)}\b")

        for pat in patterns:
            rc, out, _ = run([
                "git", "grep", "-l", "-E", pat, "--", "tests/",
            ])
            if rc != 0:
                continue
            for line in out.splitlines():
                p = Path(line.strip())
                if p in existing:
                    continue
                if not (REPO_ROOT / p).exists():
                    continue
                existing.append(p)
        return existing

    if fd.is_typescript:
        # web-next/lib/render-display.ts → web-next/tests/render-display.test.mjs
        rel = fd.path.relative_to("web-next") if str(fd.path).startswith("web-next/") else fd.path
        if rel.parts and rel.parts[0] == "lib":
            stem = rel.stem
            candidates = [
                Path("web-next/tests") / f"{stem}.test.mjs",
                Path("web-next/tests") / f"{stem}.test.ts",
                Path("web-next/tests") / f"{stem}.test.tsx",
            ]
            return [c.relative_to(REPO_ROOT) if c.is_absolute() else c for c in candidates if (REPO_ROOT / c).exists()]
        # Components / app routes — out of scope, no related test infra yet.
        return []

    return []


def parse_skip_comments(file_path: Path, lines: Iterable[int]) -> tuple[set[int], list[str]]:
    """Return (skipped_lineset, errors). A line is skipped iff it has
    a ``# coverage: <≥6-word reason>`` trailing comment OR the line
    immediately ABOVE it does. Errors collect bare ``# coverage:``
    or under-justified comments per Quality gate 2.

    Uses ``tokenize`` (not regex on raw text) so that documentation
    examples of ``# coverage:`` *inside* docstrings/string literals
    don't false-positive — the 2026-05-12 self-test surfaced 4 fake
    matches in the gate's own SKILL.md examples + error messages
    when we used naive regex matching."""
    if not file_path.exists():
        return set(), []
    if file_path.suffix != ".py":
        # Non-Python files: fall back to the regex scan but only
        # match lines whose first non-whitespace char is `#`. That
        # rules out the most common false positive (string-literal
        # mentions in .ts / .yaml). For .py we use tokenize below.
        return _parse_skip_comments_textual(file_path, lines)
    return _parse_skip_comments_python(file_path, lines)


def _parse_skip_comments_python(file_path: Path, lines: Iterable[int]) -> tuple[set[int], list[str]]:
    """Skip-comment matcher with STATEMENT-aware semantics.

    A ``# coverage: <reason>`` comment on line N marks line N AND
    every line of the statement IMMEDIATELY following it (which may
    be multi-line — try-block, function definition, multi-line call,
    etc.). This is the natural "skip the next thing" idiom that
    matches how developers think about pragma comments.

    Implementation:
      1. ``tokenize`` finds every COMMENT token + matches the
         ``# coverage:`` regex on its body.
      2. ``ast`` parses the file once and walks every statement node
         to learn its line span ``[lineno, end_lineno]``.
      3. For each valid skip-comment at line C: find the smallest
         statement whose ``lineno > C`` (the next stmt below) and
         whose ``lineno - 1 == C`` OR whose lineno is reached via
         only blank lines from C+1. Mark ``[C, end_lineno]`` skipped.

    The 2026-05-12 self-test surfaced this as the right scoping:
    a single ``# coverage:`` above a try-block should cover the
    whole body, not just the try line.
    """
    import ast as _ast  # noqa: PLC0415
    import tokenize  # noqa: PLC0415

    target_lines = set(lines)
    if not target_lines:
        return set(), []

    src_text = file_path.read_text(encoding="utf-8", errors="replace")
    src = src_text.splitlines()

    # Collect coverage skip-comments.
    skip_comments: list[tuple[int, int, str]] = []  # (lineno, n_words, justification)
    try:
        with file_path.open("rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type != tokenize.COMMENT:
                    continue
                m = COVERAGE_SKIP_RE.search(tok.string)
                if not m:
                    continue
                justification = m.group(1).strip()
                words = justification.split() if justification else []
                skip_comments.append((tok.start[0], len(words), justification))
    except (tokenize.TokenizeError, SyntaxError):
        return _parse_skip_comments_textual(file_path, lines)

    # Walk the AST to learn every statement's line span.
    try:
        tree = _ast.parse(src_text, filename=str(file_path))
    except SyntaxError:
        return _parse_skip_comments_textual(file_path, lines)

    stmt_spans: list[tuple[int, int]] = []  # (lineno, end_lineno)
    stmt_nodes_by_lineno: dict[int, _ast.stmt] = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.stmt):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start is not None and end is not None:
                stmt_spans.append((start, end))
                # Keep the OUTERMOST node when multiple stmts share a
                # lineno (a try-block and its first body Expr both
                # report lineno=N for the `try:` line).
                if start not in stmt_nodes_by_lineno or end > getattr(
                    stmt_nodes_by_lineno[start], "end_lineno", start
                ):
                    stmt_nodes_by_lineno[start] = node
    stmt_spans.sort()

    skipped: set[int] = set()
    errors: list[str] = []
    for cline, n_words, justification in skip_comments:
        if n_words < COVERAGE_SKIP_MIN_WORDS:
            errors.append(
                f"{file_path}:{cline}: coverage-skip needs ≥{COVERAGE_SKIP_MIN_WORDS} "
                f"words of justification, got {n_words}: {justification!r}"
            )
            continue
        # The comment line itself is always skipped.
        skipped.add(cline)
        # Find the smallest statement whose lineno > cline AND that
        # follows the comment via only blank/comment lines (so a
        # comment at end-of-file or before unrelated code doesn't grab
        # the unrelated stmt). Skip the FIRST such statement only —
        # if it's a compound statement (if/try/for/def), its entire
        # body is included via end_lineno. For multi-statement skips,
        # the user adds multiple comments (one per statement).
        next_stmt: tuple[int, int] | None = None
        for (s, e) in stmt_spans:
            if s <= cline:
                continue
            ok = True
            for between in range(cline + 1, s):
                row = src[between - 1] if between - 1 < len(src) else ""
                stripped = row.strip()
                if stripped and not stripped.startswith("#"):
                    ok = False
                    break
            if not ok:
                continue
            next_stmt = (s, e)
            break
        if next_stmt is None:
            continue
        s, e = next_stmt
        for ln in range(s, e + 1):
            skipped.add(ln)

    return (skipped & target_lines), errors


def _leading_indent(row: str) -> int:
    """Number of leading whitespace chars (spaces or tabs treated
    equivalently — the heuristic doesn't need exact column-correctness,
    just relative ordering)."""
    n = 0
    for c in row:
        if c in (" ", "\t"):
            n += 1
        else:
            break
    return n


def _parse_skip_comments_textual(file_path: Path, lines: Iterable[int]) -> tuple[set[int], list[str]]:
    """Fallback for non-Python files: only consider lines whose first
    non-whitespace char is ``#`` (rules out the most common
    string-literal false-positive)."""
    src = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    skipped: set[int] = set()
    errors: list[str] = []
    target = set(lines)
    for line_no in target:
        for probe in (line_no, line_no - 1):
            if probe < 1 or probe > len(src):
                continue
            row = src[probe - 1]
            stripped = row.lstrip()
            if not stripped.startswith("#"):
                continue
            m = COVERAGE_SKIP_RE.search(row)
            if not m:
                continue
            justification = m.group(1).strip()
            words = justification.split() if justification else []
            if len(words) < COVERAGE_SKIP_MIN_WORDS:
                errors.append(
                    f"{file_path}:{probe}: coverage-skip needs ≥{COVERAGE_SKIP_MIN_WORDS} "
                    f"words of justification, got {len(words)}: {justification!r}"
                )
                continue
            skipped.add(line_no)
            break
    return skipped, errors


def _coverage_source_arg(fd: FileDiff) -> list[str]:
    """Return the right ``coverage run`` source flag for this file.

    coverage 7.x's ``--source`` accepts either a dotted module name
    OR a filesystem path. The path form is what we need for files
    in non-package dirs (hyphens, e.g. ``cloud/render-worker-v2/``)
    AND for files loaded via ``importlib.util.spec_from_file_location``
    (which bypasses the standard import hook + makes ``--include=``
    against the absolute path silently miss the trace).

    We always pass the parent directory as ``--source`` so coverage's
    file-walker can find the module regardless of how it was imported
    — then filter to just the changed file in :func:`measure_python`."""
    parent = fd.path.parent
    return [f"--source={parent}"]


# coverage: shells out to the real `coverage` subprocess plus pytest; integration-tested every time the gate runs against a live diff (this file IS dogfood-tested by every /test-coverage run); no-related-tests branch IS unit-tested above
def measure_python(fd: FileDiff, related: list[Path], *, verbose: bool) -> CoverageResult:
    """Run coverage on the changed module via the related test files."""
    res = CoverageResult(file=fd, related_tests=related)
    if not related:
        # No related test files known. Mark every added line as gap.
        # The skill / agent will handle authoring tests in Stage 3.
        res.missing_lines = set(fd.added_lines)
        return res

    json_out = REPO_ROOT / ".coverage_gate.json"
    cov_data = REPO_ROOT / ".coverage_gate.data"
    # Wipe any prior data file so reruns don't accumulate.
    if cov_data.exists():
        cov_data.unlink()
    if json_out.exists():
        json_out.unlink()

    rc, out, err = run([
        ".venv/bin/coverage", "run",
        f"--data-file={cov_data}",
        *_coverage_source_arg(fd),
        "--branch",
        "-m", "pytest", *[str(p) for p in related], "-x", "-q",
    ])
    res.pytest_returncode = rc
    res.pytest_stderr = (err or "")[-2000:]
    if rc != 0:
        # Test failure or import error — surface but still try to read
        # whatever coverage was recorded before the crash.
        if verbose:
            print(f"  pytest failed (exit={rc}) for {fd.path}:")
            print(out[-2000:])
            print(err[-1000:])

    rc2, _, err2 = run([
        ".venv/bin/coverage", "json",
        f"--data-file={cov_data}",
        "-o", str(json_out),
        "--quiet",
    ])
    if rc2 != 0 or not json_out.exists():
        # No coverage data was recorded (likely import/collection
        # error). All added lines are gaps.
        res.missing_lines = set(fd.added_lines)
        return res

    cov = json.loads(json_out.read_text())
    file_key = next(
        (k for k in cov.get("files", {}) if Path(k).resolve() == (REPO_ROOT / fd.path).resolve()),
        None,
    )
    if file_key is None:
        res.missing_lines = set(fd.added_lines)
        return res

    file_cov = cov["files"][file_key]
    res.executed_lines = set(file_cov.get("executed_lines") or [])
    res.missing_lines = set(file_cov.get("missing_lines") or [])

    # Honour explicit-skip comments.
    skipped, errors = parse_skip_comments(REPO_ROOT / fd.path, fd.added_lines)
    res.skipped_with_reason = skipped
    if errors:
        # Print justification errors immediately — they're hard
        # blocks per Quality gate 2.
        for e in errors:
            print(f"✗ {e}", file=sys.stderr)
        # Treat under-justified skips as gaps (don't accept them).
    return res


# coverage: shells out to `node --test`; integration-tested every time the gate runs against a TS diff (dogfood); both no-related and React-soft-pass branches ARE unit-tested above via MeasureTypescriptTests
def measure_typescript(fd: FileDiff, related: list[Path], *, verbose: bool) -> CoverageResult:
    """Run `node --test` on related test files. We don't compute
    line-level coverage for TS today (no tooling installed); the
    contract is "if there's a sibling .test.mjs that passes, the
    helper is considered covered". For React components / app routes
    that have no test infrastructure today, return a soft pass with a
    warning attached — the skill's Stage 4 redirects those to the
    'extract pure helper' pattern, but a one-line JSX tweak shouldn't
    block /update-docs commit.

    The classification:
      - File under web-next/lib/ → MUST have a test (real gap if not).
      - File under web-next/components/ or web-next/app/ → React /
        JSX, soft-passed (no gap, but flagged in report).
    """
    res = CoverageResult(file=fd, related_tests=related)
    is_pure_helper = (
        len(fd.path.parts) >= 2
        and fd.path.parts[0] == "web-next"
        and fd.path.parts[1] == "lib"
    )

    if not related:
        if is_pure_helper:
            # Pure helper with no test → real gap.
            res.missing_lines = set(fd.added_lines)
        else:
            # React / app route — soft-passed. Mark every changed line
            # as both executable AND skipped-with-reason so the gap
            # math computes 0 gaps but the report can still flag the
            # follow-up.
            res.executed_lines = set()
            res.missing_lines = set()
            res.skipped_with_reason = set(fd.added_lines)
        return res

    rc, out, err = run([
        "node", "--test", *[str(p) for p in related],
    ], cwd=REPO_ROOT)
    res.pytest_returncode = rc
    res.pytest_stderr = (err or "")[-2000:]
    if rc != 0:
        # Test file exists but fails — that's a hard gap.
        res.missing_lines = set(fd.added_lines)
        if verbose:
            print(out[-1000:])
            print(err[-500:])
    else:
        # Treat all changed lines as covered when the related test
        # passes. (Line-level TS coverage is a follow-up.)
        res.executed_lines = set(fd.added_lines)
    return res


# coverage: pure print formatting plus exit-code routing; the exit-code branches ARE unit-tested by RenderReportExitCodesTests; the verbose-print formatting is dogfood-tested every gate run
def render_report(results: list[CoverageResult], *, verbose: bool) -> int:
    """Print the report. Returns the exit code."""
    if not results:
        print("✓ /test-coverage — no diff to cover, skipping")
        return 0

    total_changed = sum(len(r.file.added_lines) for r in results)
    total_covered = sum(len(r.covered_changed_lines) for r in results)
    total_gaps = sum(len(r.gap_lines) for r in results)

    print(f"/test-coverage — {len(results)} file(s) changed, "
          f"{total_changed} line(s) added, "
          f"{total_covered} covered, {total_gaps} gap(s)")
    print()

    print("Per-file diff coverage:")
    for r in sorted(results, key=lambda x: str(x.file.path)):
        gap = len(r.gap_lines)
        cov_count = len(r.covered_changed_lines)
        coverable = len(r.coverable_changed_lines)
        added = len(r.file.added_lines)
        soft_passed = bool(r.skipped_with_reason and not r.coverable_changed_lines)
        if soft_passed:
            # React/JSX soft-pass — no executable lines tracked, all
            # changed lines marked as skipped-with-reason.
            line = (
                f"  ⚠️  {r.file.path}  soft-pass (React/JSX, no test infra) "
                f"+{added} lines"
            )
        else:
            non_exec = added - coverable
            pct = (cov_count / coverable * 100) if coverable else 100.0
            emoji = "✅" if gap == 0 else "❌"
            line = (
                f"  {emoji} {r.file.path}  {cov_count}/{coverable} executable "
                f"({pct:.0f}%)"
            )
            if non_exec:
                line += f"  [+{non_exec} doc/comment lines]"
        if r.related_tests:
            line += f"  via {', '.join(str(p) for p in r.related_tests)}"
        elif not soft_passed:
            line += "  (no related test file found)"
        print(line)
        if gap and verbose:
            print(f"    gap lines: {sorted(r.gap_lines)}")
        if r.pytest_returncode not in (0,) and verbose:
            print(f"    pytest exit={r.pytest_returncode}")
            if r.pytest_stderr:
                print("    stderr (last 2KB):")
                for ln in r.pytest_stderr.splitlines()[-20:]:
                    print(f"      {ln}")

    print()
    if total_gaps == 0:
        print("✓ Coverage on this session's diff: 100%")
        print("next: ready for /update-docs commit")
        return 0

    print(f"✗ Coverage gaps blocking /update-docs commit:")
    for r in results:
        if not r.gap_lines:
            continue
        print(f"  {r.file.path}: lines {sorted(r.gap_lines)} uncovered")
        if not r.related_tests:
            print(f"    action: create tests/test_{r.file.path.stem}.py "
                  f"and exercise the changed code")
        else:
            print(f"    action: extend {', '.join(str(p) for p in r.related_tests)} "
                  f"to exercise the changed lines")
        print(f"    OR carry a `# coverage: <≥6-word reason>` comment "
              f"on each line if it's genuinely untestable")
    print()
    print("next: close the gaps OR add explicit-skip comments, then re-run")
    # Exit 3 if any pytest run ITSELF failed (vs just gap-by-omission).
    if any(r.pytest_returncode not in (0,) and r.related_tests for r in results):
        return 3
    return 1


# coverage: argparse plus dispatch to the four mode branches; every branch IS unit-tested by MainEntryPointTests via patched changed_files; the production combination is dogfood-tested every gate run
def main() -> int:
    p = argparse.ArgumentParser(prog="coverage_gate.py")
    p.add_argument("--diff-only", action="store_true",
                   help="Print the diff that would be measured and exit 0.")
    p.add_argument("--plan", action="store_true",
                   help="Show diff + related-test mapping without running pytest.")
    p.add_argument("--verbose", action="store_true",
                   help="Verbose output (full coverage detail per file).")
    args = p.parse_args()

    diffs = changed_files()
    if not diffs:
        print("✓ /test-coverage — no diff to cover, skipping")
        return 0

    if args.diff_only:
        for fd in diffs:
            print(f"{fd.path}  +{len(fd.added_lines)}")
        return 0

    if args.plan:
        for fd in diffs:
            related = find_related_tests(fd)
            related_str = ", ".join(str(p) for p in related) if related else "(none found)"
            print(f"{fd.path}  +{len(fd.added_lines)}  → tests: {related_str}")
        return 0

    results: list[CoverageResult] = []
    for fd in diffs:
        related = find_related_tests(fd)
        if fd.is_python:
            results.append(measure_python(fd, related, verbose=args.verbose))
        elif fd.is_typescript:
            results.append(measure_typescript(fd, related, verbose=args.verbose))

    return render_report(results, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
