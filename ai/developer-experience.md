# Developer Experience

How a human + AI agents interact with the codebase. This doc is the lookup table for "where do I run things from?" — see `ai/product-observations.md` for what the system *does* and `ai/debugging-notes.md` for what to do when it breaks.

---

## Python environment

- **Pin:** Python 3.12 (`/Users/rohit/ytFactory/.python-version`).
- **Venv:** `.venv/` at repo root. **Always invoke via `.venv/bin/python`** (absolute path in plists; explicit in `Makefile` via `PY = .venv/bin/python`). This is mandatory for the launchd daemons because launchd doesn't inherit shell init — relative `python` calls silently resolve to the wrong interpreter.
- **PYTHONPATH:** `PYTHONPATH=.` from repo root (set explicitly in plist `ProgramArguments`).

---

## Makefile (`/Users/rohit/ytFactory/Makefile`)

Minimal — intentionally not a build system. Only 4 entry points:

- `make help` — list targets.
- `make critique-runner` — start the laptop critique-runner daemon (long-poll Firestore, drive Claude/Copilot, run hard gates, push to main on green). Equivalent to `.venv/bin/python -u scripts/critique_runner.py`.
- `make critique-once` — same daemon but exits after one critique. Smoke-test mode.
- `make critique-runner-tests` — runs `tests/test_critique_runner.py`, `tests/test_critique_gates.py`, `tests/test_critique_routes.py`.
- `make test` — full pytest suite, scoped to `tests/` with `--ignore=cloud`. Cloud-service tests live next to each service and have their own venvs.

---

## Test runner

```bash
.venv/bin/pytest tests/ -x -q
```

`-x` halts on first failure (per CLAUDE.md "Working principles"). `--ignore=cloud` is set in `make test` to keep the laptop venv from collecting `cloud/<svc>/test_*.py` files (they need their own service-local venvs).

---

## Top-level `scripts/` entry points

One-line summary each. Read the docstring for usage flags.

- **`scripts/trigger_one_render.py`** — fires one MyStoriesAnimated AITA render via the Cloud Run job. Self-contained AITA story embedded in `notes` so the worker doesn't have to fetch Reddit (Cloud Run egress to reddit.com is unreliable).
- **`scripts/pull_stories.py`** — unified source-mining entry. Wraps per-niche adapters in `sources/`; produces `data/intermediate/<channel>/{raw,scripts,cast}/<slug>.json`.
- **`scripts/bulk_upload.py`** — batch upload — walks `data/intermediate/*/scripts/*.json`, finds matching mp4s + raw, skips already-uploaded; dry-run by default, `--apply` to commit.
- **`scripts/cloud_critic_loop.py`** — laptop daemon polling Firestore for cloud renders missing a vision-aware critic verdict. The Azure-backed cloud worker writes `critique.verdict = "UNGATED"`; this daemon runs the claude-CLI-backed critic locally and writes the verdict back. Closes the upload gate.
- **`scripts/critique_runner.py`** — separate critique-runner daemon (drives Claude/Copilot through the chat-panel flow).
- **`scripts/cloud_daily_snapshot.py`** — snapshots cloud state daily.
- **`scripts/sync_state_to_gcs.sh`** — laptop → GCS state sync.
- **`scripts/sync_upload_records_to_gcs.py`** — pushes upload records to GCS.
- **`scripts/audit_idle_costs.py`** — daily idle-cost audit. Catches services with `min-instances >= 1` or services running continuously > 1h. Enforces CLAUDE.md cost guardrails rules 1-2. Run with `GCP_PROJECT=ytfactory-prod-v3 python scripts/audit_idle_costs.py`.
- **`scripts/laptop_cleanup.py`** — wipes legacy local state that lives in the cloud now (post-migration disk reclaim). Model weights and per-channel branding stay put.
- **`scripts/coverage_gate.py`** — diff-coverage gate for the `/test-coverage` skill. Runs in <30s by targeting only files in `git diff HEAD`.
- **`scripts/migrate_channel_yamls.py`** — channel YAML migration helper.
- **`scripts/seed_channel_niches.py`** — seeds niche-variant YAMLs for a channel.
- **`scripts/clone_voice.py`** — voice-clone helper (TTS ref-audio generation).
- **`scripts/pull_hindi_voices.py`** — fetches Hindi voice refs for IndicF5.
- **`scripts/build_song_samples.py`** — builds Suno song reference samples.
- **`scripts/setup_x_credentials.py`** — X/Twitter credential setup (X upload path is currently dead per Q33).
- **`scripts/setup_ytdlp_cookies.sh`** — sets up yt-dlp cookies for restricted YouTube downloads.
- **`scripts/serve.sh`** / **`scripts/serve_cloud.sh`** — local + cloud server bring-up shims.
- **`scripts/playwright_mcp_signed_in.sh`** — Playwright MCP launcher attached to the user's signed-in Chrome profile (used by `/upload-via-playwright`).
- **`scripts/bootstrap_new_project.sh`** — new-project bootstrap.
- **`scripts/lint_skill_md.py`** — lints the `.md` frontmatter of skill files under `.claude/skills/`.

