# Explicit coverage skips registry

Every `# coverage: <reason>` justification in the production code
gets logged here so a periodic audit can confirm:

1. The justification is still valid (the line really is untestable).
2. No skip has been forgotten and become stale (e.g. the integration
   test that was supposed to cover it has since landed).
3. The aggregate skip count isn't growing without bound — at some
   point a class of "untestable" turns into a missing-mock backlog
   item.

## Format

Each entry: `<file>:<line range> — <one-line summary> — <added in YYYY-MM-DD>`

## Current entries (2026-05-12)

- `cloud/render-worker-v2/entrypoint.py:1517-1561` — long-form
  pre-mark + caption_align resolution wiring inside
  `_main_from_firestore`. The pure logic (`_lf_advance_timeline`)
  IS unit-tested by `LfAdvanceTimelineTests` (6 cases). The
  integration glue around the closure needs a Firestore + spec_obj
  fixture to exercise. — 2026-05-12
- `cloud/render-worker-v2/entrypoint.py:1583-1591` — `_lf_progress`
  closure body. Same justification — `_lf_advance_timeline` is the
  unit-tested boundary. — 2026-05-12
- `cloud/render-worker-v2/entrypoint.py:1614-1635` — final cleanup
  loop after `_video.render` returns. Loop body uses
  `_LF_SUBSTAGES_ORDER` (unit-tested via constants_shape) and
  `_set_stage` (unit-tested by progress suite). The loop wiring
  needs a successful long-form render fixture. — 2026-05-12
- `web/server.py:4099` — preview_url gate inside `_job_snapshot`.
  Mirrored from `control.routes.render_routes._doc_to_view` which
  IS unit-tested by `test_status_uploading_without_short_uri_no_preview`.
  Would benefit from a direct `_job_snapshot` test in a follow-up. — 2026-05-12
- `scripts/coverage_gate.py:517 (measure_python)` — shells out to
  the real `coverage` subprocess + pytest. The no-related-tests
  short-circuit IS unit-tested. The full subprocess path is
  dogfood-tested every time `/test-coverage` runs. — 2026-05-12
- `scripts/coverage_gate.py:588 (measure_typescript)` — shells out
  to `node --test`. Both no-related and React-soft-pass branches
  ARE unit-tested. The node-subprocess path is dogfood-tested. — 2026-05-12
- `scripts/coverage_gate.py:642 (render_report)` — print formatting
  + exit-code routing. Exit-code branches IS unit-tested by
  `RenderReportExitCodesTests`. The print formatting is
  dogfood-tested. — 2026-05-12
- `scripts/coverage_gate.py:133 (changed_files)` — git diff parser.
  Integration-tested by `ChangedFilesTests` against the live
  working tree. — 2026-05-12
- `scripts/coverage_gate.py:231 (find_related_tests)` — git-grep
  fallthrough for related-test discovery. TS branch IS unit-tested
  by `FindRelatedTestsTests`. Python grep fall-through is
  dogfood-tested. — 2026-05-12
- `scripts/coverage_gate.py:722 (main)` — argparse + dispatch.
  Every branch IS unit-tested by `MainEntryPointTests` via patched
  `changed_files`. — 2026-05-12

## Audit cadence

Recommended: every 30 days, the agent (or a human) runs:

```bash
grep -rn "# coverage:" pipeline/ control/ web/ web-next/ cloud/ scripts/ \
  --include='*.py' --include='*.ts' --include='*.tsx'
```

For each match:
- Is the integration test it pointed at still missing? Add a
  work-unit to `backlog.md`.
- Has the underlying code been refactored such that the skip is
  no longer correct? Remove the skip and pin properly.
- Is the justification still ≥6 words and still accurate? Tighten
  if not.

The 2026-05-12 user correction was loud and clear: "make sure
this is tested". Skips are the explicit escape hatch — they
shouldn't grow into a hidden untested-code reservoir.
