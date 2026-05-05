---
name: wallpaper-mode-ban
description: Single-source YouTube compilation as visual wallpaper is BANNED for /make-top10 renders — postmortem 2026-05-05.
type: feedback
---

# Wallpaper-mode ban (2026-05-05 postmortem)

## What happened

`top10-alien-abductions-202605` was authored with a 50-window per-rank
shotlist (the /make-top10 spec). At render time, the implementation
collapsed to **one** YouTube source URL (a 1h25m compilation called
"12 Alien Abduction Encounters Compilation" by the channel "Weird
World") and sliced 12 windows from that single source. The narration
ran independently. Result: 27-min video where:

- Source channel watermark "Credit: Strange But True S03E09" was
  visible on-screen at 10 different timestamps (left-edge text).
- Second watermark "UNEXPLAINED" appeared at 2 timestamps.
- Google Earth UI + "Apollo" overlay + a black censorship bar (the
  source's own copyright workaround) were visible at multiple points.
- 0 rank cards across 10 ranks — mute viewers had no idea when one
  case ended and the next began.
- 0 captions — `caption_mode` defaulted to `"none"` for 16:9.
- 228 of 1618 seconds (14% of runtime) was a single static frame —
  the source's static end-card thumbnail held through the entire #2
  Walton climax + #1 Hill rank + closer.
- At t=18:00, source compilation b-roll showed a small child in 8mm
  vintage home movie footage during alien-abduction narration.
- Roughly 0% of frames depicted the witness, location, era, or
  specific incident being narrated at that moment.

100 specific audio/video mismatches catalogued at
`data/critiques/top10-alien-abductions-202605-100-mismatches.md`.

User score: 1/10. Unshippable.

## Why: How to apply

**Rule**: NEVER use `shotlist["source_url"]` (top-level, single-source)
for /make-top10 renders. EVERY window MUST have its own `source_url`.

**Why**: The /make-top10 skill authors a per-rank visual storytelling
plan (witness portraits, document scans, location stock, archival
newsreels). Collapsing that plan to one compilation source destroys
the storytelling AND inherits the source's watermarks, censorship
artefacts, and end-card. Wallpaper mode is a shortcut that fails
every viewer test.

**How to apply**:

- Skill gates G11–G17 (added 2026-05-05) enforce per-window URLs +
  approved-domain checks + author-time URL verification + watermark
  scan + rank-card overlays + caption mandate. A wallpaper-mode
  shotlist cannot pass these gates.
- When sourcing per-rank URLs:
  - PREFER archive.org PD newsreel items (Universal Newsreel, USAF
    Project Blue Book file scans, gov.archives.arc.* IDs)
  - PREFER commons.wikimedia.org File: pages for witness portraits
    + scanned documents
  - PREFER Pexels / Pixabay CC0 for environmental b-roll (highways,
    forests, lakes, rural farmhouses)
  - REJECT all YouTube compilation / paranormal-content / re-uploader
    channels (Weird World, Strange But True, Unexplained, MrBallen
    reuploads, any channel that itself trims third-party
    documentary footage)
  - REJECT YouTube channels even if the SOURCE is PD-eligible — the
    re-uploader's overlays/watermarks/censorship bars are baked into
    every frame
- When in doubt, FAIL the skill rather than ship wallpaper mode. A
  Top-10 with 5 well-sourced ranks is shippable; one with 10 ranks
  of generic compilation b-roll is not.

## Recovery path (for the broken render)

For the `top10-alien-abductions-202605` slug specifically:

1. The narration WAV in cache is fine (Kokoro/am_michael, 1618s).
2. Discard the wallpaper-mode shotlist and re-author per-rank with
   verified URLs from the approved-domain list.
3. Re-run `render_footage_only.py --aspect 16:9` — TTS will be
   reused from cache (~30s saved), only the visuals + caption +
   rank-card passes need to run fresh.
