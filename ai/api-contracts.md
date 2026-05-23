# API Contracts

> Last updated 2026-05-23 (post-Q&A refactor).

The surface area an external caller (or another agent) talks to: the six plugin Protocols, the Firestore `jobs/<id>` doc, the HTTP route surface, the LLM dispatcher, and the plugin registry. Everything below cites the file:line that defines it — those are authoritative; this file paraphrases.

---

## 1. Plugin Protocols (6 slots)

Defined in `pipeline/render/contracts.py`. Engines (`short_engine`, `long_engine`) dispatch by slot name + selector field on `RenderSpec`. Engines do NOT branch on enum values — they call `get_plugin(slot, name)` and invoke the returned object. Each Protocol is `@runtime_checkable` (PEP 544 structural typing) so impls don't import the Protocol.

| # | Slot       | Protocol (file:line)                                  | Selector — `RenderSpec` field         | Output                          |
|---|------------|-------------------------------------------------------|---------------------------------------|---------------------------------|
| 1 | `audio`    | `AudioSynthesizer` — `pipeline/render/contracts.py:363` | `audio_mode` + `voice_provider`       | `AudioResult`                   |
| 2 | `timeline` | `TimelineBuilder` — `pipeline/render/contracts.py:383`  | engine-chosen (short→`asr_beats`, long→`asr_anchors`) | `Timeline = list[Segment]`      |
| 3 | `visualize`| `VisualProducer` — `pipeline/render/contracts.py:402`   | `visual_mode`                          | `VisualTrack`                   |
| 4 | `overlays` | `OverlayProducer` — `pipeline/render/contracts.py:429`  | LIST-valued: `captions_layout`, `lower_thirds`, `chapter_cards`, `overlay_timeline`, `closer_panel` flags | `list[OverlayElement]`          |
| 5 | `music`    | `MusicComposer` — `pipeline/render/contracts.py:458`    | `music_policy`                         | `Path` to mp3/wav               |
| 6 | `compose`  | `FinalMux` — `pipeline/render/contracts.py:480`         | engine-chosen                          | `Path` to final mp4             |

Method signatures (string-forward references for `RenderSpec` to avoid circular import):

- `AudioSynthesizer.synth(spec, script: dict, work_dir: Path) -> AudioResult`
- `TimelineBuilder.build(spec, script: dict, audio: AudioResult) -> Timeline`
- `VisualProducer.produce(spec, timeline: Timeline, work_dir: Path) -> VisualTrack`
- `OverlayProducer.produce(spec, timeline: Timeline, audio: AudioResult) -> list[OverlayElement]`
- `MusicComposer.compose(spec, narration_duration_s: float, sections: list[Section] | None = None) -> Path`
- `FinalMux.mux(visuals: VisualTrack, audio: AudioResult, overlays: list[OverlayElement], music: Path, spec, out_path: Path) -> Path`

The `overlays` slot is list-valued. The engine collects every active producer (one caption producer chosen by `captions_layout`, plus any of lower-third / chapter-card / anchored-footage toggled by bool flags), concatenates the lists, and hands the merged `list[OverlayElement]` to the FinalMux. Compositing order is `(layer, start_s)` — layer convention in `OverlayElement` docstring: 10 = anchored foreground, 20 = lower-thirds, 30 = chapter cards, 40 = captions, 50 = watermark.

### Fail-loud contract

`RenderFailedError` — `pipeline/render/contracts.py:119`. Plugins MUST raise this (subclass of `RuntimeError`) instead of returning a degraded artifact (solid-color visual, empty caption list, silent audio, frozen-frame tail). Error messages must include file:line of the originating site and chain the underlying cause with `raise … from exc`. Single env override `YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1` re-enables the silent-degrade path for emergency renders.

---

## 2. Plugin Registry

`pipeline/render/contracts.py:526` — single module-level dict `_REGISTRY: dict[slot, dict[name, impl]]`.

- `register_plugin(slot, name, impl)` — `pipeline/render/contracts.py:529`. Called at import time from each plugin module (e.g. `register_plugin("audio", "tts_chunked", TtsChunked())` at `pipeline/render/audio/tts_chunked.py:223`). Re-registering replaces (useful for tests). No Protocol enforcement at registration — duck typing wins, mypy / per-plugin tests catch drift.
- `get_plugin(slot, name)` — `pipeline/render/contracts.py:555`. Raises `PluginNotFound` (subclass of `LookupError`, `pipeline/render/contracts.py:512`) with the available names listed.
- `list_plugins(slot)` — `pipeline/render/contracts.py:574`. Used by the wizard / dashboard to populate dropdowns without a hardcoded mirror.

