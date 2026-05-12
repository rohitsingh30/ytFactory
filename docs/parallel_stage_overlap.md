# Inter-stage overlap — pattern + recipe

> **Established 2026-05-13** when the user pushed back on long-form
> renders showing TTS and image-gen running sequentially even though
> both are cloud-bound (cloudrun_chatterbox + cloudrun_flux2_klein,
> different L4 GPUs in asia-southeast1, no shared contention). This
> doc captures the pattern so the next "TTS ⫽ images / footage / X"
> refactor doesn't have to re-discover it.

This is the **inter-stage** sibling of
[`docs/parallel_per_beat_fanout.md`](./parallel_per_beat_fanout.md).
That doc covers fan-out *within* a stage (N image beats, N TTS chunks).
This doc covers fan-out *across* stages.

## TL;DR

When a render orchestrator can identify an **independent prefix** of
stage X — work that knows everything from the rewrite-time JSON and
needs nothing from a yet-to-run sibling stage — that prefix can be
launched on a worker thread BEFORE the sibling starts. The
duration-dependent assembly tail then re-joins both branches.

```python
from pipeline.stage_overlap import StageOverlap, gpu_safe_to_overlap

overlap_safe, reason = gpu_safe_to_overlap(
    tts_provider=cfg["tts_provider"],
    image_provider=cfg.get("image_provider"),  # None if no images
)
if overlap_safe:
    with StageOverlap(label="my-render", max_workers=1) as overlap:
        prep_fut = overlap.submit(
            "video_prep", _generate_panel_stills, panels=panels, ...
        )
        narration_wav, chunks = synth_long_narration(...)  # main thread
        dur = probe_duration(narration_wav)
        panel_pngs = prep_fut.result()  # blocks; raises on branch error
    # Now run dur-dependent assembly with both inputs ready
    video = _assemble_kenburns(panel_pngs, panels, dur, ...)
else:
    # Sequential fallback — local-GPU contention guard.
    narration_wav, chunks = synth_long_narration(...)
    dur = probe_duration(narration_wav)
    panel_pngs = _generate_panel_stills(panels, ...)
    video = _assemble_kenburns(panel_pngs, panels, dur, ...)
```

Worked example: `pipeline/render/long_form.py::_main_impl` lines
~2050-2200 (image_panels + archival_footage paths, both wired).

## Why a serial-then-overlap shape, not just `asyncio.gather`?

Three reasons.

### 1. Dependency boundary

The independent prefix has a clean cut: image stills don't need
`narration.wav`, footage downloads don't need `narration_dur`,
chapter cards don't need ASR-derived word timestamps. Identifying
the cut explicitly (and naming the dur-dependent half) makes the
data flow obvious to the next reader. Compare the muddy
`gather(tts(), images(), music())` shape, which leaves you guessing
which return values feed which downstream call.

### 2. ContextVars + OTel propagation

