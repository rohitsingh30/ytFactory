---
name: aita_cooking channel format
description: New visual format — Reddit-text overlay on cooking-video background. Constraints user flagged about source quality.
type: project
originSessionId: ab2936df-9d5e-427a-971e-5bc41f6b4817
---
The `aita_cooking` channel renders an AITA-style Reddit story as text overlay on top of a silent cooking-video background loop (the format common on TikTok/Shorts).

User-flagged constraints on source backgrounds (raised 2026-04-29 after first pull):

- **Aspect ratio**: blindly center-cropping a 16:9 cooking video to 9:16 cuts out too much. Need either vertical-native footage, or pan-and-scan over time, or smarter framing per scene.
- **Pace**: slow ASMR cooking footage (e.g. fire-cooking 1796 historic recipes) is too slow for a Short. Want fast-action cooking — chopping, sizzling pans, fast pours, knife work.

**Why:** these are aesthetic + retention concerns, not just engineering. The whole point of the cooking background is to provide *visual motion* under static Reddit text. A slow, awkwardly-cropped background defeats the purpose.

**How to apply:** when extending `pull_backgrounds.py` or picking source URLs, prefer (a) vertical-native sources (TikTok rips, vertical Shorts), (b) fast-action search terms ("fast chopping", "knife skills", "wok hei", "fast cooking compilation") over "asmr no talking" which trends slow, and (c) consider per-clip pan/zoom in compose rather than static center-crop.

**Status (2026-04-29):** v0 backgrounds were pulled with center-crop and slow ASMR source — known suboptimal. User wants to render an actual short first, then critique, then iterate on background quality.
