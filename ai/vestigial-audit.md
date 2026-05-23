# Vestigial Audit (read-only, 2026-05-23)

Three lean-engineering audits. Recommendations only — no deletions performed.

Mandatory reading verified: `CLAUDE.md`, `ai/engineering-principles.md` (LEAN +
ANTI-OVERENGINEERING sections), `ai/onboarding-qa.md` (Q33 X/Twitter dead, Q47-48
TTS/image arch, locked operating principles).

---

## Audit 1 — Cloud Run service vestigial check

### Source of truth

`CLAUDE.md:27-39` lists the deployed services as: `render-worker-v2`,
`image-z-image-turbo`, `tts-chatterbox`, `tts-indicf5`, `asr-whisper`,
`editing-agent`. The line above explicitly says *"older flux2-klein / qwen /
hidream / flux2-dev / indicparler services have been retired"*. `CLAUDE.md:70-84`
adds these as also-active: `CLOUDRUN_CLONE_VIDEO_URL` (clone-video-worker),
`CLOUDRUN_COBALT_URL` (cobalt-api), `CLOUDRUN_WEB_SERVER_URL`,
`CLOUDRUN_WEB_NEXT_URL`.

### Inventory (everything under `/Users/rohit/ytFactory/cloud/`)

`ls cloud/` (top-level dirs with `deploy.sh`):

