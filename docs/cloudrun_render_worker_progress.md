# Cloud render-worker substep routing (2026-05-11)

> **TL;DR:** the cloud render-worker (`cloud/render-worker-v2/`) runs
> tts / asr / images / compose as **one** subprocess (`pipeline.render.shorts`)
> but the dashboard timeline has **four** pills. Pre-fix, every
> sub-step msg landed on the `compose` pill and tts/asr/images flashed
> "running → done · 0.0s" before the real work started. The renderer
> log classifier now returns `(stage, msg)` tuples and the main loop
> walks the timeline forward as the renderer reaches each sub-stage.

## What broke

The dashboard's timeline UI consumes `jobs/<id>.timeline[]` from
Firestore. Each entry is `{stage, label, status, msg, ts}` with
`status ∈ {pending, running, done, error}`. Stages are seeded from
`STAGES` at job start (all `status=pending`) and walked through by
`_main_from_firestore` one at a time.

The renderer subprocess emits **per-substep** stdout markers:

| renderer marker                                  | sub-stage |
|--------------------------------------------------|-----------|
| `[1/4] TTS (cloudrun_chatterbox)`                | tts       |
| `[1/4] TTS cached`                               | tts       |
| `[2/4] faster_whisper aligning timestamps`       | asr       |
| `[2/4] beats cached`                             | asr       |
| `    22 beats, total 58.4s`                      | asr       |
| `[prompts] authoring 22 beat prompts`            | images    |
| `[3/4] cloudrun_flux2_klein: generating 22 images` | images  |
| `    [12/22] beat=07 sarah-coffee`               | images    |
| `[image-done] beat 7 of 22`                      | images    |
| `[4/4] ffmpeg compose 9:16`                      | compose   |
| `[critic] recomposing after patch`               | compose   |
| `[compose] wrote …mp4`                           | compose   |

Pre 2026-05-11, the worker did two harmful things:

1. **Stub-handlers for the non-compose sub-stages.** `_REAL_HANDLERS`
   mapped `tts`, `asr`, `images` to `lambda job, wd: time.sleep(0.05)`.
   The main loop ran each in turn, marking `running` → `done · 0.1s`
   in 200 ms total **before any real work started**. Then `compose`
   ran for 5–15 min showing tts/asr/images sub-step messages on the
   compose pill — claiming TTS finished at t≈0 s while it was
   actually running RIGHT NOW.

2. **`_classify_renderer_line` returned only the human-friendly
   `msg`** — no stage attribution. The caller (`_compose_progress`)
   pinned every msg to `key="compose"`. So the user saw
   "Composing video / compose / Synthesizing narration
   (cloudrun_chatterbox)" before ffmpeg had launched.

User-visible symptom: the timeline pills lied. "Image generation"
was claimed `done · 0.1s` in the first second of a 7-min render,
and "Composing video" carried every other sub-step's msg.

## What the fix does

### `_classify_renderer_line(line)` returns `(stage, msg) | None`

```python
def _classify_renderer_line(line: str) -> tuple[str, str] | None:
    if (m := _REGEX_TTS_START.match(s)):
        return ("tts", f"Synthesizing narration ({m.group(1).strip()})" ...)
    if (m := _REGEX_BEATS_START.match(s)):
        return ("asr", f"Aligning captions ({m.group(1)})")
    if (m := _REGEX_PROMPTS_START.match(s)):
        return ("images", f"Authoring {m.group(1)} image prompts")
    if (m := _REGEX_COMPOSE_START.match(s)):
        return ("compose", "Stitching video with ffmpeg")
    ...
```

The classifier is the **only** place the regex → sub-stage mapping
lives. Adding a new substep marker means adding one entry here +
one test case.

### Main loop skips tts/asr/images in real mode

```python
for key, _ in job_stages:
    if mode == "real" and key in ("tts", "asr", "images"):
        continue   # _compose_progress will drive these
    ...
    elif key == "compose":
        _stage_render_real(job, work_dir, progress_cb=_compose_progress)
```

`_REAL_HANDLERS` no longer has entries for tts/asr/images — those
keys are now unreachable in real mode. (Stub mode still iterates them
through `_run_stage_stub` since stub testing wants per-stage timing.)

### `_compose_progress(stage, msg)` walks the timeline forward

```python
_RENDERER_SUBSTAGES = ("tts", "asr", "images", "compose")
substage_t0: dict[str, float] = {}

def _compose_progress(stage: str, msg: str) -> None:
    new_idx = _RENDERER_SUBSTAGES.index(stage)
    # mark every earlier sub-stage "done" with its real elapsed
    for prior in _RENDERER_SUBSTAGES[:new_idx]:
        if timeline[prior].status not in (None, "done"):
            elapsed = now - substage_t0[prior]
            _set_stage(timeline, prior, "done", f"{elapsed:.1f}s")
    # stamp the start of THIS sub-stage on first sight
    substage_t0.setdefault(stage, now)
    _set_stage(timeline, stage, "running", msg)
```