Plugin modules eager-import each other through `pipeline/render/<slot>/__init__.py` so registration is complete by the time the engine runs.

Currently registered (one register_plugin call per file):

- audio: `tts_single`, `tts_chunked`, `audio_from_fixture`
- timeline: `asr_beats`, `asr_anchors`, `timeline_from_fixture`
- visualize: `ai_beat_slideshow`, `archival_shotlist`, `longform_panels`, `footage_filler`, `footage_windows`, `visuals_from_fixture` (plus `motion_clips`, `hybrid_beat_footage` documented in the Protocol)
- overlays: `word_caption_pngs`, `sentence_caption_ass`, `chapter_card`, `lower_third`, `anchored_footage`, `closer_panel` (NEW, R4), `noop`
- music: `silent` (registered as name `"none"`), plus `ducked_loop`, `single_bed`, `section_mood` documented in the `MusicPolicy` enum
- compose: `beat_slideshow_mux`, `section_video_mux` documented in `FinalMux`

---

## 3. Firestore `jobs/<job_id>` doc

Single source of truth for one render's lifecycle. Owner: `control/core/jobs.py`. Schema documented at the top of that file plus enforced by the helper functions below.

| Field             | Type     | Set by                                                       |
|-------------------|----------|--------------------------------------------------------------|
| `job_id`          | str      | `create_job` (`control/core/jobs.py:169`)                    |
| `channel`         | str      | `create_job` — channel slug (`mystoriesanimated` etc.)       |
| `topic`           | str      | `create_job`                                                 |
| `proposal`        | dict     | `create_job` — full `ShortProposal.model_dump()`             |
| `owner_uid`       | str?     | `create_job` — Firebase UID; None for scheduler / anon       |
| `slug`            | str      | `create_job` — derived from topic until worker overwrites    |
| `render_kind`     | str      | `create_job` — `short` if `length_s≤120` else `long_form`    |
| `status`          | str      | `STATUS_*` constants — `pipeline/rendering/uploading/researching/done/failed` (`control/core/jobs.py:161-166`) |
| `stage`           | str      | `mark_stage` — one of `queued / rewrite / cast / images / tts / asr / compose / gcs_upload / youtube_upload / research_handoff / dispatching / done` |
| `cloud_execution` | str?     | `_enqueue_render_job` — Cloud Run execution name when backend=cloudrun |
| `created_at`      | datetime | `create_job`                                                 |
| `updated_at`      | datetime | every write — `_utcnow()` (`control/core/jobs.py:48`)        |
| `short_uri`       | str?     | `mark_done` — `gs://ytfactory-prod-v3-artifacts/jobs/<id>/short.mp4` |
| `youtube_url`     | str?     | `mark_done` — `https://youtu.be/<id>` when YT publish succeeded |
| `thumb_uri`       | str?     | `mark_done` — GCS path                                       |
| `error`           | str?     | `mark_failed` — capped at 2000 chars (`control/core/jobs.py:255`); cleared on `mark_done` |
| `render_spec`     | dict?    | cloud worker writes `RenderSpec.to_dict()` so the dashboard can show the interpreted inputs (see `pipeline/render/spec.py:534`) |

Backend toggle: `YTFACTORY_QUEUE_BACKEND=firestore` selects `_FirestoreJobs` else `_MemoryJobs` (tests). Firestore client targets `os.environ["GOOGLE_CLOUD_PROJECT"]` defaulting to `"ytfactory-prod"` (`control/core/jobs.py:95`) — production is `ytfactory-prod-v3` and is set by the deployed service's env.

Helper API:
- `create_job(...)` — `control/core/jobs.py:169`
- `mark_stage(job_id, status, stage, **extra)` — `:210`
- `mark_done(job_id, short_uri, youtube_url?, thumb_uri?, slug?, render_kind?)` — `:215`
- `mark_failed(job_id, stage, error, slug?, render_kind?)` — `:236`
- `get_job(job_id)` — `:264`

The reciprocal `ConfirmResponse` Pydantic model (`control/core/jobs.py:36`) is returned from `/api/render` and the scheduler — `{job_id, task_id, proposal}`.

The proposal payload is `ShortProposal` — `control/core/schema.py:77`: `channel`, `format` (niche), `topic`, `source_kind`, `source_ref`, `length_s` (default 55), `notes`, `channel_overrides: dict`, `internal_only: bool`. Test-fixture topics get auto-flagged `internal_only=True` by `_apply_test_fixture_autoflag` (`control/core/jobs.py:273`).

