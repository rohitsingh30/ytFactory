# /make-hindutava-long — learnings index

Created 2026-05-07 via /make-skill. Empty on day 1.

Each regression caught by /critique-video on a rendered hindutava-long
mp4 gets classified ONE-OFF (this script) vs CLASS-OF-BUG (every
future render). CLASS-OF-BUG findings ship a fix to the right pipeline
module + a regression note appended here AND mirrored to
`hindutavaanimated/learnings/<topic>.md` per CLAUDE.md dual-save.

## Topic files

<!-- appended over time as regressions land -->

## First-run watch-list (anticipated, not yet hit)

- Character drift across 60+ panels — cast.json appearance_lock
  propagation must be airtight; Shorts had this bug at smaller
  panel counts too.
- Chapter-pacing miss — one chapter feels rushed/dragged. Tighten
  the §6.5 chapter-structure gate (min/max paragraph-count per
  chapter) on second occurrence.
- Centerpiece undermargined — viewer tunes out at the iconic
  moment. Verify each centerpiece has ≥10 paragraph words +
  `shot_size: extreme_close|wide`.
- Whisper-mlx-4bit bunching ~25 words at one timestamp on long
  Hindi audio (the 2026-05-07 krishna-govardhan-dharan render
  bug). The `pipeline/beats.py:save_beats` forward-progress
  monotonicity guard catches this; verify it's still active on
  long-form.
- Hindi noun-glossary miss (पर्वत-as-tree, उँगली-as-finger
  ambiguity, सिंदूर-as-blood, नदी-as-road) — surfaced in the
  /critique-video run on the Shorts render but glossary file
  not yet shipped. Long-form will hit the same class.
