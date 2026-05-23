# Runtime Flows

> Last updated 2026-05-23 (post-Q&A refactor).

End-to-end render lifecycle from user click to mp4 in GCS. Cite the
exact file:line so the next agent doesn't have to re-grep.

## The end-to-end render pipeline

```
User                                                Cloud
┌──────────────────────────┐                 ┌────────────────────────────┐
│ /app/create wizard       │                 │ Firestore: jobs/<job_id>   │
│ (web-next/app/app/create │ POST /api/render│  status=pending → ...      │
│  /page.tsx)              │ ───────────────►│                            │
└──────────────────────────┘                 └────────────────────────────┘
                                                   ▲             │
                                                   │             │
                                                   │             ▼
                                                   │     ┌──────────────────┐
                                                   │     │ Cloud Run JOB    │
                                                   │     │ render-worker-v2 │
                                                   │     │ entrypoint.py    │
                                                   │     │ _main_from_      │
                                                   │     │   firestore()    │
                                                   │     └──────────────────┘
                                                   │             │
                                                   │             ▼
                                            7 stages, written back to jobs/<job_id>:
                                            rewrite → cast → (images || tts → asr) → compose → upload
                                                                 │
                                                                 ▼
                                                  gs://ytfactory-prod-v3-artifacts/
                                                       jobs/<job_id>/short.mp4
```

## Stage 0 — user triggers a render

1. User opens `/app/create` (`web-next/app/app/create/page.tsx`).
2. Picks mode (Channel-focused vs Clone-a-video; AI-generated is
   disabled per `onboarding-qa.md` Q20), channel, form (short/long),
   niche, topic (typed or "Auto-generate"), voice, audio mode, visual
   source, music bed, advanced fields.
3. Submit → `renderApi.enqueue(...)` → `POST /api/render` →
   `control/routes/render_routes.py` (the canonical and only location;
   the legacy flat-shim `control/render_routes.py` is the migration
   holdout slated for deletion).
4. The route validates the payload, rate-limits, and calls
   `control.core.jobs._enqueue_render_job(proposal, owner_uid=...)`
   (`control/core/jobs.py:301`).

## Stage 1 — job creation + dispatch

`_enqueue_render_job` (`control/core/jobs.py:301`):

1. Test-fixture auto-flag (`jobs.py:344`) — topics matching the dev
   pattern get `internal_only=True`.
2. Mint `job_id = uuid4().hex`.
3. `create_job(job_id, ...)` writes the doc to Firestore
   (`jobs.py:362-368`). Initial status `pending`, stage `queued`.
   Slug + render_kind seeded (`jobs.py:187-196`).
4. **Pre-warm** cloud GPU services for this channel (`jobs.py:382`) —
   `warm_async_http(channel)` fires HTTP warmup against
   chatterbox/indicf5/image services so the first synth call doesn't
   pay cold-start latency.
5. Dispatch by `render_backend()` (`jobs.py:393`):
   - `cloudrun` → `cloud_run.trigger_render_job(job_id)`
     (`control/core/cloud_run.py:90`) calls `JobsClient.run_job` with
     `YTFACTORY_JOB_ID=<job_id>` as a per-execution env override
     (`cloud_run.py:124`).
   - `sim` → in-process `control/core/sim_worker.py` picks it up.
   - `laptop` → DEPRECATED, treated as `sim`.
6. Job doc updated: `stage=dispatching, cloud_execution=<execution_name>`.

## Stage 2 — Cloud Run Job execution boot

`cloud/render-worker-v2/entrypoint.py::_main_from_firestore`
(`entrypoint.py:2290`):

1. Read `YTFACTORY_JOB_ID` from env.
2. `_preflight()` (`entrypoint.py:166`) checks `GOOGLE_CLOUD_PROJECT`,
   `YTFACTORY_BUCKET`, Azure OpenAI keys, at least one TTS URL, ASR
   provider. Returns a list of `(env_key, friendly_message)` — empty
   = OK. Failures exit before any Firestore write.