| Dir | `deploy.sh`? | Wired in render-worker JOB env (`deploy.sh:106`)? | Real Python caller? | CLAUDE.md says deployed? |
|---|---|---|---|---|
| `render-worker-v2/` | yes (`cloud/render-worker-v2/deploy.sh:1`) | self | n/a (it IS the job) | yes |
| `image-z-image-turbo/` | yes | `CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL` set at `deploy.sh:106` | `pipeline/images/images_cloudrun.py` | yes |
| `tts-chatterbox/` | yes | `CLOUDRUN_TTS_CHATTERBOX_URL` at `deploy.sh:106` | `pipeline/tts/cloudrun.py` | yes |
| `tts-indicf5/` | yes | `CLOUDRUN_TTS_INDICF5_URL` at `deploy.sh:106` | `pipeline/tts/cloudrun.py:929` (`_synth_cloudrun_indicf5`) | yes |
| `asr-whisper/` | yes | `CLOUDRUN_ASR_URL` at `deploy.sh:106` | `pipeline/asr/cloudrun.py` (faster-whisper) | yes |
| `editing-agent/` | yes | NOT in `deploy.sh:106` env block | `pipeline/editing/cloudrun.py:47`, called from `cloud/render-worker-v2/entrypoint.py:2129` | yes (optional 8th stage) |
| `clone-video-worker/` | yes | NOT in `deploy.sh:106` env block | `pipeline/voice_clone.py:26,123`, `pipeline/footage/yt_dlp_cloudrun.py:70`, `control/routes/clone_video_routes.py:472` | yes |
| `cobalt-api/` | yes | NOT in `deploy.sh:106` env block | **NONE** (see below) | yes (claimed) |
| `stats-refresh/` | yes (Cloud Run JOB) | NOT in `deploy.sh:106` env block (it's a separate JOB, not called from the render worker) | `web/server.py:2409, 2466-2471` | NOT listed |
| `weights-staging/` | yes (Cloud Run JOB) | NOT applicable (one-shot init job) | `scripts/bootstrap_new_project.sh:231` | NOT listed |
| `web-server/` | yes | NOT in env (it IS the orchestrator) | n/a | yes |
| `web-next/` | yes | NOT in env (Next.js frontend) | n/a | yes |
| `_shared/` | n/a (sourced helpers — `auth_setup.sh`, `sync.sh`, `otel_init.py`) | n/a | sourced from every `deploy.sh` (e.g. `cloud/render-worker-v2/deploy.sh:18`) | n/a (helper dir) |
| `iam/` | no (IAM grant scripts) | n/a | sourced by `cloud/clone-video-worker/deploy.sh:28` | n/a |
| `_bench/` | per-subdir (7 bench dirs) | NO — bench fixtures only | n/a | n/a (parked) |

### `_bench/` — confirmed parked fixtures, NOT deployed services

`cloud/_bench/README.md:1-39`: *"Cloud Run service scaffolds that are not on the
production path. Kept around for revival — re-run `<dir>/deploy.sh` to push to
`ytfactory-prod-v2` when one of these is needed."*

Contents (each has a `deploy.sh` so the README's revival promise is real, but
none are deployed to v3 today):

- `_bench/tts-f5/`, `_bench/tts-higgs/`, `_bench/tts-cosyvoice/`,
  `_bench/tts-indicparler/` — superseded by `tts-chatterbox` + `tts-indicf5`.
- `_bench/image-z-image-turbo/` — pre-cutover scaffold for the current
  production `image-z-image-turbo` (which lives one level up). The bench copy
  is the OLD scaffold; not the live deploy.
- `_bench/image-hidream/`, `_bench/image-qwen/` — never deployed.

`_bench/` is a documented holding area, not a service. The `_bench/README.md`
itself describes the production list — and that production list is now stale
(it still says `cloud/image-flux2-klein/` is prod, which `CLAUDE.md:27` says is
retired).

### Suspect-specific findings

**`cobalt-api`** — `cloud/cobalt-api/deploy.sh:17` deploys
`ytfactory-cobalt-api`. The `deploy.sh:70` final echo tells the operator to
export `CLOUDRUN_COBALT_URL=${URL}`. **No Python file in `pipeline/`,
`control/`, `web/`, `cloud/render-worker-v2/`, or `cloud/clone-video-worker/`
reads `CLOUDRUN_COBALT_URL` or `CLOUDRUN_COBALT_API_URL`** (grep across all
`.py` files, excluding `__pycache__` + `.claude/worktrees/`, returns zero
hits). The ONLY runtime references are:
- `pipeline/cloud/services.py:211-218` — service catalog entry (admin-panel
  health display only).
- `pipeline/cloud/deploys.py:128` — comment.
- `CLAUDE.md:81` — documentation claim.

`clone-video-worker/server.py` does NOT import or call cobalt
(`grep cobalt /Users/rohit/ytFactory/cloud/clone-video-worker/server.py` →
empty). Same with its Dockerfile + deploy.sh.

**Verdict on cobalt-api: RETIRE** — no caller exists. It's listed in the
service catalog and CLAUDE.md only as historical/aspirational. If the
clone-video-worker once chained through it, that wiring is gone.

**`clone-video-worker`** — multiple active callers:
- `pipeline/voice_clone.py:26,123` (voice cloning via `/download`).
- `pipeline/footage/yt_dlp_cloudrun.py:68-74` (`CLOUDRUN_YT_DLP_URL` falls back
  to `CLOUDRUN_CLONE_VIDEO_URL` — same container, same `/download` endpoint).
- `control/routes/clone_video_routes.py:472,548-564` (the /app/create/clone
  wizard route — `RuntimeError` if `CLOUDRUN_CLONE_VIDEO_URL` unset).

The URL is NOT baked into `cloud/render-worker-v2/deploy.sh:106` though
— which means the render-worker JOB can't call it. It's used from the
laptop / `control/` / the web-server. Production wiring lives outside the
render-worker, so the absence from `deploy.sh:106` is correct.

**Verdict on clone-video-worker: KEEP** — actively used by the
clone-video wizard + footage download path.

**`stats-refresh`** — `web/server.py:2395-2483` (`/api/dashboard/refresh-research-cache`)
triggers `ytfactory-stats-refresh` via `control.core.cloud_run.execute_job_async`.
Pre-2026-05-10 this was an in-thread `fetch_all()` that timed out the FE
(`server.py:2398-2407`). Cloud Scheduler invokes it hourly per
`cloud/stats-refresh/deploy.sh:23-27` + `wire_scheduler.sh`. Production-critical
for dashboard freshness.

**Verdict on stats-refresh: KEEP** — production callers exist (web/server.py
+ Cloud Scheduler cron). The fact that CLAUDE.md doesn't list it under "active
in production" (`CLAUDE.md:70-84`) is a CLAUDE.md gap — the service IS live.

**`editing-agent`** — wired via `pipeline/editing/cloudrun.py:47` (reads
`CLOUDRUN_EDITING_AGENT_URL`). Sole consumer in the cloud render path is
`cloud/render-worker-v2/entrypoint.py:2109-2160` (`_stage_editing_agent_real`),
gated on `proposal.editing.enabled` per `entrypoint.py:2133`. The circuit
breaker at `pipeline/editing/circuit_breaker.py:1-65` resets at the top of
every render to prevent cross-render contamination.

Note: `CLOUDRUN_EDITING_AGENT_URL` is **NOT** in the
`cloud/render-worker-v2/deploy.sh:106` env block (only TTS / image / ASR URLs
are). That means render-worker JOB executions don't have the env var unless
the env was added manually post-deploy — and `deploy.sh:79-83` warns that
`--set-env-vars` is REPLACE-not-merge, so manual additions are wiped on every
redeploy. Practical implication: the editing-agent stage runs against the
laptop-fallback path (circuit breaker open → local executor) on every
post-deploy job UNLESS the operator manually re-adds the env var.

**Verdict on editing-agent: KEEP, BUT FRAGILE** — optional 8th stage with
laptop fallback; the env-var gap at `deploy.sh:106` means cloud usage isn't
reliably wired today. Either bake `CLOUDRUN_EDITING_AGENT_URL` into
`deploy.sh:106` or remove the editing service entirely (the laptop ffmpeg
fallback path at `pipeline/editing/executor.py` covers the same EDL flow).

**`weights-staging`** — `cloud/weights-staging/deploy.sh:17` declares a Cloud
Run JOB `ytfactory-weights-staging`. It's a ONE-SHOT init job (per `deploy.sh`
header at line 2 + Hugging Face → GCS staging in `stage.py:49,276`), not a
service called per-render. Sole runtime caller: `scripts/bootstrap_new_project.sh:228,231`
when bootstrapping a new GCP project. Not on the render hot path.

