# Render-process deadline policy — 25 minutes minimum

Set by user 2026-05-07 after a Hindi mythology Short render was killed
mid-image-gen during local-mflux fallback, invalidating cached audio +
beat-split work. The full re-render had to start from TTS again.

## Rule

**Do not kill a render-pipeline process before 25 minutes have
elapsed**, unless:
- The user explicitly asks for it.
- The process is genuinely hung (no progress lines in the output for
  >5 min AND past the 25-min mark).
- An obvious better path exists (e.g. the script source is broken
  and the render can never finish).

Applies to:
- Cloud TTS calls (per-chunk retries + fallback chain)
- Cloud image gen calls (per-image retries + circuit-breaker fallback
  to local mflux)
- The full `make_shorts.py` / `make_long_form.py` background runs
- Critic-loop regen passes (~5 image regens after critic verdict)

## Why renders take this long

A typical Hindi mythology Short with 13 authored beats:

| Stage | Wall-time |
|---|---|
| TTS — cloud IndicParler chunked, 15 paragraphs serial | 1-2 min warm, 4-5 min cold |
| Beats — Whisper word-timestamps + force-13 alignment | <1 min |
| Image gen — cloudrun_flux2_klein, 13 images | 1-2 min warm |
| Image gen — local mflux fallback after cloud timeout | 8-10 min |
| Critic — opus vision pass + regen of 5 patched beats | 3-4 min |
| Compose — 177 caption PNGs + ffmpeg slideshow | 4-5 min |
| **Total worst-case** | **22-25 min** |

A render that goes warm-cloud throughout finishes in ~10 min. A
render that hits cloud-image timeout and falls back to local mflux
needs the full 25-min budget. Killing at 15 min wastes the
synthesised audio and forces a full re-run.

## Implementation notes

- **Bash backgrounded commands**: use `run_in_background=true` and
  let them run. The harness should NOT kill them on a 10-min cap.
- **TaskStop**: reserve for explicit user instruction OR a clearly
  hung process past the 25-min mark.
- **Cloud per-call timeouts** (`CLOUDRUN_TTS_TIMEOUT=180`,
  `CLOUDRUN_IMAGE_TIMEOUT=900` in .env) bound a SINGLE attempt,
  not the overall render. Leave them as-is.

## Owner

The agent / harness driving the render. Not the user.

## Memory

[`memory/feedback_25min_render_deadline.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_25min_render_deadline.md)
