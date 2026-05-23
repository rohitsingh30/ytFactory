# ytFactory — agent instructions

This file is loaded into every Claude Code session in this repo.

## What this is

ytFactory generates short-form and long-form YouTube videos end-to-end:
script (LLM) → cast → TTS narration → ASR alignment → AI image panels →
captions → compose → upload. Most heavy work runs on Cloud Run; the
laptop is the control plane.

## Repo layout

- `pipeline/` — channel-agnostic render code
  - `render/engine.py` — dispatches by `RenderSpec.kind` → short or long engine
  - `render/short_engine.py`, `render/long_engine.py` — the two engines
  - `render/video.py` — unified entry: `render_via_engines(spec, …)`
  - `render/spec.py` — typed `RenderSpec` dataclass
  - `render/contracts.py` — Protocol definitions + plugin registry
  - `render/{audio,timeline,visualize,overlays,music,compose}/` — 6 plugin slots
  - `llm/cli.py` — 3-backend dispatcher (`cli` / `azure_openai` / `anthropic_sdk`)
  - `audio/` — TTS facade; real providers under `pipeline/tts/`
  - `images/` — image-gen dispatcher; **production model = Z-Image-Turbo**. Refiner in `pipeline/images/prompt_refiner.py` is calibrated for z-turbo (80-250-word structured prompts, positive-only, lighting-token-heavy).
  - `channels/<channel>.yaml` — per-channel config (source of truth)
  - `variants/<channel>/<v>.yaml` — variant overlays on top of channel config
- `control/` — laptop control plane (FastAPI routes, Firestore queue, scheduler)
- `cloud/` — Cloud Run services (only the ones below are deployed)
  - `render-worker-v2/` — the JOB that executes a render
  - `image-z-image-turbo/` — the production image-gen GPU service
  - `tts-chatterbox/` — English TTS
  - `tts-indicf5/` — Hindi TTS (used by `hindutavaanimated`)
  - `asr-whisper/` — faster-whisper for word alignment
  - `editing-agent/` — optional polish stage
  - `_shared/` — sync.sh, otel_init.py, auth_setup.sh (sourced by every deploy.sh)
  - `iam/` — IAM grant scripts
- `web/`, `web-next/` — FastAPI server + Next.js admin/wizard UI (both Cloud Run)
- `tests/` — pytest
- `scripts/` — laptop CLI entry points + ops utilities
- `<channel>/` — per-channel render output (gitignored): `narrations/`, `scripts/`, `cache/`, `shorts/`

## The render path

1. `control/core/jobs.py::create_job()` writes Firestore `jobs/<id>`
2. `cloud_run.trigger_render_job(<id>)` → `gcloud run jobs execute ytfactory-render-worker-v2`
3. `cloud/render-worker-v2/entrypoint.py::_main_from_firestore` reads the job
4. Stages: rewrite → cast → images → tts → asr → compose → upload
5. The render stage calls `pipeline.render.video.render_via_engines(spec, …)`
6. `engine.pick_engine(spec)` returns `render_short` or `render_long`
7. Engines call plugins from the 6 slots, each plugin registered via
   `register_plugin` at import time
8. Final mp4 → `gs://ytfactory-prod-v3-artifacts/jobs/<id>/short.mp4`

## Channels

In rotation (`pipeline.channels.channel_rotation()`):

- `historyrecapped` — Shorts + long-form sleep history
- `hindutavaanimated` — Hindi mythology Shorts + long-form
- `mystoriesanimated` — AITA / TIFU / wiki / today-in-history Shorts (many variants)
- `sportsrecapped` — sports Shorts + docs
- `cosmosdecoded` — physics+space Shorts + long-form

Out of rotation (configs exist, not auto-scheduled):

- `rhymetimejunction`, `scrollpulse`

Source of truth for any channel rule: `pipeline/channels/<channel>.yaml`
plus the variant overlay if one applies. Read the YAML.

## Cloud Run service env vars

Active in production (everything else listed previously was for retired services):

```text
CLOUDRUN_TTS_CHATTERBOX_URL       (English TTS)
CLOUDRUN_TTS_INDICF5_URL          (Hindi TTS)
CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL  (sole production image model)
CLOUDRUN_ASR_URL                  (faster-whisper)
CLOUDRUN_EDITING_AGENT_URL        (optional 8th stage)
CLOUDRUN_CLONE_VIDEO_URL          (yt-dlp / clone-video-worker)
CLOUDRUN_WEB_SERVER_URL           (web prod surface)
CLOUDRUN_WEB_NEXT_URL             (Next.js admin/wizard UI)
```

`YTFACTORY_LLM_BACKEND` selects the LLM backend (`cli` / `azure_openai` /
`anthropic_sdk`). Cloud worker default is `azure_openai`; laptop default
is `cli`.

## Tests

```bash
.venv/bin/pytest tests/ -x -q
```

## Working principles

- Read the code before writing or editing. Comments and docstrings can
  drift; the function body is authoritative.
- If a stage can't produce its real output, raise an exception. Don't
  return a placeholder (solid-color mp4, empty caption list, silent
  audio) that makes the render look successful when it isn't.
- Channel rules live in `pipeline/channels/<channel>.yaml` plus its
  variant overlay. Don't restate them in code or docs.
- Cloud Run service deploys: every `cloud/<svc>/deploy.sh` sources
  `cloud/_shared/auth_setup.sh` to use Application Default Credentials.
  Don't skip the source.
- When you make a fix that future agents need to know about, write a
  test that fails when the bug returns. A failing test is durable;
  prose claiming a fix is not.

## Cost guardrails (read before changing any `deploy.sh`)

Five non-negotiable rules — all five have a corresponding line item on
a real GCP bill that bit us. Full background in
`docs/cost_optimized_deploy.md` (Iron Rules) and
`docs/optimization_checklist.md` (sections 3.4, 5.6, 5.7).

1. **`min-instances=0`** on every GPU service. Idle = ₹0.
2. **`max-instances=1`** on every GPU service. The render pipeline
   calls each provider serially per chunk; `concurrency=2` already
   covers in-instance pipelining. A second instance just doubles the
   L4 spend.
3. **`--region=asia-southeast1`** on every `gcloud builds submit`.
   Without it, the build runs in the global pool (US Iowa) and the
   image push to `asia-southeast1` AR crosses the Pacific — that's
   the "Artifact Registry Inter Region Egress Intercontinental" SKU.
4. **Double-checked `threading.Lock()` around `_model()` / `_pipe()`**
   in every GPU service. A naked `if _MODEL is None:` races on
   cold-start `/readyz` + first inference call, double-loads into
   VRAM, and OOMs the L4.
5. **Don't bake huge weights into Docker images.** z-image-turbo
   is the exception (justified by the gcsfuse cold-load latency); for
   every other model use the GCS Fuse mount. Bigger image = more
   push egress (rule #3) and more AR storage.

If you change a `deploy.sh` and any of rules 1-3 is violated, the
weekly audit in `cost_optimized_deploy.md` will catch it — but only
on the next audit. Better to not regress in the first place.