**Verdict on weights-staging: KEEP (utility), but mark NEEDS-DECISION** — it's
correctly classified as a one-shot build-time utility. The question is: is
the project still spinning up new GCP projects often enough to justify a
deployable JOB vs a laptop script? If only one or two new projects per year,
collapse to a laptop-run Python script.

### Retired services still referenced in code

`cloud/render-worker-v2/entrypoint.py:41-47, 217-261` (preflight) still
checks/whitelists URLs for `CLOUDRUN_TTS_INDICPARLER_URL`, `CLOUDRUN_TTS_F5_URL`,
`CLOUDRUN_TTS_URL`, `CLOUDRUN_IMAGE_FLUX2_KLEIN_URL`, `CLOUDRUN_IMAGE_QWEN_URL`,
`CLOUDRUN_IMAGE_HIDREAM_URL`. All retired per CLAUDE.md.

`pipeline/cloud/services.py:84-164` still lists `tts-f5`, `tts-higgs`,
`tts-cosyvoice`, `tts-indicparler`, `image-flux2-klein`, `image-qwen`,
`image-hidream` in the service catalog (admin panel).

`pipeline/images/images_cloudrun.py:35,503` still hardcodes
`CLOUDRUN_IMAGE_FLUX2_KLEIN_URL` as the canonical env var (CLAUDE.md says
this service is retired; production env var is now
`CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL`).

`pipeline/tts/cloudrun.py:22-29` documents `CLOUDRUN_TTS_URL` as the required
env var; per CLAUDE.md it's not in active production.

These are **stale code references** to retired services, not separate
services to retire. They cause the admin panel to show 6 "unconfigured"
chips and the preflight checker to look for env vars that will never be set.

### Verdict table

