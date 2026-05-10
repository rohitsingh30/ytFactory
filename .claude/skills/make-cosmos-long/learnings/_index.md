# /make-cosmos-long — learnings index

Append-only log of regressions caught by `/critique-audio` or
`/critique-video` on outputs of this skill, classified ONE-OFF vs
CLASS-OF-BUG per CLAUDE.md and skill Section 9.

Format: `YYYY-MM-DD — slug — type — what fired — fix location`

Types:
- `ONE-OFF` — fix in the failing JSON, no pipeline/SKILL.md change
- `CLASS-OF-BUG` — fix in pipeline code or SKILL.md prompt; mirror to
  cosmosdecoded/learnings/<topic>.md

When a CLASS-OF-BUG fires twice, escalate: add a Section 6 pre-render
quality gate that blocks emit on detection.

This skill was split out from the retired `/make-cosmos-decoder` on
2026-05-08; learnings prior to that date were authored against the
paired-output skill but apply equally to long-form-only outputs.

---

<!-- entries below — newest first -->

2026-05-07 — eht-2019-m87 — CLASS-OF-BUG — shotlist Wikimedia URL guesses ~60% miss rate (35/56 MANUAL on long-form) — fix in [shotlist_url_guess_miss_rate.md](shotlist_url_guess_miss_rate.md); mirrored to cosmosdecoded/learnings/. Inherited from retired /make-cosmos-decoder on skill split (2026-05-08).
