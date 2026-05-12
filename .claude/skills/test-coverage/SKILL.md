---
name: test-coverage
description: After every code change in pipeline/ control/ web/ web-next/ cloud/* or scripts/ — measure coverage on the diff, flag uncovered lines, generate tests for them, and block the next /update-docs commit if non-trivial gaps remain. Locks the project at 100% coverage on NEW changes from this point forward (the existing 50k-LoC backlog is incremental). Use after touching any production .py / .ts / .tsx file, after fixing any bug, before any /update-docs commit, or when the user says "test this", "check coverage", "did we miss anything", "make sure this is tested". Auto-invoked.
---

# /test-coverage — pin every code change with tests

> **Cross-cutting skill — not a niche producer.** Reads existing code,
> existing tests, the git diff, and either pins changes with new tests
> or fails loudly. Does not author channel content.

You are wearing the **Verifier** hat: every line of code that landed
this session must either (a) be exercised by a test that would fail
on the buggy state OR (b) carry an explicit `# coverage: explained
why this is untestable` comment.

## Context — why this skill exists

Established 2026-05-12 after a single render session surfaced
**three** bugs (long-form cascade-coercion, preview_url status-window
gate, hard-coded 9:16 PlayerCard) that all could have been caught by
tests pinning the changed lines. The prior workflow was "agent edits
code → /update-docs commits → user runs the render → bug surfaces".
This skill closes the loop by inserting **"agent edits code → COVER
the changed lines → /update-docs commits"**.

**Realistic scope.** The whole repo is ~50k LoC across pipeline/,
control/, web/, web-next/, cloud/*, scripts/. A single-session
"100% across everything" sweep is multi-week. This skill enforces
**100% coverage on lines changed THIS session** (the diff against
`HEAD`). The historical backlog is tracked separately — see
`learnings/backlog.md`.

## When to run it

**Auto-invoke** (Claude should run this without being asked):

1. Immediately after any tool call that modifies a file under:
   - `pipeline/` (any `.py`)
   - `control/` (any `.py`)
   - `web/` (any `.py`)
   - `web-next/` (any `.ts` / `.tsx` outside `node_modules` / `.next`)
   - `cloud/*/` (any `.py` server / entrypoint)
   - `scripts/` (any `.py`)
2. After `/update-docs` writes a "CODE:" line into a commit.
3. Before any `git commit` that includes production code (NOT
   docs-only commits).
4. On explicit ask: "test this", "check coverage", "did we miss
   anything", "make sure this is tested".

**Don't auto-invoke** for:
- Edits to `tests/` only (the tests themselves run; coverage measures
  the production code they hit, but the trigger is on production
  edits).
- Edits to `docs/`, `.claude/`, `*.md`, channel content (`<channel>/`
  scripts / narrations / etc.).
- Edits to `.gitignore`, lockfiles (`package-lock.json`, `uv.lock`,
  etc.), or generated files.

If you're unsure whether an edit qualifies, err on running — the
skill exits in <5s with `no diff to cover — skipping` when the
working tree has no production-code changes.

## How to run it

The whole flow is wrapped by `scripts/coverage_gate.py`. The skill
runs the script, reads its output, and only opens the more
expensive "author missing tests" branch if the script reports a
gap. Use the script standalone (`.venv/bin/python
scripts/coverage_gate.py`) for any non-agent workflow.

### Stage 1 — Detect what changed

```bash
.venv/bin/python scripts/coverage_gate.py --diff-only
```

Internally this runs:

```bash
git diff --name-only HEAD -- \
  'pipeline/**/*.py' 'control/**/*.py' 'web/**/*.py' \
  'web-next/**/*.ts' 'web-next/**/*.tsx' \
  'cloud/**/*.py' 'scripts/**/*.py' \
  ':(exclude)**/__pycache__/**' \
  ':(exclude)web-next/.next/**' \
  ':(exclude)web-next/node_modules/**' \
  ':(exclude)tests/**'