| Service | `deploy.sh` exists | Verdict | Notes |
|---|---|---|---|
| `render-worker-v2` | yes | KEEP | The render JOB itself |
| `image-z-image-turbo` | yes | KEEP | Sole production image-gen |
| `tts-chatterbox` | yes | KEEP | English production TTS |
| `tts-indicf5` | yes | KEEP | Hindi production TTS |
| `asr-whisper` | yes | KEEP | Caption alignment |
| `editing-agent` | yes | KEEP (fragile) | Optional 8th stage; env-var missing from `deploy.sh:106` |
| `clone-video-worker` | yes | KEEP | Voice clone + yt-dlp + clone-video wizard |
| `stats-refresh` | yes | KEEP | Hourly stats refresh; CLAUDE.md should list it |
| `web-server` | yes | KEEP | FastAPI orchestrator |
| `web-next` | yes | KEEP | Next.js admin/wizard UI |
| `cobalt-api` | yes | RETIRE | Zero Python callers; only doc references |
| `weights-staging` | yes | NEEDS-DECISION | One-shot init JOB; could be a laptop script |
| `_bench/*` (7 dirs) | each has `deploy.sh` | RETIRE | Acknowledged parked per `_bench/README.md`; if not actually revived in 6 months, delete |
| `_shared/`, `iam/` | n/a | KEEP | Helper dirs, not services |

### Recommendation

**11 KEEP** (10 prod + 1 fragile), **1 RETIRE outright** (cobalt-api), **1
NEEDS-DECISION** (weights-staging), **7 parked in `_bench/`** (RETIRE if not
revived). The bigger problem isn't unused services — it's **stale env-var
references in the canonical preflight + service catalog**: 6 retired services
(`flux2-klein`, `qwen`, `hidream`, `tts-f5`, `tts-higgs`, `tts-cosyvoice`,
`tts-indicparler`) still appear in `pipeline/cloud/services.py:84-164` and
`cloud/render-worker-v2/entrypoint.py:41-261`. The admin panel shows ghost
chips; the preflight looks for env vars that are intentionally unset. Highest-
leverage cleanup: prune `services.py` to the active 10 + drop the retired
preflight checks. Cobalt-api removal is then a 1-line catalog edit + a `git
rm cloud/cobalt-api/`.

---

## Audit 2 — LLM 3-backend dispatcher justification

### Source files

- `pipeline/llm/cli.py:56-91` — `_VALID_BACKENDS = {BACKEND_CLI,
  BACKEND_AZURE, BACKEND_ANTHROPIC}`; `_choose_backend()` precedence at
  `cli.py:67-91`.
- `pipeline/llm/cli.py:593-622` — three-way dispatch.
- `pipeline/llm/cli.py:1416-1467` — `_call_anthropic_sdk` body.
- `cloud/render-worker-v2/deploy.sh:106` — sets `YTFACTORY_LLM_BACKEND=azure_openai`
  in the production JOB env.
- `cloud/render-worker-v2/deploy.sh:134-137` — documented operator switch to
  `anthropic_sdk` via `gcloud run jobs update`.

### Caller inventory (where each backend is invoked)

**`cli` (default on laptop):**
`_choose_backend()` returns `cli` whenever the `claude` binary is on PATH
(`cli.py:85-86`). This is the laptop default — and the only backend that
supports vision-aware calls (`add_dirs` + `allowed_tools` — see
`cli.py:584-591`). Critic + audio_critic + clone-video flow all use this path.
Confirmed actively used per `onboarding-qa.md:174` (Q37) — *"Critic +
audio_critic can be SKIPPED and done on laptop"* via the `cli` backend.

**`azure_openai` (default in cloud worker):**
Set at `cloud/render-worker-v2/deploy.sh:106` —
`YTFACTORY_LLM_BACKEND=azure_openai`. Every cloud render uses this. Confirmed
in `ai/source-of-truth.md:26` and `CLAUDE.md:86-88`. Implementation at
`pipeline/llm/cli.py:_call_azure_openai` (around line 1100-1300 — not read in
full, but referenced from the dispatcher at `cli.py:593-602`).

**`anthropic_sdk`:**
- The implementation exists at `pipeline/llm/cli.py:1416-1467`.
- Tests cover it at `tests/test_pipeline_llm.py:219-298` (mocked Anthropic
  SDK).
