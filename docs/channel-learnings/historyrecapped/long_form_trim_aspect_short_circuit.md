---
name: Long-form trim must stream-copy when source aspect already matches output
description: _trim_clip_letterbox in render_long_form.py applies a gblur+split+overlay+x264 filter chain that wastes 30-40 min of CPU and can crash on long single 1080p clips. When src_w==out_w && src_h==out_h && no grade filter, stream-copy instead.
type: feedback
---

`_trim_clip_letterbox` in `historyrecapped/scripts/render_long_form.py` is designed to letterbox 4:3 archival sources into a 16:9 output canvas using a blurred copy of the same frame as side bars. That is the right thing for `archive.org` 480p Capra documentaries. It is the **wrong thing** for 1080p YouTube re-uploads of 16:9 documentaries, which already match the output canvas.

**Why:** v0.1 of `western-front-1914-1918-sleep` (2026-05-04) used a single 5640s clip from the 1080p Apocalypse documentary (`fAdzgcpfedk`). The trim ran for 35+ min at 240% CPU on a single ffmpeg process applying `[bg]gblur sigma=22 + scale; [fg]scale; [bg2][fg2]overlay; fps=30; libx264 -preset veryfast -crf 20`, then crashed with `RuntimeError: ffmpeg failed` — no useful stderr because the renderer wraps ffmpeg with `-loglevel error` and the actual ffmpeg signal was opaque. Bypassing the function manually with `ffmpeg -ss 60 -t 5640 -c:v copy -an` finished in **3 seconds** and produced a clean 4 GB clip that the rest of the pipeline accepted unchanged.

The filter chain on a 1920×1080 source produces a frame visually identical to the input. The gblur, the split, the overlay — all do nothing observable.

**How to apply:**

- The function now probes source dimensions before building the filter graph. If `src_w == out_w && src_h == out_h && grade_filter is None`, it stream-copies the trim (`-an -c:v copy`) and returns. Otherwise it falls through to the original filter chain.
- The grade-filter exception is required because the warm-firelight color grade (long_form_visual_signature.md) needs re-encoding even on aspect-matched sources.
- Logs print `[trim] aspect-match 1920x1080 == 1920x1080 — stream-copy` so the short-circuit is visible.
- For 4:3 archival sources (640×480 archive.org Capra films, 720×480 NTSC) the original filter chain still runs.

**Class-of-bug rule:** any time the renderer applies a heavy filter chain to a long source, first check whether the filter actually does anything visible. The blurred letterbox is the canonical example, but the same principle applies to fps conversion (60→30 on a sleep video that doesn't care about fps), unnecessary scaling, etc. **A no-op filter on a one-hour 1080p clip is not free** — it's 30-40 min of CPU and a real crash risk on aspect-matched sources where the filter is also pointless.

Memory mirror: `/Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/feedback_long_form_trim_aspect_short_circuit.md`
