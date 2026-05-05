# Ken Burns: keep it static on looped stills

**Class-of-bug fix, 2026-05-05.** Mirror of memory entry `feedback_kenburns_zoompan_slow_on_stills.md` (per CLAUDE.md dual-save rule).

## What broke

`historyrecapped/scripts/render_footage_only.py` previously used a `zoompan` filter to add slow zoom-in motion to image-only windows. On looped still inputs the filter is pathologically slow — a single 10.5-second 9:16 clip took 30+ minutes on M2 Max, and the renderer silently appeared to hang.

Surfaced 2026-05-05 during the first cosmos-decoder render (`eddington-1919-eclipse-short`). All 7 beats are stills. With 4 parallel workers the renderer locked the machine for ~30 min before being killed. Same pathology hit `pipeline/cosmos_footage_prep.py` in its first version.

## What was changed

1. **`historyrecapped/scripts/render_footage_only.py::_ken_burns_filter`** — drop the `zoompan` layer; replace with a static scale + pad (`scale=W:H:force_original_aspect_ratio=decrease,overlay`). Foreground sits centered on a blurred letterbox of the same source.
2. **Image clip ffmpeg command** — add `-preset ultrafast -tune stillimage -crf 23 -framerate 30` for the still-image encode path.
3. **`pipeline/cosmos_footage_prep.py::_kenburns`** — same static treatment, also with `-tune stillimage`.

Result: the same 10.5-second clip now encodes in ~15s. The 7-clip Short rendered in ~107s for the trim phase (vs. timed out at 30+ min/clip).

## Trade-off

No intra-clip zoom motion. The cut tempo (5–15s per beat) provides enough movement at the Shorts scale. For long-form 25-min videos with the same cut tempo, this is also acceptable. If on-clip motion ever becomes required, use `crop=` with time-varying `x` / `y` expressions — much faster than zoompan.

## How to apply going forward

- Any renderer or prep tool that encodes looped stills must NEVER use `zoompan`.
- Use `-tune stillimage` whenever encoding from `-loop 1 -i still.jpg`.
- Validate any new ken-burns helper by timing it against a 10s 1080×1920 clip — anything over 30s is broken.

## Cross-impact

- HistoryRecapped Shorts that include image stills (paper page screenshots, document scans) now also encode fast. Pure-video windows are unaffected (`_trim_clip_letterbox` path unchanged).
- Cosmos Decoded long-form prep was already on the static pattern; only the renderer needed catching up.
- File rename note: this is wider than just Cosmos Decoded — `pipeline/cosmos_footage_prep.py` should eventually be promoted to `pipeline/footage_prep.py` as cross-channel infra.