- Selected via env-var precedence at `cli.py:89-90` (only if neither `claude`
  CLI is on PATH NOR Azure creds are set, BUT `ANTHROPIC_API_KEY` is).
- `cloud/render-worker-v2/deploy.sh:134-137` echoes the operator switch
  command, but it's just a documentation echo — it does NOT actually run.
- **Production usage today: NONE.** Reasons:
  - Cloud worker default is `azure_openai` (deploy.sh:106).
  - Laptop default is `cli` (CLAUDE.md:88; `claude` binary on PATH).
  - The env-var precedence at `cli.py:79-91` reaches `anthropic_sdk` ONLY
    when (a) explicit env override is set, OR (b) `claude` CLI absent AND
    Azure creds absent AND `ANTHROPIC_API_KEY` present — not a configuration
    that exists in any active deployment per the audit grep.
- `ai/prd.md:463-468` already calls this out: *"anthropic_sdk: is this ever
  set anywhere? Verify env grep + git log. If anthropic_sdk is unused: delete
  the `_call_anthropic_sdk` function + the backend dispatch branch."*
- Counter-evidence: `ai/refactor-plan.md:36` claims *"All 3 are INTENTIONAL
  per deploy.sh:134-137"* — but `deploy.sh:134-137` is just an `echo` of the
  operator switch command, not actual production wiring. The "intent" exists;
  the **usage** does not.

### The pricing/billing argument

The docstring at `cli.py:14-16` justifies `anthropic_sdk` as *"Pay-per-token,
opens a separate billing line."* That's a real lever — IF the project ever
wanted to A/B compare Anthropic vs Azure spend on equivalent prompts. But:
- No active production deployment uses it.
- No A/B test infrastructure consumes the backend.
- The operator-toggle workflow at `deploy.sh:134-137` is undocumented in
  observability / runbooks — operators wouldn't know when to flip it.

### Tests

`tests/test_pipeline_llm.py:219-298` exists. It validates the SDK adapter
mechanically (constructor stub, JSON schema branch, error path). Tests
prevent regression — but tests are not a substitute for production use. Per
the engineering charter: *"abstractions used only once"* is forbidden; an
abstraction that exists only to pass its own tests is the strongest form of
this anti-pattern.

### Verdict table

| Backend | Production callers | Tests | Status |
|---|---|---|---|
| `cli` | laptop dev + critic + audio_critic + vision stages | yes | KEEP |
| `azure_openai` | every cloud render (deploy.sh:106) | yes | KEEP |
| `anthropic_sdk` | none (operator-toggle only — never flipped) | yes | RETIRE or NEEDS-DECISION |

### Recommendation

The 3-way dispatcher can collapse to **2 backends**. The `anthropic_sdk`
branch is a documented escape hatch that's never been deployed. Per the
LEAN charter (`engineering-principles.md:629-647, 691-707`): *"abstractions
used only once"*, *"factories without need"*. The dispatcher is currently a
2.5-way factory pretending to be 3-way.

Two paths:
1. **DELETE** `anthropic_sdk` branch + `_call_anthropic_sdk` + its tests +
   the deploy.sh echo + the `BACKEND_ANTHROPIC` constant. Net delete: ~80
   lines pipeline + ~100 lines tests + 4 lines deploy.sh. The `_choose_backend`
   precedence chain at `cli.py:67-91` simplifies to 3 cases (explicit env →
   claude on PATH → Azure creds). Saves a small but real maintenance tax
   (every change to the dispatcher's shared logic — telemetry, max_tokens,
   schema augmentation — currently must verify all 3 branches).
2. **KEEP** as a 1-line emergency escape hatch IF the project has a real
   "Azure outage → switch billing line" runbook. Today no such runbook exists
   in `docs/` (grep returns only the `deploy.sh:134-137` echo). Without the
   runbook, KEEP is just "leaving dead code that looks alive."

Recommended path: **DELETE**. If a future Azure outage forces a switch, add
the SDK back in one PR — the existing tests are a useful reference. Don't pay
the maintenance tax today for a hypothetical future incident.

