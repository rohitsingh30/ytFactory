---
name: Footage cut-in is the SportsStoriesAnimated USP — not optional
description: For sports shorts, the real-broadcast footage cut at the climactic moment is the entire reason the channel exists; static-cartoon-only renders are not shippable
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
User watched the v1 Aguero 93:20 short (animation-only, no footage) and explicitly flagged: "I don't see the actual footage stitched in between the short, that is the usp." The footage cut is not a v2 nice-to-have — it's the differentiator that makes this channel worth doing.

**Why:** Tifo Football's editorial style is well-trodden; what makes our version different is that we DO show the actual moment ("real life footage of the moment in edit"). Without that, the short is just "Tifo with a slightly different palette," and the user doesn't see the point.

**How to apply:**
- Treat any sports short rendered without at least one `kind: footage` beat as INCOMPLETE — never present such a render as a finished short.
- Required pipeline pieces (when shipping is the goal): `pipeline/footage.py` exists; `compose.py` must dispatch on `beat["kind"]` and stitch the mp4 in place of the static image; `make_shorts.py` must preserve `kind` + `footage:{url,in_s,out_s,audio_mix}` from the script JSON through alignment.
- For demo/iteration on aesthetics only (style, character, palette), animation-only is acceptable as a temp render — but it's not the deliverable.
- Bootstrap path: the user (or wiki research) provides a YouTube URL + frame-accurate in_s / out_s for each footage beat. Auto-finding the moment in a long video is v2.