After the renderer subprocess returns, a final safety net marks any
sub-stage still in `pending` / `running` as `done` (covers cache
hits that fired before the tailer's first poll cycle).

### Outer loop doesn't overwrite the per-substage timing

The outer loop's standard `_set_stage(timeline, key, "done",
f"{elapsed:.1f}s")` post-handler is **skipped for compose in real
mode**. Otherwise it would overwrite each sub-stage's correct elapsed
time with the umbrella wall-clock total.

## Sub-stage execution order

The renderer fires substep markers in this order (matches the actual
`pipeline.render.shorts` execution):

1. **tts** — `[1/4] TTS …` (or `[1/4] TTS cached`)
2. **asr** — `[2/4] … timestamps` → `    N beats, total Xs`
3. **images** — `[prompts] authoring N beat prompts` → `[3/4] <provider>: generating N images` → `    [k/N] beat=…` per image → `[image-done] beat k of N` per image
4. **compose** — `[4/4] ffmpeg compose 9:16` → `[compose] wrote …mp4`

`_RENDERER_SUBSTAGES = ("tts", "asr", "images", "compose")` is the
canonical order. Adding a new sub-stage between two existing ones
(e.g. `prompts` becoming a separate timeline pill) means:

1. Add the key to `_RENDERER_SUBSTAGES` at the right position.
2. Add the key to `STAGES`.
3. Update the classifier to route prompts markers to `("prompts", …)`
   instead of `("images", …)`.
4. Skip `prompts` in the main-loop pre-mark like tts/asr/images.

## Cache-hit edge case

When `[1/4] TTS cached` fires before the tailer's first poll cycle,
the worker can advance directly to `images` markers without ever
seeing a tts msg. The walk-forward logic in `_compose_progress`
marks tts (and asr, if also cached) `done` with msg `"—"` (no
substage_t0 stamped) so the timeline never leaves a pill in
`pending` forever. The post-renderer safety net does the same for
any sub-stage that escaped both the per-marker walk and the cache
shortcut.

## Why this matters beyond UX

The same `(stage, msg)` tuple flows into Firestore. A future
analytics layer can `GROUP BY stage` and get **honest** stage
durations — pre-fix every render claimed `tts: 0.1s, asr: 0.1s,
images: 0.1s, compose: 7m20s`, which is useless for budget /
SLO work.

## Testing

`tests/test_cloudrun_render_worker_progress.py` — 26 tests:

- 14 classifier tests (one per regex family + edge cases) assert
  `(stage, msg)` tuples.
- 6 tailer tests assert dedupe / unknown-line filtering / partial-line
  buffering.
- 2 subprocess-spawn tests (with a child script that writes substep
  markers to stdout) assert end-to-end the callback receives
  `(stage, msg)` tuples in order.
- **4 stage-walk tests** assert the live-walk contract:
  - Sub-step lands on correct pill (compose stays pending while tts
    is running).
  - Advancing to asr marks tts done.
  - Skipping straight to images marks BOTH tts and asr done (cache
    hit).
  - Full walk through compose: every prior sub-stage ends up in
    `done`, compose pill carries the LAST substep msg only.

## Generalised rule

**Any composite subprocess in a stage-loop UI must route sub-steps
to the correct stage, not pin them to the umbrella.** Pinning
collapses information and lies about timing. The pattern:

1. Classifier returns `(target_stage, msg)`.
2. Main loop SKIPS the umbrella-collapsed sub-stages so they don't
   pre-flash status.
3. The umbrella stage's progress callback walks the timeline
   forward; later sub-stages auto-mark earlier ones "done".
4. A post-subprocess safety net marks anything left as "done"
   (covers cache hits / shortcut paths).

## See also

- `docs/cloudrun_render_worker.md` — the worker overall.
- `cloud/render-worker-v2/entrypoint.py` — the implementation.
- `web/server.py::_classify_line` — laptop-side equivalent
  (already correct because the laptop renderer emits per-stage
  events directly through the SSE bus, not a tailed log).
- `web-next/app/jobs/[id]/page.tsx` — the dashboard consumer of
  the timeline.

---

## 2026-05-12 — Long-form pipeline needs its OWN substage taxonomy

The same routing pattern bit a SECOND time, on the long-form
dispatch path. User reported "5 / 7 stages complete · 71%" 20 s
into a 30-min render — every pill except `compose` and `upload`
flashed "done · —" while the actual long-form rewrite hadn't even
finished. Plus the front-end fetched `/api/jobs/<id>/preview.mp4`
and got a 404 because the mp4 didn't exist yet.

### Bug chain

1. **Worker pre-marked `rewrite` as "done"** *before* invoking
   `pipeline/render/video.py:render_long_form()`. But the actual
   long-form rewrite happens INSIDE that call (LLM authoring of
   the sectioned envelope) — the pill lied for 5–15 min while
   the rewrite actually ran.
2. **`_lf_progress` only knew the SHORT substages** — it reused
   `_RENDERER_SUBSTAGES = ("tts","asr","images","compose")`. When
   `video.render_long_form` emitted `progress_cb("rewrite",
   "long-form rewrite (1800s target)")` as its FIRST progress
   event, the unknown-stage defensive coercion at the top of the
   callback turned `"rewrite"` into `"compose"`. The cascade-walker
   then marked tts/asr/images all "done · —" at t≈0s.
3. **Frontend mounted `<video>` while still rendering.** PlayerCard's
   `ready` flag included `status === "rendering"`, AND `previewSrc`
   fell back to constructing the URL when the backend hadn't emitted
   one. Backend `preview_url` gate was also too loose (fired on
   `uploading` before `short_uri` was populated). See
   `feedback_preview_url_artifact_gate.md`.

### Fix — long-form-aware taxonomy + no-cascade guard

```python
# Long-form substage taxonomy. Sibling to _RENDERER_SUBSTAGES.
_LF_SUBSTAGES_ORDER  = ("rewrite", "tts", "images", "compose")
_LF_SUBSTAGE_ALIASES = {"narrate": "tts"}  # video.render_long_form
                                            # emits "narrate" for chunked TTS