```

If the list is empty → exit 0 with `no diff to cover — skipping`.

### Stage 2 — For each changed Python file

For each `<path>/<module>.py`:

1. **Find related test files.** Search order:
   - `tests/test_<module>.py`
   - `tests/test_<package>_<module>.py`
   - `tests/test_<package>/<module>.py`
   - Grep `tests/` for any `import <package>.<module>` or `from
     <package> import <module>`.

2. **Run coverage** scoped to the module (not the whole repo —
   that takes minutes):
   ```bash
   .venv/bin/coverage run \
     --source="<dotted.module.path>" \
     --branch \
     -m pytest <list of related test files> -x -q
   .venv/bin/coverage report --show-missing
   .venv/bin/coverage json -o /tmp/cov_<module>.json
   ```

3. **Compute diff-coverage.** Cross-reference the `git diff`'s
   added/modified line numbers against the `coverage.json`
   `executed_lines` and `missing_lines`. Any added/modified line
   in `missing_lines` is a gap.

4. **Decide:**
   - All changed lines covered → ✅ continue.
   - Uncovered changed lines → enter Stage 3 (gap closer).

### Stage 3 — Close the gap

For each uncovered changed line, **author a test** that would have
failed on the pre-change state:

1. Read the surrounding function. Identify its public contract
   (inputs, outputs, side effects).
2. If the function is **pure** (no I/O, no Firestore, no subprocess) →
   write a unit test that calls it with representative inputs.
3. If the function is **I/O-bound but mockable** (Firestore writes,
   GCS uploads, gcloud invocations, LLM calls) → write a test that
   patches the boundary (`unittest.mock.patch`) and asserts the
   right call shape + behavioural outcome.
4. If the function is **genuinely untestable in isolation** (real
   GPU inference, real network to a paid API, browser DOM) → add
   an inline `# coverage: <one-sentence why>` comment on the
   uncovered lines AND open an entry in `learnings/explicit-skips.md`
   for visibility. This is the explicit escape hatch.

Re-run Stage 2 to confirm the gap is closed.

### Stage 4 — For each changed TypeScript file

`web-next/` has no React testing infrastructure today (no jest /
vitest installed). The pragmatic rule:

1. **If the file is a pure helper** (`web-next/lib/*.ts`, no JSX,
   no React hooks):
   - Expect a sibling test at `web-next/tests/<helper>.test.mjs`
     using Node's built-in `node --test` runner (zero deps,
     Node 20+).
   - Run: `cd web-next && node --test tests/<helper>.test.mjs`
   - If absent, write one.

2. **If the file is a React component** (`web-next/app/**/*.tsx`,
   `web-next/components/**/*.tsx`):
   - **Extract pure logic into a helper** in `web-next/lib/` and
     pin THAT (the 2026-05-12 PlayerCard pattern: aspect/kind
     derivation moved to `lib/render-display.ts` and tested
     standalone).
   - The remaining JSX presentation layer is out of scope until
     vitest + @testing-library/react ships (see
     `learnings/frontend-test-infra.md` for the standing TODO).
   - Type-check is the minimum gate: `cd web-next && npx tsc
     --noEmit`. Run it on every component edit.

3. **If the file is a route / API binding** (`web-next/app/api/**`):
   - Test via the Python control-plane integration tests
     (`tests/test_*_routes.py`) — the API contract is owned by
     the backend.

### Stage 5 — Run the targeted suite + report

After the per-file passes, run:

```bash
.venv/bin/python -m pytest <every test file touched in Stage 3> -q
```

Plus:
```bash
cd web-next && npx tsc --noEmit
cd web-next && node --test tests/**/*.test.mjs    # if any
```

Print a compact report:

```
✓ /test-coverage — N production files, M test files

Per-file diff coverage:
  pipeline/render/spec.py                       12/12 (100%)
  cloud/render-worker-v2/entrypoint.py          45/45 (100%)
  web-next/lib/render-display.ts                 8/8  (100%)

Tests added this run:
  tests/test_cloudrun_render_worker_progress.py::LfAdvanceTimelineTests (6 cases)
  tests/test_jobs_id_fallthrough.py::test_status_uploading_without_short_uri_no_preview
  web-next/tests/render-display.test.mjs (12 cases)

Tests run:
  pytest tests/test_render_routes.py tests/test_*progress.py — 64 passed
  npx tsc --noEmit                                            — clean
  node --test web-next/tests/                                 — 12/12 passed

Coverage on this session's diff: 100%
Backlog (untouched): tracked in learnings/backlog.md (~38% repo-wide)

next: ready for /update-docs commit
```

