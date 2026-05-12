# /make-skill — learnings index

This file is the running log of regressions and improvements for the
meta-skill itself. Every time `/make-skill` produces a skill that
later misbehaves, append a one-line entry below pointing at:

- the regression (what the generated skill missed)
- the heuristic # that should have caught it
- the fix (added a heuristic / tightened wording / changed a default)

Format: `YYYY-MM-DD — <generated-skill> — heuristic #X — <fix>`

Heuristics live in `heuristics.md` (the 51-item canonical list).

---

<!-- regressions appended below -->

- 2026-05-12 — 10 skills (image-edit, make-cosmos-long,
  make-cosmos-short, make-football-explainer, make-hindutava-long,
  make-history-short, make-tweet-reaction, parallel-render,
  tune-ai-extraction, voice-bench) — heuristics 30b + 30c — failed
  to load at session start. 6 overflowed 1024-char cap; 4 had
  unquoted `key: value` patterns in description that broke YAML
  parsing (3 overlap). Fix: trimmed overflows + switched 4 to
  folded block scalar (`description: >-`). New heuristic 30c
  added; mechanical lint gate at `scripts/lint_skill_md.py`
  (parses + cap-checks every SKILL.md). See heuristics.md
  regression log for the full table.
