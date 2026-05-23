# External Integrations

Every external system this codebase talks to. The env var names referenced below are the contract — actual values live in `.env` (laptop) and Secret Manager / Cloud Run env (cloud); do NOT inline the values here.

GCP project: **`ytfactory-prod-v3`**. Production bucket: `gs://ytfactory-prod-v3-artifacts/`. Region: `asia-southeast1`. The legacy v2 bucket `gs://ytfactory-prod-v2-state/` still appears in some routes (`control/routes/state_routes.py` docstring) — verify which is current before relying on it.

---

## 1. Google Cloud Platform

| Integration                | What for                                                                                              | Auth                                                                                       | Where in code                                                                                                  |
|----------------------------|-------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| Firestore                  | `jobs/<id>` doc, queue's `tasks/` collection, chat sessions, rate-limit counters, telemetry events    | ADC via `google.cloud.firestore.Client(project=GOOGLE_CLOUD_PROJECT)`                       | `control/core/jobs.py:90` (`_FirestoreJobs`); queue + chat sessions follow the same pattern                    |
| GCS                        | All media artifacts: `gs://<bucket>/jobs/<id>/short.mp4` + per-job working dirs; channel-state bucket | ADC; cloud worker uses service account `render-runner@<project>.iam`                       | `pipeline/upload/upload.py` (record reads), `control/storage.py::signed_url`, `pipeline/storage` modules        |
| Cloud Run **services**     | All GPU/CPU services (TTS, image-gen, ASR, editing-agent, clone-video, web, web-next, yt-dlp)         | ID-token from ADC; audience = service URL (`pipeline.cloudrun_auth.get_id_token`)          | `pipeline/tts/cloudrun.py:75-110` (`_service_url`), `pipeline/images/images_cloudrun.py`, `pipeline/asr/cloudrun.py` |
| Cloud Run **JOB**          | `ytfactory-render-worker-v2` — the render-worker JOB that reads Firestore + executes the pipeline    | Service-account `render-runner@…iam.gserviceaccount.com`                                   | `cloud/render-worker-v2/deploy.sh:96`, dispatched by `control/core/cloud_run.trigger_render_job` from `control/core/jobs.py:396` |
| Cloud Build                | Builds Cloud Run service images (`gcloud builds submit --region=asia-southeast1`)                    | ADC                                                                                        | Every `cloud/<svc>/deploy.sh` (must be `--region=asia-southeast1` per Cost Rule #3, `CLAUDE.md`)               |
| Artifact Registry          | Container images for every Cloud Run service                                                          | ADC                                                                                        | Built by `cloud/*/deploy.sh`; pushed during `gcloud builds submit`                                             |
| Secret Manager             | OAuth refresh tokens (`youtube-token-<account>`), `AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY`         | ADC; secrets mounted at `/secrets/<name>/value` on Cloud Run (see `pipeline/upload/upload.py:76` `SECRETS_ROOT`) | `pipeline/upload/upload.py:79` (`_secret_mount_path`), `cloud/render-worker-v2/deploy.sh:105` (`--update-secrets`) |
| OpenTelemetry              | Span + metric export; cold-start + ffmpeg + gcloud + yt-dlp invocations are spans                    | Configured via env: `OTEL_EXPORTER`, `OTEL_SERVICE_NAME`, `OTEL_SERVICE_VERSION`           | `cloud/_shared/otel_init.py`, `pipeline/observability/instrumentations.py`                                     |
| Cloud Run metadata env     | `K_SERVICE`, `K_REVISION`, `CLOUD_RUN_JOB`, `CLOUD_RUN_EXECUTION`, `CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_REGION` — read to identify "am I running in cloud?" | (Read-only env vars provided by Cloud Run runtime)                                         | `control/server_dev.py`, observability modules                                                                  |

Project-wide env vars used to identify project + region:
- `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT` — project id
- `GOOGLE_CLOUD_REGION` / `CLOUD_RUN_REGION` / `YTFACTORY_CLOUDRUN_REGION` — region
- `YTFACTORY_BUCKET` — primary artifact bucket (set to `ytfactory-prod-v3-artifacts` in `cloud/render-worker-v2/deploy.sh:106`)
- `YTFACTORY_STATE_BUCKET` / `YTFACTORY_CACHE_BUCKET` — secondary buckets
- `BIGQUERY_BILLING_DATASET` / `BILLING_ACCOUNT_ID` — used by cost-snapshot in `control/routes/cloud_routes.py`

---

## 2. YouTube

| Integration                | What for                                                                              | Auth                                                                                                  | Where in code                                                                                          |
|----------------------------|---------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| YouTube Data API v3 (upload) | Resumable mp4 upload, set privacy/publish-at, set custom thumbnail                  | OAuth 2.0 Installed App flow per channel; refresh tokens at `~/.config/ytfactory/youtube_token_<account>.json` (laptop) or `/secrets/youtube-token-<account>/value` (Cloud Run) | `pipeline/upload/upload.py` — scopes at `:52`, `upload_short` at `:1601`, `_run_local_server_with_chrome_profile` at `:563` |
| YouTube Data API v3 (analytics) | Per-upload public stats for the dashboard                                          | Public API key in env `YOUTUBE_API_KEY`                                                                | `control/routes/dashboard_routes.py` (per module docstring)                                            |
| Playwright fallback (Studio) | Manual upload path when API quota is exhausted (`HttpError 403 quotaExceeded`)     | Attaches via CDP to a non-default signed-in Chrome profile (`Chrome-Debug`)                            | Run via the `upload-via-playwright` skill — drives `studio.youtube.com` end-to-end. `scripts/playwright_mcp_signed_in.sh` boots the CDP-attached Chrome; `YTFACTORY_CHROME_PROFILE_DIR` selects the profile |
| Cross-engagement (Playwright) | After upload, like + subscribe across every owned channel                          | Same signed-in Chrome profile as the upload path                                                       | `pipeline/research/cross_engage.py:425` (`sync_playwright()`)                                          |

OAuth scopes (`pipeline/upload/upload.py:52`): `youtube.upload`, `youtube.readonly` (sub-count for the Part-2 cliffhanger watcher), `youtube` (videos.rate likes + subscriptions.insert).

---

## 3. LLM providers

The dispatcher at `pipeline/llm/cli.py:524` (`call_claude_cli`) selects by `YTFACTORY_LLM_BACKEND`:

| Provider              | Backend value        | What for                                                              | Auth                                                                                       | Where                                                                                                  |
|-----------------------|----------------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| Azure OpenAI          | `azure_openai`       | **Cloud render-worker default** — rewrite / cast / prompts / critic / prompt-refine | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` (Secret Manager); `AZURE_OPENAI_API_VERSION`; `AZURE_OPENAI_MODEL` (production: `gpt-5.3-chat`); `AZURE_OPENAI_TOKEN_PARAM=max_completion_tokens` for reasoning deployments | `_call_azure_openai` at `pipeline/llm/cli.py:1070`; deployed env in `cloud/render-worker-v2/deploy.sh:90-93` |
| Anthropic (CLI)       | `cli`                | **Laptop default** — same stages; supports vision-aware kwargs (`add_dirs`, `allowed_tools`) | OAuth Pro/Max plan via the `claude` binary on PATH; no API key needed                       | `_call_claude_cli_subprocess` at `pipeline/llm/cli.py:630`                                             |
| Anthropic SDK         | `anthropic_sdk`      | Pay-per-token; separate billing                                       | `ANTHROPIC_API_KEY` env (Secret Manager: `ytfactory-anthropic-key`)                         | `_call_anthropic_sdk` (referenced from `pipeline/llm/cli.py:603`); model id default `claude-opus-4-7` (`:490`) |
| Azure OpenAI Whisper  | (Whisper-specific)   | Clone-a-video ASR fallback                                            | `AZURE_OPENAI_WHISPER_ENDPOINT` + `AZURE_OPENAI_WHISPER_API_KEY` + `AZURE_OPENAI_WHISPER_DEPLOYMENT` + `AZURE_OPENAI_WHISPER_API_VERSION` | `control/routes/clone_video_routes.py:330`                                                              |

Per-stage model override env: `YTFACTORY_MODEL_<STAGE>` (e.g. `YTFACTORY_MODEL_REWRITE=sonnet`). Per-stage output-token budget: `YTFACTORY_MAX_TOKENS_<STAGE>`. Reasoning effort: `YTFACTORY_REASONING_EFFORT_<STAGE>` (Azure gpt-5.x / o1 / o3 only). Cost guard: `YTFACTORY_AZURE_DAILY_CAP_USD`.

---

## 4. Image generation

**Production runs Z-Image-Turbo exclusively** (`pipeline/images/images.py:1` docstring: "All image generation runs on Cloud Run NVIDIA L4 via Z-Image-Turbo. Other providers (FLUX.2 klein, FLUX.2 dev, Qwen-Image, HiDream, all azure_* mirrors) were removed 2026-05-16"). Every channel YAML in `pipeline/channels/` sets `image_provider: cloudrun_z_image_turbo`.

| Provider                       | Service                                | Env var                                                          |
|--------------------------------|----------------------------------------|-------------------------------------------------------------------|
| Z-Image-Turbo (PRODUCTION)     | `cloudrun_z_image_turbo`               | `CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL`                                |
| (Image providers below are still wired in `pipeline/images/images_cloudrun.py` + deploy script but unused in `image_provider` configs as of 2026-05-22:) |||
| Flux2 Klein                    | `cloudrun_flux2_klein`                 | `CLOUDRUN_IMAGE_FLUX2_KLEIN_URL`                                  |
| Flux2 Dev                      | `cloudrun_flux2_dev`                   | `CLOUDRUN_IMAGE_FLUX2_DEV_URL`                                    |
| Qwen-Image                     | `cloudrun_qwen_image`                  | `CLOUDRUN_IMAGE_QWEN_IMAGE_URL`                                   |

Shared image-gen env: `CLOUDRUN_IMAGE_TIMEOUT`, `CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` (cloud worker), `CLOUDRUN_IMAGE_FALLBACK_MODE`. Dispatcher: `pipeline/images/images_cloudrun.py` — see `_generate_cloudrun_z_image_turbo` at `:447`. Optional prompt-refiner pre-step is toggled by `YTFACTORY_PROMPT_REFINER=1` (set in `cloud/render-worker-v2/deploy.sh:106`).

---

## 5. TTS providers

Dispatcher: `pipeline.audio.synthesize` → `pipeline/tts/cloudrun.py` for cloud providers, local modules under `pipeline/tts/` for laptop fallbacks. The cloud worker sets `CLOUDRUN_TTS_DISABLE_FALLBACK=1` so cloud failures hard-error instead of falling back to laptop (which has no GPU there).

| Provider             | Service                       | Env var                                                            | Where                                              |
|----------------------|-------------------------------|--------------------------------------------------------------------|----------------------------------------------------|
| Chatterbox (Resemble AI, MIT)  | `cloudrun_chatterbox` | `CLOUDRUN_TTS_CHATTERBOX_URL`                                      | Cloud Run `tts-chatterbox` service; `cloud/tts-chatterbox/`. Default for English channels |
| IndicF5 (AI4Bharat)            | `cloudrun_indicf5`    | `CLOUDRUN_TTS_INDICF5_URL`                                          | Cloud Run `ytfactory-tts-indicf5`; `cloud/tts-indicf5/`. HindutavaAnimated Hindi voice |
| IndicParler (AI4Bharat)        | `cloudrun_indicparler`| `CLOUDRUN_TTS_INDICPARLER_URL`                                      | Cloud Run `ytfactory-tts-indicparler`; `cloud/tts-indicparler/` |
| F5-TTS-MLX                     | `f5_tts` / `cloudrun_f5` | `CLOUDRUN_TTS_F5_URL` (when wired) — currently routed via generic `CLOUDRUN_TTS_URL` | `pipeline/tts/f5.py` (laptop); cloud variant in `pipeline/tts/cloudrun.py` |
| Kokoro 82M (Apache 2.0)        | `kokoro`              | (laptop-local; Apple Silicon Metal)                                | `pipeline/tts/kokoro.py`. Laptop dev default     |
| StyleTTS2                      | (broken in default venv) | n/a                                                              | `pipeline/tts/styletts2.py` — fails at first call (per `pipeline/tts/__init__.py` docstring) |
| (Higgs, CosyVoice in dispatcher but post-cleanup unused — `pipeline/tts/cloudrun.py:79`) | | |                                                    |

Shared TTS env: `CLOUDRUN_TTS_URL` (generic fallback), `CLOUDRUN_TTS_TIMEOUT` (default 180s), `CLOUDRUN_TTS_DISABLE_FALLBACK`. Channel YAML selects via `tts_provider` (all 6 current channels use `cloudrun_chatterbox` except HindutavaAnimated which uses `cloudrun_indicf5`).

---

## 6. ASR

| Provider                 | What for                                              | Env var                                                          | Where                                                                  |
|--------------------------|-------------------------------------------------------|-------------------------------------------------------------------|------------------------------------------------------------------------|
| Cloud Run Faster-Whisper | Word-level alignment of TTS narration → ASR beats     | `CLOUDRUN_ASR_URL` (set in `cloud/render-worker-v2/deploy.sh:106`); `CLOUDRUN_ASR_TIMEOUT`; `CLOUDRUN_ASR_DISABLE_FALLBACK` | `cloud/asr-whisper/` (service); `pipeline/asr/cloudrun.py` (laptop client); selected via `YTFACTORY_ASR_PROVIDER=faster_whisper` |

---

## 7. Sung audio (Suno)

| Provider          | What for                                       | Env var               | Where                                                                       |
|-------------------|------------------------------------------------|-----------------------|-----------------------------------------------------------------------------|
| Suno API          | Rhyme Time Junction — sung Hinglish nursery rhymes (NOT TTS) | `SUNOAPI_API_KEY`  | `pipeline/tts/song.py`; activated when `RenderSpec.audio_mode == SONG` (`pipeline/render/spec.py:158`) and channel YAML sets `audio_provider: sunoapi` |

Rhyme Time Junction is the only production user. The cloud worker dispatches via the same `MusicPolicy` and audio plugin slots — but the audio plugin returns a sung mp3 instead of a TTS wav.

---

## 8. Content sources (auto-pull)

| Source                 | What for                                                      | Auth                                                                      | Where                                                                         |
|------------------------|---------------------------------------------------------------|---------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| Reddit public JSON API | MyStoriesAnimated (AITA / TIFU / wiki / TIH), ScrollPulse (Reddit threads), discover endpoint topic feed | Public — no API key; some adapters can use a logged-in fetcher selected by `REDDIT_FETCH_BACKEND` | Used by the `make-mystories-short` / `make-reddit-thread` skills + `scripts/pull_stories.py`; channel YAML sets `source_adapter: reddit_video` (mystoriesanimated) |
| Wikipedia              | History Recapped (today-in-history, mining), Cosmos Decoded (physics topics), sportsrecapped (wiki_research step) | Public API; `YTFACTORY_WIKI_MAX_ARTICLE_CHARS` caps payload                | `pipeline/wiki_research` (referenced from `pipeline/channels/sportsrecapped.yaml:5`); also list-page scraper in `control/routes/discover_routes.py:263` |
| archive.org            | History Recapped + Cosmos Decoded footage-only renders        | Public                                                                    | Footage shotlist sources; consumed by `visualize.archival_shotlist` (`pipeline/render/visualize/archival_shotlist.py`) |
| Wikimedia              | Same use case as archive.org — public-domain stills + footage | Public                                                                    | Footage shotlist sources                                                       |
| NASA / NTRS / ESA / CERN / LIGO / LoC / Smithsonian | Cosmos Decoded footage                            | Public                                                                    | Curated shotlists per render — see `cosmosdecoded/shotlist/<slug>.json`        |
| YouTube (CriticalPast, Periscope Film, etc.) | History Recapped archival short footage    | yt-dlp via Cloud Run `CLOUDRUN_YT_DLP_URL` (NEVER for re-upload of source channels) | `pipeline/footage/` adapters; `CLOUDRUN_YT_DLP_DISABLE_FALLBACK` toggle      |
| X / Twitter scraper    | SportsRecapped tweet-reaction Shorts                          | Logged-in Chrome profile via Playwright                                   | `pipeline/social/x_scrape.py:33`, `pipeline/social/x_screenshot.py:21` (`sync_playwright`)             |

---

## 9. Auxiliary services

| Integration                   | What for                                                 | Env var                                                            | Where                                                                 |
|-------------------------------|----------------------------------------------------------|---------------------------------------------------------------------|-----------------------------------------------------------------------|
| Cloud Run editing-agent       | Optional polish stage between compose + upload           | `CLOUDRUN_EDITING_AGENT_URL`, `CLOUDRUN_EDITING_AGENT_TIMEOUT`     | `cloud/editing-agent/`; activated via `proposal.editing.enabled`     |
| Cloud Run clone-video         | Clone-a-video pipeline (download → keyframes → fingerprint) | `CLOUDRUN_CLONE_VIDEO_URL`, `CLOUDRUN_CLONE_VIDEO_TIMEOUT`         | `control/routes/clone_video_routes.py`                                |
| Cobalt (video downloader)     | yt-dlp alternative for some clone-video sources          | `CLOUDRUN_COBALT_URL`                                                | Used by clone-video pipeline                                          |
| Cloud Run yt-dlp              | Server-side yt-dlp                                       | `CLOUDRUN_YT_DLP_URL`                                               | Footage fetch                                                          |
| Cloud Run web / web-next      | FastAPI server + Next.js admin UI                        | `CLOUDRUN_WEB_SERVER_URL`, `CLOUDRUN_WEB_NEXT_URL`                  | `web/`, `web-next/` (each shipped as its own Cloud Run service)       |
| ffmpeg / ffprobe              | All muxing + duration probing                            | `FFMPEG_BIN`, `FFPROBE_BIN`; `YTFACTORY_FFMPEG_WORKERS`              | Every compose plugin                                                  |

---

## 10. Auth + session env (control plane)

`YTFACTORY_AGENT_TOKEN` (bearer for `/api/agent/lease` + `/api/scheduler/tick`), `YTFACTORY_SESSION_SECRET` (PIN session cookie sign), `YTFACTORY_SESSION_TTL_S`, `YTFACTORY_ADMIN_EMAILS` / `YTFACTORY_ADMIN_DOMAINS`, `YTFACTORY_OWNER_IPS`, `YTFACTORY_ALLOWED_HOSTS`, `YTFACTORY_AUTH_REDIRECT_URI`, `YTFACTORY_WEB_OAUTH_CLIENT(_PATH)`, `YTFACTORY_WEBSITE_URL`, `YTFACTORY_COOKIE_SECURE`. Set in deployed Cloud Run env, not in code.

---

## 11. Render-worker dispatch env

Set by `control/core/jobs.py` + read by `control/core/cloud_run.py`:
- `YTFACTORY_RENDER_BACKEND` — `cloudrun` (production) / `sim` (in-process worker) / `laptop` (deprecated)
- `YTFACTORY_CLOUDRUN_JOB` — `ytfactory-render-worker-v2`
- `YTFACTORY_CLOUDRUN_REGION` — `asia-southeast1`
- `YTFACTORY_JOB_ID` — set by the worker entrypoint per execution; LLM telemetry tags this on every span
- `YTFACTORY_QUEUE_BACKEND` — `firestore` / `memory`
- `YTFACTORY_RENDER_MODE` — `real` / `stub` (production: `real`)