---

## Audit 3 — Plugin system "still earning its keep"

### The promise (per `contracts.py:9-75`)

The plugin system landed 2026-05-14 to consolidate 4 hardwired renderers
(shorts.py 3344 LoC, sports_doc.py 1306, long_form.py 2734, footage_only.py
946 — ~8.3k LoC of orchestration with duplication) into 2 engines (short +
long) + 6 plugin slots with multiple impls each. The justification is in the
"History" section of `contracts.py:9-34` — and it's strong: the same
loudnorm bug had to be fixed in two places (commit `ac4b396`, 2026-05-12)
before the consolidation.

### What's registered today

Plugin registrations (`grep register_plugin /Users/rohit/ytFactory/pipeline/render`):

| Slot | Registered impls | Test-only fixtures | Production impls |
|---|---|---|---|
| `audio` | tts_single, tts_chunked, audio_from_fixture | audio_from_fixture | tts_single, tts_chunked (2) |
| `timeline` | asr_beats, asr_anchors, timeline_from_fixture | timeline_from_fixture | asr_beats, asr_anchors (2) |
| `visualize` | ai_beat_slideshow, archival_shotlist, footage_filler, footage_windows, longform_panels, visuals_from_fixture | visuals_from_fixture | 5 production impls |
| `overlays` | word_caption_pngs, sentence_caption_ass, lower_third, chapter_card, anchored_footage, noop | none (noop is fallback, not test) | 6 production impls |
| `music` | ducked_loop, single_bed, section_mood, silent | none | 4 production impls |
| `compose` | beat_slideshow_mux, section_video_mux | none | 2 production impls |

Total: 24 registrations across 6 slots; production impls per slot: 2 / 2 / 5
/ 6 / 4 / 2.

### How the engines actually select impls

**`short_engine.py:164-168, 439-466`** (the plugin-name resolvers):

```
audio_name     = "song_suno"  if spec.audio_mode == SONG else "tts_single"
                  (with spec.extra["audio_plugin"] override for tests)
timeline_name  = spec.extra.get("timeline_plugin") or "asr_beats"
visualize_name = spec.extra.get("visualize_plugin") or spec.visual_mode.value
music_name     = spec.extra.get("music_plugin") or spec.music_policy.value
compose_name   = spec.extra.get("compose_plugin") or "beat_slideshow"
overlays       = list collected from spec.captions_enabled + spec.lower_thirds
                  + spec.chapter_cards + spec.overlay_timeline flags
                  (short_engine.py:469-538)
```

**`long_engine.py:94-98, 224-230`** — same shape:

```
audio_name     = "song_suno"  if SONG else "tts_chunked"
timeline_name  = default "asr_anchors"
visualize_name = spec.visual_mode.value
music_name     = spec.music_policy.value
compose_name   = default "section_video"
```

`engine.pick_engine(spec)` at `engine.py:45-67` hardcodes:
- `RenderKind.SHORT` → `render_short`
- `RenderKind.LONG_FORM | SPORTS_DOC | FOOTAGE_ONLY` → `render_long`

### The aspirational-vs-actual gap

`spec.py:149-155` defines `VisualMode` enum values:

```
AI_BEAT_SLIDESHOW, MOTION_CLIPS, HYBRID_BEAT_FOOTAGE, FOOTAGE_WINDOWS,
LONGFORM_PANELS, ARCHIVAL_SHOTLIST, SPORTS_OVERLAY_TIMELINE
```

`grep register_plugin.\"visualize\"` finds impls for: `ai_beat_slideshow`,
`archival_shotlist`, `footage_filler`, `footage_windows`, `longform_panels`,
`visuals_from_fixture`.

**Missing impls** (enum value exists, no plugin registered):
- `motion_clips` — referenced in `spec.py:685` (`if cfg.get("motion_provider")
  return MOTION_CLIPS`) → would raise `PluginNotFound` if any channel YAML
  ever set `motion_provider`. Currently none do.