The queue's `tasks/` collection is separate — `TaskEnvelope` at `control/core/schema.py:55`, `TaskKind` enum at `:18`. One job spawns one or more tasks. The cloud-run backend short-circuits the queue and triggers the Cloud Run JOB directly (`_enqueue_render_job` at `control/core/jobs.py:301` — branches on `YTFACTORY_RENDER_BACKEND`).

---

## 4. HTTP route surface

`control/routes/*` is the canonical router location (per ADR-028).
The legacy flat route files (`control/{agent,dashboard,niche,
scheduler}_routes.py`) have been deleted. Two top-level shims remain:

- `control/chat_routes.py` — `POST /api/chat`, `/api/chat/confirm`
  (chat-driven proposal helper). No v2 sibling; owns shared helpers
  consumed by `control/chat_service.py`.
- `control/render_routes.py` — a narrow shim retained while the last
  callers migrate to `control/routes/render_routes.py`. Scheduled for
  deletion once the migration completes.

Both servers — `web/server.py` (production) and `control/server_dev.py`
(local dev) — mount the same canonical router set from
`control/routes/*`. `server_dev.py` additionally mounts
`control.chat_routes.router` (no v2 equivalent). The two
`include_router(...)` surfaces are kept in sync; a Phase-2 audit
diffed the two and confirmed they match.

### `control/routes/` (canonical, mounted in both prod + dev)

| File                       | Purpose (from module docstring)                                                                 |
|----------------------------|-------------------------------------------------------------------------------------------------|
| `agent_routes.py`          | Laptop-agent verbs: `POST /agent/heartbeat`, `POST /agent/lease`, ack                          |
| `auth_pin.py`              | Single-user PIN session gate for `/app/*` and write endpoints                                  |
| `channels_routes.py`       | Channel registry + customization schema for the `/app/create` wizard                           |
| `clone_video_routes.py`    | Clone-a-video backend — analyses a video URL, emits a niche fingerprint                        |
| `cloud_routes.py`          | Cloud admin tab — service health, cost snapshots, warm                                         |
| `critique_routes.py`       | Critique-chat (2026-05-11) — two HTTP endpoints; the rest is Firestore `onSnapshot`            |
| `dashboard_routes.py`      | Per-upload YouTube analytics — uses Data API v3 `YOUTUBE_API_KEY`                              |
| `discover_routes.py`       | `POST /api/discover/{channel}` + `/feed` — auto-generate topic (fetch-first, LLM fallback)     |
| `music_routes.py`          | `GET /api/music/catalog` + sample audio                                                        |
| `niche_routes.py`          | Public read-only: `/api/niches`, `/api/voices`, `/api/voice_clones`                            |
| `niche_specs_routes.py`    | Per-channel niche JSON CRUD (`NicheDoc`)                                                       |
| `oauth_web_routes.py`      | `/api/oauth/start` + callback — connect a YouTube channel from the studio UI                   |
| `render_routes.py`         | `POST /api/render`, `GET /api/jobs[/{id}/preview.mp4|artifact/{kind}]`, `/publish`, `/cancel`, `/api/queue`, `/api/critiques/{channel}/{slug}`, `/api/health` |
| `scheduler_routes.py`      | `POST /api/scheduler/tick` — Cloud Scheduler fires every 30 min; bearer `YTFACTORY_AGENT_TOKEN` |
| `script_jobs_routes.py`    | `POST /api/jobs/from_script` — skill → cloud render entry; validates entry-point allow-list    |
| `song_sample_routes.py`    | `GET /api/songs/sample/{filename}`                                                             |
| `state_routes.py`          | Channel-state CRUD against `gs://ytfactory-prod-v2-state` (note: bucket name predates v3 migration; verify) |
| `telemetry_routes.py`      | `/api/telemetry/overview?hours=24` totals + success-rate                                       |
| `voices_routes.py`         | Voice catalogue, sample wav, clone-upload                                                      |

Adding new endpoints: write under `control/routes/`. Do not
re-introduce top-level flat route files.

---

## 5. LLM dispatcher

Public entry point `call_claude_cli(...)` — `pipeline/llm/cli.py:524`. The name is preserved for back-compat; `call_llm` is an alias (`:627`). Dispatches by `YTFACTORY_LLM_BACKEND`:

| Backend          | Const                 | Where it runs                                              |
|------------------|-----------------------|------------------------------------------------------------|
| `cli`            | `BACKEND_CLI`         | Laptop default — shells out to `claude` binary (OAuth Pro/Max). Vision-aware (`add_dirs`, `allowed_tools=["Read"]`). |
| `azure_openai`   | `BACKEND_AZURE`       | Cloud render-worker default per `deploy.sh:90` — `openai.AzureOpenAI` |
| `anthropic_sdk`  | `BACKEND_ANTHROPIC`   | Documented escape hatch (ADR-031). Operator switches via `YTFACTORY_LLM_BACKEND=anthropic_sdk` + `--update-secrets ANTHROPIC_API_KEY=...` per the runbook at `cloud/render-worker-v2/deploy.sh:134-137`. 5 unit tests in `test_pipeline_llm.py`. Not vestigial. |

