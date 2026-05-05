# Long-form (footage-only) renderer — model inventory + memory ceiling

Last updated 2026-05-04. Snapshot of every ML model that loads into MLX /
unified memory during a `historyrecapped/scripts/render_long_form.py` run
**after the 2026-05-04 image-panels retirement.** If a future change adds
another model, update this file.

## What the renderer loads, when, and how big

| Stage | Module | Model | Size (in unified mem) | Singleton? | Loaded for footage-only? |
|---|---|---|---|---|---|
| 1 — TTS (F5 ONLY) | `pipeline/audio.py::_f5_get_model` | `lucasnewman/f5-tts-mlx` (F5-TTS-MLX) | ~1.35 GB | yes (`_F5_MODEL`) | **YES** — every run; renderer hard-rejects any other provider |
| 1 — TTS ref cache | `pipeline/audio.py::_f5_get_ref` | RMS-normalised ref WAV mx.array | ~MB | yes (`_F5_REF_CACHE` dict) | yes |
| 4 — Captions (authored, default) | `render_long_form.py::build_caption_pngs_from_chunks` | none | 0 | n/a | yes — pure ffprobe + PIL |
| 4 — Captions (whisper fallback) | `pipeline/asr.py::_transcribe_whisper` → `mlx_whisper.transcribe` | `mlx-community/whisper-large-v3-mlx-4bit` | ~1.5 GB | yes (mlx_whisper internal `ModelHolder.get_model`) | **only if `caption_align: whisper`** in config |
| 2 — Video (footage path, default) | ffmpeg only | n/a | 0 | n/a | **YES** |
| 2 — Video (image_panels, capped) | `pipeline/images.py` | `z_image_turbo` (1344x768 SD) | ~3–4 GB | no — cold-loads on first call | **only if `render_mode: image_panels` AND panels ≤ `panel_max_count` (default 24)** |

**Footage-only ceiling:** ~1.35 GB MLX / GPU footprint (F5-TTS-MLX only).
ffmpeg encode is CPU-bound via libx264 `-preset veryfast` and uses RAM,
not Metal. Should not trigger Metal command-buffer timeouts.

**Hybrid (footage + few image panels):** small image runs are fine — the
89-panel pure-image timeline that crashed western-front-1914-1918-sleep
on 2026-05-04 is the failure mode we engineered out, not all image gen.
Before image gen runs, `render_long_form.py::main` drops the F5-TTS-MLX
singleton (`_F5_MODEL = None`, `_F5_REF_CACHE.clear()`) and calls
`mx.metal.clear_cache()` so the diffusion model starts on a clean Metal
heap (~3–4 GB headroom instead of fighting the resident TTS state). Cap
panel count at `long_form.panel_max_count` (default **24**) — pure
image-panel timelines beyond that have empirically hit
`kIOGPUCommandBufferCallbackErrorTimeout` and are blocked by `SystemExit`.

## Why the prior runs OOM'd

The Metal command-buffer timeout in `/tmp/render_western3.log` (panel 51/89,
2026-05-04) was **not** an OOM in the Python sense — it was Metal's
`kIOGPUCommandBufferCallbackErrorTimeout`. Three contributing factors:

1. **z_image_turbo cold-loads mid-run.** Look at panel 51 in the log:
   `wall=278.1s per_step=69.5s` vs ~20s/step steady-state. The Metal queue
   was being re-populated, almost certainly because the diffusion model
   was unloaded (memory pressure?) and re-loaded.
2. **F5-TTS-MLX (1.35 GB) was already resident** from stage 1, so the
   GPU was carrying ~5 GB of MLX state when z_image_turbo's compute
   buffers tried to schedule. Unified memory fragments under that load.
3. **89 panels × 4 steps × 1344×768** is a long pipeline of Metal
   command buffers; any single buffer exceeding the watchdog timer
   crashes the whole process.

The class-of-bug fix removes all three causes for the typical long-form
run by defaulting to `archival_footage` (no image gen, no Metal pressure).
Image gen remains available in capped quantities for chapter cards / hero
shots: `panel_max_count` (default 24) is the hard ceiling, and the F5
singleton is explicitly dropped + Metal cache cleared before the
diffusion model loads, so it never has to compete with resident TTS state.

## TTS singleton pattern (load-bearing)

`pipeline/audio.py:820–872` is the canonical singleton for F5-TTS-MLX.
The block is heavily commented because upstream `f5_tts_mlx.generate.generate()`
re-instantiates `F5TTS.from_pretrained` on every call (cfm.py:131), which
turns 178 chunks × 1.35 GB reload into a ~9 hr render. Our `_synth_f5_tts`
calls `f5tts.sample()` directly with the cached model + cached ref audio.

Same pattern applies to Chatterbox (`_CHATTERBOX_MODEL`), StyleTTS2
(`_STYLETTS2_MODEL`), Indic Parler (`_INDIC_PARLER_MODEL`). Whisper is
singleton'd inside `mlx_whisper`'s `ModelHolder` so we don't need to
do it ourselves.