Per-channel scripts: `scripts/historyrecapped/`, `scripts/sportsrecapped/`. Each holds channel-specific build helpers (e.g. `historyrecapped/scripts/build_100footage.sh` for the all-archival Shorts path referenced in the channel YAML).

Ops scripts: `scripts/ops/upload_next.py` (the scheduled-upload entry — fired by `com.ytfactory.upload-next.plist` 10x/day) and `scripts/ops/bulk_render_queue.py`.

---

## Pipeline module entry

```bash
.venv/bin/python -m pipeline.render.long_form ...
```

The render-worker subprocess uses this form (verified at `pipeline/render/video.py:735` — `"pipeline.render.long_form exited with code …"`). Top-level direct invocations of `pipeline.render` are rare; renders are normally fired from the UI → Firestore job → Cloud Run job. For a smoke test, `scripts/trigger_one_render.py` is the entry point.

---

## Launchd daemons (`control/com.ytfactory.*.plist`)

5 daemons. Each is a per-user LaunchAgent — installed via `cp control/com.ytfactory.<name>.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.ytfactory.<name>.plist`.

- **`com.ytfactory.cloud-critic.plist`** — starts `scripts/cloud_critic_loop.py` on login, keep-alive on crash. Closes the cloud render → vision-critic gap (cloud worker can't run claude CLI → emits `UNGATED` → laptop daemon polls Firestore + runs local critic + writes verdict back). Throttle 30s. **Pins `GOOGLE_CLOUD_PROJECT=ytfactory-prod-v2`** — see open question Q-014 (may be stale post-v3 migration).
- **`com.ytfactory.cloud-snapshot.plist`** — fires `scripts/cloud_daily_snapshot.py` on schedule.
- **`com.ytfactory.critique-runner.plist`** — long-running critique-runner daemon (the `make critique-runner` target). Works against production Firestore so the live website's chat panel can talk to the laptop.
- **`com.ytfactory.state-sync.plist`** — laptop → GCS state sync via `scripts/sync_state_to_gcs.sh`.
- **`com.ytfactory.upload-next.plist`** — fires `scripts/ops/upload_next.py --count 1` at 10 scheduled slots per day (07:00, 09:24, 11:48, 14:12, 16:36, 19:00, 21:24, 23:48, 02:12, 04:36 local). No timezone field — set the Mac to the desired TZ (the comment in the plist recommends IST). All paths absolute. Sources `.env` explicitly (launchd ignores user shell init).

**Pattern observation:** All 5 plists set `EnvironmentVariables` with absolute PATH including `/Users/rohit/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, plus `CLOUDSDK_CONFIG` + `GOOGLE_CLOUD_PROJECT`. Pre-fix attempts to skip these silently failed with "command not found" inside launchd's minimal PATH (audit D3.18). Don't remove these blocks even if they look redundant.

Logs land in `~/Library/Logs/ytfactory/<daemon>.{stdout,stderr}.log` (cloud-critic) or `/tmp/upload_next.log` (upload-next).

---

## Cloud service deploy pattern (`cloud/<svc>/deploy.sh`)

Each Cloud Run service has its own `cloud/<svc>/deploy.sh`. Per CLAUDE.md "Working principles":

- Every deploy.sh sources `cloud/_shared/auth_setup.sh` (ADC). Don't skip the source.
- Every `gcloud builds submit` uses `--region=asia-southeast1` (CLAUDE.md cost guardrail rule 3).
- Every GPU service sets `min-instances=0`, `max-instances=1` (rules 1-2).
- `cloud/_shared/sync.sh` and `cloud/_shared/otel_init.py` are shared utilities sourced by deploys.

Services: `render-worker-v2`, `image-z-image-turbo`, `tts-chatterbox`, `tts-indicf5`, `asr-whisper`, `editing-agent`, `clone-video-worker`, `cobalt-api`, `stats-refresh`, `web-server`, `web-next`. (The `_bench/` directory under `cloud/` is excluded from the laptop pytest collector via `conftest.py:21`.)

**Env var source of truth:** `cloud/render-worker-v2/deploy.sh:90` — see MEMORY.md "Render-worker env truth." `gcloud run jobs update --update-env-vars` writes are wiped on the next deploy.

---

## Skill ecosystem (`.claude/skills/`)

33 skills authored for this repo. They are slash commands the user types (`/make-mystories-short`, `/critique-video`, `/voice-bench`, etc.). Each skill is a `.md` frontmatter + body file under `.claude/skills/<name>/`. Linted by `scripts/lint_skill_md.py`. Categories:

- **Authoring** (`/make-*`): `make-mystories-short`, `make-hindutava-short`, `make-hindutava-long`, `make-history-short`, `make-sleep-history`, `make-cosmos-short`, `make-cosmos-long`, `make-cosmos-decoder`, `make-sports-doc`, `make-football-explainer`, `make-rivalry-recap`, `make-ranking`, `make-top10`, `make-tweet-reaction`, `make-reddit-thread`, `make-rhyme`, `make-katha`, `make-movie-short`, `make-script`.
- **Critique** (`/critique-*`): `critique-video`, `critique-audio`. Polish: `editing-agent`.
- **Cloning / format research**: `clone-video-format`.
- **Quality / tuning**: `voice-bench`, `tune-ai-extraction`, `image-edit`, `test-coverage`.
- **Operations**: `upload-via-playwright`, `parallel-render`, `create-youtube-channel`.
- **Process / docs**: `update-docs`, `make-skill`, `create-handoff-eval`, `ingest-critiques`.

The skill list in any session is surfaced via system-reminder. Authoring a new skill is itself a skill (`/make-skill`).

---

## `/app/create` UI workflow (the canonical render trigger)

Documented in `ai/product-observations.md` "Operator surfaces." Summary path: pick **Mode** (Channel-focused) → pick **Channel** → fill **Customize** (form, niche, topic [manual or Auto-generate], voice, audio mode, visual source, music, advanced) → submit → routes to `/app/render/<jobId>`.

Per Q36, every render starts here. Cron-fired renders do not exist.

---

## File-system conventions

- **`pipeline/channels/<channel>.yaml`** — source of truth per channel. Read the YAML, not the docs.
- **`pipeline/variants/<channel>/<v>.yaml`** — variant overlays on top of the channel YAML.
- **`<channel>/`** — per-channel render output (gitignored): `narrations/`, `scripts/`, `cache/`, `shorts/`, `long_form/`, `branding/`, `uploads/`, `learnings/`.
- **`audit_data/`** — historical per-render summaries from pre-fix renders (Q-013 in open-questions). Use as historical reference only.

---

## Cost guardrails (CLAUDE.md)

Five non-negotiable rules — read them before changing any `deploy.sh`:
1. `min-instances=0` on every GPU service.
2. `max-instances=1` on every GPU service.
3. `--region=asia-southeast1` on every `gcloud builds submit`.
4. Double-checked `threading.Lock()` around `_model()` / `_pipe()` in every GPU service.
5. Don't bake huge weights into Docker images (z-image-turbo is the documented exception).

Weekly audit: `scripts/audit_idle_costs.py`.
