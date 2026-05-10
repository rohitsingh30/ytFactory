---
name: Scrub source watermarks from broadcast cut-in footage
description: The real-broadcast cut-in (the channel's USP) must have any source-channel watermark, scoreboard graphic overlay, or rights logo programmatically scrubbed before it lands in the render.
type: feedback
---
The real-broadcast cut-in is this channel's whole reason to exist (see
`footage_is_usp.md`). But the broadcast clips we pull — almost always
re-uploaded by third-party highlight channels — arrive with a station
watermark or uploader logo burned in. Scrub it before render.

**Why:** A visible third-party logo on the payoff shot shouts "re-upload"
to the viewer at the exact moment we want them most invested. It also
materially increases the ContentID-match risk on the one shot we cannot
afford to have claimed/blocked. Decided cross-channel 2026-05-04 — same
rule applies on History Recapped.

**How to apply:**
- Add the scrub pass between download and the existing
  `footage_blurred_letterbox.md` 16:9→9:16 step in `pipeline/footage.py`.
- Per-source bbox table in `pipeline/footage.py` keyed by URL/host —
  most highlight channels reuse the same logo position across their
  library, so static-bbox `ffmpeg -vf delogo` is enough.
- Watch out for animated scoreboard overlays at the start of the clip —
  if `footage_window_must_include_buildup.md` is honored properly, the
  scoreboard usually drops away before our `in_s`. If it lingers into
  the cut, either narrow the window or add a tracked-ROI inpaint.
- Validate with `/critique-video` after the first scrubbed render —
  delogo residue is more visible against the grass/crowd background of
  a sports broadcast than against the muted tones of war archival.
