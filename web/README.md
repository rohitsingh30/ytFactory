# `web/` — ytFactory web UI

Self-contained doc for the web layer. The pipeline is documented in the project root's `DESIGN.md`; this is just the website.

```
web/
├── server.py          ← FastAPI app: routes, SSE, subprocess wrapper, auth
├── static/
│   ├── index.html     ← single-page UI (Tailwind CDN + vanilla JS, no build)
│   └── voice_samples/ ← lazy-cached preview wavs (auto-populated, gitignore)
└── README.md          ← this file
```

---

## What it does (in one paragraph)

A user opens `http://<host>:8765/`, picks a niche, picks a voice (38 voices across 9 languages), hits Generate. The browser POSTs to `/api/jobs`, gets a `job_id`, and opens an SSE stream. The backend spawns `pull_stories.py` and then `make_shorts.py` as subprocesses, tails their stdout, and parses the structured progress prefixes (`[1/4]`, `[prompts]`, `[3/4] mflux: generating N images`, `[critic] score=`) into typed JSON events fanned out over SSE. The browser updates a stage-by-stage progress UI, fills in scene cards as `prompts.json` is authored, populates thumbnails as each `img_NN.png` lands, and **evolves a single right-side preview panel** through three states: idle spinner → `<audio>` of the narration (the moment TTS finishes, ~5s in) → `<video>` of the final mp4 (when ffmpeg finishes, ~7 min in). Auth is a single bearer token; tunnel via cloudflared for public URL.

---

## Architecture

### Backend stack

- **FastAPI** (matches your `~/trading` project) + **uvicorn**
- **sse-starlette** for the live event stream
- No DB. Job state is in-memory (`JOBS: dict[str, Job]`); cached artifacts on disk under `data/cache/<slug>/` (the same paths the pipeline already uses)
- No background queue. One asyncio task per job. Multiple jobs can run in parallel, but on this Mac the Flux model serializes anyway because it holds MPS — keep job concurrency = 1 for now
- **No DB migrations to worry about.** Restart the server → in-memory job dict resets. Past mp4s persist on disk so you can still serve `data/shorts/<slug>.mp4` directly.

### Frontend stack (deliberately minimal)

- **Tailwind CDN** (`https://cdn.tailwindcss.com`) — yes the console warns about prod use, that's fine
- **Vanilla JS** with `fetch`, `EventSource`, plain DOM. No bundler, no framework.
- One file: `web/static/index.html`. Inline `<script>`. ~340 lines.
- Why vanilla: matches your `~/trading` HTML+JS pattern; lets the website ship with zero install / build step / Node toolchain. If you eventually want Next.js (matches `~/shofferai`), it's a separate decision — don't drift in.

### Process model on the host

```
                ┌─────── browser ─────────┐
                │  / (SPA)                │
                │  EventSource ◄──────────┼──── SSE stream
                └────────────┬────────────┘
                             │ HTTP+SSE
                             ▼
                ┌──── uvicorn :8765 ──────┐
                │  web/server.py          │
                │   - auth middleware     │
                │   - /api/* endpoints    │
                │   - asyncio.create_task │── per-job background coroutine
                │     run_job(job)        │
                └────────┬────────────────┘
                         │ asyncio.create_subprocess_exec
                         ▼
            ┌──── pull_stories.py ────┐
            │  reddit / wiki / tih    │
            └────────┬────────────────┘
                     ▼ stdout (line by line)
            ┌──── make_shorts.py ─────┐
            │  TTS → beats → prompts  │
            │  → images → compose     │
            │  → critic               │
            └─────────────────────────┘
```

`run_subprocess()` in `server.py` reads stdout line-by-line, calls `parse_stdout_line()` to translate the pipeline's existing structured prefixes into `StageEvent`s, and broadcasts to all live SSE subscribers via per-job `asyncio.Queue` fanout.

---

## Endpoint reference

### Public (no auth)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | `{ok: true, auth_required: bool}` — for tunnel health checks |
| GET | `/auth?token=<TOKEN>` | Sets `yt_tok` cookie, redirects to `/`. With no `token` query param, shows a paste-the-token form. |

