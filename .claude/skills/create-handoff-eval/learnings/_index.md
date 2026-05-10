# /create-handoff-eval — learnings index

One-liner per finding from prior runs. Detail in
`<topic>.md` siblings.

## 2026-05-08 — skill authored

- Reverse-engineered from `docs/eval_handoff_2026_05_08.md`
  (MyStoriesAnimated TIFU 4-pack). 8-section structure, ~150-line
  target, channel-specific evaluation criteria.

## 2026-05-08 — output path migrated to contract spec

- Output path moved from `docs/eval_handoff_<date>_<channel>.md` to
  `/Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch-slug>.md` per
  `/Users/rohit/evals/AGENT_CONTRACT.md`. Skill now uses
  `pipeline.evals.write_handoff()` which auto-suffixes `-vN` on
  collision and registers each batch slug in `<project>/STATUS.md`
  via `ensure_status_row()`. Reverse-direction skill is
  `/ingest-critiques` (reads critiques back).