def _lf_advance_timeline(timeline, substage_t0, stage, msg, *, now):
    """Pure progression helper. Resolves aliases, coerces unknown
    stages to compose WITHOUT cascading priors, only marks a prior
    pill 'done' if we actually saw it START (substage_t0 has the
    key). Pre-skipped pills (cast / asr-when-authored) keep their
    pre-mark; truly never-ran pills stay 'pending' rather than
    lying."""
    resolved = _LF_SUBSTAGE_ALIASES.get(stage, stage)
    if resolved not in _LF_SUBSTAGES_ORDER:
        resolved = "compose"
    new_idx = _LF_SUBSTAGES_ORDER.index(resolved)
    for prior in _LF_SUBSTAGES_ORDER[:new_idx]:
        if timeline_status_of(prior) in (None, "done"):
            continue
        if prior not in substage_t0:
            continue                      # phantom — never started
        elapsed = now - substage_t0[prior]
        _set_stage(timeline, prior, "done", f"{elapsed:.1f}s")
    substage_t0.setdefault(resolved, now)
    _set_stage(timeline, resolved, "running", msg)
```

Long-form pre-marks (set BEFORE invoking `_video.render`):

| pill    | pre-mark                                                              | why                                                                                       |
|---------|-----------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| rewrite | `running` ("long-form rewriter authoring envelope")                  | rewrite happens INSIDE video.render_long_form; was wrongly pre-marked "done" pre-fix      |
| cast    | `done` ("skipped — long-form has no cast stage")                     | long-form has no per-character voice casting                                              |
| asr     | `done` ("skipped — captions aligned from authored TTS chunk timings") if `long_form.caption_align != "whisper"`; else left `pending` | default authored alignment uses TTS chunk timings; whisper alignment has no telemetry hook so we don't lie about progress |

### Generalised rule (extends the parent rule above)

The original rule was "any composite subprocess in a stage-loop UI
must route sub-steps to the correct stage". The 2026-05-12 add-on:
**each pipeline kind (short, long-form, future kinds) needs its OWN
substage taxonomy + prior-cascade guard**, because the substages
they emit aren't the same set:

- **Short** (`pipeline.render.shorts`) emits tts/asr/images/compose.
  Whole pipeline is one subprocess; cascade is safe (a later
  substage starting genuinely implies all earlier ones finished).
- **Long-form** (`pipeline.render.video.render_long_form`) emits
  rewrite/narrate/compose inline AROUND a subprocess that emits
  tts/images/compose. Cascade is UNSAFE for the inline events
  because they fire before any subprocess substage; the prior-walk
  must be guarded by `prior in substage_t0`.

When wiring a new render kind:

1. Decide whether its substage emissions are a subset / superset /
   reordered relative to `_RENDERER_SUBSTAGES`.
2. If different, define a sibling `_<KIND>_SUBSTAGES_ORDER` tuple
   and (optionally) `_<KIND>_SUBSTAGE_ALIASES` dict.
3. Pass the kind-specific tuple into the prior-walk; gate
   "mark prior done" on `prior in substage_t0` so phantom
   transitions can't cascade.
4. Pin with a `<Kind>AdvanceTimelineTests` class — at minimum a
   no-cascade test for the FIRST substage and an alias-resolution
   test if the kind uses any.

### Testing

`tests/test_cloudrun_render_worker_progress.py::LfAdvanceTimelineTests`
— 6 new tests pin the long-form contract:

- `test_constants_shape` — defends against accidental rename.
- `test_rewrite_event_does_not_cascade_to_done` — THE bug.
- `test_narrate_alias_routes_to_tts_pill` — alias resolution +
  rewrite→done with accurate elapsed.
- `test_unknown_stage_coerced_to_compose_no_cascade` — defence.
- `test_full_long_form_walk_marks_done_with_real_elapsed` — e2e.
- `test_repeat_event_for_same_stage_is_idempotent_for_t0` — `[3/5]`
  and `[4/5]` both routing to compose don't reset t0.

Plus `tests/test_jobs_id_fallthrough.py::ControlPlaneFallthroughTests::test_status_uploading_without_short_uri_no_preview`
pins the preview-gate fix.
