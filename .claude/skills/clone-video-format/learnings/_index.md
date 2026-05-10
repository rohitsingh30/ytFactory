# /clone-video-format — learnings index

This file is the running log of regressions and improvements for the
clone-video-format skill. Every time the skill produces a fingerprint
that turns out to be wrong (the generated /make-* skill produces a
video that doesn't match the source), append a one-line entry below
pointing at:

- the regression (which fingerprint field was wrong / missing)
- the source URL that triggered it
- the classification (ONE-OFF / CLASS-OF-BUG)
- the fix (corrected the schema in SKILL.md step 5 / added a quality
  gate / etc.)

Format: `YYYY-MM-DD — <source-url-domain>/<id> — <field> —
<ONE-OFF|CLASS-OF-BUG> — <fix>`

Class-of-bug regressions also need a topic file in this directory
AND a mirror under
`~/.claude/projects/-Users-rohit-ytFactory/memory/` per CLAUDE.md
dual-save rule.

---

<!-- regressions appended below -->
- 2026-05-08 — youtu.be/BQkYsINy95k — `--download-sections` overwrite — CLASS-OF-BUG — SKILL.md step 1+2 updated to default whole-file ≤15min; topic file [`yt_dlp_section_overwrite.md`](yt_dlp_section_overwrite.md); memory mirror `feedback_yt_dlp_section_overwrite.md`.
- 2026-05-08 — youtu.be/BQkYsINy95k — `python -m mlx_whisper.transcribe` silent-exit — CLASS-OF-BUG — SKILL.md step 4 updated to use Python API; topic file [`mlx_whisper_cli_python_api.md`](mlx_whisper_cli_python_api.md); cross-channel doc `docs/whisper_mlx_cli_bug.md`; memory mirror `feedback_mlx_whisper_cli_silent_exit.md`.
- 2026-05-08 — youtu.be/BQkYsINy95k — fingerprint.json alone insufficient for downstream /make-skill — WORKFLOW-IMPROVEMENT — SKILL.md step 5b added (8K-12K word ANALYSIS.md alongside fingerprint).