Selection precedence in `_choose_backend` (`pipeline/llm/cli.py:67`): explicit env → `claude` on PATH → Azure creds → `ANTHROPIC_API_KEY` → CLI fallback. Vision-aware kwargs (`add_dirs`, `allowed_tools`) raise `ClaudeCLIError` on the SDK backends — cloud worker only invokes pure-text stages.

### Public signature (`pipeline/llm/cli.py:524`)

```python
call_claude_cli(
    prompt: str,
    *,
    output_json: bool = True,
    json_schema: dict | None = None,
    strict_schema: bool = False,
    add_dirs: list[Path] | None = None,        # cli backend only
    allowed_tools: list[str] | None = None,    # cli backend only
    model: str = "haiku",                      # tier alias: haiku / sonnet / opus
    timeout_s: int = 600,                       # DEFAULT_TIMEOUT_S
    budget_usd: float = 2.00,                   # DEFAULT_BUDGET_USD
    stage: str | None = None,                   # telemetry tag + model/maxtokens lookup
) -> str | dict
```

### Per-stage model defaults (`pipeline/llm/cli.py:152`)

All stages default to `opus` (`cast`, `rewrite`, `rewrite_long_form`, `prompts`, `critic`, `audio_critic`, `imitate_analyze`, `imitate_apply`) except `prompt_refine` which is `haiku`. Override per stage via `YTFACTORY_MODEL_<STAGE>` (e.g. `YTFACTORY_MODEL_REWRITE=sonnet`).

Tier → concrete-model maps:
- Azure (`_AZURE_TIER_DEFAULTS` at `:404`): `haiku→gpt-4o-mini`, `sonnet→gpt-4o-mini`, `opus→gpt-4o`. Production runs with `AZURE_OPENAI_MODEL=gpt-5.3-chat` overriding the per-tier defaults — set in `cloud/render-worker-v2/deploy.sh:92`.
- Anthropic (`_ANTHROPIC_TIER_DEFAULTS` at `:487`): `haiku→claude-haiku-4-5`, `sonnet→claude-sonnet-4-5`, `opus→claude-opus-4-7`.

### Per-stage max_tokens (`pipeline/llm/cli.py:186`)

`rewrite_long_form` = 64000; `rewrite`/`cast`/`prompts`/`critic`/`audio_critic`/`imitate_*`/`prompt_refine` = 8192; fallback = 4096. Per-stage env override `YTFACTORY_MAX_TOKENS_<STAGE>`. Auto-bump on `finish_reason=length` retries with doubled budget capped at `YTFACTORY_MAX_TOKENS_AUTO_BUMP_CEILING` (default 64000, `:253`). For Azure deployments where `max_completion_tokens` is required (gpt-5.x / o1 / o3), the dispatcher discovers the right key on first call and caches per-deployment; `AZURE_OPENAI_TOKEN_PARAM` skips discovery.

### Reasoning effort (`pipeline/llm/cli.py:304`)

`_DEFAULT_REASONING_EFFORT_BY_STAGE`: `rewrite_long_form="medium"` (reverted from `minimal` on 2026-05-23 per P3.4/ADR-030; pinned by test so future cost-pressure flips are visible), `critic="medium"`. Fallback = `minimal`. Override per-stage `YTFACTORY_REASONING_EFFORT_<STAGE>` (value: minimal/low/medium/high/off/none). Discovered per-deployment; on 400 rejection the param is dropped + remembered. `AZURE_REASONING_EFFORT_DISABLE=1` skips entirely.

### Errors

- `ClaudeCLIError` (`:118`) — subprocess error, SDK HTTP error, JSON parse error, schema-validation error
- `ContentFilterError` (`:122`, subclass) — Azure `finish_reason=content_filter`. Callers (notably `rewrite_long_form._generate_all_section_bodies`) catch and fall back to the section's brief instead of crashing the whole render.

### JSON robustness

`_parse_inner_json` (`pipeline/llm/cli.py:806`) is tolerant of markdown fences + bracket-balance over strings. `_salvage_truncated_json` (`:888`) recovers a partial dict from mid-string truncation (mid-section gpt-5.x output truncations). Both happen below the call site; callers just see `dict | list`.
