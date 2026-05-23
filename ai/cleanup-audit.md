# Cleanup audit — repo-wide, READ-ONLY

Charter rule: every file must justify its existence (current caller / current
consumer / charter-required). Scope: every file/dir that is NOT already
covered by `/ai/refactor-plan.md` Part A → Principle 1 (which already
catalogues the duplicate `control/*.py` ↔ `control/core/*.py` pairs, the
duplicate `control/{agent,dashboard,niche,render,scheduler}_routes.py`,
the legacy `pipeline/render/video.py::render()`, the 4 dead env URLs in
the worker deploy, the `prod-v2` references, etc).

Method: grep for every candidate's callers across `pipeline/`, `control/`,
`web/`, `cloud/`, `scripts/`, `tests/`, `.claude/skills/`. Cite caller
files when found, or cite "0 callers" when not. Per task brief, did not
deep-read `pipeline/llm/*`, `pipeline/render/*`, `pipeline/images/*`,
`pipeline/tts/*` (W1/W2 are touching those concurrently).

---

## 1. Confirmed waste (recommend delete)

These have zero current callers, demonstrably stale content, or refer
to deleted features. Cite-of-evidence in the "Why" column.

### 1a. Stale per-channel scripts directories — `scripts/historyrecapped/` + `scripts/sportsrecapped/`

The 11 files in `scripts/historyrecapped/` and 6 files in
`scripts/sportsrecapped/` are pre-generalization residue from before
channel content was reorganized into `<channel>/` dirs. Skills reference
the new paths (`historyrecapped/scripts/render_footage_only.py`,
`sportsrecapped/scripts/find_b_roll.py`), not these.

| File | Why waste | Evidence |
|---|---|---|
| `scripts/historyrecapped/auth.py` | 0 external refs | grep shows only self-ref |
| `scripts/historyrecapped/author_panels.py` | 0 external refs | grep |
| `scripts/historyrecapped/branding.py` | 0 external refs | grep |
| `scripts/historyrecapped/build_100footage.sh` | Self-declared legacy in README.md:103 ("pre-generalization scripts kept for reference… Don't add new work to them") | scripts/historyrecapped/README.md:103 |
| `scripts/historyrecapped/build_long_form_thumbnail.py` | 0 external refs | grep |
| `scripts/historyrecapped/download_long_form_sources.py` | 0 external refs | grep |
| `scripts/historyrecapped/final_v2.py` | Self-declared legacy in README.md:103 | same |
| `scripts/historyrecapped/regen_audio_caps.py` | Self-declared legacy in README.md:103 | same |
| `scripts/historyrecapped/upload_long_form.py` | 0 external refs; reads `historyrecapped/config.yaml` which DOES NOT EXIST (channel YAML lives at `pipeline/channels/historyrecapped.yaml`) | grep + `ls historyrecapped/config.yaml` → ENOENT |
| `scripts/historyrecapped/upload.py` | Same as upload_long_form.py — stale config.yaml path | grep |
| `scripts/historyrecapped/README.md` | Documents stale workflow: references `workers/heavy/render_short.py` (deleted per refactor-plan Phase 5 + Q32) and `historyrecapped/scripts/render_footage_only.py` that doesn't exist in either location | scripts/historyrecapped/README.md:8, 53 |
| `scripts/sportsrecapped/avatar.py` | 0 external refs | grep |
| `scripts/sportsrecapped/branding.py` | 0 external refs | grep |
| `scripts/sportsrecapped/find_b_roll.py` | Skills reference `sportsrecapped.scripts.find_b_roll` (a different path under the channel dir, gitignored) — not `scripts/sportsrecapped/find_b_roll` | `.claude/skills/make-football-explainer/SKILL.md:135` |
| `scripts/sportsrecapped/find_commentary_takes.py` | Same; skills reference `sportsrecapped.scripts.find_commentary_takes` | grep |
| `scripts/sportsrecapped/find_match_clips.py` | Same | grep |
| `scripts/sportsrecapped/research_rivalry.py` | Skills reference `sportstoriesanimated/scripts/research_rivalry.py` (different channel slug, different dir) | `.claude/skills/make-skill/SKILL.md:121` |