- `hybrid_beat_footage` — used in `spec.py:610,614` as the default for
  history/cosmos shorts → would raise `PluginNotFound`. The history/cosmos
  shorts apparently don't actually render via the short engine path that
  reads `visual_mode.value` literally, OR they're broken on this path.
- `sports_overlay_timeline` — used in `spec.py:607,696`.

Also referenced but not registered: `song_suno` — `short_engine.py:449` +
`long_engine.py:229` will return the string `"song_suno"` whenever
`spec.audio_mode == SONG`, and `get_plugin("audio", "song_suno")` would
raise `PluginNotFound`. `rhymetimejunction` and the sung-song flow at
`onboarding-qa.md:Q29,Q67` reference this but it doesn't exist as a plugin.

This isn't strictly an over-engineering smell — multiple-impl plugin
systems gracefully accommodate "stub now, fill later". But it means the
"6 slots × N impls" count is misleading. The honest count is **fewer
working impls than the registry suggests**, with some enum values
guaranteed to crash if a channel ever hits them.

### Per-slot earning-its-keep analysis

**audio (2 production impls):**
- Selection driver: `audio_mode + voice_provider` (`short_engine.py:439-450`).
- Engine difference: short uses `tts_single`, long uses `tts_chunked`. The
  pick is **hardcoded by engine**, not by spec — short_engine always returns
  `tts_single`, long_engine always returns `tts_chunked`.
- The only way to swap impl is via `spec.extra["audio_plugin"]` (test
  override).
- **In practice, this slot has 1 effective impl per engine.** If short
  always uses `tts_single` and long always uses `tts_chunked`, the abstraction
  is doing the work of a single `from .tts_single import synth` or
  `from .tts_chunked import synth` import. The `audio_from_fixture` impl is
  test-only. The hypothetical `song_suno` is unimplemented.
- **Verdict: NEEDS-DECISION.** If `song_suno` lands and the per-engine
  hardcoding disappears, the slot earns its keep. Today it's a 1-impl-per-
  engine indirection.

**timeline (2 production impls):**
- Same pattern: short hardcodes `asr_beats`, long hardcodes `asr_anchors`
  (`short_engine.py:165`, `long_engine.py:95`). The default constant is the
  only path; no spec field switches between them.
- **Verdict: NEEDS-DECISION.** 1-impl-per-engine indirection today.

**visualize (5 production impls):**
- Driven by `spec.visual_mode.value` → string → registry lookup
  (`short_engine.py:166, 458`).
- Channels DO pick different impls per `spec.py:591-617`:
  mystories=ai_beat_slideshow, history short=hybrid_beat_footage (unregistered!),
  history long=archival_shotlist, sports doc=sports_overlay_timeline
  (unregistered!).
- **Working multi-impl selection** for the channels that pick one of the 5
  registered impls.
- **Verdict: KEEP-as-plugin.** This is the slot the architecture justification
  is real for — the registered impls (`ai_beat_slideshow`,
  `archival_shotlist`, `longform_panels`, `footage_windows`, `footage_filler`)
  are genuinely different visualize strategies that benefit from the
  Protocol-driven dispatch. The unregistered enum values are a parallel
  issue (broken/aspirational paths).

**overlays (6 production impls):**
- LIST-VALUED slot — multiple producers run per render, picked by spec flags
  (`short_engine.py:469-538`). Captions producer is exactly one of {word,
  sentence}; others are independent booleans.
- **Working multi-impl selection** with multiple producers active per render.
- **Verdict: KEEP-as-plugin.** Genuinely list-valued; the alternative would
  be 6 hardcoded function calls inside each engine.

**music (4 production impls):**
- Driven by `spec.music_policy.value` → string → registry (`short_engine.py:167,
  461`).
- Multiple impls genuinely selectable.
- **Verdict: KEEP-as-plugin.**

**compose (2 production impls):**
- Engine-hardcoded: short defaults to `beat_slideshow`, long to
  `section_video` (`short_engine.py:168, 465`).
- Same 1-impl-per-engine pattern as audio + timeline. No spec field switches.
- **Verdict: NEEDS-DECISION.** 1-impl-per-engine indirection today.

