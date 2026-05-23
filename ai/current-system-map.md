# Current System Map

> Last updated 2026-05-23 (post-Q&A refactor).

What's in the repo *now*, with file/dir paths so an agent can jump
directly to the code. Not history — current state.

## Top-level layout

```
/Users/rohit/ytFactory/
├── pipeline/            # channel-agnostic render code (the library)
├── control/             # laptop control plane — FastAPI + Firestore
├── cloud/               # Cloud Run services + deploy.sh per service
├── web/                 # legacy FastAPI server (Cloud Run service `web-server`)
├── web-next/            # Next.js admin/wizard UI (Cloud Run service `web-next`)
├── scripts/             # laptop CLI entry points + ops utilities
├── tests/               # pytest
├── ai/                  # mental-model docs (this dir)
├── docs/                # ops + cost docs (cited from CLAUDE.md)
├── data/                # cross-channel state AND per-channel render output
│                        #   (channel artifact roots moved here 2026-05-23)
└── CLAUDE.md            # the agent instructions
```

Note: per-channel render output directories now live under
`<repo>/data/<channel>/` (not `<repo>/<channel>/`). The move landed
2026-05-23 (paths.py task #46). Nothing is generated at the repo root
any more. The on-disk layout *under* the channel root is unchanged.

## `pipeline/` — render library

```
pipeline/
├── channels/            # 7 channel YAMLs:
│                        #   cosmosdecoded, hindutavaanimated,
│                        #   historyrecapped, mystoriesanimated,
│                        #   rhymetimejunction, sportsrecapped,
│                        #   scrollpulse (newest — 2026-05-23).
├── variants/            # niche overlay YAMLs. Two channels have variants today:
│   ├── mystoriesanimated/   # 13 variants (aita_*, tifu, today_in_history,
│   │                        #   wiki_oddities, ...)
│   └── sportsrecapped/      # sportsrecapped variants
├── render/              # the engine + 6 plugin slots
│   ├── engine.py        # pick_engine(spec) — short vs long dispatcher
│   ├── spec.py          # RenderSpec dataclass + build_spec()
│   ├── contracts.py     # 6 Protocols + register_plugin/get_plugin registry
│   ├── video.py         # render_via_engines() + render_long_form() — public entries
│   ├── short_engine.py  # render_short() — single-pass TTS, beats
│   ├── long_engine.py   # render_long() — chunked TTS, sections
│   ├── spec_enrich.py   # populate_render_extras() — bridges script data to spec.extra
│   ├── artifacts.py     # emit_artifact() — live preview upload to GCS
│   ├── input_registry.py# apply_overrides() — form-override validators
│   ├── __main__.py      # CLI entry — replacement for the deleted
│                        #   pipeline.render.long_form __main__
│   ├── audio/           # AudioSynthesizer impls
│   │   ├── tts_single.py, tts_chunked.py, audio_from_fixture.py
│   ├── timeline/        # TimelineBuilder impls
│   │   ├── asr_beats.py, asr_anchors.py, timeline_from_fixture.py
│   ├── visualize/       # VisualProducer impls
│   │   ├── ai_beat_slideshow.py, longform_panels.py,
│   │   ├── archival_shotlist.py, footage_windows.py, footage_filler.py,
│   │   ├── visuals_from_fixture.py
│   ├── overlays/        # OverlayProducer impls (list-valued slot)
│   │   ├── word_caption_pngs.py, sentence_caption_ass.py,
│   │   ├── lower_third.py, chapter_card.py, anchored_footage.py,
│   │   ├── closer_panel.py (new, R4 — wired via spec.closer_panel),
│   │   ├── noop.py
│   ├── music/           # MusicComposer impls
│   │   ├── ducked_loop.py, single_bed.py, section_mood.py, silent.py
│   ├── compose/         # FinalMux impls
│   │   ├── beat_slideshow_mux.py, section_video_mux.py
│   ├── qa/, shared/     # support modules
├── llm/                 # LLM dispatcher + rewrite + cast + prompts
│   ├── cli.py           # 3-backend dispatcher (cli/azure_openai/anthropic_sdk)
│   │                    #   anthropic_sdk = documented escape hatch (ADR-031)
│   ├── rewrite.py, rewrite_long_form.py
│   ├── cast.py, prompts/
│   ├── script_schema.py, critic.py
├── audio/               # TTS facade
├── tts/                 # real TTS providers (cloudrun.py — chatterbox/indicf5/...)
├── images/              # image-gen dispatcher + prompt_refiner.py
│                        #   (REFINER_VERSION="v2-zturbo", 80-250 word structured)
├── captions/            # caption rendering (word PNGs, ASS sentence)
├── compose.py           # legacy compose (large file)
├── paths.py             # RenderPaths — canonical channel layout
│                        #   (channel_root → data/<channel>/ as of 2026-05-23)
├── channels.py          # channel_rotation(), _channel_yaml_path()
├── niches.py            # NICHE_CHANNEL map
├── stage_overlap.py     # gpu_safe_to_overlap() — parallel-stage gate
├── upload.py            # YouTube + record writing
├── beats.py             # Word/Segment dataclasses
├── observability/       # OTel init, propagation
├── cloud/               # cloud-side helpers (warm.py, services.py registry)
├── part2_watcher.py     # AITA Part-2 cliffhanger watcher (writer wired,
│                        #   reader/daemon NOT wired — see [[F24]])
└── critique/            # cloud_poller.py — laptop-side critic daemon
```

Deleted in the 2026-05-23 refactor (no longer on disk):

- `pipeline/sources/multi_source.py` — premature abstraction with a
  single test caller, zero production usage (R6).
- `pipeline/animation.py` — zero callers; animation pipeline isn't on
  the production path (Q46 model lock). Channel YAML comments still
  reference it (`rhymetimejunction.yaml:14`,
  `variants/mystoriesanimated/aita_animated_motion.yaml:46`) — stale.
- `data/research/cloud_image/` — legacy klein experiment artifacts.
- `scripts/setup/` — empty package.

## `control/` — control plane

The flat-vs-nested module fork has been resolved. Only the canonical
nested layout (`control/core/*`, `control/routes/*`) ships. The five
legacy top-level route files and seven legacy top-level state modules
are deleted.

```
control/
├── core/                # canonical state modules
│   ├── jobs.py          # create_job, mark_stage, mark_done, mark_failed,
│   │                    #   _enqueue_render_job
│   ├── queue.py         # InMemoryQueue / FirestoreQueue
│   ├── cloud_run.py     # trigger_render_job() — Cloud Run Job SDK + CLI fallback
│   ├── scheduler.py     # round-robin auto-scheduler
│   ├── rate_limit.py    # per-IP daily quota in ratelimits/<date>/...
│   ├── schema.py        # ShortProposal, TaskEnvelope, TaskKind, TaskStatus
│   ├── auth.py          # OAuth helpers
│   ├── sim_worker.py    # in-process worker (when YTFACTORY_RENDER_BACKEND=sim)
│   └── storage.py
├── routes/              # canonical FastAPI route modules
│   ├── render_routes.py # POST /api/render → _enqueue_render_job
│   ├── channels_routes.py, niche_routes.py, niche_specs_routes.py,
│   ├── voices_routes.py, music_routes.py, song_sample_routes.py,
│   ├── dashboard_routes.py, scheduler_routes.py, telemetry_routes.py,
│   ├── critique_routes.py, discover_routes.py, agent_routes.py,
│   ├── cloud_routes.py, state_routes.py, oauth_web_routes.py,
│   ├── clone_video_routes.py, script_jobs_routes.py, auth_pin.py
├── chat_routes.py       # POST /api/chat, /api/chat/confirm — chat-driven
│                        #   proposal helper (still top-level; no v2 sibling)
├── chat_service.py      # shared chat helpers
├── render_routes.py     # NOTE: only top-level legacy module remaining; thin
│                        #   shim, kept while cleanup task migrates callers
├── server_dev.py        # local dev entry point (matches web/server.py
│                        #   routers, per ADR-028)
└── *.plist              # 5 launchd plists (cloud-critic, cloud-snapshot,
                         #   critique-runner, state-sync, upload-next)
                         #   — laptop-side scheduled jobs
```

Per ADR-028, `control/core/*` is the canonical state-module location;
the flat siblings (`control/{jobs,queue,scheduler,rate_limit,storage,
schema,auth}.py`) and the four legacy route files
(`control/{agent,dashboard,niche,scheduler}_routes.py`) have been
deleted. `control/render_routes.py` is the lone holdout — kept as a
thin shim during caller migration.

## `cloud/` — Cloud Run services

Per-service dir layout: `Dockerfile`, `server.py`, `deploy.sh`,
`requirements.txt`, `otel_init.py`, `cloud_run_json_exporter.py`,
optional `cloudbuild.yaml`.

13 entries on disk (post-2026-05-23 cleanup):

```
cloud/
├── render-worker-v2/    # the render JOB (not service)
│   ├── entrypoint.py    # _main_from_firestore() — reads job, walks 7 stages
│   └── writeback.py     # P3.7 — post-render sanity verifier (no duration floor)
├── image-z-image-turbo/ # SOLE production image-gen (Z-Image-Turbo, 6B S3-DiT)
├── tts-chatterbox/      # primary TTS for English channels
├── tts-indicf5/         # Hindi TTS for hindutavaanimated
├── asr-whisper/         # faster-whisper for word alignment
├── editing-agent/       # optional polish stage (proposal.editing.enabled)
├── clone-video-worker/  # /clone-video service
├── stats-refresh/       # per-channel YouTube analytics refresh Job
├── web-server/          # legacy FastAPI server deployment
├── web-next/            # Next.js admin/wizard UI deployment
├── weights-staging/     # weights-staging helper (used by
│                        #   scripts/bootstrap_new_project.sh; ⚠️ R9 — no
│                        #   recent invocations; surfaced for explicit
│                        #   keep/kill user decision)
├── _shared/             # auth_setup.sh, otel_init.py, sync.sh
│                        #   — sourced by every deploy.sh
└── iam/                 # IAM grant scripts (slimmed in 2026-05-23 cleanup)
```

Deleted in the 2026-05-23 cleanup:

- `cloud/_bench/` — TTS A/B bench scaffolds (the 7 retired scaffolds:
  `image-flux2-klein`, `image-flux2-dev`, `image-qwen`, `image-hidream`,
  `tts-indicparler`, plus the related bench harness).
- `cloud/cobalt-api/` — clone-video flow uses `clone-video-worker`
  directly; cobalt-api wrapper was retired.
- 4 dead env-URL exports in `cloud/render-worker-v2/deploy.sh:106`
  (the env keys for the retired image services + tts-indicparler).

## `web-next/` — Next.js wizard + admin UI

```
web-next/app/
├── app/
│   ├── create/
│   │   ├── page.tsx     # the 3-step wizard. Mode → Channel → Customize.
│   │   │                #   Submits to renderApi.enqueue → /app/render/<jobId>
│   │   └── clone/       # "Clone a video" submode
│   ├── render/[jobId]/
│   │   └── page.tsx     # render-detail page; polls /api/jobs/<id> for
│   │                    #   timeline + live artifacts + final mp4
│   ├── queue/, admin/, channels/, cloud/, settings/, telemetry/,
│   ├── layout.tsx, page.tsx
└── api/[...path]/       # Next.js API proxy → FastAPI control plane
```

## `scripts/` — laptop CLI entry points

Top-level scripts include `make_shorts.py` (per-channel CLI render),
`pull_stories.py` (Reddit/Wiki source pull), and channel-specific
scripts under `<channel>/scripts/`. Many `make_shorts.py`-style flows
are referenced by channel YAML comments but are largely superseded
by the cloud worker path now (per `onboarding-qa.md` Q32 — all 7
channels route through the engine; channel-specific scripts in YAML
comments are stale).

## `tests/`

`pytest`. Run with `.venv/bin/pytest tests/ -x -q` (per CLAUDE.md).
Tests cover the spec builder, contracts registry, individual plugins,
the long-form rewrite gates, and engine dispatch. Cloud-integration
paths are marked `# coverage:` to acknowledge they're exercised only
by real renders.

## Channel dirs (gitignored render outputs)

Each of the 7 channel YAMLs in `pipeline/channels/` resolves to
`<repo>/data/<channel>/` on disk (the 2026-05-23 paths.py task #46
move). Layout is the canonical `RenderPaths` shape
(`pipeline/paths.py:157`):

```
data/<channel>/
├── narrations/          # tracked authored scripts (JSON)
├── shotlist/            # tracked footage-only sidecars
├── uploads/             # tracked YouTube upload records
├── learnings/, scripts/, branding/, music/, songs/,
│ emoji/, footage_plan/  # channel-wide tracked + branding
├── raw/, cast/          # tracked authored + gitignored cast
├── shorts/, long_form/, cache/, scratch/  # gitignored render output
└── critiques/           # frames gitignored, JSONs tracked
```

Niched channels nest per-slug subdirs under `<niche>/`:
`data/mystoriesanimated/reddit_amitheasshole/narrations/<slug>.json`.
Mapping at `pipeline.niches.NICHE_CHANNEL`. `RenderPaths` resolves
both shapes.
