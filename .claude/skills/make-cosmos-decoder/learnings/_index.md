# /make-cosmos-decoder — learnings index

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

---

<!-- entries below — newest first -->

(empty — skill created 2026-05-05)
