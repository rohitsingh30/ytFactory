# Architecture

> Last updated 2026-05-23 (post-Q&A refactor).

How ytFactory's parts connect. Code is the source of truth — every claim
here is pinned to a file:line so a future agent can re-verify when this
doc drifts.

## The three runtime planes

1. **Control plane** (laptop dev + `cloud/web-server/` + `cloud/web-next/`)
   FastAPI routes under `control/routes/` accept render proposals from
   the wizard at `/app/create`, write a job doc to Firestore via
   `control/core/jobs.py:create_job` (`control/core/jobs.py:169`), and
   dispatch a render via `control/core/cloud_run.py:trigger_render_job`
   (`control/core/cloud_run.py:90`). The control plane never executes a
   render itself — it is a state machine that mutates Firestore and
   triggers Cloud Run. Per ADR-028, `control/core/*` is the canonical
   state-module location; the flat siblings have been deleted.

2. **Render worker** — Cloud Run **Job** (not service) named
   `ytfactory-render-worker-v2`, one execution per render. Entry point
   `cloud/render-worker-v2/entrypoint.py::_main_from_firestore`
   (`cloud/render-worker-v2/entrypoint.py:2290`). Reads
   `YTFACTORY_JOB_ID` from env (set per-execution by the SDK call at
   `control/core/cloud_run.py:124`), fetches `jobs/<job_id>` from
   Firestore, walks the seven stages declared in `STAGES`
   (`cloud/render-worker-v2/entrypoint.py:85`), writes the mp4 + thumb
   to `gs://<bucket>/jobs/<job_id>/` and updates the job doc. The
   post-render sanity check lives in the extracted `writeback.py`
   module beside `entrypoint.py` (P3.7 — no duration floor; sanity-only).

3. **Cloud Run GPU services** under `cloud/*/` — each is a service (not
   a job), HTTP-callable, GPU-attached, autoscaled `min=0,max=1`. The
   worker calls them via the `CLOUDRUN_*_URL` env vars set by
   `cloud/render-worker-v2/deploy.sh`. Today's services on disk
   (post-2026-05-23 cleanup, see also `pipeline/cloud/services.py`):
   `image-z-image-turbo/`, `tts-chatterbox/`, `tts-indicf5/`,
   `asr-whisper/`, `editing-agent/`, `clone-video-worker/`,
   `stats-refresh/`, `web-server/`, `web-next/`, `weights-staging/`.
   The 4 dead env-URL exports (flux2-klein, flux2-dev, qwen,
   indicparler) and the corresponding service scaffolds have been
   removed (Phase 3 of refactor-plan.md). `pipeline/cloud/services.py`
   lists exactly 8 services that are actually deployed and called.

## Render dispatch: the engine + 6 plugin slots

Every render in ytFactory is described by a single
[`RenderSpec`](`pipeline/render/spec.py:393`). The spec is built once at
the top of the worker via
[`build_spec`](`pipeline/render/spec.py:794`) and never mutated again.
The renderer dispatches as:

```
render_via_engines(spec, ...)        # pipeline/render/video.py:81
    └─> pick_engine(spec)            # pipeline/render/engine.py:45
            └─> render_short  OR  render_long
                └─> get_plugin("<slot>", spec.<field>.value)
```

`pipeline/render/video.py` exports only two public functions today:
`render_via_engines` (the engine entry; the worker calls this for
short kind) and `render_long_form` (the long-form orchestrator that
runs rewrite → cast → in-process `render_via_engines`). The legacy
`render()` shim has been deleted (ADR-027 implemented).

Six slots, declared as Protocols in
[`pipeline/render/contracts.py`](`pipeline/render/contracts.py:1`):

| Slot       | Protocol            | Selected by               | Plugin dir                          |
|------------|---------------------|---------------------------|-------------------------------------|
| audio      | `AudioSynthesizer`  | `audio_mode`+`voice_provider` | `pipeline/render/audio/`        |
| timeline   | `TimelineBuilder`   | engine default (`asr_beats` / `asr_anchors`) | `pipeline/render/timeline/` |
| visualize  | `VisualProducer`    | `spec.visual_mode.value`  | `pipeline/render/visualize/`        |
| overlays   | `OverlayProducer`   | LIST-valued — `captions_enabled`, `lower_thirds`, `chapter_cards`, `overlay_timeline`, `closer_panel` | `pipeline/render/overlays/` |
| music      | `MusicComposer`     | `spec.music_policy.value` | `pipeline/render/music/`            |
| compose    | `FinalMux`          | engine default (`beat_slideshow` / `section_video`) | `pipeline/render/compose/` |

Plugins self-register at import via
`register_plugin(slot, name, impl)` (`pipeline/render/contracts.py:529`).
The engine has zero `if visual_mode == ...` branches — it calls
`get_plugin(slot, name)` and that's it. Adding a new visual mode is a
new file + one `register_plugin` call.

