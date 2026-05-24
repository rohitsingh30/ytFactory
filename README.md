# ytFactory

End-to-end YouTube video generator for the operator's own channels.
Idea → uploaded mp4, fully automated: script (LLM) → cast → AI image
panels → TTS narration → ASR alignment → captions → compose → upload.

Internal tool. The operator drives renders from a Next.js wizard at
`/app/create`; the laptop control plane queues jobs to Firestore; a
Cloud Run **Job** (`ytfactory-render-worker-v2`) executes each render,
calling out to GPU-backed Cloud Run services (TTS, image-gen, ASR).

## Channels

Seven channels share one render pipeline. Any channel can render in any
of three visual modes (AI image-gen / motion video / archival footage)
and either audio mode (TTS narration / sung Suno song). The split is
per-render, not per-channel.

| Channel | Format | Audio | Visual |
|---|---|---|---|
| `mystoriesanimated` | Reddit/AITA/TIFU/wiki/TIH Shorts (13+ niche variants) | Chatterbox TTS | AI image-gen (Z-Image-Turbo, watercolor + ink) |
| `historyrecapped` | War-history Shorts + long-form sleep narrations | Chatterbox TTS | Archival YouTube footage (Shorts), AI panels (long-form) |
| `hindutavaanimated` | Hindi Mahabharat / Ramayan Shorts + long-form | IndicF5 (Hindi) | AI image-gen (Amar Chitra Katha style) |
| `cosmosdecoded` | Physics / space "how we knew" Shorts + long-form | Chatterbox TTS | Footage-only (NASA / ESA / CERN / archive.org) |
| `sportsrecapped` | Football moments + long-form docs | Chatterbox TTS | Tifo-style art + broadcast cut-ins |
| `rhymetimejunction` | Hinglish kids rhymes | Sung Suno song (not TTS) | Image-to-video motion |
| `scrollpulse` | Reddit thread + split-screen gameplay overlay Shorts | Chatterbox TTS | Reddit card + pre-rendered gameplay loop |

**In rotation** (`pipeline.channels.channel_rotation()`):
`mystoriesanimated`, `historyrecapped`, `hindutavaanimated`,
`cosmosdecoded`, `sportsrecapped`.

**Out of rotation** (configs exist, not auto-scheduled):
`rhymetimejunction`.

**In-progress:** `scrollpulse` YAML — channel approved (Q21-Q23);
`pipeline/channels/scrollpulse.yaml` is pending (refactor plan
item P6.1).

Source of truth for any channel rule:
`pipeline/channels/<channel>.yaml` plus the variant overlay at
`pipeline/variants/<channel>/<niche>.yaml` if one applies. Read the
YAML — not the code, not these docs.

## Production stack

- **GCP project:** `ytfactory-prod-v3`
- **Region:** `asia-southeast1`
- **Artifact bucket:** `gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/`
- **State store:** Firestore (`jobs/`, queue, scheduler, rate-limits)

**Deployed Cloud Run surfaces:**

| Service / Job | Type | Role |
|---|---|---|
| `ytfactory-render-worker-v2` | Job | One execution per render; walks the 7 stages |
| `ytfactory-image-z-image-turbo` | Service (GPU) | Production image-gen — Z-Image-Turbo 6B S3-DiT |
| `tts-chatterbox` | Service (GPU) | English TTS — primary for 6/7 channels. The lone GPU service without the `ytfactory-` prefix (audit D3.22; kept since renaming would invalidate `CLOUDRUN_TTS_CHATTERBOX_URL` everywhere). |
| `ytfactory-tts-indicf5` | Service (GPU) | Hindi TTS — `hindutavaanimated` |
| `ytfactory-asr-whisper` | Service (GPU) | faster-whisper word alignment |
| `ytfactory-editing-agent` | Service | Optional polish stage (8th, opt-in) |
| `ytfactory-clone-video-worker` | Service | yt-dlp / clone-video back-end |
| `ytfactory-stats-refresh` | Service | YouTube stats ingest |
| `ytfactory-web-server` | Service | FastAPI control-plane prod surface |
| `ytfactory-web-next` | Service | Next.js admin/wizard UI |

## Render pipeline

Every render walks 7 stages. They are **not strictly linear** —
later stages overlap when GPU + data dependencies allow.

```
rewrite ──► cast ──► prompts ──┬─► images ──┐
                                │            │
                                └─► tts ──► asr ──► compose ──► upload
```

Sequence (Q45):

1. `rewrite` — single LLM call. Fetches real source material (Reddit /
   Wiki / archive / supplied seed) and **synthesizes one engaging
   script**. LLM is editor on real material, not author from nothing.