3. Fetch job doc: `jobs/<job_id>` via Firestore client
   (`entrypoint.py:141-143`).
4. Build the `RenderSpec` via `pipeline.render.spec.build_spec` from
   `proposal` + channel YAML + variant overlay (the worker invokes
   this on the long-form branch; short branch builds it inline).
5. Walk `STAGES` (`entrypoint.py:85`), writing
   `{status, stage, ...}` back to Firestore at each transition.

## Stage 3 — the seven stages

```
STAGES = [
    ("rewrite", "Rewriting script"),
    ("cast",    "Casting voice & visuals"),
    ("images",  "Generating images"),
    ("tts",     "Synthesizing narration"),
    ("asr",     "Aligning captions"),
    ("compose", "Composing video"),
    ("upload",  "Uploading to GCS"),
]
```

(`cloud/render-worker-v2/entrypoint.py:85`.)

### Worker → engine entry

The worker invokes `pipeline.render.video.render_long_form` directly
on the long-form branch (`entrypoint.py:2411`). For short renders the
worker calls `render_via_engines` (entrypoint:1690, :1778). The
legacy `render()` shim that previously sat in front of these has been
deleted (ADR-027). `render_long_form` itself drives rewrite → cast →
in-process `render_via_engines` (`pipeline/render/video.py:274`).

### Stage ordering — sequential vs parallel

Per `onboarding-qa.md` Q34 + Q45 the stages aren't strictly linear:

1. **`rewrite → cast`** — sequential. `_stage_rewrite_real`
   (`entrypoint.py:720`) calls `pipeline.llm.rewrite` (short) or
   `pipeline.llm.rewrite_long_form.rewrite_long_form` (long).
   `_stage_cast_real` (`entrypoint.py:889`) reads the rewritten script
   and emits `cast.json` with structured per-character fields (age,
   hair, build, clothing, signature_prop — P4.3, ADR-009).
3. **`tts || images`** — parallel **when** the stage-overlap gate
   approves. `pipeline.stage_overlap.gpu_safe_to_overlap` checks the
   TTS + image providers; both must be cloud-bound. Approved →
   `short_engine._run_overlapped` (`short_engine.py:332`) fires the
   visualize plugin on a worker thread using a **preliminary
   timeline** built from authored script texts (`short_engine.py:272`),
   while audio + ASR run on the main thread. On laptop fallback
   (local TTS / local image-gen) the engine drops to sequential
   (`short_engine.py:400`).
4. **`asr` after `tts`** — `asr_anchors` (long) or `asr_beats`
   (short) needs the synthesized audio to align word/section
   timestamps. Runs after TTS regardless of overlap state.
5. **`compose` after all** — `compose_plugin.mux(visuals, audio,
   overlays, music, spec, out_path)` (`short_engine.py:256`,
   `long_engine.py:150`).
6. **`upload`** — `_stage_upload_real` (`entrypoint.py:2103`).
7. **Optional `editing-agent` stage** between compose + upload —
   activated when `proposal.editing.enabled` is truthy
   (`entrypoint.py:80-82`). Calls `_stage_editing_agent_real`
   (`entrypoint.py:2109`). Not in the static `STAGES` list to keep
   the dashboard from showing a "skipped" pill on every job.

### Long-form rewrite retry path (post-2026-05-23 P3.1–P3.6)

`pipeline/llm/rewrite_long_form.py`:

1. Outline LLM call (`_call_outline_with_retry`, line 690) emits
   `target_words` per section. After it returns, `_outline_sum_in_band`
   (line 674) checks `sum(section.target_words)` vs the user's total
   target ±15% (P3.3). On 1st bad-sum the outline retries ONCE with the
   error appended to `extra_rules`; on 2nd bad-sum the rewrite now
   *logs* the error and proceeds rather than raising — the downstream
   `validate_long_form_envelope` critic catches length violations
   authoritatively, and ADR-023 says gates prefer progress over
   termination.