If gaps remain (Stage 3 couldn't close them OR the user explicitly
said "skip coverage on this"), the report ENDS with:

```
✗ /test-coverage — coverage gaps blocking /update-docs commit

  pipeline/foo.py: lines 142-156 uncovered (changed this session)
    why: depends on real Cloud Run dispatch, no mock surface yet
    action: add a mock at pipeline/foo.py:_dispatch_cloud_run_job,
            OR carry a `# coverage: explained` comment with the
            justification

next: close the gaps OR add explicit-skip comments, then re-run
```

`/update-docs` should refuse to commit while gaps remain (Stage 8
of `/update-docs` runs `/test-coverage` first if any CODE: line
is in the diff).

## Quality gates (mechanical, run BEFORE printing the summary)

1. **Diff-coverage = 100%** on every changed line that doesn't carry
   an explicit `# coverage:` comment. Block on hit.
2. **No `# coverage:` comment without justification.** A bare
   `# coverage:` line is a TODO not an explanation. Require at
   minimum a 6-word reason.
3. **No new tests with `pass` body** OR `assert True`. Tests must
   make at least one assertion against the production code's
   behaviour.
4. **No tests that assert the BUG.** Spot-check each new test: would
   it have failed on the pre-change state? (Same rule as
   `/update-docs` Quality gate 10 fix-claim verifier — a test that
   asserts `assertEqual(status, "broken")` when the bug was sending
   "broken" green-lights the bug instead of catching it.)
5. **Test files use existing pytest conventions** —
   `class FooTests(unittest.TestCase)` or pytest-style top-level
   `test_*` functions; no new framework imports unless the user
   explicitly approves.
6. **TypeScript helpers tested via `node --test`** — no new jest /
   vitest dependency added without the user's say-so. If a React
   component genuinely needs DOM testing, surface that as a
   follow-up in `learnings/frontend-test-infra.md` instead of
   silently `npm install`ing a test framework.
7. **No flaky tests.** If a test depends on time, network, or
   filesystem layout, mock it. Sleep-based tests are forbidden
   (use `freezegun` if temporal; mock the clock if timing).
8. **Run only the relevant suite.** Never `pytest tests/` (the full
   155-file suite takes too long). Run `pytest <changed-related
   test files>` only. If you genuinely need broader confidence,
   explicitly call out a "broader sweep recommended" in the report
   and let the user decide.

## What this skill DOES NOT do

- It does NOT retroactively cover the existing 50k-LoC backlog. The
  historical untested code is logged in `learnings/backlog.md` for
  incremental work; full-repo 100% is a multi-week project tracked
  separately.
- It does NOT install new test frameworks without approval.
- It does NOT block on coverage of `# coverage:` lines once an
  explicit justification is present.
- It does NOT touch channel content (`<channel>/scripts/`,
  `<channel>/narrations/`, etc.) — those have their own
  per-channel critique skills.

## Self-learning hook

If the user later corrects how `/test-coverage` decided what to
test ("you should have mocked X, not Y", "this test asserts the
buggy behaviour"):

1. Classify the meta-bug:
   - ONE-OFF (single bad test) → fix the test, no skill change.
   - CLASS-OF-BUG (the heuristic is wrong) → update Stage 3 of
     this SKILL.md AND append to
     `learnings/<topic>.md`.
2. Mirror to
   `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_test_coverage_<topic>.md`
   per the dual-save rule.
3. If the same meta-bug fires twice, escalate to a hard quality gate
   in section "Quality gates" above.

## Reference

- `scripts/coverage_gate.py` — the executable implementation; the
  skill is the user-facing wrapper around it. Run standalone with
  `.venv/bin/python scripts/coverage_gate.py`.
- `learnings/backlog.md` — historical untested-code list.
- `learnings/explicit-skips.md` — registry of every `# coverage:`
  justification, so a periodic review can confirm they're still
  valid.
- `learnings/frontend-test-infra.md` — standing TODO for adding
  vitest + @testing-library/react so React components can be
  pinned. Currently only pure helpers are testable.
- `docs/post_upload_analysis.md` — the parent analysis taxonomy
  this skill plugs into (its findings often surface PIPELINE-BUG
  or CLASS-OF-BUG entries that need pinning).
