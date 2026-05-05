# /make-top10 — learnings index

Append-only log of regressions and improvements for this skill.
Format: `YYYY-MM-DD — <slug> — heuristic # — <what fired> — <fix>`

The skill itself is at `../SKILL.md`. Class-of-bug fixes belong in
`pipeline/<module>.py` or the SKILL.md prompt, not here. This file
holds one-off notes + pointers to class-of-bug topic files.

---

<!-- regressions appended below -->

- 2026-05-05 — `top10-alien-abductions-202605` — G11/G15/G17 (NEW) —
  shipped in single-source wallpaper mode: 100 audio/video mismatches,
  third-party watermarks visible, no rank cards, no captions, 14% of
  runtime is a static frame —
  fix: gates G11-G17 added to SKILL.md §7; ban contract at
  `wallpaper_mode_ban.md`; renderer `caption_mode` default for 16:9
  flipped from `"none"` → `"long_form"` in
  `scripts/historyrecapped/render_footage_only.py`.
