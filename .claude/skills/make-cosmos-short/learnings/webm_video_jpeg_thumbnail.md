---
name: Wikimedia .webm video URLs return JPEG thumbnails through pipeline/cosmos_footage_prep.py
description: When a shotlist entry sets `source_type: video` against a `commons.wikimedia.org/wiki/File:foo.webm` URL, the Wikimedia API responds with an `image/jpeg` Content-Type — apparently auto-thumbnailing the video. The prep tool then RuntimeErrors with "expected video, got Content-Type 'image/jpeg'". Fix: switch to a still source (search Wikimedia for an SXS/numerical-relativity-simulation JPEG) OR teach the prep tool to accept JPEG-thumbnails as still-frames-of-a-video.
type: feedback
---

# Wikimedia .webm video URLs return JPEG thumbnails through cosmos_footage_prep.py

**Rule:** when authoring a Cosmos Decoded shotlist, do NOT set
`source_type: video` against a `commons.wikimedia.org/wiki/File:foo.webm`
URL. Use a still image of the same content with `source_type: still_ken_burns`,
or wait for the prep tool to be fixed (Section 3 below).

**Why:** 2026-05-08 first run on `ligo-2015-gw150914-short` had beat 4
pointing at `File:BBH_gravitational_lensing_of_gw150914.webm` with
`source_type: video`. The Wikimedia API responded with the file's
auto-generated JPEG thumbnail (Content-Type `image/jpeg`) rather than
the .webm bytes. `pipeline/cosmos_footage_prep.py` validated the
content-type and raised:

```
RuntimeError: expected video, got Content-Type 'image/jpeg'
```

This is presumably an API quirk where Wikimedia serves the *poster
frame* of a video as the default response unless you explicitly hit
the underlying upload URL (`upload.wikimedia.org/wikipedia/commons/...`).
The prep tool's resolver path goes through the API, not the upload CDN.

**How to apply (immediate, author-side):**

1. **Don't reference .webm video files in the shotlist.** Search
   Wikimedia for a still-frame JPEG of the same simulation (e.g.
   `MergingBlackHoles_V2.jpg`, `Black_Hole_Merger.jpg`). Set
   `source_type: still_ken_burns`. The prep tool's Ken Burns pipeline
   handles the still cleanly.
2. **Carry `_wikimedia_search_query`** so future-you (or the next
   curator) can re-verify if the still page goes away.

**How to apply (pipeline-side fix, follow-up):**

Extend `pipeline/cosmos_footage_prep.py::_fetch_video_from_wikimedia`
(or equivalent) to:

1. If Content-Type is `image/jpeg|png|webp`, fall back to the upload
   URL (`upload.wikimedia.org/wikipedia/commons/<hash>/<File:name>.webm`)
   resolved via the same imageinfo response.
2. If the upload URL also can't be fetched as video bytes (rare —
   Wikimedia thumbnails .webm via the thumb endpoint), gracefully
   degrade to still_ken_burns mode using the JPEG thumbnail and log
   a warning so the curator knows the Short uses a single frame
   instead of the moving simulation.

**Status (2026-05-08):** mitigated for the LIGO short by switching
beat 4 to `MergingBlackHoles_V2.jpg`. Pipeline-side fix not yet
shipped. **Two-strikes-then-escalate** rule applies — if this fires
on the next /make-cosmos-short slug, the pipeline fix becomes
mandatory before further runs.

**Project-doc mirror:** `cosmosdecoded/learnings/webm_video_jpeg_thumbnail.md`.
