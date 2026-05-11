"""Hard quality gates for the critique-runner agent loop (2026-05-11).

The agent claims its fix is done. The runner does NOT trust that
claim — it re-runs every gate independently before pushing to main.

Gates (in dependency order, fail-fast):

1. ``ensure_diff_exists`` — git diff against HEAD must show ≥ 1
   modified .py / .yaml / .json file. Empty diff = agent did
   nothing useful → reject.

2. ``ensure_tests_added`` — ≥ 1 file under ``tests/`` was added or
   modified by the agent. "Added appropriate tests for the change"
   was a hard rule the user set when we agreed direct-to-main.

3. ``run_pytest_full`` — the existing pytest suite (no slow markers
   added) must pass. Catches regressions the agent didn't notice.

4. ``run_diff_coverage`` — every executable line the agent ADDED in
   non-test files must be covered by the test run. We don't have
   ``diff-cover`` installed; we parse ``coverage json`` ourselves
   against ``git diff --unified=0``. 100% diff-coverage is the bar.

Gate results stream into the chat as ``action="gate_running"`` →
``action="gate_passed"`` / ``action="gate_failed"`` so the user
can watch the verification happen in real time.

Design + state machine in
[`docs/critique_chat.md`](../../docs/critique_chat.md).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """One gate's outcome.

    ``passed`` is the only field a caller acts on; everything else is
    debug / chat-display surface. ``msg`` is short (≤ 200 chars) and
    safe to render as a chat chip; ``detail`` may be multi-line.
    """
    name: str
    passed: bool
    msg: str
    duration_s: float = 0.0
    detail: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class GateRunReport:
    """Full report for a single agent turn's gate sweep."""
    all_passed: bool
    results: list[GateResult]
    total_duration_s: float

    @property
    def first_failure(self) -> GateResult | None:
        for r in self.results:
            if not r.passed:
                return r
        return None


# ---------------------------------------------------------------------------
# Individual gates
# ---------------------------------------------------------------------------


_DIFF_FILE_EXTS = (".py", ".yaml", ".yml", ".json", ".md", ".tsx", ".ts", ".js", ".css", ".html")
_TEST_PATH_PREFIXES = ("tests/", "test_")