Rule: any new TTS / ASR / diffusion provider in `pipeline/` MUST be
singleton-cached before the first chunk runs. Don't expose a
`from_pretrained` call from inside a per-chunk hot loop.

## Caption alignment — authored, not whisper

`pipeline/render/long_form.py::main()` defaults `caption_align` to
`"authored"`. Authored alignment uses the canonical narration text
(`script["narration"]`) plus `ffprobe` durations of cached TTS chunks —
no ASR model is loaded.

Whisper alignment (`caption_align: whisper`) is the legacy fallback. It
loads `whisper-large-v3-mlx-4bit` (1.5 GB) on top of F5-TTS-MLX. Avoid
unless authored alignment fails — which it shouldn't, since the chunk
wavs and chunk text are produced from the same `_split_into_chunks` call
in the same Python process.

## Caption rendering — libass ASS, not PNG fan-out (Tier-1 fix 2026-05-05)

The default caption renderer is now **libass via ffmpeg's `subtitles=`
filter**: one `.ass` file → one `-i` input → one filter step,
regardless of cue count. The legacy PNG-overlay fan-out path is kept
as a fallback for ffmpeg builds without libass (auto-detected via the
memoised `_ffmpeg_has_libass()` probe — falls back automatically + logs
a one-line pointer to the `brew install` fix).

| Cue count | PNG fan-out peak ffmpeg RAM | libass ASS peak ffmpeg RAM |
|---|---|---|
| 100 | ~1.5 GB | ~1.5 GB |
| 300 | ~3.5 GB | ~1.5 GB |
| 600 | ~6.5 GB | ~1.5 GB |
| 800 | ~10 GB (often OOM) | ~1.5 GB |

Why PNG fan-out balloons: each `-i caption.png` keeps a 1920×1080×4
= 8 MB RGBA buffer hot, and each overlay step adds a filter graph
node evaluated for every output frame. 800 cues × 8 MB + filter
graph state crosses what ffmpeg can allocate contiguously on a busy
unified-memory heap.

**Prerequisite for the libass path** — install ffmpeg with libass:

```bash
brew uninstall ffmpeg
brew install homebrew-ffmpeg/ffmpeg/ffmpeg   # required deps include libass, libfreetype, fontconfig
```

Verify: `ffmpeg -hide_banner -h filter=subtitles | head -3` should
print "Render text subtitles onto input video using the libass library."

## Parallelism — where it's safe, where it's not

Threading is enabled for **CPU-bound** stages where the heavy work is in
ffmpeg subprocesses or PIL C extensions (both release the GIL):

| Stage | Helper | Cap | Speedup measured |
|---|---|---|---|
| Long-form footage trim (`build_video_track`) | `pipeline.parallel.run_parallel` | 4 (default) | ~3-4× |
| Shorts footage trim (`_build_silent_video`) | same | 4 | ~3-4× |
| Caption PNG render (`build_caption_pngs_from_chunks`, fallback only) | same | 8 | ~5-6× (PIL is faster than ffmpeg) |

Worker cap is `pipeline.parallel._default_workers()` = `min(cpu//2, 4)` for
ffmpeg jobs (each libx264 -preset veryfast already uses ~3 internal threads
so 4×3≈12 saturates the M2 Max perf cores). PIL caption renders use 8.
Override with `YTFACTORY_FFMPEG_WORKERS=N` if running on different hardware.

Trim ffmpeg invocations now pin `-threads 3` to keep the libx264 thread
count predictable under parallel-4 fan-out. Final mux is unchanged
(single instance — uses all available cores).

**Threading is intentionally NOT applied to:**
- F5-TTS-MLX, mlx_whisper, z_image_turbo: shared Metal device, concurrent
  ops serialise + fragment unified memory (the class of bug that caused the
  2026-05-04 GPU command-buffer timeout).
- The final mux ffmpeg pass: already internally threaded; fan-out hurts.
- Anything that mutates shared state from multiple threads.

ProcessPool is NOT used anywhere — we don't have pure-Python CPU
bottlenecks. The cost (fork overhead, doubled memory) would only add load.

## Power-state preflight (the WindowServer watchdog class of bug)

What looks like a "memory error" or random crash on a long-form render
is sometimes actually macOS killing **WindowServer**, not Python — or
the kernel aborting a Metal command buffer mid-flight (looks like a
SIGABRT on `com.Metal.CompletionQueueDispatch` in
`~/Library/Logs/DiagnosticReports/Python-*.ips`).

Incident F743A4C5 (2026-05-04) and the two SIGABRT crashes on
2026-05-04 22:39 + 2026-05-05 00:50 all share the same preconditions:

- `lowPowerMode: 1`
- `displayState: "OFF"` (lid closed / display asleep)
- F5-TTS-MLX or z_image_turbo holding the Metal command queue

Why it happens: under Low Power Mode the GPU is clocked down. Long
Metal command buffers stretch from ~5–20s to 40s+. WindowServer also
needs the GPU to drive any UI and can't get a slot in time. The kernel
watchdog interprets that as a hung WindowServer and either kicks
WindowServer (system logs out / panics) or aborts the GPU command
buffer (Python process dies with SIGABRT on the Metal completion queue).