2. `cast` — smaller LLM call. Reads the script, emits structured
   per-character fields (age, hair, build, clothing, signature prop)
   for character consistency across image panels.
3. `prompts` — per-beat / per-section image-prompt authoring (Z-Image-
   Turbo-shaped: 80-250 word structured, positive-only).
4. `tts` ‖ `images` — parallel. TTS narration synthesised; image panels
   generated. `tts` uses one of `tts_single` (short engine, one call)
   or `tts_chunked` (long engine, ~380-char chunks).
5. `asr` — faster-whisper word alignment over the TTS output (after
   `tts` finishes).
6. `compose` — final mp4 mux (after `images` + `asr`).
7. `upload` — YouTube only. (X/Twitter upload is dead code per Q33.)

Optional laptop-side stages (post-hoc, not in cloud path):
`critic` + `audio_critic` via `pipeline/critique/cloud_poller.py` +
`scripts/cloud_critic_loop.py`.

### Engine + plugin dispatch

The render stage entry point is
`pipeline.render.video.render_via_engines(spec, ...)`. There is no
longer a legacy `render()` — `render_via_engines` is canonical.

```
render_via_engines(spec, ...)              # pipeline/render/video.py
    └─► pick_engine(spec)                  # pipeline/render/engine.py
            └─► render_short OR render_long
                    └─► get_plugin(slot, name)
```

Six plugin slots, declared as Protocols in
`pipeline/render/contracts.py` and self-registering at import:

| Slot | Protocol | Selected by | Dir |
|---|---|---|---|
| `audio` | `AudioSynthesizer` | `audio_mode` + `voice_provider` | `pipeline/render/audio/` |
| `timeline` | `TimelineBuilder` | engine default | `pipeline/render/timeline/` |
| `visualize` | `VisualProducer` | `spec.visual_mode.value` | `pipeline/render/visualize/` |
| `overlays` | `OverlayProducer` | list-valued | `pipeline/render/overlays/` |
| `music` | `MusicComposer` | `spec.music_policy.value` | `pipeline/render/music/` |
| `compose` | `FinalMux` | engine default | `pipeline/render/compose/` |

A new visual mode = one new file + one `register_plugin(...)` call.

## Image model

Production image-gen is **Z-Image-Turbo** (Tongyi-MAI, 6B
**Scalable Single-Stream DiT** (S3-DiT), Qwen3-4B text encoder,
CFG-distilled via Decoupled-DMD). Key facts the refiner and prompts
are calibrated for:

- Negative prompts ignored (`guidance_scale=0.0`). Use positive
  framing only (`"correct anatomy"`, not `"no extra fingers"`).
- Optimal prompt length: **80-250 words**, structured shot + subject +
  age/appearance + clothing/palette + environment + lighting + mood +
  style + safety.
- Lighting tokens are high-impact: `"soft diffused daylight"`,
  `"cinematic warm key light"`, `"noir high-contrast"`,
  `"rim lighting"`.
- Character consistency: rely on cast spec verbatim in each panel
  prompt (no LoRA today).

`pipeline/images/prompt_refiner.py` produces these 80-250 word z-turbo
prompts. Z-turbo is the only image model in production today.

## LLM backends

`YTFACTORY_LLM_BACKEND` selects one of three backends, all
intentional:

| Backend | When |
|---|---|
| `cli` | Laptop dev default — Claude Code CLI |
| `azure_openai` | Cloud worker default — Azure OpenAI |
| `anthropic_sdk` | Cost / quota fallback |

Dispatcher: `pipeline/llm/cli.py`.

## Repo layout

```
/Users/rohit/ytFactory/
├── pipeline/            # channel-agnostic render library
│   ├── channels/        # 6 channel YAMLs (scrollpulse pending)
│   ├── variants/        # niche overlays (mystoriesanimated/, sportsrecapped/)
│   ├── render/          # engine + 6 plugin slots
│   ├── llm/             # rewrite + cast + prompts + 3-backend dispatcher
│   ├── tts/             # cloudrun.py — provider dispatch
│   ├── images/          # image-gen dispatcher + z-turbo prompt refiner
│   ├── captions/        # word PNGs, ASS sentence captions
│   ├── audio/, observability/, critique/, cloud/
│   ├── paths.py         # RenderPaths — canonical per-channel layout
│   ├── channels.py      # channel_rotation()
│   ├── niches.py        # NICHE_CHANNEL map
│   ├── upload.py        # YouTube upload + record writing
│   └── stage_overlap.py # parallel-stage gate
├── control/             # laptop control plane (FastAPI + Firestore)
│   ├── core/            # canonical state modules (jobs, queue,
│   │                    #   scheduler, rate_limit, cloud_run, auth, …)
│   ├── routes/          # FastAPI routers (render, channels, niche, …)
│   └── server_dev.py    # local dev entry
├── cloud/               # Cloud Run services + deploy.sh per service
│   ├── render-worker-v2/    # the render Job
│   ├── image-z-image-turbo/ # production image-gen
│   ├── tts-chatterbox/, tts-indicf5/, asr-whisper/
│   ├── editing-agent/, clone-video-worker/
│   ├── web-server/, web-next/, stats-refresh/
│   ├── _shared/         # auth_setup.sh, otel_init.py, sync.sh
│   └── iam/             # IAM grant scripts
├── web/                 # legacy FastAPI server (Cloud Run web-server)
├── web-next/            # Next.js admin/wizard UI (Cloud Run web-next)
├── scripts/             # laptop CLI entry points + ops utilities
├── tests/               # pytest
├── data/                # cross-channel state: research, telemetry, cache, _bench
├── ai/                  # 18-doc knowledge base — mental model, decision log
├── docs/                # ops + cost docs
├── CLAUDE.md            # agent instructions (loaded into every session)
└── README.md            # this file
```