def _run_git(args: list[str], *, cwd: Path) -> str:
    """Wrap ``git`` with a clean error path. Returns stdout (str).

    ``check=True``: gates depend on git being healthy; any
    git-internal failure should fail the gate immediately rather
    than be swallowed and produce a misleading "no diff" result.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _diff_file_list(repo_root: Path, *, base: str = "HEAD", staged: bool = True) -> list[str]:
    """Return the list of files changed since ``base`` (default HEAD).

    Defaults to ``staged=True`` so the gate inspects ``git diff
    --cached HEAD``: this matches what the critique runner will be
    looking at right before commit (it ``git add -A``s the agent's
    work — including brand-new untracked files — before running
    gates). Tests that don't stage their changes must pass
    ``staged=False`` explicitly.

    Only files matching :data:`_DIFF_FILE_EXTS` are returned — keeps
    binary blobs (mp4 / png / wav) out of the gate set so a stray
    artifact doesn't trip "diff exists" or "tests added".
    """
    args = ["diff", "--name-only"]
    if staged:
        args.append("--cached")
    args.append(base)
    out = _run_git(args, cwd=repo_root)
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip() and any(line.strip().endswith(ext) for ext in _DIFF_FILE_EXTS)
    ]


def ensure_diff_exists(repo_root: Path, *, base: str = "HEAD", staged: bool = True) -> GateResult:
    """Gate 1: agent must have produced at least one file change."""
    t0 = time.time()
    files = _diff_file_list(repo_root, base=base, staged=staged)
    elapsed = time.time() - t0
    if not files:
        return GateResult(
            name="diff_exists",
            passed=False,
            msg="agent produced no file changes",
            duration_s=elapsed,
            detail=(
                "git diff --name-only "
                f"{'--cached ' if staged else ''}HEAD returned no .py/.yaml/.json/.md/.ts(x) "
                "files. The agent claimed `done` but committed nothing — refuse the push."
            ),
        )
    return GateResult(
        name="diff_exists",
        passed=True,
        msg=f"{len(files)} file(s) changed",
        duration_s=elapsed,
        metadata={"files": files},
    )


def ensure_tests_added(repo_root: Path, *, base: str = "HEAD", staged: bool = True) -> GateResult:
    """Gate 2: at least one file under ``tests/`` must be added or modified.

    Checks ``--diff-filter=AM`` (added or modified) so a delete of a
    test file alone doesn't satisfy the gate. The user explicitly
    required test coverage for every fix when they agreed
    direct-to-main.
    """
    t0 = time.time()
    args = ["diff", "--name-only", "--diff-filter=AM"]
    if staged:
        args.append("--cached")
    args.append(base)
    out = _run_git(args, cwd=repo_root)
    files = [line.strip() for line in out.splitlines() if line.strip()]
    test_files = [
        f for f in files
        if (
            f.startswith(_TEST_PATH_PREFIXES[0])
            or Path(f).name.startswith(_TEST_PATH_PREFIXES[1])
        )
        and f.endswith(".py")
    ]
    elapsed = time.time() - t0
    if not test_files:
        return GateResult(
            name="tests_added",
            passed=False,
            msg="no tests added or modified",
            duration_s=elapsed,
            detail=(
                "Direct-to-main is gated on `add appropriate tests for the change`. "
                f"git diff --diff-filter=AM {'--cached ' if staged else ''}HEAD shows "
                "no tests/ files were touched."
            ),
        )
    return GateResult(
        name="tests_added",
        passed=True,
        msg=f"{len(test_files)} test file(s) touched",
        duration_s=elapsed,
        metadata={"test_files": test_files},
    )


# Pytest output we care about for chat display:
#   ============= 245 passed, 2 skipped in 9.11s =============   (default)
#                 245 passed, 2 skipped in 9.11s                 (with -q --no-header)
# Equals-frame is optional so the parser works in both modes.
_PYTEST_SUMMARY_RE = re.compile(
    r"=*\s*(?P<summary>(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?)(?:,\s*)?)+)\s+in\s+[\d.]+s\s*=*"
)


def run_pytest_full(
    repo_root: Path,
    *,
    extra_args: list[str] | None = None,
    timeout_s: int = 600,
    coverage_data_file: str | None = None,
) -> GateResult:
    """Gate 3: full pytest suite must pass (exit 0).

    ``coverage_data_file`` opt-in: when set, runs pytest under
    ``coverage run --data-file=<path>`` so :func:`run_diff_coverage`
    has fresh data without needing a second test pass.

    Note we don't add ``-x`` (fail-fast) — we want the FULL summary
    so the chat chip can show "5 failed, 240 passed" instead of
    "1 failed, then stopped" which understates the regression
    surface.
    """
    t0 = time.time()
    cmd: list[str]
    if coverage_data_file:
        cmd = [
            "coverage", "run",
            f"--data-file={coverage_data_file}",
            "-m", "pytest", "-q", "--no-header",
        ]
    else:
        cmd = ["python", "-m", "pytest", "-q", "--no-header"]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            name="pytest_full",
            passed=False,
            msg=f"pytest timed out after {timeout_s}s",
            duration_s=time.time() - t0,
            detail="Suite ran longer than the gate timeout. Tighten or skip slow tests.",
        )

    elapsed = time.time() - t0
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    summary_match = _PYTEST_SUMMARY_RE.search(out)
    summary = summary_match.group("summary").strip() if summary_match else "(unparsed)"

    if proc.returncode != 0:
        # Extract the FAILED-test names so the chat chip is actionable.
        failed_re = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
        failures = failed_re.findall(out)[:10]
        detail_lines = [f"pytest exit={proc.returncode}", f"summary: {summary}"]
        if failures:
            detail_lines.append("first failures:")
            detail_lines.extend(f"  - {f}" for f in failures)
        return GateResult(
            name="pytest_full",
            passed=False,
            msg=f"pytest failed: {summary}",
            duration_s=elapsed,
            detail="\n".join(detail_lines),
            metadata={"failures": failures, "summary": summary},
        )

    return GateResult(
        name="pytest_full",
        passed=True,
        msg=f"pytest green: {summary}",
        duration_s=elapsed,
        metadata={"summary": summary},
    )


# ---------------------------------------------------------------------------
# Diff coverage parser
# ---------------------------------------------------------------------------


# Hunk header in unified diff:  @@ -<old_start>,<old_count> +<new_start>,<new_count> @@
_HUNK_RE = re.compile(r"^@@\s+-(?P<oldstart>\d+)(?:,\d+)?\s+\+(?P<newstart>\d+)(?:,(?P<newcount>\d+))?\s+@@")


def _added_lines_for_file(repo_root: Path, file_path: str, *, base: str = "HEAD", staged: bool = True) -> list[int]:
    """Parse ``git diff -U0 [--cached] <base> -- <file>`` to extract the
    new line numbers of every ADDED line in the new revision.

    Returns line numbers (1-based) in the ``HEAD``-after-changes view.
    Only "+" lines that aren't headers count; "-" lines and context
    are ignored.

    -U0 (zero context) is critical — with default 3-line context the
    parser would treat unchanged neighbouring lines as added.
    """
    args = ["diff", "-U0"]
    if staged:
        args.append("--cached")
    args += [base, "--", file_path]
    out = _run_git(args, cwd=repo_root)
    added: list[int] = []
    new_line_no: int | None = None
    for raw in out.splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            new_line_no = int(m.group("newstart"))
            continue
        if new_line_no is None:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            # File-header markers — not a real "+" addition.
            continue
        if raw.startswith("+"):
            added.append(new_line_no)
            new_line_no += 1
        elif raw.startswith("-"):
            # Removed lines don't advance the new-file cursor.
            continue
        elif raw.startswith(" "):
            # Context line (rare with -U0 but possible at hunk edges).
            new_line_no += 1
        # Other prefixes ("\\ No newline at end of file" etc.) are ignored.
    return added


def _is_test_file(rel_path: str) -> bool:
    return rel_path.startswith(_TEST_PATH_PREFIXES[0]) or Path(rel_path).name.startswith(_TEST_PATH_PREFIXES[1])


def run_diff_coverage(
    repo_root: Path,
    *,
    coverage_data_file: str,
    base: str = "HEAD",
    staged: bool = True,
) -> GateResult:
    """Gate 4: every added line in non-test .py files must be covered.

    Reads the coverage data produced by :func:`run_pytest_full` and
    cross-references each added line against the per-file
    ``executed_lines`` set. Missed lines → gate fails with the
    exact (file, line) pairs in ``detail`` so the agent's next turn
    has a precise to-do list.
    """
    t0 = time.time()
    # Convert .coverage data file to JSON via the coverage CLI; this
    # avoids depending on coverage's Python API (whose internals shift
    # between minor versions).
    json_path = Path(coverage_data_file).with_suffix(".gate.json")
    proc = subprocess.run(
        ["coverage", "json",
         f"--data-file={coverage_data_file}",
         "-o", str(json_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return GateResult(
            name="diff_coverage",
            passed=False,
            msg="coverage json export failed",
            duration_s=time.time() - t0,
            detail=(proc.stderr or "")[:1000],
        )

    try:
        cov = json.loads(json_path.read_text())
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="diff_coverage",
            passed=False,
            msg=f"coverage json parse failed: {exc}",
            duration_s=time.time() - t0,
        )

    files_info = cov.get("files", {})
    # ``files_info`` keys are repo-relative (e.g. "pipeline/x.py").

    diff_files = [
        f for f in _diff_file_list(repo_root, base=base, staged=staged)
        if f.endswith(".py") and not _is_test_file(f)
    ]

    missed: list[tuple[str, int]] = []
    covered_lines = 0
    total_lines = 0
    for f in diff_files:
        added = _added_lines_for_file(repo_root, f, base=base, staged=staged)
        if not added:
            continue
        info = files_info.get(f) or {}
        executed = set(info.get("executed_lines", []) or [])
        excluded = set(info.get("excluded_lines", []) or [])
        missing = set(info.get("missing_lines", []) or [])
        # Coverage tracks only EXECUTABLE source lines. Anything not in
        # (executed ∪ missing ∪ excluded) is blank / comment / docstring /
        # decorator continuation — skip from the denominator. Without
        # this filter every blank line the agent inserts would count as
        # an uncovered "added" line.
        executable = executed | missing | excluded
        for ln in added:
            if ln in excluded:
                # Marked `# pragma: no cover` — exclude from the gate.
                continue
            if executable and ln not in executable:
                # Non-executable line (blank / comment) — not coverable.
                continue
            total_lines += 1
            if ln in executed:
                covered_lines += 1
            else:
                missed.append((f, ln))

    elapsed = time.time() - t0

    if total_lines == 0:
        # Edge case: agent only changed comments/docstrings/blank lines.
        # ensure_diff_exists already passed, so something WAS touched —
        # nothing executable, nothing to cover. Pass with a note.
        return GateResult(
            name="diff_coverage",
            passed=True,
            msg="no executable lines added (docs/comments only)",
            duration_s=elapsed,
        )

    if missed:
        first = missed[:25]
        detail_lines = [
            f"{covered_lines}/{total_lines} executable lines covered "
            f"({100.0 * covered_lines / total_lines:.1f}%)",
            f"{len(missed)} missed line(s):",
        ]
        detail_lines.extend(f"  - {f}:{ln}" for f, ln in first)
        if len(missed) > len(first):
            detail_lines.append(f"  … and {len(missed) - len(first)} more")
        return GateResult(
            name="diff_coverage",
            passed=False,
            msg=f"diff coverage {covered_lines}/{total_lines} (missed {len(missed)})",
            duration_s=elapsed,
            detail="\n".join(detail_lines),
            metadata={"missed": missed, "covered": covered_lines, "total": total_lines},
        )

    return GateResult(
        name="diff_coverage",
        passed=True,
        msg=f"100% diff coverage ({covered_lines}/{total_lines} lines)",
        duration_s=elapsed,
        metadata={"covered": covered_lines, "total": total_lines},
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_all_gates(
    repo_root: Path,
    *,
    base: str = "HEAD",
    staged: bool = True,
    pytest_extra_args: Iterable[str] | None = None,
    pytest_timeout_s: int = 600,
    coverage_data_file: str | None = None,
) -> GateRunReport:
    """Run every gate in order; stop after the first failure.

    Fail-fast keeps the chat snappy: there's no point running a
    20-minute pytest if the agent committed zero files. The
    ``coverage_data_file`` is auto-named under ``repo_root/.coverage.gate``
    if not supplied so we don't conflict with the user's manual
    coverage runs.

    ``staged`` (default ``True``): the runner stages the agent's
    work via ``git add -A`` BEFORE invoking us, so we inspect
    ``--cached`` diffs. Tests can pass ``staged=False`` to operate
    on working-tree changes when that's convenient.
    """
    if coverage_data_file is None:
        coverage_data_file = str(repo_root / ".coverage.gate")

    started = time.time()
    results: list[GateResult] = []

    # Gate 1: diff exists.
    g = ensure_diff_exists(repo_root, base=base, staged=staged)
    results.append(g)
    if not g.passed:
        return GateRunReport(False, results, time.time() - started)

    # Gate 2: tests added.
    g = ensure_tests_added(repo_root, base=base, staged=staged)
    results.append(g)
    if not g.passed:
        return GateRunReport(False, results, time.time() - started)

    # Gate 3: full pytest under coverage.
    g = run_pytest_full(
        repo_root,
        extra_args=list(pytest_extra_args) if pytest_extra_args else None,
        timeout_s=pytest_timeout_s,
        coverage_data_file=coverage_data_file,
    )
    results.append(g)
    if not g.passed:
        return GateRunReport(False, results, time.time() - started)

    # Gate 4: diff coverage.
    g = run_diff_coverage(
        repo_root,
        coverage_data_file=coverage_data_file,
        base=base,
        staged=staged,
    )
    results.append(g)

    return GateRunReport(
        all_passed=all(r.passed for r in results),
        results=results,
        total_duration_s=time.time() - started,
    )