**Per ADR-032**, the 6-slot abstraction stays. Three of the slots
(audio/timeline/compose) are kind-discriminated by `spec.kind` (short
vs long engines pick their fixed impl); the other three
(visualize/overlays/music) are spec-driven multi-impl. Collapsing the
three kind-discriminated slots to direct imports would re-implement
the `if spec.kind == SHORT: ... else: ...` discriminator in three
`if/else` blocks per engine — net zero LoC, higher coupling. Keep the
abstraction.

`spec.extra["<slot>_plugin"]` overrides the resolved plugin name —
used by tests + fixture loading (see `short_engine.py:457` for the
visualize override).

The closer-panel overlay is wired via `spec.closer_panel: bool`
(`pipeline/render/spec.py:465`). When True, both short and long
engines' `_collect_overlays` invoke `get_plugin("overlays",
"closer_panel")` (`short_engine.py:600`; long engine imports the same
helper). The legacy dead code in `compose.py:711-719` that forced
`closer_panel_path = None` has been deleted.

## RenderSpec source-precedence

`build_spec` (`pipeline/render/spec.py:794`) merges 4 sources, highest
priority last:

1. Inferred defaults (e.g. `length_s > 120 → kind=LONG_FORM` per
   `_LONG_FORM_THRESHOLD_S` at `pipeline/render/spec.py:619`)
2. Base channel YAML (`pipeline/channels/<channel>.yaml`)
3. Variant overlay (`pipeline/variants/<channel>/<niche>.yaml`)
4. Form overrides (`proposal.channel_overrides` from the wizard)

Phantom niches (form picks a niche with no overlay YAML on disk) do NOT
silently fall back — they get a `spec.notes` entry surfaced in the
dashboard (`pipeline/render/spec.py:849`).

Form overrides pass through `pipeline.render.input_registry.apply_overrides`
(`pipeline/render/spec.py:881`) which can reject invalid values and
record them in `cfg["_dropped_inputs"]`. Voice override is the canary
case — see `spec.py:992`.

## Channel vs niche

A **channel** is a YouTube destination + a render style profile
(`pipeline/channels/<channel>.yaml`). A **niche** is a topic-area
overlay on that channel (`pipeline/variants/<channel>/<niche>.yaml`)
that tweaks tone, source adapter, prompts. Niches are NOT per-channel
exclusive — `mystoriesanimated` has 13 variants in `pipeline/variants/`;
`sportsrecapped` has its own set. Other channels (`historyrecapped`,
`cosmosdecoded`, `rhymetimejunction`, `hindutavaanimated`,
`scrollpulse`) currently have no variant overlays — they render
directly from base channel YAML.

`pipeline.niches.NICHE_CHANNEL` is the authoritative map from
`variant_yaml → (channel, niche_dir)`. `RenderPaths.from_channel_yaml`
(`pipeline/paths.py:213`) reads it.

## Image-gen architecture

Z-Image-Turbo is the SOLE production image model (ADR-024, locked
operating principle 6). All 7 channel YAMLs set
`image_provider: cloudrun_z_image_turbo`. The earlier FLUX.2 klein
service was never on the production path and has been retired (no
`cloud/image-flux2-klein/` source dir, no env URL in deploy.sh).

The refiner at `pipeline/images/prompt_refiner.py` has been rewritten
for z-turbo (P4.1, ADR-008): `REFINER_VERSION = "v2-zturbo"` at
`prompt_refiner.py:85`. The refiner now emits 80-250-word structured
prompts (positive-only, lighting-token-heavy: "soft diffused
daylight", "cinematic warm key light", "noir high-contrast",
"rim lighting") instead of the klein-shaped 4-10-word output. Cached
refined-* fields from the klein era auto-invalidate via the version
bump.

## LLM dispatcher

`pipeline/llm/cli.py` exports a 3-backend dispatcher: `cli` (laptop
default, shells out to `claude` binary), `azure_openai` (cloud worker
default per `deploy.sh:90`), and `anthropic_sdk`. Per ADR-031, the
anthropic_sdk backend is a *documented escape hatch*, not vestigial:
the operator switches via `YTFACTORY_LLM_BACKEND=anthropic_sdk +
--update-secrets ANTHROPIC_API_KEY=...` per the runbook at
`cloud/render-worker-v2/deploy.sh:134-137`. The backend has 5 unit
tests in `test_pipeline_llm.py` and exists as a cost-fallback when
Azure OpenAI is unavailable. Default in prod stays
`azure_openai`; anthropic_sdk fires only when explicitly toggled.

`reasoning_effort` defaults at `pipeline/llm/cli.py:304`. Per ADR-030
the `rewrite_long_form` stage default is `medium` (reverted from
`minimal` on 2026-05-23, P3.4).

## Cast schema

Per P4.3 (Q70, ADR-009) the cast LLM emits **structured fields per
character** rather than a free-form description. Required fields:
`age`, `hair`, `build`, `clothing`, `signature_prop`,
`default_emotion`, plus the supporting-characters array each with the
same shape. `pipeline/llm/cast.py` enforces these in the schema; the
spec-enrich step at `pipeline/render/spec_enrich.py:222` assembles
the structured fields into `spec.extra["character_descriptions"]`
(plural) and back-fills the singular `spec.extra["character_description"]`
from the narrator's spec string so downstream beat-prompt assembly
keeps working unchanged.

## IndicF5 chunk-0 ref_text behavior

Per P4.4 (Q71, ADR-016) the IndicF5 chunked TTS path at
`pipeline/tts/cloudrun.py:825` requires `ref_audio_text` and swaps the
ref_text to chunk-0's emitted text from chunk 1 onward so the timbre
stays stable across a long render (`cloudrun.py:799-805`). The
caller-supplied ref_text is used only for chunk 0; later chunks reuse
chunk-0's own narration as the reference. Hindutava renders now ship
comprehensible Hindi.

## State stores

- **Firestore** (`ytfactory-prod-v3`) — job state, queue tasks,
  scheduler state, rate limits. Collections enumerated in
  `state-management.md`.
- **GCS** `gs://ytfactory-prod-v3-artifacts/` — final mp4s, thumbs, and
  per-job artifacts laid out as `jobs/<job_id>/{short.mp4, thumb.jpg,
  state.json, renderer.log, video/, script/, ...}`. Path conventions in
  `state-management.md`.
