# Long-form (footage-only) renderer — model inventory + memory ceiling

Last updated 2026-05-04. Snapshot of every ML model that loads into MLX /
unified memory during a `scripts/historyrecapped/render_long_form.py` run
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

`render_long_form.py::main()` defaults `caption_align` to `"authored"`
(line 1413). Authored alignment uses the canonical narration text
(`script["narration"]`) plus `ffprobe` durations of cached TTS chunks —
no ASR model is loaded.

Whisper alignment (`caption_align: whisper`) is the legacy fallback. It
loads `whisper-large-v3-mlx-4bit` (1.5 GB) on top of F5-TTS-MLX. Avoid
unless authored alignment fails — which it shouldn't, since the chunk
wavs and chunk text are produced from the same `_split_into_chunks` call
in the same Python process.

## Parallelism — where it's safe, where it's not

Threading is enabled for **CPU-bound** stages where the heavy work is in
ffmpeg subprocesses or PIL C extensions (both release the GIL):

| Stage | Helper | Cap | Speedup measured |
|---|---|---|---|
| Long-form footage trim (`build_video_track`) | `pipeline.parallel.run_parallel` | 4 (default) | ~3-4× |
| Shorts footage trim (`_build_silent_video`) | same | 4 | ~3-4× |
| Caption PNG render (`build_caption_pngs_from_chunks`) | same | 8 | ~5-6× (PIL is faster than ffmpeg) |

Worker cap is `pipeline.parallel._default_workers()` = `min(cpu//2, 4)` for
ffmpeg jobs (each libx264 -preset veryfast already uses ~3 internal threads
so 4×3≈12 saturates the M2 Max perf cores). PIL caption renders use 8.
Override with `YTFACTORY_FFMPEG_WORKERS=N` if running on different hardware.

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
is sometimes actually macOS killing **WindowServer**, not Python.
Incident F743A4C5 (2026-05-04) traced the symptom to:

- `lowPowerMode: 1`
- `displayState: "OFF"` (lid closed / display asleep)
- F5-TTS-MLX or z_image_turbo holding the Metal command queue
- `WATCHDOG: monitoring timed out for service ... WindowServer main thread`
  for 40s+ → kernel kills WindowServer → system logs out / panics

Why it happens: under Low Power Mode the GPU is clocked down. Long
Metal command buffers stretch from ~5–20s to 40s+. WindowServer also
needs the GPU to drive any UI and can't get a slot in time. The kernel
watchdog interprets that as a hung WindowServer and kicks it.

`render_long_form.py::_preflight_power_check` runs before any work and:

- **Hard-rejects** Low Power Mode (`pmset -g` shows `lowpowermode 1`).
- **Warns** when on battery — long-form renders should be on AC. If you
  must run on battery, wrap with `caffeinate -dimsu` (display + idle +
  mouse + system + user wake) NOT just `-i`. Display-off + heavy MLX
  is exactly the failure mode above.

Override with `YTFACTORY_SKIP_POWER_CHECK=1` if you understand the risk
(e.g. desktop M2 Studio where WindowServer is on a different display).

## MLX heap hygiene during long TTS runs

The F5-TTS-MLX `sample()` call accumulates cached graph state across
chunks. On a 200+ chunk long-form run that growth pushes us toward
Metal command-buffer timeouts even without image gen. The chunk loop
in `synth_long_narration` calls `mx.clear_cache()` every
`YTFACTORY_MLX_FLUSH_EVERY` chunks (default 10). This is cheap (single
ms) and prevents the slow leak that turns chunk N+200 into a Metal
grenade.

## Files

- `pipeline/audio.py` — F5-TTS-MLX singleton, ref cache, normalisation
- `pipeline/beats.py::transcribe_words` → `pipeline/asr.py::_transcribe_whisper` — whisper path (fallback only)
- `scripts/historyrecapped/render_long_form.py::build_caption_pngs_from_chunks` — authored caption alignment (default)
- `historyrecapped/config.yaml::long_form` — channel config (render_mode, caption_align, tts_provider)

## Long-form is F5-only at the renderer entry point

`render_long_form.py::main` raises `SystemExit` if `long_form.tts_provider`
is anything other than `f5_tts`. The Kokoro branch was removed from
`synth_long_narration` to make this a structural guarantee — there's no
silent fallback path. Other channels' Shorts paths still use Kokoro /
Chatterbox / StyleTTS2 / Indic Parler as appropriate (those providers
live in `pipeline/audio.py::synthesize`), but **long-form is locked to
F5.**
