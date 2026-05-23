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

## Post-render: critique is the default

After **every** completed cloud render, the default flow is:

1. **Download** the mp4 to the canonical laptop path:
   ```
   gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/short.mp4
   → data/<channel>/<niche_state_dir>/shorts/<slug>.mp4
   ```
   Example: a mystoriesanimated/tifu render at job `24c5887a...` lands
   at `data/mystoriesanimated/reddit_tifu/shorts/<slug>.mp4`.
   Use the `niche_channel_map()[<variant>][0]` value for the niche dir
   (see `pipeline/channels.py::niche_channel_map`).
2. **Run `/critique-video <path>`** on the downloaded mp4. The skill
   samples 1fps + 0.3s/0.8s hook frames, watches through 17 lenses,
   writes `data/critiques/<slug>.md` with a per-frame fix table +
   class-of-bug system fixes.
3. **Address the CLASS-OF-BUG fixes** before the next render —
   one-offs can stay logged; class-of-bug fixes lift the next 100
   shorts, not just this one.

Don't ship without critiquing. Don't queue more renders without
addressing the class-of-bug findings from the previous critique.

See `.claude/skills/critique-video/SKILL.md` for the full lens list.

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
2. **`max-instances` matches the caller's fan-out shape.** Pick ONE
   of these patterns and document the choice in the `deploy.sh` next
   to the flag:
   - **Serial-only services** (TTS chunks, ASR chunks, anything the
     caller hits with a single in-flight request) → `max-instances=1`.
     A second instance would just double L4 spend.
   - **Fan-out services** (image gen — called in parallel from
     `pipeline/render/visualize/ai_beat_slideshow.py` and
     `pipeline/render/shared/long_form_lib.py::_generate_panel_stills`
     via `ThreadPoolExecutor`) → `max-instances=4`. Cost ceiling
     is total GPU-seconds, NOT instance count: 4 parallel for 9 min
     costs the same as 1 serial for 36 min, but doesn't blow the
     worker's 60-min Cloud Run `--task-timeout`. The 2026-05-23 SIGKILL
     of job `a0aac53e` was caused by drift between the comment
     (which said `max-instances=1`) and the actual deployed config
     (`max-instances=4`) on `ytfactory-image-z-image-turbo`: the
     PIPELINE was serial because the developer trusted the comment,
     so all 60 panels queued through a single instance and the
     render hit the task-timeout. Now the deploy.sh comments
     explicitly call out which class each service belongs to.
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

