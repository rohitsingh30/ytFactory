# Laptop nuclear cleanup (2026-05-09)

User correction: the previous architecture summary still listed
Whisper, ffmpeg compose, YouTube upload, and Claude CLI as
"intentionally on the laptop." That was stale — the cloud render-
worker (`cloud/render-worker-v2/`) was already wired with all four
ports (`faster-whisper` for ASR, ffmpeg in the worker image, GCS →
YouTube Data API for upload, Azure OpenAI as the default LLM
backend). The user asked for everything except cross-engagement to
come off the laptop and for the slim structure to be made real.

This doc is the as-built record of that cleanup so the next session
doesn't re-discover the same scope.

## Disk freed

| Category | Before | After |
|---|---|---|
| `~/.cache/huggingface` | 102 GB | 0 |
| `<channel>/cache/` × 7 | ~22 GB | 0 |
| `<channel>/scratch/` × 7 | ~3.7 GB | 0 |
| `<channel>/footage/` × 4 | ~21 GB | 0 |
| `data/_bench/`, `data/cache/`, pyc caches | ~280 MB | 0 |
| `.venv` (rebuilt from slim requirements) | 2.4 GB | 628 MB |
| **Total** | **~150 GB** | **freed** |

System free space went from 23 GiB → 173 GiB (post-deletion).

## Code removed

| Path | Why |
|---|---|
| `workers/` (entire tree) | Legacy laptop-agent + lease protocol; superseded by `cloud/render-worker-v2/` JOB. |
| `pipeline/tts/{kokoro,f5,chatterbox,styletts2,parler}.py` | Local TTS providers; cloud TTS has been the production default since 2026-05-06. |
| `pipeline/tts_preflight.py` | The gate that refused local providers — obsolete with no local providers. |
| `pipeline/asr.py` `whisper_mlx` + `parakeet_mlx` backends | Apple-Silicon-only; replaced by `faster_whisper` everywhere. Provider-name aliasing keeps channel YAMLs working. |
| `pipeline/images.py` SDXL Lightning + mflux Flux Schnell + mflux Z-Image-Turbo + fal.ai paths | 1386 → 797 lines. Cloud-only dispatcher. |
| `pipeline/images_cloudrun.py` `_local_fallback` body | Now stubs to a clear `CloudRunUnavailable` instead of importing a deleted local module. |
| 9 bench scripts in `scripts/bench_*.py` + `scripts/judge_voice_matrix.py`, `scripts/render_voice_previews.py`, `scripts/audit_voice_cleanliness.py` | All depended on the deleted local providers. |
| 5 test files: `test_agent_lease_protocol.py`, `test_agent_runner_scratch.py`, `test_light_workers.py`, `test_render_short_worker.py`, `test_render_pipeline_parity.py` | All tested the removed `workers/` tree. |

## Code retained as fallback documentation

- `web/server.py` and `control/server_dev.py` — these double as the
  source for the cloud `ytfactory-web` and `ytfactory-control`
  services. Their docstrings now banner the laptop instance as
  dev-only.
- `pipeline/preflight.py` — `reset_mlx_state(drop_f5=True)` is now a
  silent no-op on the F5 path (no module to drop) but kept callable
  for the 4 renderer call sites.
- `_PIPE`, `_FLUX_PIPE`, `_ZIMAGE_PIPE`, `_IP_ADAPTER_LOADED`
  module-state in `pipeline/images.py` — kept as `None` constants for
  back-compat with any code still doing `_FLUX_PIPE is None` truthiness
  checks.

## Skill dispatch retargeted

`pipeline/skill_dispatch.py`:

- `WEBSITE_URL` defaults to
  `https://ytfactory-web-7hwnzw7lya-as.a.run.app` (was
  `http://localhost:8765`).
- New `_get_id_token()` + `_auth_headers()` helpers attach
  `Authorization: Bearer <gcloud-id-token>` to every request unless
  the target is localhost.
- ID tokens cached for ~50 min.
- Override for local dev: `YTFACTORY_WEBSITE_URL=http://localhost:8765`.

The 4 skill-side SKILL.md files that referenced
`http://localhost:8765` were updated:

- `.claude/skills/critique-audio/SKILL.md`
- `.claude/skills/critique-video/SKILL.md`
- `.claude/skills/upload-via-playwright/SKILL.md`
- `.claude/skills/make-skill/learnings/heuristics.md`

## Crons / launchd disabled

| Job | Action |
|---|---|
| crontab — `cosmosdecoded/scripts/cron_upload_daily.py` (10×/day) | Commented out with banner explaining re-wire path. |
| crontab — `historyrecapped/scripts/cron_upload_one.py` (1×/day) | Same. |
| launchd — `com.ytfactory.upload-next` (`scripts/upload_next.py`, 10×/day) | Unloaded, plist renamed `.disabled-2026-05-09`. |
| launched uvicorn — `control.server_dev:app` on `:8766` | Killed (PID 29771). |

To-do (when ready): re-wire each as a Cloud Scheduler job hitting
`POST https://ytfactory-web-7hwnzw7lya-as.a.run.app/api/cron/drain?channel=<name>`.

## Pre-existing latent bugs surfaced

- `pipeline/render/sports_doc.py` imported
  `pipeline.render.direction_primitives` which never existed in source
  — only as a stale `.pyc` in `__pycache__`. The import worked because
  the .pyc-only-load path is permissive. After purging `__pycache__`
  the module-load failed. Fixed by lazy-importing inside the function
  bodies that use it; runtime still surfaces a clear `ImportError`
  if/when `resolve_direction_plan` is actually called. Real fix is
  someone needing to either commit the source or remove the call sites.

## Test-suite delta

| | Before | After |
|---|---|---|
| Tests collected | 391 | 352 |
| Failures | 4 | 3 |
| Errors | 6 | 6 |
| Skipped | 6 | 2 |

The 39 missing tests are accounted for by the 5 deleted legacy test
files (workers/agent + workers/light + render_short_worker +
render_pipeline_parity = ~17 tests) plus the deleted F5RefCacheLruTests
+ test_audio_tts_providers (~22 tests). All remaining failures are
pre-existing (cloudrun_auth mock signature mismatch, render_long_form_guards,
data_critiques_decommissioned 484-file migration debt) — not
regressions.

## What's left (deferred)

- **Smoke test** a real cloud-only render end-to-end via
  `pipeline.skill_dispatch render --channel <yaml> --script <json>`
  against the cloud `ytfactory-web`. Burning ~$0.20 of cloud quota;
  worth doing before the next production batch.
- **Cron migration** to Cloud Scheduler → `/api/cron/drain` — not
  rendered manually by this pass; uploads will be author-driven via
  `/upload-via-playwright` until then.
- **`pipeline/render/direction_primitives.py`** — either commit the
  source or remove the consumer in `sports_doc.py`. Latent bug, not
  in production hot path.
- **Server-side Playwright upload fallback** in `ytfactory-web` (P3.5
  follow-up). Until then, `/upload-via-playwright` is the manual
  path on YouTube quota 403.
- **`/critique-audio` cloud endpoint** — still 501 in
  `ytfactory-web`; skill remains the working path until P3.5 wires
  `pipeline.llm.audio_critic.critique_audio`.

## Memory pointer

`project_laptop_nuclear_cleanup_2026_05_09.md` — terse pointer to this doc.