**Recommendation**: delete both `scripts/historyrecapped/` and
`scripts/sportsrecapped/` directories entirely. The README in
historyrecapped/ EXPLICITLY says these are pre-generalization scripts.

### 1b. Cloud `_shared` script with zero callers

| File | Why waste | Evidence |
|---|---|---|
| `cloud/_shared/redeploy_for_otel.sh` | 0 callers across cloud/, scripts/, Makefile, docs/, CLAUDE.md. The other _shared scripts (auth_setup.sh, sync.sh, otel_init.py, submit_build.sh) ARE called by every deploy.sh — this one isn't. Likely a one-time migration script left behind. | grep `redeploy_for_otel` → 0 hits outside the file itself |

### 1c. Cloud `iam/` scripts with zero callers (subset)

The `cloud/iam/` directory is referenced in deploy.sh comments as
"recovery commands the operator runs by hand," so most files there are
justified as one-time IAM grant scripts. However:

| File | Why waste | Evidence |
|---|---|---|
| `cloud/iam/grant_telemetry.sh` | 0 refs anywhere outside its own file | grep returned only self-match |
| `cloud/iam/grant_per_service_telemetry.sh` | 0 refs anywhere | grep |
| `cloud/iam/create_per_service_sas.sh` | 0 refs anywhere | grep |
| `cloud/iam/wire_token_health_cron.sh` | 0 refs anywhere | grep |

(KEEP: `cloud/iam/grant_token_writeback.sh`, `grant_web_runner.sh`,
`grant_web_runner_secrets.sh`, `verify_web_runner.sh` — all of those are
referenced from `pipeline/upload/upload.py`, `web/server.py`,
`cloud/web-server/deploy.sh`, or `cloud/clone-video-worker/deploy.sh`.)

### 1d. Top-level `conftest.py` is at risk of merge into `tests/conftest.py`

Two conftest.py files exist. They are NOT the same file:

| File | Lines | Role |
|---|---|---|
| `/conftest.py` | 93 | (1) `collect_ignore_glob = ["cloud/_bench/*"]` (only used at REPO root level), (2) googleapiclient sys.modules restore fixture |
| `/tests/conftest.py` | 247 | Different googleapiclient + youtube DIR guard fixtures, focused on monkey-patch leak prevention |

Root conftest IS needed for the `collect_ignore_glob` because the
`cloud/_bench/` collection-skip can only be configured at the repo
root (per pytest's discovery rules — a tests/conftest.py can't ignore
siblings). The googleapiclient fixture is duplicated though.

**Recommendation**: merge the googleapiclient duplicate into
`tests/conftest.py` (keep root conftest.py purely for the
`collect_ignore_glob`). Saves ~30 LoC duplicate state-management code.

### 1e. `scripts/setup/__init__.py` — empty file in empty package

| File | Why waste | Evidence |
|---|---|---|
| `scripts/setup/__init__.py` | Empty file; `scripts/setup/` directory contains nothing else; nothing imports `scripts.setup` anywhere | grep `scripts.setup` shows 0 hits. `ls scripts/setup/` shows only `__init__.py` |

### 1f. Empty `docs/editing_research/` directory

| Dir | Why waste | Evidence |
|---|---|---|
| `docs/editing_research/` | Empty directory. 0 refs anywhere | `ls docs/editing_research/` → empty |

### 1g. Empty `data/cache/`, `data/clone_video_requests/`, `data/intermediate/` dirs

| Dir | Active writer? | Files now | Recommendation |
|---|---|---|---|
| `data/cache/` | Yes — `pipeline/voice_clone.py:6` writes there; gitignored | empty at time of audit | Keep dir (gitignored); .gitkeep optional |
| `data/clone_video_requests/` | Yes — `control/routes/clone_video_routes.py:50` writes | empty | Keep dir |
| `data/intermediate/` | LEGACY ONLY — `pipeline/part2_watcher.py:70` calls it the "legacy data/intermediate/ layout (transition window)"; new writes go to `<channel>/[<niche>/]cache/<slug>/` per pipeline/paths.py:106 | empty | KEEP for transition fallback, BUT add to known-fragility.md as legacy-only |

---

## 2. Likely waste (needs human decision)