Threading workers do **NOT** auto-inherit
[`contextvars.ContextVar`](https://docs.python.org/3/library/contextvars.html)
state from the parent. The
[`pipeline.observability.context.RenderContext`](../pipeline/observability/context.py)
ContextVar (channel / slug / job_id / run_id) and the OpenTelemetry
active span both live in ContextVars. Without explicit propagation,
every span inside the worker becomes a detached root span and the
dashboard's per-render envelope linkage breaks.

`StageOverlap.submit` captures `contextvars.copy_context()` **per
future** and runs the callable inside that context. Both the
RenderContext and OTel current span follow the worker thread.

The "per-future" qualifier matters:
[`Context.run`](https://docs.python.org/3/library/contextvars.html#contextvars.Context.run)
cannot be entered concurrently — re-using a single Context across
two workers raises `RuntimeError: cannot enter context: <Context …>
is already entered`. Capturing per-call costs O(small) — Context is
a copy-on-write immutable map.

### 3. GPU contention gate

Cloud-bound TTS (`cloudrun_chatterbox`, `cloudrun_f5`,
`cloudrun_indicf5`, …) and cloud-bound image-gen
(`cloudrun_flux2_klein`, …) hit physically distinct L4 GPUs in
`asia-southeast1` — overlap is free.

Local providers (`f5_tts` / `kokoro` / `mflux` / `z_image_turbo`)
all dispatch to the same Metal command queue on M2 Max. Concurrent
diffusion + TTS triples per-step latency and can trigger Metal
command-buffer timeouts (see
[`docs/long_form_model_inventory.md`](./long_form_model_inventory.md)
and the dual-save memory
[`feedback_gpu_one_render_at_a_time.md`](../../.claude/projects/-Users-rohit-ytFactory/memory/feedback_gpu_one_render_at_a_time.md)).

`gpu_safe_to_overlap(tts_provider=, image_provider=)` returns
`(False, reason)` whenever **either** provider is local-GPU. The
orchestrator must then fall back to sequential. `image_provider=None`
is allowed for orchestrators that don't generate images
(footage_only / sports_doc) — only the TTS-cloud check fires.

Set `YTFACTORY_DISABLE_STAGE_OVERLAP=1` to globally force sequential
if debugging suggests overlap is the trigger of a regression.

## Failure semantics

When one branch raises, in-flight sibling work is **NOT** cancelled
(`concurrent.futures.Future.cancel` cannot stop an already-running
HTTP/ffmpeg/TTS call anyway, and killing it would discard valuable
cache writes that a retry could re-use).

`StageOverlap.__exit__` waits for ALL submitted futures to settle,
then re-raises the first exception. On Python 3.11+ subsequent
exceptions are surfaced via `ExceptionGroup` so you see every cause,
not just the first.

If the with-block body itself raises, the body's exception
propagates and branch errors are logged at WARNING (not silently
lost — pre-fix, fire-and-forget branches could swallow exceptions
which is the worst-of-all-worlds).

## Cloud-worker timeline walker

The dashboard's 7-pill long-form timeline shows TTS / images /
compose etc. Pre-overlap, a strict cascade rule fired: when a later
substage starts running, all earlier pills get marked done. With
overlap, two pills (`tts` + `images`) genuinely run simultaneously
— cascading would falsely mark TTS done the moment images starts.

Two changes in the cloud worker:

1. **Explicit done markers from the renderer.** `pipeline/render/long_form.py`
   emits `[1/5] tts done X.Ys` and `[2/5] video prep done X.Ys`
   AFTER the corresponding stage finishes. The classifier
   (`pipeline/render/video.py::_maybe_emit_long_form_progress`)
   recognises the standalone `done` token and forwards as
   `progress_cb("<stage>_done", "X.Ys")`.

2. **Selective cascade in the walker.**
   `cloud/render-worker-v2/entrypoint.py::_lf_advance_timeline`
   defines `_LF_OVERLAPPING_SUBSTAGES = frozenset({"tts", "images"})`.
   When the new substage is also in the overlap set, the cascade
   SKIPS prior overlap-eligible pills — they wait for their own
   explicit done event. When the new substage is downstream of the
   overlap region (`compose`), the cascade DOES fire — by then both
   overlap branches are guaranteed finished even if the renderer
   didn't emit explicit done events (graceful downgrade for older
   renderers).

The `<key>_done` event marks the pill done with the message body
as the elapsed-time text, and DOES NOT touch any other pill.

## Backwards compatibility

* Older renderers (no explicit done markers) — when `compose` starts,
  the cascade still fires on `tts` and `images`, so the timeline
  finishes correctly even without the new markers.
* Older cloud workers (don't recognise `<key>_done`) — the renderer's
  legacy `[1/5] narration N chunks → narration.wav` line is still
  emitted, and older walkers parse it as a TTS-running event then
  cascade on the next substage start.
* Local CLI runs — `gpu_safe_to_overlap` returns False whenever a
  local provider is selected, so the laptop CLI flow stays
  sequential and Metal contention can't surface.

## Where this is wired (2026-05-13)

| Orchestrator | Independent prefix | Dur-dependent tail |
|---|---|---|
| `pipeline/render/long_form.py` (image_panels) | `_generate_panel_stills` | `_adjust_panel_holds_to_dur` + `_assemble_panel_kenburns` |
| `pipeline/render/long_form.py` (archival_footage) | `_trim_shotlist_clips` | `_concat_and_pad` (uses `dur` as target) |
| `pipeline/render/sports_doc.py` | `_prep_raw_entries` (yt-dlp + trim for ALL footage_plan entries) | `_gather_overlays` (whisper-aligns anchors) + per-array dispatch via id-keyed dict |
| `pipeline/render/footage_only.py` | `_build_silent_video` (yt-dlp + trim + concat shotlist) | `_burn_video` / `_mux_audio_no_captions` |

Channels affected (every channel using one of the above + a
cloud-bound TTS provider):

* `historyrecapped` long-form sleep videos
* `historyrecapped` Shorts (archival)
* `sportsrecapped` long-form-doc
* `cosmosdecoded` long-form + short cuts
* `hindutavaanimated` long-form kathaa (footage_only)
* any new channel scaffolded with `render_style: footage_only` or
  `long_form.render_mode: archival_footage` / `image_panels`

`pipeline/render/shorts.py` is **NOT** wired — its dependency chain
is hard-sequential (`TTS → ASR → beats → prompts → images → compose`)
because beats are derived from ASR word timestamps and prompts are
authored from beat boundaries. The image-gen stage genuinely cannot
start before TTS finishes on the Shorts path. (The intra-stage
fan-out already lives — see `docs/parallel_per_beat_fanout.md` —
which gives the per-beat overlap that's actually applicable to
Shorts.)

## Sweep recipe — find candidates to apply this pattern to

```bash
# Orchestrators that call synth_long_narration (or audio.synthesize)
# AND have an independent prefix (yt-dlp / image gen / chapter cards)
# in a later stage:
grep -rn "synth_long_narration\|audio.synthesize" pipeline/render/
```

Today's known unrefactored candidates:

* **None on the long-running stages.** All four orchestrators are
  wired as of 2026-05-13.
* **Music bed loading inside `pipeline/render/shorts.py`** could be
  moved into the image-gen branch (currently runs after TTS but
  before image gen). Marginal win because music-bed mixing is fast.

## Cost: marginal $/mo of overlap

**$0.** Same total GPU-seconds whether sequential or parallel —
Cloud Run bills per request-handling time, not per container-uptime.
Splitting work across N containers costs the same total as one
container handling N serial requests.

The wall-clock improvement IS the entire economic benefit:
~min(tts_wall_s, images_wall_s) per render, multiplied across
every render in the day's batch.

## Cross-references

* [`docs/parallel_per_beat_fanout.md`](./parallel_per_beat_fanout.md)
  — the intra-stage fan-out sibling
* [`docs/parallel_bulk_renders.md`](./parallel_bulk_renders.md) —
  the cross-render parallelism story (different scale entirely)
* [`docs/cloudrun_image.md`](./cloudrun_image.md) §"Per-call timings"
  — measured cloud image-gen latency
* [`docs/cloudrun_tts.md`](./cloudrun_tts.md) §"Chunk fan-out" —
  measured cloud TTS chunk concurrency
* `feedback_gpu_one_render_at_a_time.md` (memory) — the local-GPU
  rule the gate enforces
* `feedback_stage_overlap_pattern.md` (memory) — terse pointer to
  this doc

## Test surface

* `tests/test_stage_overlap.py` — 30+ tests pinning the helper
  (gate, ContextVar propagation, ExceptionGroup, render-context
  propagation, orchestrator shape).
* `tests/test_render_long_form_parallel.py` — pins the long-form
  overlap path with deterministic `threading.Event` happens-before
  assertions; covers both render modes + negative cases (local
  image_provider blocks; env disable blocks; local TTS blocks).
* `tests/test_cloudrun_render_worker_progress.py::LfAdvanceTimelineParallelTests`
  — pins the parallel-aware walker (overlap concurrency, explicit
  done events, alias on done, unknown-substage defence, downstream
  cascade).
* `tests/test_render_video.py::MaybeEmitLongFormProgressDoneMarkersTest`
  — pins the classifier's done-marker recognition.