2. N parallel section bodies fire (`_generate_all_section_bodies`,
   line 1017). Each section's prompt instructs the model to emit a
   `word_count` field (P3.1) — used for emitted-vs-actual telemetry,
   never as the gate signal (actual word count via `len(text.split())`
   wins per ADR-005).
3. Per-section retry uses **iterative-extend** (P3.6, ADR-007). The
   first attempt fails the per-section ±10% gate → retry path at line
   1394 sends the failed draft back to the LLM with
   `prev_short_draft=<failed text>` (`_call_section_body_llm` line
   1040). The retry prompt instructs "expand to N words by adding
   source detail" rather than re-rolling from scratch. 1 extend
   attempt; hard-fail on 2nd miss.
4. Reasoning effort for `rewrite_long_form` is `medium` (P3.4,
   ADR-030, reverted 2026-05-23 from the 2026-05-13 `minimal`
   calibration). Pinned by test so future cost-pressure flips are
   visible.

### Per-beat image-gen retry (P4.2)

`pipeline/render/visualize/ai_beat_slideshow.py`:

1. Per-beat: generate → `_image_quality_ok(png_path)` (line 108)
   checks luminance/variance via PIL.
2. If quality fails OR diffusion raises: 1 retry with a stronger
   prompt and a wider seed (`seed_base + (i + n_total) * seed_stride`,
   line 554).
3. Only AFTER the per-beat retry budget exhausts does the beat count
   toward `_PER_BEAT_FAILURE_THRESHOLD = 0.10` (line 97). Render-level
   kill fires only when >10% of beats fail **post-retry**.

### Caption density gate (P5.2)

`pipeline/render/short_engine.py:288` (`_enforce_caption_density`,
also imported by long_engine at line 148). Runs after
`_collect_overlays` returns. Skipped when
`getattr(spec, "captions_enabled", True)` is False. Checks that the
layer-40 (caption) elements collectively cover the audio duration
above the channel-tunable threshold; logs success rate
(`caption_density_gate: coverage=...`) and raises on shortfall.

### Per-stage what's read / written

| Stage    | Reads                                                                  | Writes                                                                                       |
|----------|------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| rewrite  | Job proposal (Firestore), channel YAML, variant YAML, source adapter   | `data/<channel>/[<niche>/]narrations/<slug>.json` (legacy long-form dict), `.envelope.json`   |
| cast     | narration JSON                                                         | `data/<channel>/[<niche>/]cast/<slug>.json` (structured per-character fields, P4.3)           |
| images   | narration JSON, cast JSON, spec.extra (`character_description` + `character_descriptions`) | `data/<channel>/[<niche>/]cache/<slug>/panel_*.png` or `img_*.png` |
| tts      | narration JSON, voice ref audio (channel YAML)                         | `data/<channel>/[<niche>/]cache/<slug>/narration.wav` (+ chunk timings for long; IndicF5 swaps ref_text to chunk-0 from chunk 1) |
| asr      | narration.wav                                                          | `data/<channel>/[<niche>/]cache/<slug>/beats.json` (Timeline as JSON)                         |
| compose  | visuals, narration.wav, music bed, overlay assets, captions            | `data/<channel>/[<niche>/]shorts/<slug>.mp4` (short) or `.../long_form/<slug>.mp4`            |
| upload   | mp4 + thumbnail                                                        | `gs://<bucket>/jobs/<job_id>/short.mp4`, `.../thumb.jpg`; YouTube upload + `uploads/<slug>.json` |

### Short vs long-form branching

`spec.kind` (short / long_form / sports_doc / footage_only) picks the
engine via `pipeline/render/engine.py:45`:

- `RenderKind.SHORT` → `pipeline.render.short_engine.render_short`
- `RenderKind.LONG_FORM` / `SPORTS_DOC` / `FOOTAGE_ONLY` →
  `pipeline.render.long_engine.render_long`

