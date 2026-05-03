# ytFactory

Automated YouTube Shorts factory. Local-first stack: Kokoro TTS,
mlx-whisper, SDXL-Turbo (MPS), ffmpeg.

## Production channels

Two YouTube channels are the active publishing targets:

| Channel | YAML | What | Aesthetic |
|---|---|---|---|
| **MyStoriesAnimated** | `channels/mystoriesanimated.yaml` | Animated Reddit stories (AITA / TIFU / etc.) | Flat 2D crayon, thick black outlines, pastel fills |
| **SportsStoriesAnimated** | `channels/sportstoriesanimated.yaml` | Animated football (soccer) moments | Tifo Football editorial line-art, cream parchment, muted palette + broadcast cut-ins |

Branding assets (channel icon + banner, sized for YouTube spec) live at
`data/intermediate/<channel>/branding/{avatar.png,banner.png}`.
Other channel YAMLs in `channels/` are research/variant configs — the
two above are what ships.

## Rendering paths

Two parallel rendering paths exist in this repo:

- **Spec-driven path** (`render_from_spec.py`) — declarative
  YAML-described videos. Templates from primitives, auto-layout,
  arbitrary overlays. Used by the `aita_cooking` variant. **Read
  [PIPELINE.md](./PIPELINE.md) and [SPEC.md](./SPEC.md).**
- **Slideshow path** (`make_shorts.py`) — the per-beat-image
  pipeline with Ken Burns zoom + crossfades. Both production channels
  use this path with `image_provider: z_image_turbo`. SportsStoriesAnimated
  layers `kind: footage` beats that cut to real broadcast clips at
  the climactic moment. See the "make_shorts.py path" section below.

For architecture and decisions, see [DESIGN.md](./DESIGN.md).

## Setup

```bash
brew install ffmpeg
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Quick start — spec-driven (preferred)

```bash
# 1. pull raw stories
.venv/bin/python pull_stories.py reddit \
    --subreddit AmItheAsshole --limit 5 --channel aita_cooking

# 2. write scripts (in Claude Code: /make-script)

# 3. fill the channel's spec template with this script's content
.venv/bin/python make_spec.py \
    --channel channels/aita_cooking.yaml \
    --script  data/intermediate/aita_cooking/scripts/<slug>.json

# 4. render
.venv/bin/python render_from_spec.py \
    --spec data/intermediate/aita_cooking/specs/<slug>.yaml
```

Output: `data/shorts/<slug>.mp4`.

To set up backgrounds for the cooking channel, pick a candidate from
the curated queue at `data/cooking_bg_queue.yaml` (status:
`recommended`, top entries are best fit) and pass its `url` plus the
chop windows it suggests:

```bash
.venv/bin/python pull_backgrounds.py \
    --url "https://www.youtube.com/watch?v=<id>" \
    --num-clips 5 --clip-len 25 --check-faces
```

The queue is research-only — entries record metadata + suggested chop
ranges (chapter-derived where possible) without downloading the
source. See `data/_cooking_bg_research.py` for the helper that
populates it. Or skip the queue and pass `--query "..."` to search
fresh.

See [PIPELINE.md](./PIPELINE.md) for the full walkthrough,
[SPEC.md](./SPEC.md) for the spec format reference.

## make_shorts.py path (slideshow + animation)

The older renderer for channels that use per-beat image gen
(slideshow) or AnimateDiff (motion).

```bash
.venv/bin/python make_shorts.py
.venv/bin/python make_shorts.py \
    --script data/intermediate/aita_text/scripts/some-aita.json \
    --channel channels/aita_animated.yaml