**Tier-1 fix (2026-05-05)** — extracted the preflight to
`pipeline/preflight.py::power_check(label=...)` and wired it into the
top of EVERY renderer entry point:

- `pipeline/render/long_form.py::main` (via the `_preflight_power_check`
  shim — kept for backward compat with tests that patch it by name)
- `pipeline/render/footage_only.py::render`
- `pipeline/render/shorts.py::make_short`
- `pipeline/render/sports_doc.py::main`

Behaviour:

- **Hard-rejects** Low Power Mode (`pmset -g` shows `lowpowermode 1`).
- **Warns** when on battery — long-form renders should be on AC. If you
  must run on battery, wrap with `caffeinate -dimsu` (display + idle +
  mouse + system + user wake) NOT just `-i`. Display-off + heavy MLX
  is exactly the failure mode above.

Override with `YTFACTORY_SKIP_POWER_CHECK=1` if you understand the risk
(e.g. desktop M2 Studio where WindowServer is on a different display).

## MLX heap hygiene during long TTS runs

Two layers:

**Layer 1 — periodic flush during chunked TTS** (long-form only). The
F5-TTS-MLX `sample()` call accumulates cached graph state across
chunks. On a 200+ chunk long-form run that growth pushes us toward
Metal command-buffer timeouts even without image gen. The chunk loop
in `synth_long_narration` calls `mx.clear_cache()` every
`YTFACTORY_MLX_FLUSH_EVERY` chunks (default 10). Cheap (single ms) and
prevents the slow leak that turns chunk N+200 into a Metal grenade.

**Layer 2 — drop F5 at the renderer-stage boundary** (Tier-1 fix
2026-05-05). After the stage-1 TTS print, every renderer calls
`pipeline.preflight.reset_mlx_state(drop_f5=True, label=...)` which
dispatches `audio.reset_f5_state()` + `mlx.core.clear_cache()`. F5
holds ~1.35 GB resident — leaking it into the next stage was a
contributing cause of the 2026-05-04 / 2026-05-05 SIGABRT-on-Metal
crashes. The previous code only dropped F5 in long_form's
`image_panels` branch; now ALL renderers (long_form both branches,
footage_only, shorts when `tts_provider == f5_tts`, sports_doc when
`tts_provider == f5_tts`) get the drop.

## _trim_clip_letterbox three-tier short-circuit (Tier-1 fix 2026-05-05)

`_trim_clip_letterbox` now checks the source resolution + grade
filter and picks the cheapest path:

1. **Exact dimension match + no grade** → `-c:v copy` stream copy
   (zero re-encode).
2. **Aspect match within 1% (any source resolution) + no grade** →
   plain `scale + setsar=1` re-encode. Skips the
   `split→gblur sigma=22→overlay` chain entirely. The blurred
   letterbox is a visual no-op when the source already covers the
   output canvas.
3. **Otherwise** (4:3 source, grade requested, or aspect mismatch)
   → full split+gblur+overlay chain.

Previously only tier 1 fired (exact 1920×1080); tier 2 was a new
rubber-duck-recommended addition. Saves 10–40 min on long-form
sleep videos that draw from mixed-resolution archival uploaders
(720p / 1080p / 1440p of the same 16:9 documentary).

## Files

- `pipeline/preflight.py` — shared power_check + reset_mlx_state
- `pipeline/audio.py` — F5-TTS-MLX singleton facade
- `pipeline/tts/f5.py` — F5-TTS-MLX singleton + ref cache + reset_state
- `pipeline/beats.py::transcribe_words` → `pipeline/asr.py::_transcribe_whisper` — whisper path (fallback only)
- `pipeline/render/long_form.py::build_captions_ass` — authored OR whisper alignment, libass output
- `pipeline/render/long_form.py::build_caption_pngs_from_chunks` — PNG fallback (legacy)
- `pipeline/render/long_form.py::final_mux` — accepts `captions_ass` (preferred) or `caption_cues` (legacy)
- `pipeline/render/long_form.py::_trim_clip_letterbox` — three-tier short-circuit
- `historyrecapped/config.yaml::long_form` — channel config (render_mode, caption_align, caption_style, tts_provider)
- `tests/test_long_form_ass_captions.py` — ASS file structure + escape + style coverage
- `tests/test_preflight_and_resets.py` — power_check + reset_mlx_state + per-renderer wiring guards

## Long-form is F5-only at the renderer entry point

`pipeline/render/long_form.py::main` raises `SystemExit` if
`long_form.tts_provider` is anything other than `f5_tts`. The Kokoro
branch was removed from `synth_long_narration` to make this a structural
guarantee — there's no silent fallback path. Other channels' Shorts
paths still use Kokoro / Chatterbox / StyleTTS2 / Indic Parler as
appropriate (those providers live in `pipeline/audio.py::synthesize`),
but **long-form is locked to F5.**