The short engine has the overlap fast-path; the long engine is
strictly sequential (`long_engine.py:60-157`) because chunked TTS
already pipelines internally.

Within each engine, `spec.visual_mode` picks the visualize plugin
(`short_engine.py:457`, `long_engine.py:96`). The engine has zero
`if visual_mode == ...` branches — it's pure registry dispatch.

## Stage 4 — live artifact emission during render

The worker emits per-stage artifacts to GCS while the render is still
running, so the user-facing render-detail page (`/app/render/<jobId>`)
can show progress instead of waiting for the final mp4:

- `pipeline.render.artifacts.emit_artifact(job_id, kind, local_path,
  extras={...})` — called from `video.py:253-275` (envelope + script),
  `video.py:374-397` (narration.wav, beats.json, per-panel images),
  `video.py:412-425` (final mp4 preview).
- Artifacts land at `gs://<bucket>/jobs/<job_id>/{video,script,
  panels,narration,beats,envelope}/` (per `entrypoint.py:2085-2086`).
- Firestore job doc gets `artifacts[]` updated so the polling UI sees
  them.

## Stage 5 — the render-detail page

`/app/render/<jobId>` (`web-next/app/app/render/[jobId]/page.tsx`)
polls the job doc (`GET /api/jobs/<job_id>`) every ~2s. It shows:

- The 7-pill timeline (one per `STAGES` entry, plus an editing pill
  when opted in).
- Live artifact previews (the LiveArtifactsCard reads
  `jobs/<id>/{video,script,panels,...}` from GCS).
- `notes[]` from the spec (phantom-niche warnings, dropped overrides).
- On final: `youtube_url`, `short_uri`, `thumb_uri`.
- On failure: `error` field with the extracted last traceback (the
  `_extract_last_traceback` machinery in `pipeline/render/video.py:642`
  walks past OTel exporter noise to find the real fatal traceback).

## Stage 5b — writeback verification (P3.7)

Between compose and upload, `cloud/render-worker-v2/writeback.py`
runs sanity checks on the mp4: valid h264+aac streams, non-zero
duration, no truncation. **The duration-floor check was dropped
(P3.7, ADR-010)** — a 1232s mp4 for a 1800s target no longer fails
writeback. Length policing moves entirely to upstream rewrite gates;
writeback is sanity-only.

## Stage 6 — terminal states

- **Success** — `mark_done(job_id, short_uri, youtube_url?, thumb_uri?)`
  (`control/core/jobs.py:215`). Sets `status=done, stage=done,
  short_uri=gs://..., error=None`.
- **Failure** — `mark_failed(job_id, stage, error)`
  (`control/core/jobs.py:236`). Sets `status=failed`. Error truncated
  to 2000 chars (`jobs.py:255`).

## Triggers other than the wizard

Per `onboarding-qa.md` Q36 (locked) — **every render is triggered
from `/app/create`**. The round-robin scheduler at
`control/core/scheduler.py:1` exists in code and persists state in
Firestore (`scheduler_state/main`), but the cron triggering is for
**upload scheduling**, not render initiation. The launchd plists in
`control/com.ytfactory.*.plist` are laptop-side scheduled jobs
(cloud-critic, cloud-snapshot, critique-runner, state-sync,
upload-next) — they don't kick renders.

## Critic loop — laptop, post-hoc

`spec.critic_loop=True` opts a render into the vision-bearing critic
(`short_engine.py:548`, `long_engine.py:160`). The critic uses the
Claude CLI (`allowed_tools=["Read"]`) which is laptop-only — cloud
renders bail early (`short_engine.py:571-578`) and the laptop-side
`pipeline.critique.cloud_poller` (`control/com.ytfactory.cloud-critic.
plist`) grades them post-hoc from Firestore. Per `onboarding-qa.md`
Q37: critic + audio_critic can be skipped and done on laptop; cloud
worker runs only the 3 mandatory LLM stages (rewrite / cast /
prompts).
