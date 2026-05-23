---
name: Uniform 1080x1349 inner box for 9:16 stills — cover-crop instead of fit-with-padding
description: pipeline/cosmos_footage_prep.py::_still_to_video used to fit-with-padding so portraits filled ~70% of frame height while landscapes filled ~32%. Photo-to-photo size jumps felt jarring (LIGO short v4 user crit "when barry photo came it was shown large, it should be consistent"). Fix: cover-crop every 9:16 source to a fixed 1080x1349 (4:5) inner box, then letterbox to 1080x1920 outer. Trade-off: landscapes lose left/right edges to the crop.
type: feedback
---

# Uniform 1080x1349 inner box for 9:16 stills

**Rule:** every 9:16 still produced by
`pipeline/cosmos_footage_prep.py::_still_to_video` cover-crops to a fixed
**1080x1349 (4:5)** inner box, then letterboxes to 1080x1920 outer.
Eliminates the visible-image-height inconsistency between portrait
and landscape sources.

**Why:** LIGO Short v3 had:
- Hanford aerial (1920x1080, aspect 1.78): visible image 1080x607 = 32% of frame height
- BBH simulation (800x616, aspect 1.30): visible image 1080x832 = 43% of frame height
- Barry Barish (1947x2434, aspect 0.80): visible image 1080x1349 = **70% of frame height**

User crit: *"when barry photo came it was shown large, it should be
consistent width and height of the actual image we are going to keep"*.

**How to apply (shipped 2026-05-08):**

In `pipeline/cosmos_footage_prep.py::_still_to_video`, the 9:16 path now
uses cover-crop to a uniform 1080x1349 inner box:

```python
if aspect == "9:16":
    inner_w, inner_h = 1080, 1349  # 4:5 — Instagram portrait standard
    vf = (
        f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=increase,"
        f"crop={inner_w}:{inner_h},"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,format=yuv420p"
    )
else:
    # 16:9 long-form: keep existing letterbox-fit (sources predominantly landscape)
```

**Trade-off:** landscape sources lose left/right edges to the crop.
For LIGO Hanford aerial, the L-arm extending into the desert is
partially cropped — center detail (the actual L-shape detector) is
preserved. For BBH simulation, the orange ripples extending outward
are partially cropped — the central two pearls + main spacetime
distortion stay visible.

**When to revisit:** if a future Short relies on edge content of a
landscape source (e.g. a wide-angle photo where the subject is in
the corner), the cover-crop will hide it. The fix in that case is to
either (a) pre-crop the source so the subject is centered, or (b)
revert to fit-with-padding for that specific window via a shotlist
override field (not yet implemented; raise an issue if needed).

**Why 4:5 specifically:** matches Instagram portrait standard,
balances "feels like a portrait" vs. "leaves enough room for
captions". 1080x1080 (square) felt too tight on landscapes;
1080x1620 (5:6) felt too tall on portraits. 1080x1349 is the
empirical sweet spot from the LIGO render iteration.

**Channel applicability:**
- 9:16 Shorts (cosmosdecoded, historyrecapped, sportsrecapped,
  hindutavaanimated): NEW behavior active
- 16:9 long-form: unchanged (long-form sources predominantly
  landscape so visible-area inconsistency doesn't surface)

**Project doc:** `cosmosdecoded/learnings/uniform_inner_box_1080x1349.md`.

The fix lives in shared pipeline code, so every channel's Shorts get
the new behavior automatically — no per-channel SKILL.md edit needed.