- **Local channel dirs** `data/<channel>/[<niche>/]{narrations,cache,
  shorts,long_form,uploads,...}` — gitignored render output + tracked
  narration/upload JSON. Single source of truth for the layout:
  `pipeline/paths.py::RenderPaths` (`pipeline/paths.py:157`); the
  channel root now resolves to `data/<channel>/`
  (`pipeline/paths.py:287-298`).

## Gates as repair triggers (locked operating principle)

Per ADR-023 (promoted from ADR-003): every gate in the pipeline
follows the granular-retry-then-raise shape. Gate fires → retry the
failing piece (section / beat / chunk) with a bounded retry cap →
raise + propagate on exhaustion. Quote: *"Gates STAY. They are repair
triggers, not termination signals."* This is the single most
architecturally-consequential principle in the system and shapes
every retry path in the rewrite, image-gen, and TTS stages.

## The 5 cost rules

Pinned in `CLAUDE.md` lines 90-128 and enforced per-`deploy.sh`. They
exist because each has cost a real GCP bill line:

1. `min-instances=0` on every GPU service.
2. `max-instances=1` on every GPU service.
3. `--region=asia-southeast1` on every `gcloud builds submit`
   (otherwise build runs in global pool → cross-Pacific egress to
   AR).
4. Double-checked `threading.Lock()` around `_model()` / `_pipe()` in
   every GPU service — naked `if _MODEL is None:` races on
   `/readyz` + first inference call.
5. Don't bake huge weights into Docker images (use GCS Fuse mount).

## What's structurally fragile

1. **Default project drift.** `control/core/cloud_run.py:50` declares
   `DEFAULT_PROJECT = "ytfactory-prod-v2"` and
   `cloud/render-worker-v2/entrypoint.py:136`/`:139` defaults to
   `ytfactory-prod-v2` + `ytfactory-prod-v2-artifacts`. The real
   project is **`ytfactory-prod-v3`**. Phase 2/3 of refactor-plan.md
   is supposed to fix this — verify post-merge that the bare defaults
   point at v3.

2. **`spec.kind` discriminator has 4 values but `pick_engine` maps 3
   of them to the same engine** (`engine.py:63`). The transitional
   collapsing of `SPORTS_DOC` + `FOOTAGE_ONLY` into `LONG_FORM` is
   half-done — the enum still exists. The actual discriminator now is
   `visual_mode`, not `kind`.

3. **Overlay caption "fail-loud" applies only to captions.** The other
   overlay producers (`lower_third`, `chapter_card`,
   `anchored_footage`, `closer_panel`) at `short_engine.py:516-538` +
   600-606 still warn-and-skip on failure. If a render opts into
   chapter cards and the plugin raises, the render ships without
   chapter cards and the user has no indication. The "user opted in"
   defense is weak.

4. **`build_spec` NEVER raises** (`spec.py:817`). Every error becomes a
   `spec.notes` entry. This is intentional but it means a render can
   start with a partially-resolved spec; bugs in apply_overrides only
   surface via the notes field that the dashboard MAY or may not
   render.

5. **`part2_watcher` writer/reader split [[F24]].**
   `pipeline/upload/upload.py` writes
   `data/intermediate/<chan>/part2_pending/<slug>.json` sidecars on
   every Part-1 cliffhanger upload. `pipeline/part2_watcher.py`
   defines the poll-and-fire reader, but no plist / cron / launchd
   entry invokes it as a daemon. Sidecars accumulate; Part-2 never
   auto-fires. Fix: add a launchd plist or cron schedule pointing at
   the watcher, OR delete the writer side along with the watcher.
   Half-built — call this out, don't paper over.