### 2a. `pipeline/cosmos_footage_prep.py` — possibly orphan

- Has only test refs + self-refs (CLI module). 0 imports from another
  pipeline/control/web module.
- Tests at `tests/test_footage_cosmos_prep.py` exist purely to lift
  coverage on this CLI; they don't prove the module is actually used in
  the production render flow.
- The `/make-cosmos-decoder` and `/make-cosmos-short` skills hand the
  raw shotlist directly to `historyrecapped/scripts/render_footage_only.py`
  per their SKILL.md — they don't reference `cosmos_footage_prep`.
- DECISION NEEDED: is the skill expected to invoke
  `python -m pipeline.cosmos_footage_prep --channel cosmosdecoded
  --slug <s>` between authoring and render? If yes, this module is
  necessary plumbing and the skill SKILL.md is missing the invocation
  line. If no, the module is dead and should go.

### 2b. `pipeline/animation.py` — 0 callers

| Module | Status | Evidence |
|---|---|---|
| `pipeline/animation.py` | 0 callers anywhere in `pipeline/`, `control/`, `web/`, `cloud/`, `scripts/`, `tests/` | grep `pipeline\.animation\b` returns only `.claude/skills/tune-ai-extraction/SKILL.md` (which lists it as a tunable surface, not a caller) |

Could be a true orphan, or could be reserved for a future render
pathway. Read the module header before deleting.

### 2c. `pipeline/part2_watcher.py` — borderline orphan

- Only `tests/test_utils_part2_watcher.py` imports it (coverage test).
- `pipeline/upload/upload.py:1440` mentions it in a docstring fallback
  comment.
- Documentation suggests it's a legacy transition-window watcher for the
  pre-2026-05-09 `data/intermediate/` layout.
- DECISION: keep if any in-flight render still emits to
  `data/intermediate/<chan>/part2_pending/`; delete (and delete the
  test) otherwise.

### 2d. `cloud/warm_image_services.sh` — references retired flux2-klein

The file's `url_for()` case statement points `flux` →
`ytfactory-image-flux2-klein-…run.app`. Per CLAUDE.md, the flux2-klein
service has been retired (z-image-turbo is the sole production image
model now). The script either needs an update to default to `zimage`
only, or it needs to die. Tests/skills reference it as a "warm
before render" hook so the surface is live; the contents are stale.

(`cloud/warm_tts_services.sh` may have a similar drift — needs an
inline read.)

### 2e. `cloud/_bench/README.md` — stale content

References `ytfactory-prod-v2`, lists `cloud/image-flux2-klein/` as
the live production image-gen service, and treats
`cloud/_bench/image-z-image-turbo/` as "WIP" — but per CLAUDE.md the
opposite is true (z-image-turbo IS production; flux2-klein is RETIRED).