```

### Provider matrix

| Knob | Default | Alt | What changes |
|---|---|---|---|
| `asr_provider` | `whisper_mlx` | `parakeet_mlx` | One MLX model for stages 1+5; ~10× faster on M-series. Optional: `pip install parakeet-mlx`. |
| `tts_provider` | `kokoro` | `f5_tts` | Zero-shot voice cloning from a 5-15s reference. Optional: `pip install f5-tts-mlx`. Channel must set `tts_voice` to a WAV path and `tts_ref_text` to its transcript. |
| `image_provider` | `sd_turbo` | `sdxl_lightning` | (slideshow path only) Quality jump for static beat images. |
| `motion_provider` | _(unset)_ | `animatediff_toonyou`, `animatediff_lcm` | **Replaces slideshow with continuous 2D animation per beat.** AnimateDiff + ToonYou pulls ~3.7 GB on first run. See `channels/aita_animated_motion.yaml`. |

## Skills (slash commands)

Available inside the Claude Code session:

- **`/make-script`** — pull from Reddit / Wikipedia / TIH / YouTube and
  write 50-80 word hook-first narrations.
- **`/make-movie-short`** — directed cinematic version: writes a
  cast + shot list + per-shot prompts on top of the script.
- **`/critique-video`** — viewer-style frame-by-frame reaction to a
  rendered Short.

## Telemetry dashboard

Every pipeline run is recorded as JSONL at `data/telemetry/events-YYYY-MM-DD.jsonl` (one file per UTC day, append-only). The web UI surfaces the rollups at the **📊 Telemetry** button in the header. Time window is selectable (1h / 24h / 7d / 30d / 1y).

The headline panel is **Latency hotspots** — pipeline stages ranked by cumulative wall time with a % share bar. The dominant stage colors red when it's >50% of total time so the bottleneck is impossible to miss. Below it: per-niche success rate with p50/p90, image QC retry % per niche (silent retry doubling shows up as red here), error groups bucketed by traceback fingerprint (collapses N raw failures into a few root causes), and a critical-path waterfall of the slowest recent job. The classic per-stage / LLM / errors tables are still rendered below the engineering view.

Events written:

| Event | Category | Fired by | Records |
| --- | --- | --- | --- |
| `job_started` / `job_finished` | `job` | `web/server.py` | end-to-end ms, niche, success, error |
| `stage_done` | `pipeline` | `emit()` in `web/server.py` | per-stage ms (pull, rewrite, cast, tts, beats, prompts, image, compose, critic) |
| `stage_error` | `pipeline` | `emit()` | failure message **tail** (last 600 chars — Python tracebacks put the exception at the end) + which stage |
| `job_cancelled` | `pipeline` | `emit()` | user-cancellations (browser closed / superseded). Tracked separately from `stage_error` so they don't pollute the success rate |
| `image_attempt` | `pipeline` | `make_shorts.py` (per QC retry) | beat, attempt, qc pass/fail, qc reason, provider, seed — feeds the retry % rollup |
| `llm_call` | `llm` | `pipeline/llm.py:call_claude_cli` | model, latency, input/output tokens, **notional** cost (CLI envelope's `total_cost_usd` — API list price, not billed on Pro/Max), success |

API: `GET /api/telemetry/{overview,latency,stages,llm,errors,timeline}?hours=N`. The writer (`pipeline/telemetry.py`) is fire-and-forget — telemetry failures never break a render. Override the log dir with `YTFACTORY_TELEMETRY_DIR` if you want it on a different volume.

## Latency tuning

The image stage dominates wall time (97% on a typical job). Three opt-in knobs:

| Env var | Effect |
| --- | --- |
| `YTFACTORY_PERSIST_IMAGE_PIPE=1` | Server warms a single diffusion pipe at startup and exposes `/api/_internal/render`. `make_shorts.py` POSTs each render through HTTP instead of cold-loading its own pipe. Saves the 15–30 s cold load per job. Falls back to local generation on any worker error. Default off because the server then holds 3–7 GB of GPU state. |
| `YTFACTORY_IMAGE_WORKER_PROVIDER` | Provider to warm at startup (default `sdxl_lightning`). Set to whichever provider your active channels use. |
| `YTFACTORY_TELEMETRY_DIR` | Move the JSONL log to a different volume. |

Per-channel provider tuning: `scripts/bench_image_providers.py --providers sdxl_lightning z_image_turbo --steps-sweep 2 4 6 8 --channel channels/aita_animated.yaml` — runs the same prompt across every (provider × steps) cell, reports cold/warm/per-step, and prints a per-provider Pareto pick (fastest + quality-within-30%) plus an overall winner. JSON summary lands at `data/_bench/<run_id>/summary.json`.

Word caption PNGs (~150 per video, ~7–15 s of CPU) pre-render in a background thread during image gen via `compose.prerender_word_captions` — the wipe-then-render contract is preserved by exposing `compose.wipe_stale_per_beat_artefacts` and calling it from `make_shorts.py` BEFORE the prerender thread starts. Compose's word-PNG render is now idempotent on file presence.

## Tests

```bash
.venv/bin/python -m unittest discover tests/
```

Coverage: spec interpreter (resolve_value, time tokens, positions,
template expansion), `make_spec` substitution, `pipeline/llm` JSON
parsing, `pipeline/script_check` shape validators,
`pipeline/visualizability` scoring, `pipeline/quality_gate` image
checks, `pipeline/prompts` schema validation, `pipeline/critic`
regen-corrections, `pipeline/cast` loader. See `tests/README.md`.

## Layout

- `sources/` — source adapters (reddit_api, wikipedia, today_in_history, youtube_video)
- `pipeline/` — internal helpers (audio, beats, captions, images, compose, …)
- `pull_stories.py` — stage 1 orchestrator
- `pull_backgrounds.py` — bg-loop puller (channel-specific)
- `make_spec.py` — stage 3 (script + channel template → resolved spec)
- `render_from_spec.py` — stage 4 (spec → mp4)
- `make_shorts.py` — older slideshow/animation orchestrator
- `channels/` — per-channel YAMLs (manifest + spec template)
- `data/intermediate/<channel>/` — `raw/` + `scripts/` + `specs/`
- `data/cache/<slug>/` — per-short cached intermediates
- `data/shorts/` — final .mp4 outputs
- `assets/` — reference images, fonts, cooking_loops/
- `data/cooking_bg_queue.yaml` — curated queue of YouTube source candidates for cooking backgrounds (metadata-only, with suggested chop ranges)
- `data/_cooking_bg_research.py` — yt-dlp helper that populates the queue without downloading video files
- `tests/` — unit tests for the spec interpreter
- `.claude/skills/` — slash commands (`/make-script`, `/critique-video`, …)
