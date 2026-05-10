---
name: Scrub source watermarks from all archival footage (Shorts + long-form)
description: Every archival clip used in History Recapped — Shorts AND long-form sleep — must have its source-channel watermark/logo/lower-third programmatically removed before render.
type: feedback
---
For BOTH the Shorts pipeline and the long-form sleep pipeline, every
archival clip pulled from archive.org / DroneScapes / CriticalPast /
Periscope Film / any other source must have its source-channel watermark,
station bug, lower-third, or rights logo programmatically scrubbed before
it lands in the rendered output.

**Why:** A visible third-party watermark (a) signals to the YouTube viewer
that we're a re-uploader, killing trust + retention; (b) increases ContentID
match risk on long-form runs (overlay = derivative-work signal); (c)
cheapens the channel grid when one watermarked tile sits next to clean
ones. Decided 2026-05-04 — applies retroactively going forward.

**How to apply:**
- Add the scrub pass between download and the existing letterbox/fps-unify
  step in `pipeline/footage.py` (Shorts) and the long-form footage stitcher
  used by `historyrecapped/scripts/render_footage_only.py` /
  `historyrecapped/scripts/build_100footage.sh`.
- Detection: per-source bbox table (DroneScapes / CriticalPast / Periscope
  Film logos sit in the same corner across their entire library) → ffmpeg
  `-vf delogo=x=…:y=…:w=…:h=…` is the cheap default. For animated /
  moving overlays fall back to a tracked-ROI inpaint pass.
- Per-source bbox table belongs alongside the existing letterbox + `-r 30`
  config in `pipeline/footage.py`, keyed by source URL/host.
- Validate with `/critique-video` at full density on the first scrubbed
  render — delogo residue often leaves a soft rectangle the eye notices
  but a coarse pass misses.
- Long-form: this is in addition to the existing rule that footage MUST
  come from archive.org PD entries (see `long_form_sources.md`) — PD
  doesn't waive the scrub requirement, since most PD reuploaders still
  burn their channel logo on top.