Keep the directory itself (the bench services are intentional parked
scaffolds per the file's own description) but rewrite this README to
match reality, OR roll into the Phase 6 README rewrite already in
refactor-plan.

### 2f. `pipeline/channels.yaml` location

Lives at `pipeline/channels.yaml` (file), not under
`pipeline/channels/` (the directory of per-channel YAMLs). CLAUDE.md
calls `pipeline/channels/<channel>.yaml` the source of truth for per-
channel rules but doesn't tell the reader where the channel-MANIFEST
lives. Not waste; just a naming-collision footgun for new contributors.
Could rename to `pipeline/channels_manifest.yaml`. Listed for human
decision.

### 2g. Docstring references to non-existent docs

Several modules reference docs that DON'T exist in `docs/`:

| Referring file | Docs path referenced | Exists? |
|---|---|---|
| `pipeline/telemetry.py:2` | `docs/telemetry.md` | NO |
| `control/routes/critique_routes.py` | `docs/critique_chat.md` | NO |
| `pipeline/critique/runner.py` | `docs/critique_chat.md`, `docs/critique_runner_ops.md` | NO |
| `pipeline/tts/cloudrun.py:929` | `docs/cost_optimized_deploy.md` | YES |
| `requirements-control.txt:31` | `docs/youtube_stats_refresh.md` | NO |
| `requirements-control.txt:64` | `docs/critique_chat.md` | NO |
| `.claude/skills/upload-via-playwright/SKILL.md` | `docs/playwright_with_signed_in_chrome.md` | NO |
| `.github/workflows/tests.yml:79` | `docs/post-audit-2026-05-14.md` | NO |

Two options:
- **Author the missing docs** (lean: at least telemetry.md + critique_chat.md
  are pointed at from production code) — addresses charter Principle 4.
- **Strip the references** if the underlying knowledge is now in
  `/ai/*.md` (e.g. `/ai/external-integrations.md`, `/ai/known-fragility.md`).

Either way the current state — production code pointing at missing
docs — is broken-promise documentation.

### 2h. `pipeline/voice_clone.py` ↔ `pipeline/voice/voice_clone.py` shim

`pipeline/voice/voice_clone.py` is a 5-line re-export shim. The original
`pipeline/voice_clone.py` lives at top level. Only `web/server.py` and
the tests use the shim path (`pipeline.voice.voice_clone`); the script
`scripts/clone_voice.py` and `pipeline/footage/yt_dlp_cloudrun.py`
import the top-level path directly.

This is the same pattern as `pipeline/{audio,quality,utils}/*` shims
(11 total). Either:
- Migrate every caller to the package path and delete the flat module,
  OR
- Delete the shim and standardize on the flat module.

Either is acceptable; pick one and apply consistently. Charter principle
1 says "every layer must have a purpose" — these shims have NO purpose
except backcompat for a half-finished refactor.

### 2i. `requirements.txt` lists deps for deleted features

| Line | Dep | Status |
|---|---|---|
| `requirements.txt:30` | `tweepy>=4.14` — for `pipeline/x_upload.py` (deleted) | DEAD |
| `requirements.txt:29` | Comment "Stage 8b — X (Twitter) cross-post (pipeline/x_upload.py)" | DEAD (refers to deleted file) |

Remove both. (`requirements-control.txt` does NOT list tweepy — that
file is clean.)

### 2j. `requirements.txt` vs `requirements-control.txt` — keep separate

After reviewing both: they ARE genuinely different.
- `requirements.txt` (52 lines) = laptop venv: heavy ML deps (torch,
  diffusers, kokoro-onnx, playwright, etc.) + GCP clients + FastAPI.
- `requirements-control.txt` (84 lines) = slim Cloud Run image: NO
  torch/diffusers/kokoro/whisper; adds production-only OTel deps,
  google-cloud-run, google-cloud-secret-manager, sse-starlette,
  firebase-admin.

The slim Cloud Run image needs both fewer ML deps and more
production-only deps. Merging would force the laptop venv to install
opentelemetry-resourcedetector-gcp and firebase-admin (unnecessary
locally) and force the Cloud Run image to install torch (multi-GB
bloat). **Keep both as-is.** Note: confirm via search of every Cloud
Run service Dockerfile that they actually install
requirements-control.txt (not requirements.txt) before declaring this
fully justified. (Spot-check: `cloud/web-server/Dockerfile` confirmed.)

### 2k. Tests referencing private/legacy modules

| Test file | Notes |
|---|---|
| `tests/test_audit_d344_d347.py` | Specific audit IDs from a past incident — purpose unclear today. Investigate whether the audit findings have been integrated; if so, this test is regression-coverage and should stay. Spot-keep. |
| `tests/test_web_voice_sample_q237.py`, `tests/test_web_cloudrun_job_trigger_q241.py` | Bug-regression tests for specific Q-IDs. Per CLAUDE.md "write a test that fails when the bug returns" — these are the durable proof. KEEP. |
| `tests/test_utils_part2_watcher.py` | Couples to potentially-dead `pipeline/part2_watcher.py` (see 2c). Delete together. |
| `tests/test_footage_cosmos_prep.py` | Couples to potentially-dead `pipeline/cosmos_footage_prep.py` (see 2a). Delete together. |

### 2l. `pipeline/channels.py` legacy comment

Comment at `pipeline/channels.py:4` refers to `pipeline/niche_schema.py`
defaults. That file does exist (`pipeline/schemas/niche_schema.py`).
Minor docstring drift; not waste, but worth a one-line fix during
Phase 6 docs sweep.

---

## 3. Justified — keep (one-line proof each)

### 3a. Top-level config files

| File | Proof |
|---|---|
| `Makefile` | `critique-runner` target run by laptop daemon + by `control/com.ytfactory.critique-runner.plist` (the plist literally invokes `make critique-runner`); `test` target used by docs (developer-experience.md:42) |
| `.coveragerc` | Implicitly read by `coverage run` invocations in `scripts/coverage_gate.py:578-583` (no `--rcfile` flag = default discovery) |
| `.dockerignore` | Referenced by `cloud/web-server/Dockerfile:63` + .gcloudignore inheritance |
| `.gcloudignore` | Referenced by `cloud/web-server/.gcloudignore:10-11` + comments in Dockerfiles + every `cloud/<svc>/deploy.sh` that uses `gcloud builds submit` |
| `firebase.json`, `firestore.rules`, `firestore.indexes.json` | `firebase.json` references both. The set is consumed by the Firebase CLI (`firebase deploy --only firestore`). Direct grep returns 0 internal refs, which is expected — Firebase CLI auto-discovers them by filename. Charter-required: Firestore security rules ARE production data integrity. |
| `.python-version` | `3.12` matches the Dockerfile FROM lines (`python:3.12-slim` in render-worker + web-server) and the GitHub Actions setup (`tests.yml:17`). Consumed by pyenv for laptop dev. |
| `requirements.txt` | Laptop ML dep manifest (52 lines, see 2j). |
| `requirements-control.txt` | Cloud Run slim image (84 lines, see 2j). |
| `conftest.py` (root) | `collect_ignore_glob = ["cloud/_bench/*"]` — must be at repo root for pytest's discovery; see 1d. |
| `tests/conftest.py` | googleapiclient + YOUTUBE_DIR guard fixtures, born from a 2026-05-10 token-issue post-mortem (per its docstring). |
| `CLAUDE.md` | Project rules. |

### 3b. `docs/` content

| File | Proof |
|---|---|
| `docs/cost_optimized_deploy.md` | Referenced by `CLAUDE.md:115`, `pipeline/tts/cloudrun.py:929`, tests. Iron rules background. |
| `docs/optimization_checklist.md` | Referenced by `CLAUDE.md:117`. |
| `docs/editing_research/` | Empty — see 1f. Delete. |

### 3c. `scripts/` top-level scripts (justified)

| File | Caller / proof |
|---|---|
| `scripts/audit_idle_costs.py` | Referenced by `scripts/bootstrap_new_project.sh` (ops bundle) + docs |
| `scripts/bootstrap_new_project.sh` | Referenced by `docs/optimization_checklist.md` + `docs/cost_optimized_deploy.md` + dev-experience docs |
| `scripts/build_song_samples.py` | Called by `control/routes/song_sample_routes.py` |
| `scripts/bulk_upload.py` | Documented entry point (`ai/developer-experience.md:43`) |
| `scripts/clone_voice.py` | Documented + referenced by `.claude/skills/create-youtube-channel/SKILL.md` |
| `scripts/cloud_critic_loop.py` | Started by `control/com.ytfactory.cloud-critic.plist`; mentioned in README + pipeline/critique/cloud_poller.py |
| `scripts/cloud_daily_snapshot.py` | Fired by `control/com.ytfactory.cloud-snapshot.plist`; called from `pipeline/cloud/snapshot.py` |
| `scripts/coverage_gate.py` | The `/test-coverage` skill's primary executable; invoked from 7+ tests |
| `scripts/critique_runner.py` | Started by `Makefile critique-runner` target + `control/com.ytfactory.critique-runner.plist` |
| `scripts/gcs_lifecycle.json`, `scripts/gcs_lifecycle.md` | GCS lifecycle policy + docs — applied via `gsutil lifecycle set` (operator-run) |
| `scripts/laptop_cleanup.py` | Documented dev tool |
| `scripts/lint_skill_md.py` | Referenced by `/make-skill` learnings + `/update-docs` SKILL |
| `scripts/migrate_channel_yamls.py` | Has its own test (`tests/test_migrate_channel_yamls.py`) |
| `scripts/playwright_mcp_signed_in.sh` | Documented in dev-experience + external-integrations docs |
| `scripts/pull_hindi_voices.py` | Documented; referenced by `pipeline/channels/hindutavaanimated.yaml` |
| `scripts/pull_stories.py` | Referenced by 4+ skills (`make-script`, `make-ranking`, `make-mystories-short`, `make-movie-short`) |
| `scripts/seed_channel_niches.py` | Documented; tests cover it indirectly via niche_schema |
| `scripts/serve.sh`, `scripts/serve_cloud.sh` | Local dev server runners; documented |
| `scripts/setup_ytdlp_cookies.sh` | Documented in dev-experience |
| `scripts/sync_state_to_gcs.sh` | Fired by `control/com.ytfactory.state-sync.plist`; referenced by `control/routes/script_jobs_routes.py` |
| `scripts/sync_upload_records_to_gcs.py` | Documented batch task |
| `scripts/trigger_one_render.py` | Documented one-shot render trigger |
| `scripts/ops/upload_next.py` | Fired by `control/com.ytfactory.upload-next.plist` |
| `scripts/ops/bulk_render_queue.py` | Documented |
| `scripts/_shared/safety_scan_source.py` | Referenced by `/make-top10` skill |
| `scripts/setup/__init__.py` | EMPTY — see 1e, recommend delete |

### 3d. `data/` subdirs

| Dir | Active writer |
|---|---|
| `data/_bench/` | `pipeline/cloud/health.py` + `pipeline/cloud/snapshot.py` (cloud_health_baseline.json + cloud_cost/cloud_deploys/cloud_health dirs) |
| `data/_jobs/` | `web/server.py:606 _persist_job` writes job snapshots |
| `data/cache/` | `pipeline/voice_clone.py`, `pipeline/thumbnails.py` |
| `data/clone_video_requests/` | `control/routes/clone_video_routes.py:50` |
| `data/critiques/` | `pipeline/tts/text_normalize.py` + `pipeline/paths.py` |
| `data/cron/` | Has `scrollpulse/` subdir; referenced from `.claude/skills/make-reddit-thread/SKILL.md`. Not many writers; verify scrollpulse cron output is intentional. |
| `data/intermediate/` | LEGACY path per docstrings — see 1g. Keep as transition fallback. |
| `data/music/` | `control/routes/music_routes.py`, `control/routes/song_sample_routes.py` |
| `data/research/` | `pipeline/research/youtube.py` + `channel_assets.py` |
| `data/song_samples/` | `control/routes/song_sample_routes.py` |
| `data/sources/` | `pipeline/sources/youtube_video.py:120` |

### 3e. `cloud/` services + utility dirs

| Dir | Proof |
|---|---|
| `cloud/asr-whisper/` | CLAUDE.md production service |
| `cloud/clone-video-worker/` | Referenced via `CLOUDRUN_CLONE_VIDEO_URL`; deploy.sh + tests |
| `cloud/cobalt-api/` | Referenced via `CLOUDRUN_COBALT_URL` in CLAUDE.md |
| `cloud/editing-agent/` | CLAUDE.md production service |
| `cloud/image-z-image-turbo/` | CLAUDE.md production image-gen service |
| `cloud/render-worker-v2/` | CLAUDE.md production render JOB |
| `cloud/stats-refresh/` | Cloud Run YouTube stats ingest; tested via `test_cloud_deploy_hardening.py:288` |
| `cloud/tts-chatterbox/` | CLAUDE.md production English TTS |
| `cloud/tts-indicf5/` | CLAUDE.md production Hindi TTS |
| `cloud/web-next/`, `cloud/web-server/` | Production web services |
| `cloud/weights-staging/` | Weights staging JOB; tested via `test_cloud_deploy_hardening.py:286` |
| `cloud/_bench/` | Parked scaffolds for revival; README intentional (but stale — see 2e) |
| `cloud/_shared/` | Sourced by every `deploy.sh`; otel_init shared by every cloud service |
| `cloud/iam/` | One-time IAM grant scripts; most referenced from production docstrings as recovery commands. 4 are orphans (see 1c) |
| `cloud/warm_image_services.sh` | Called by `pipeline/cloud/warm.py`; content stale (see 2d) |
| `cloud/warm_tts_services.sh` | Called by `pipeline/cloud/warm.py` |

### 3f. `pipeline/` flat modules (per-module verification)

| Module | Caller count | Notes |
|---|---|---|
| `pipeline/align.py` | 2 (shim re-export at pipeline/audio/align.py + tests via shim) | Production module |
| `pipeline/animation.py` | 0 callers — see 2b | DECISION NEEDED |
| `pipeline/asr.py` | 8 | Production |
| `pipeline/asr_cloudrun.py` | 5 | Production |
| `pipeline/beats.py` | 18 | Production |
| `pipeline/captions.py` | 13 | Production |
| `pipeline/channels.py` | 13 | Manifest reader |
| `pipeline/channels.yaml` | Read by `pipeline/channels.py:29` | Source of truth |
| `pipeline/cloudrun_auth.py` | 5 direct + 8 via shim | Production |
| `pipeline/compose.py` | 9 | Production |
| `pipeline/cosmos_footage_prep.py` | 5 (all tests + self) — see 2a | DECISION NEEDED |
| `pipeline/critic_long_form.py` | 3 | Production critic |
| `pipeline/era_anchor.py` | 7 | Used by render prompts |
| `pipeline/era_taxonomy.yaml` | Read by `pipeline/era_anchor.py:38` | Data file |
| `pipeline/niche_specs.py` | 8 | Production |
| `pipeline/niches.py` | 12 | NICHE_CHANNEL source of truth |
| `pipeline/parallel.py` | 4 | Production (shim at pipeline/utils/parallel.py) |
| `pipeline/part2_watcher.py` | Test-only + docstring — see 2c | DECISION NEEDED |
| `pipeline/paths.py` | 31 | Used throughout |
| `pipeline/preflight.py` | 7 | Production (shim at pipeline/quality/preflight.py) |
| `pipeline/probe.py` | 10 | Production (shim at pipeline/quality/probe.py) |
| `pipeline/stage_overlap.py` | 5 | Production |
| `pipeline/telemetry.py` | 15 | Production |
| `pipeline/thumbnails.py` | 5 | Production |
| `pipeline/transcribe.py` | 4 | Production |
| `pipeline/voice_clone.py` | 2 direct + 1 via shim | Used by web/server + scripts/clone_voice |

### 3g. `pipeline/` subdirs that earned their existence

| Subdir | Proof |
|---|---|
| `pipeline/audio/` | 5 shim files (align, audio, asr, beats, transcribe) + real audio modules used widely |
| `pipeline/auth/` | `pipeline/auth/identity.py` — used by tests + web |
| `pipeline/channels/` | Per-channel YAML configs (charter source of truth) |
| `pipeline/cloud/` | health/snapshot/warm/cost/deploys/services/skill_dispatch — laptop control plane for cloud |
| `pipeline/critique/` | runner, gates, cloud_poller, agent, messages — critique system |
| `pipeline/editing/` | Optional 8th-stage editing-agent integration |
| `pipeline/footage/` | footage.py, footage_plan_lint, yt_dlp_cloudrun — sports/cosmos channels |
| `pipeline/images/` | Excluded from deep audit (W2 owns) |
| `pipeline/llm/` | Excluded from deep audit (W1 owns) |
| `pipeline/observability/` | OTel + Cloud Logging plumbing |
| `pipeline/quality/` | evals + 2 shims (preflight, probe) |
| `pipeline/render/` | Excluded from deep audit (W2 owns) |
| `pipeline/research/` | YouTube + Wiki research |
| `pipeline/schemas/` | customization, long_form_schema, niche_schema + 1 shim (paths) |
| `pipeline/social/` | reddit_card, reddit_scrape, x_scrape, x_screenshot — used by scrollpulse + sports tweet-reaction |
| `pipeline/sources/` | youtube_video |
| `pipeline/text/` | hindi_normalize — used by Hindi TTS pipeline |
| `pipeline/tts/` | Excluded from deep audit (W2 owns) |
| `pipeline/upload/` | upload.py (x_upload.py confirmed deleted) |
| `pipeline/utils/` | azure_auth, state_client + 1 shim (parallel) |
| `pipeline/variants/` | Per-channel variant overlays |
| `pipeline/voice/` | voice_catalog, voice_clone (one shim) |
| `pipeline/voice_refs/` | Reference audio + catalog.yaml for TTS voice cloning |

### 3h. `control/` (excluding refactor-plan items)

| File | Proof |
|---|---|
| `control/chat_routes.py` | Imported by server_dev.py:31, scheduler.py:32, render_routes.py:15. NOT in the 5-route-duplicate cleanup list. KEEP. |
| `control/chat_service.py` | Imported by control/chat_routes via `control.chat_service`. KEEP. |
| `control/com.ytfactory.*.plist` (5 files) | All 5 launchd daemons; `tests/test_plist_script_paths.py` enforces they exist + reference valid paths. KEEP all 5. |
| `control/core/sim_worker.py` | Started by server_dev.py:89 (`from control.core.sim_worker import start_sim_worker`) |
| `control/core/cloud_run.py` | trigger_render_job is the canonical render dispatch entry; tested via `tests/test_control_cloud_run.py` |
| `control/routes/*` (18 files) | All mounted by web/server.py + control/server_dev.py (verified by hand) |

### 3i. `tests/` (no orphans found)

No tests target deleted modules (verified by sampling first-import lines
+ checking each missing-target permutation). No old/new test pairs
discovered. 198 test_*.py files + critique/, render/ subdirectories all
have plausible production module mappings.

(Caveat: did NOT exhaustively verify each test imports a still-existing
target — only spot-checked. If any test imports `pipeline.x_upload`,
`scripts.setup_x_credentials`, etc., those would error at collection
time; would have been caught when the parallel `pytest -x -q` baseline
runs in Phase 0.)

---

## Appendix A — Quick stats

- Files audited: 280+ files across docs/, scripts/, data/, cloud/,
  pipeline/ (flat), control/ (excluding deep refactor-plan items),
  tests/, and root-level configs.
- Confirmed waste (delete): 24 files / 2 dirs.
- Likely waste (needs decision): 12 items (modules + classes of stale ref).
- Justified - keep: ~250 files.
- Top 5 biggest waste candidates:
  1. `scripts/historyrecapped/` — entire dir (11 files) — pre-generalization legacy, README self-declares as such.
  2. `scripts/sportsrecapped/` — entire dir (6 files) — skills reference channel-dir paths, not these.
  3. 4 orphan `cloud/iam/` scripts — 0 callers anywhere.
  4. `pipeline/animation.py` + `pipeline/cosmos_footage_prep.py` + `pipeline/part2_watcher.py` — three near-orphan modules with only test+self refs (DECISION NEEDED).
  5. Broken-promise documentation: 8 docs-paths referenced from production code that don't exist on disk (e.g., `docs/telemetry.md`, `docs/critique_chat.md`, `docs/post-audit-2026-05-14.md`).

## Appendix B — Suggested cleanup sequence

If/when the cleanup is approved (NOT done in this audit — read-only):

1. **Phase A (zero-risk deletes)**: `scripts/setup/__init__.py`,
   `docs/editing_research/`, the 4 orphan `cloud/iam/*.sh` scripts,
   `cloud/_shared/redeploy_for_otel.sh`. Total: 7 files.
2. **Phase B (verify-and-delete)**: `scripts/historyrecapped/` +
   `scripts/sportsrecapped/` entire dirs — only after sweep-grep
   confirms zero hidden references in workflow yamls, plist files,
   or skill SKILL.md instructions.
3. **Phase C (decisions)**: `pipeline/animation.py`,
   `pipeline/cosmos_footage_prep.py`, `pipeline/part2_watcher.py` —
   make the call with the human + delete tests if modules go.
4. **Phase D (cleanups)**: strip `tweepy` from requirements.txt;
   rewrite `cloud/_bench/README.md`; rewrite `cloud/warm_image_services.sh`
   to drop flux2-klein; either write the 8 missing docs or strip the
   stale doc-refs from production code.
5. **Phase E (shim consolidation, optional)**: pick a side on the
   pipeline shim pattern (flat vs subpackage), migrate every caller,
   delete the 11 shims OR delete the flat modules. Charter principle 1
   says these layers have NO purpose; pick one.