### Auth-gated (token via cookie / `?token=` / `Authorization: Bearer`)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | The single-page UI |
| GET | `/static/*` | Static assets (Tailwind already CDN, but voice_samples lives here) |
| GET | `/api/niches` | `{niches: [{key, label, description, color_from, color_to, …}]}` |
| GET | `/api/voices` | `{languages: [{code, label, flag}], voices: [{id, label, lang, accent, tone, sample_url}], default}` |
| GET | `/api/voices/{voice_id}/sample.wav` | Lazy-synth + cache. First call ~3s; subsequent calls hit the cache file. Sample text is per-language (an AITA hook in the voice's native language). |
| POST | `/api/jobs` | Body: `{niche: str, options: {voice: str}}` → `{job_id: str}`. Spawns `pull_stories.py` + `make_shorts.py --tts-voice <id>` and starts emitting SSE events. |
| GET | `/api/jobs/{job_id}` | Snapshot — `{job_id, niche, state, slug, error, stage_started, stage_done, events, beat_prompts, mp4_url}`. Useful for catch-up after a reconnect. |
| GET | `/api/jobs/{job_id}/events` | SSE stream of stage events. See "SSE event schema" below. |
| GET | `/api/jobs/{job_id}/audio` | `narration.wav` (404 until `tts.done`). |
| GET | `/api/jobs/{job_id}/short` | Final mp4 (404 until `done`). |
| GET | `/api/jobs/{job_id}/thumb/{i}` | Per-beat illustrated scene `img_NN.png`. |
| GET | `/api/jobs/{job_id}/closer` | The rendered `closer_panel.png`. |
| GET | `/api/telemetry/overview?hours=N` | KPI tiles: jobs started/finished/success/failed, success rate, avg/p50/p95/max job ms, llm calls/tokens/cost, plus per-niche breakdown. |
| GET | `/api/telemetry/latency?hours=N` | Engineering rollup. `hotspots` (stages by % share of total wall time, p50/p90/max), `job_health` (per-niche success rate + p50/p90), `image_retries` (QC retry % per niche), `error_groups` (stage_errors bucketed by traceback fingerprint), `slowest_recent_job` (timeline of the longest finished job). Default window is 7d. |
| GET | `/api/telemetry/stages?hours=N` | Per-stage rows: count, avg/p50/p95/max ms across `pull`, `rewrite`, `cast`, `tts`, `beats`, `prompts`, `image`, `compose`, `critic`. |
| GET | `/api/telemetry/llm?hours=N` | Per-model rows for claude CLI calls: calls, errors, input/output tokens, cost USD, avg + p95 latency. |
| GET | `/api/telemetry/errors?hours=N&limit=M` | Most recent failures (stage_error and any event with `success=false`). |
| GET | `/api/telemetry/timeline?hours=N` | Hourly buckets: jobs / errors / llm calls — for sparkline-style charts. |
| POST | `/api/_internal/render` | (opt-in) Single-image render through the long-lived in-process diffusion pipe. Activated by starting uvicorn with `YTFACTORY_PERSIST_IMAGE_PIPE=1`. `make_shorts.py` POSTs here when `YTFACTORY_IMAGE_WORKER_URL` is set. Single-tenant `asyncio.Lock` serializes the GPU. Body: `{kwargs: {prompt, seed, out_path, width, height, steps, provider, ip_adapter_image?, ip_adapter_scale?, extra_negative?}}`. |

### Auth model

`YTFACTORY_TOKEN` env var when starting uvicorn enables the gate. Three accept paths checked in `_check_auth`:

1. Cookie `yt_tok=<TOKEN>` (set by `/auth?token=...`, 7-day, HttpOnly, SameSite=Lax)
2. Query `?token=<TOKEN>` — useful for SSE / `<audio>` / `<video>` (browser doesn't send custom headers on those)
3. Header `Authorization: Bearer <TOKEN>` — for programmatic clients

Compared via `secrets.compare_digest`. `/auth` and `/healthz` are open. **Do not expose the server publicly without setting the token** — every job runs subprocesses, so an unauth'd public URL is RCE.

---

## SSE event schema

`GET /api/jobs/{id}/events` is a `text/event-stream`. Each event has:

```
event: <stage>
data: {"job_id": "...", "ts": 1714..., "stage": "...", "status": "...", "message": "...", "data": {...}}
```

Stages and their statuses:

| Stage | `start` | `progress` | `done` |
| --- | --- | --- | --- |
| `pull` | "Pulling story from <niche>" | "[filter] DROP …" / "Selected story: <slug>" | "Wrote N raw, M scripts" |
| `rewrite` | "Rewriting narration" | — | (closes when next stage starts) |
| `cast` | "Casting narrator" | — | (same) |
| `tts` | "Synthesising voice (Kokoro)" | — | "TTS cached" / (auto from `[1/4]` line) |
| `beats` | "Aligning words to audio (Whisper)" | — | "N beats / X.Xs" |
| `prompts` | "Authoring N scene prompts" | — | "Scene prompts ready" |
| `image` | "Generating N images" | "Image i / N" | (single done at chain end) |
| `compose` | "Composing video (ffmpeg)" | — | "Wrote <slug>.mp4" |
| `critic` | "Auto-critiquing the Short" | — | "Score N — <one_line_take>" |
| `done` | — | — | "Short ready" with `data.mp4` URL |
| `error` | — | — | (any failure) |

A heartbeat `event: ping\ndata: {}` is sent every 15s when no real event has fired so the connection doesn't time out.

The frontend keeps both an SSE subscription **and** a 2.5s polling loop on `/api/jobs/{id}` — polling fills in `beat_prompts` (which isn't in the events) and also catches up on disconnect. SSE is the fast path; polling is the safety net.

---

## Telemetry

Modeled on `~/shofferai/apps/web/lib/telemetry.ts` + `apps/web/app/api/admin/telemetry/route.ts`, but file-backed (JSONL) since this project has no Postgres. Lives in two places:

- **Writer**: `pipeline/telemetry.py` — `track(event, category, success, duration_ms, job_id, metadata)` appends one line to `data/telemetry/events-YYYY-MM-DD.jsonl` (daily rotation, single mutex, never raises).
- **Read API**: `/api/telemetry/{overview,latency,stages,llm,errors,timeline}` — read the JSONL on demand and compute rollups in pure Python. No DB, no background aggregation; the file is small enough (a few MB / month / single user) that streaming through it per request beats both the complexity and the latency.

### Where events come from

```
emit(job, StageEvent)                   ← every stage start/done/error
   └── stage_done                       (records ms vs job.stage_started)
   └── stage_error                      (records message TAIL + stage + niche)
   └── job_cancelled                    (when StageEvent.data.cancelled=True;
                                         kept SEPARATE from stage_error so
                                         user-cancellations don't pollute the
                                         success rate)

_run_job_with_telemetry(job)            ← wraps run_job
   └── job_started                      (niche, voice, options at submission)
   └── job_finished  (in finally)       (state, total ms, error, slug)

make_shorts.py (per QC retry)           ← one row per image generate() call
   └── image_attempt                    (beat, attempt, qc=pass|fail, qc_reason,
                                         provider, seed; feeds the retry %
                                         rollup, makes silent retry doubling
                                         visible)

pipeline/llm.call_claude_cli(...)       ← one row per claude CLI call
   └── llm_call                         (model, prompt_chars, in/out tokens,
                                         cost_usd from envelope, latency)
```

`run_subprocess()` propagates `YTFACTORY_JOB_ID` to spawned `pull_stories.py` / `make_shorts.py` so the LLM calls *inside* those subprocesses get attributed to the right job. The `llm.py` wrapper reads the env var and stamps it onto the row.

### Stage_error message handling

Errors store the **last 600 chars** of the captured stdout/traceback (configured in `web/server.py:emit`). Python tracebacks put the exception type + message at the *end*, not the start, so taking the head produces useless fingerprints (`...<5 lin` etc). The `_traceback_fingerprint()` helper in `/api/telemetry/latency` strips file/line numbers from the last non-empty line so two failures with the same root cause group together even if their stacks differ.

### Dashboard UI

`<section id="telemetry">` in `index.html` is shown when `#telemetry-btn` (header) is clicked. The blocks, in render order:

1. **KPI tiles** — jobs / success rate / avg duration / llm calls / llm cost / stage errors
2. **Latency hotspots** — stages by % share of total wall time. Dominant stage (>50%) renders red.
3. **Job health** — per-niche success rate + p50/p90 (tighter than the legacy avg-only view)
4. **Image QC retries** — per-niche retry % from `image_attempt` events. Red >30%, amber >15%
5. **Error groups** — `stage_error` events bucketed by traceback fingerprint with niches/stages/last seen
6. **Slowest recent job — critical path** — vertical waterfall of the longest finished job's stage events
7. **Per niche** — legacy table from `overview.by_niche`
8. **Per pipeline stage** — legacy table with avg/p50/p95/max + gradient bar sized to `avg / max(avg)`
9. **LLM calls** — per-model rollup
10. **Recent errors** — last 50 failures from the `errors` endpoint, with relative time

Time-window dropdown (`#tlm-window`) is `?hours=` for all five endpoints; `loadTelemetry()` runs five `fetch`es in parallel and renders.

### Operational notes

- Override the log dir: `export YTFACTORY_TELEMETRY_DIR=/path/to/dir`
- To wipe local history: `rm data/telemetry/events-*.jsonl` (no service restart needed)
- Long-term retention: out of scope. If the directory grows too big, sweep with `find data/telemetry -name 'events-*.jsonl' -mtime +90 -delete`. Anything > 30d should probably go into a real time-series store (not in scope for v1).
- Never raises: `pipeline/telemetry.py:track()` swallows OSError + JSON errors and logs to stderr. Pipeline correctness must not depend on telemetry.

---

## Persistent image worker (opt-in)

Default: every job's `make_shorts.py` subprocess cold-loads its own diffusion pipe (~3–7 GB, 15–30 s on Apple Silicon). The IP-adapter reference cache (`pipeline/images.py:_IP_REF_CACHE`) also resets per process. Both costs amortize once you cross a handful of jobs/week.

Activate the in-process worker by starting uvicorn with `YTFACTORY_PERSIST_IMAGE_PIPE=1`:

- `lifespan` warms the diffusion pipe on a background task at startup (`_warm_image_pipe()`); server returns 200 immediately, renders that arrive before warmup finishes block on the GPU lock then go straight through.
- `/api/_internal/render` accepts a single render and dispatches it through `pipeline/images.generate()` inside the server. An `asyncio.Lock` (`_IMAGE_GPU_LOCK`) serializes concurrent jobs — the GPU is single-tenant.
- `run_subprocess()` injects `YTFACTORY_IMAGE_WORKER_URL` into the spawned `make_shorts.py` env when the worker is active. `pipeline/images.generate()` reads that env var and calls `_generate_via_worker()` (POSTs JSON over `urllib`), falling back to local generation on any worker error so a server crash doesn't take the pipeline down.

Tunables:

- `YTFACTORY_IMAGE_WORKER_PROVIDER` — provider to warm at startup (default `sdxl_lightning`). Set to whichever provider your active channels actually use.
- `YTFACTORY_IMAGE_WORKER_URL_OVERRIDE` — override the URL the server tells the subprocess to POST to (default `http://127.0.0.1:8765/api/_internal/render`).

Cost of activating: the server process now holds GPU state. Don't enable it on a host where uvicorn is colocated with anything else that wants the GPU.

---

## Frontend file walkthrough

`web/static/index.html` is structured as:

1. `<head>` — Tailwind CDN + small `<style>` block for animations/state classes
2. `<body>`:
   - Sticky `<header>` with logo + back button
   - `<section id="picker">` — niche grid + voice grid + Generate button
   - `<section id="lobby">` — progress bar + 2-col layout (stages + scenes left, evolving preview panel right)
3. `<script>` (inline) — single state object, three async loaders, SSE subscriber, polling loop

Key state object:

```js
const state = {
  niche: null,           // selected niche key
  voice: null,           // selected voice id
  niches: [], voices: [],
  jobId: null,
  jobStart: 0,           // ms timestamp for elapsed clock
  audioShown: false,     // right-panel state machine
  videoShown: false,
};
```

Right-panel state machine — see `showAudioPanel(jobId)` and `showVideoPanel(jobId, mp4Url)`. Once `videoShown` is true, audio is suppressed (you don't want both playing). The panel structure has three sibling divs (`#preview-idle`, `#preview-audio`, `#preview-video`); only one is `display:block` at a time.

Stage card rendering is in `renderStageCards()`. Each stage has a `weight` field that sums to 100 across all stages — the progress bar % is `Σ(weight × phase_factor)` where active=0.4, done=1.0. Image generation gets weight 45 because that's the long pole of total runtime.

---

## Run / deploy

### Run locally

Token is generated once and stored at `/tmp/ytfactory_token`:

```bash
# Set env var when starting uvicorn:
YTFACTORY_TOKEN=$(cat /tmp/ytfactory_token) \
  .venv/bin/uvicorn web.server:app --host 127.0.0.1 --port 8765
```

For a frontend-iteration session, add `--reload --reload-dir web` so HTML/JS edits hot-reload:

```bash
YTFACTORY_TOKEN=$(cat /tmp/ytfactory_token) \
  .venv/bin/uvicorn web.server:app --host 127.0.0.1 --port 8765 \
  --reload --reload-dir web
```

Visit `http://127.0.0.1:8765/auth?token=<TOKEN>` once, the cookie is set for 7 days.

### Deploy publicly

Pipeline runs on the user's M-series Mac (depends on local Flux/Whisper/Kokoro models, ~10 GB, MPS-accelerated). Cloud Run is not viable. Pattern matches `~/shofferai/.github/skills/tunnel/SKILL.md`:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

Prints a public `https://xxx.trycloudflare.com` URL. Ephemeral. For an always-on URL, see Cloudflared "named tunnel" docs in the shofferai skill — needs DNS CNAME setup.

### Process supervision

Currently none — uvicorn dies if the laptop sleeps. Future: a launchd plist that restarts uvicorn + cloudflared on demand.

---

## How to extend (common scenarios)

### Add a new niche

1. Add an entry to `NICHES` in `server.py` with `subreddit` (or `wiki_page`) and the channel YAML path
2. If it's a new pull source, add a branch in `run_job()`'s `pull_args` selection
3. The frontend picks up the new niche automatically via `/api/niches`

### Add a new voice

1. Add to `VOICES` list in `server.py` with `lang` matching one of the `LANGUAGES` codes
2. Make sure the voice id exists in Kokoro: `pipeline.audio._kokoro().get_voices()`
3. (Optional) If the language isn't already in `LANGUAGES`, add it AND add an entry in `SAMPLE_TEXT_BY_LANG` so the preview reads in that language
4. (Optional) Confirm `_VOICE_PREFIX_LANG` in `pipeline/audio.py` covers the voice's language prefix

### Wire a new endpoint

Pattern is in `server.py`:

```py
@app.get("/api/jobs/{job_id}/something")
async def job_something(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404, "job not found")
    ...
```

Auth middleware applies automatically — only `/healthz` and `/auth` bypass.

### Add a new stage event

If you change pipeline stdout, add a regex in `server.py:parse_stdout_line` and a corresponding entry in `STAGES` in `static/index.html`. Both halves must update together — drift = silent UI lies.

### Replace Tailwind CDN with a build

```bash
# In web/static/:
npx tailwindcss -i input.css -o tailwind.css --minify
# Then in index.html, replace:
<script src="https://cdn.tailwindcss.com"></script>
# with:
<link rel="stylesheet" href="/static/tailwind.css">
```

Cosmetic — the CDN warning is the only reason to do this.

---

## What's intentionally NOT here

- No DB. Job history is in-memory; it dies on restart. Past mp4s persist on disk so the data-loss is just the events log.
- No user accounts. Single shared token. Multi-user belongs in `~/shofferai`'s NextAuth pattern, not here.
- No bundled frontend. Vanilla JS by intent — keeps the deploy story simple. If you want Tailwind built or React mounted, that's a deliberate decision and a separate doc.
- No queue (Celery/Redis/RQ). Asyncio + subprocess is enough for one user on one Mac.
- No Cloud Run / Vercel deploy config. Pipeline runs on the laptop; the website goes wherever the laptop is. Cloudflared tunnel = the deploy.

---

## Open frontend follow-ups

These are the natural next changes (already known from this build):

1. **Language tabs above the voice grid** — `/api/voices` already returns the `languages` array. With 38 voices, ungrouped is overwhelming.
2. **Wire the "Run auto-critic" button** — currently a placeholder. Backend needs a `/api/jobs/{id}/critique` endpoint (calls `pipeline/critic.py:critique_short`). Stub the frontend with the expected payload shape; backend session lands the endpoint.
3. **Closer-panel preview as a third right-panel state** — show `/api/jobs/{id}/closer` between audio mode and video mode.
4. **Job history dropdown** — cache `job_id`s in localStorage; offer "Past jobs" → reload snapshot.
5. **Fade transitions on right-panel state swaps** — currently abrupt.
6. **Per-niche channel YAMLs** — TIFU / MaliciousCompliance / etc. all currently fall back to `aita_animated.yaml`. Each should get its own `closer_format` etc.

The first two are high-value; rest are polish.