### Verdict table

| Slot | Production impls | Spec-driven selection? | Verdict |
|---|---|---|---|
| audio | 2 (tts_single, tts_chunked) | NO — engine hardcodes | NEEDS-DECISION (collapse to direct import unless `song_suno` ships) |
| timeline | 2 (asr_beats, asr_anchors) | NO — engine hardcodes | NEEDS-DECISION (collapse to direct import) |
| visualize | 5 | YES — `spec.visual_mode` switches | KEEP-as-plugin |
| overlays | 6 | YES — list-valued, multiple producers per render | KEEP-as-plugin |
| music | 4 | YES — `spec.music_policy` switches | KEEP-as-plugin |
| compose | 2 (beat_slideshow, section_video) | NO — engine hardcodes | NEEDS-DECISION (collapse to direct import) |

### Recommendation

The plugin system earns its keep for **3 of 6 slots** (visualize, overlays,
music) where the spec drives genuine impl selection. The other 3 slots
(audio, timeline, compose) currently have 1 effective impl per engine — the
indirection is theoretical, not exercised.

That's not enough to dismantle the system. The architecture history
(`contracts.py:9-34`) shows the OLD 4-renderer split duplicated logic enough
that 2 engines + 6 slots cleared 8.3k LoC of duplication. Even with 3 slots
running as 1-impl-per-engine today, the surface area is sane and the
abstraction is non-obtrusive — `register_plugin` is a single function call,
`get_plugin` is a dict lookup, and the Protocols are duck-typed (no
inheritance contract).

Concrete tightening recommendations (not deletions):

1. **Honest count.** Document in `contracts.py` (or `ai/architecture.md`)
   that 3 of the 6 slots are engine-hardcoded today. Don't sell "6 plugin
   slots, fully dynamic" — sell "3 dynamic slots (visualize / overlays /
   music) + 3 conventionally-defaulted slots (audio / timeline / compose)".

2. **Fix the unregistered enum values.** Either register stubs that raise a
   clean "not implemented for this channel yet" error, or remove the enum
   values that no impl backs (`MOTION_CLIPS`, `HYBRID_BEAT_FOOTAGE`,
   `SPORTS_OVERLAY_TIMELINE`, `song_suno` audio impl). Today a channel YAML
   that triggers any of them will raise `PluginNotFound` mid-render with a
   misleading message ("plugin X not found in slot=visualize; available: …").

3. **Don't add slot #7.** The `contracts.py:7` and `contracts.py:47` say
   "seven plugin slot Protocols" but only 6 are actually instantiated
   (audio/timeline/visualize/overlays/music/compose). The `(registry helpers)`
   counted at `contracts.py:47` is not a slot. Documentation drift.

4. **Resist collapsing the 3 dynamic slots.** Per the LEAN charter, ask
   "can this be simpler?" — but for visualize/overlays/music, the answer is
   no without re-introducing the duplication that the 2026-05-14
   consolidation explicitly eliminated. Keep the system; just be honest
   about what it actually does today.

---

## Cross-audit summary

- **Audit 1 — Cloud Run services:** 11 KEEP (10 prod + editing-agent fragile),
  1 RETIRE outright (cobalt-api, zero Python callers), 1 NEEDS-DECISION
  (weights-staging), 7 parked in `_bench/` (RETIRE if not revived). Stale
  references to 6 retired services pollute `pipeline/cloud/services.py` and
  `cloud/render-worker-v2/entrypoint.py` preflight.
- **Audit 2 — LLM dispatcher:** Collapses cleanly from 3 → 2. `anthropic_sdk`
  has tests but no production deployment, no runbook, and CLAUDE.md treats it
  as "intentional." Delete unless an Azure-outage runbook is written.
- **Audit 3 — Plugin system:** 3 of 6 slots (visualize, overlays, music) are
  spec-driven and earn the abstraction; 3 of 6 (audio, timeline, compose) are
  engine-hardcoded 1-impl-per-engine. Don't collapse — the historical
  duplication-cost was real — but document the honest split and prune the
  enum values that no impl backs.