Per-channel render outputs live at the repo root in `<channel>/` dirs
(`narrations/`, `shotlist/`, `uploads/` tracked; `shorts/`, `long_form/`,
`cache/`, `scratch/` gitignored). Layout is canonical via
`pipeline.paths.RenderPaths` (`pipeline/paths.py:157`).

**In-progress:** moving per-channel artifact dirs from repo root into
`data/<channel>/` is planned but not done yet — `paths.py` is the
single seam to update when this lands.

## Running locally

Local dev is rare — production Cloud Run is the source of truth. Use
local dev only when changing control-plane code.

```bash
# Python 3.12 required (see .python-version)
pyenv install 3.12 && pyenv local 3.12
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-control.txt

# Set env (Azure OpenAI keys, GCP creds, agent token)
source .env

# Run control plane locally
.venv/bin/uvicorn control.server_dev:app --host 127.0.0.1 --port 8765
```

Key env vars (see `cloud/render-worker-v2/deploy.sh:106` for the full
Cloud Run set — that file is the source of truth for the worker
environment):

```text
CLOUDRUN_TTS_CHATTERBOX_URL
CLOUDRUN_TTS_INDICF5_URL
CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL
CLOUDRUN_ASR_URL
CLOUDRUN_EDITING_AGENT_URL
CLOUDRUN_WEB_SERVER_URL
CLOUDRUN_WEB_NEXT_URL
YTFACTORY_LLM_BACKEND          # cli | azure_openai | anthropic_sdk
```

## Tests

```bash
.venv/bin/pytest tests/ -x -q
```

When you fix a bug, add a test that fails when the bug returns
(per `CLAUDE.md`).

## Operating principles

Locked in `ai/onboarding-qa.md` (the cofounder Q&A — read it first):

1. **Gates are repair triggers, not termination signals.** When a
   gate fires, retry the failing piece granularly (one section, one
   beat, one chunk). Don't loosen the gate, don't kill the whole
   render.
2. **LLM is an editor on real material, not an author from nothing.**
   Fetch real sources first; LLM synthesises them into an engaging
   script. One LLM call handles pick + synthesise + write.
3. **Z-Image-Turbo is the canonical image model.** All image-side
   prompting, refinement, and gates are calibrated to z-turbo.
4. **Models are fixed** — don't propose switching TTS / image / LLM
   providers; work within current model behaviour.
5. **All 7 channels must be functional** — no hierarchy of
   importance. Channel-creation is a repeatable workflow.
6. **Negative-framing in prompts often amplifies what you're trying
   to avoid.** Use positive specifications.
7. **MVP target:** all 7 channels producing clean automated
   end-to-end videos. Volume, latency, cost are post-MVP.

## Where to look

For depth beyond this README, the 18-doc knowledge base at
`/ai/` is the project brain. Start with:

- `ai/onboarding-qa.md` — locked Q&A, attack set, operating principles
- `ai/architecture.md` — runtime planes, engine, plugin slots,
  RenderSpec precedence
- `ai/current-system-map.md` — file/dir map with paths
- `ai/refactor-plan.md` — current refactor cycle, what's in-progress
- `ai/decision-log.md` — ADRs (gates as repair triggers, z-turbo
  canonical, render_via_engines canonical, control/core canonical, …)
- `ai/known-fragility.md`, `ai/tech-debt.md`,
  `ai/improvement-opportunities.md` — running backlog

For any **channel rule** specifically:
`pipeline/channels/<channel>.yaml` + the variant overlay if one
applies. Always the YAML. Don't restate channel rules in code or docs.
