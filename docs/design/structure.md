# ytFactory — Project Structure (source of truth)

The clean rebuild. Design leads; code conforms. This documents the architecture,
the file layout, and the rules every file obeys. Diagrams: [docs/design/diagrams/](diagrams/).

## Architecture — 6 rings, dependencies point inward

```
config ─▶ app ─▶ stages ─▶ core ◀─ ai / services / infra
```

- **`core/`** — frozen data contracts + Protocol interfaces + the Stage/Context/Orchestrator spine. Depends on nothing.
- **`stages/`** — the 13 pipeline steps. Pure orchestration.
- **`ai/`** — implementations of the model Protocols (LLM / image / TTS / ASR).
- **`services/`** — deterministic collaborators (ffmpeg, yt-dlp, PIL, YouTube API).
- **`infra/`** — the injected `Telemetry` / `Storage` / `Config` impls (only place env/GCS/logging live).
- **`app/`** — composition root: wires impls into stages, picks the stage list, runs the Orchestrator.

Hard rule: a stage never imports another stage, a concrete model, or `infra/`. Only
`app/` names concrete classes. Enforced by an import-boundary test.

## File map

```
pipeline/
├── core/                       # FROZEN interfaces + spine  (built & verified)
│   ├── contracts.py            # typed artifacts + 11 enums + styling config — DATA ONLY
│   ├── models.py               # 8 render model Protocols
│   ├── stage.py                # Stage Protocol + Artifact keys
│   ├── context.py              # Context bus + Telemetry/Storage/Config Protocols
│   └── orchestrator.py         # Orchestrator + RenderResult
│
├── stages/                     # 12 steps — orchestration only, ≤120 LoC each
│   ├── source_stage.py         # RenderSpec        → SourceRouter·det  → SOURCE
│   ├── script_stage.py         # SOURCE            → ScriptService        → SCRIPT
│   ├── director_stage.py       # SCRIPT            → DirectorLLM      → DIRECTOR   (animation only)
│   ├── scene_stage.py          # SCRIPT,DIRECTOR   → SceneDefinitionAI         → STORYBOARD (per-beat kind + Scene|ShotRef)
│   ├── chunk_stage.py          # STORYBOARD,DIRECTOR → ChunkingAI+assemble → CHUNKS (audio_prompt + one visual)
│   ├── audio_stage.py          # CHUNKS            → AudioGenerationModel           → AUDIO
│   ├── align_stage.py          # AUDIO, anchors    → ASRModel           → TIMELINE
│   ├── overlay_stage.py        # TIMELINE,spec     → CaptionService·det → OVERLAYS
│   ├── music_stage.py          # spec,AUDIO        → MusicBedService·det  → MUSIC
│   ├── compose_stage.py        # CHUNKS,TIMELINE,OVERLAYS,AUDIO,MUSIC → Compositor·det (renders chunk visuals + composes all) → VIDEO
│   ├── upload_stage.py         # VIDEO,SCRIPT      → UploadService·det      → UPLOAD
│   └── critique_stage.py       # VIDEO,AUDIO,SCRIPT → CritiqueSkill     → CRITIQUE
│
├── ai/                         # implementations of core.models Protocols
│   ├── llm/
│   │   ├── client.py           # 3-backend LLM dispatcher (cli/azure/anthropic) — SOLE LLM call site
│   │   ├── script.py           # ScriptService
│   │   ├── director.py         # DirectorLLM   (cast + style lock)
│   │   ├── scene.py            # SceneDefinitionAI      (segment + per-beat kind + Scene|ShotRef)
│   │   ├── prompt.py           # ChunkingAI     (image-prompt refiner → ImagePrompt)
│   │   └── critique.py         # CritiqueSkill   (visual + audio)
│   ├── image/z_image_turbo.py  # ImageGenerationModel      (HTTP → GPU service)
│   ├── tts/chatterbox.py       # AudioGenerationModel        (English)
│   ├── tts/indicf5.py          # AudioGenerationModel        (Hindi)
│   └── asr/whisper.py          # ASRModel        (HTTP → whisper align)
│
├── services/                   # deterministic collaborators
│   ├── source/{router,reddit,wikipedia,today_in_history,youtube}.py  # SourceRouter adapters
│   ├── visual/provider.py      # VisualProvider router (Compositor-internal) → ImageGenerationModel | FootageCloneRetrieval per chunk
│   ├── footage/retriever.py    # FootageCloneRetrieval (yt-dlp + trim a ShotRef → clip)
│   ├── captions/renderer.py    # CaptionService  (Timeline → caption overlays, per CaptionStyle)
│   ├── music/composer.py       # MusicBedService    (pick bed + duck plan)
│   ├── compose/compositor.py   # Compositor  (renders each chunk's visual via VisualProvider, then ffmpeg-composes all → mp4)
│   └── publish/{publisher,thumbnail,metadata}.py  # YouTube upload + thumb + Metadata
│
├── infra/                      # injected cross-cutting impls — ONLY place env/GCS/logging live
│   ├── telemetry.py            # events + spans → Cloud Logging + GCS artifact dump
│   ├── storage.py              # GCS + local artifact I/O
│   └── config.py               # channel YAML, service URLs, voice refs, learnings()
│
├── app/                        # composition root — the ONLY place concrete classes are named
│   ├── spec_builder.py         # channel YAML + variant + job params → RenderSpec
│   ├── wiring.py               # instantiate ai/ + services/ per spec, inject into stages
│   ├── stage_list.py           # ordered stage list for a spec (skip Director/Scene when no animation)
│   └── worker.py               # render-worker entry: Firestore job → Context → orchestrator.run
│
└── config/                     # DATA (not code)
    ├── channels/<channel>.yaml
    └── variants/<channel>/<v>.yaml
```

## Responsibility rules

| Ring | May do | May NOT do | Size |
|---|---|---|---|
| `core/contracts` | hold typed fields | any method / I/O | — |
| `core/*` protocols | declare interfaces | any implementation | — |
| `stages/` | read/write artifacts, build a Request from bus+`Config`, call **one** collaborator, raise on failure | call an LLM/HTTP/ffmpeg, read env, touch files except via `ctx` | ≤120 LoC |
| `ai/` | turn one Request→Output via LLM/GPU; own prompt template + parse / HTTP payload | know the artifact bus or stage order | ≤150 LoC |
| `services/` | deterministic work (ffmpeg/PIL/yt-dlp/API) | know the artifact bus or stage order | ≤200 LoC |
| `infra/` | env vars, GCS, Cloud Logging, YAML load | business logic | — |
| `app/` | name concrete classes, inject, sequence | business logic | — |

Collaborators arrive by **constructor injection** from `app/wiring.py` — a stage holds
`self.model: ScriptService` (the Protocol), never the concrete class.

## Clean-code rules
1. One responsibility per file, size caps above; exceed → split.
2. Dependencies point inward; import-boundary test fails the build on violation.
3. Constructor injection only — no import-time globals, no service locator.
4. Contracts are frozen dumb data.
5. Fail loud — a stage that can't produce real output raises; never a placeholder.
6. No magic literals — URLs/paths/model-ids/prompts come from `Config` or named constants.
7. Typed end-to-end, pyright-clean, a Protocol at every seam.
8. One test file per class; stage tests inject fake models (no network/GPU).
9. Naming: `XStage` / `AudioGenerationModel` (Protocol) / `ChatterboxTTS` (concrete) / `XRequest`·`XResult`.
10. No dead code, no TODO stubs; one-line docstring per file stating its responsibility.

## Locked decisions
- Layout role-based; fresh rewrite (old tree = reference only, deleted after).
- **Visual is per-chunk**: one chunk = one narration segment (model-decided length) + one
  audio + ONE visual (`VisualKind.ANIMATION`=image | `VisualKind.FOOTAGE`=clip). A render
  mixes them (combo); continuous footage = a longer chunk. No whole-render branch.
- Channel offers a visual-palette; user picks; `RenderSpec.visual_palette` records it;
  `SceneDefinitionAI` assigns each chunk a kind from the palette.
- No Ideation model — `ScriptService` rewrites + enriches the source; topics come from the wizard or a deterministic scheduler rotation.
- Chapters = `Script.sections` (empty Shorts, populated long-form). **Short + long = same code**, config + duration differ.
- Song mode out of v1 (`AudioMode.VOICE` only).
- `FootagePlanModel` folded into `SceneDefinitionAI` → 8 render models.
- Learnings loop: `CritiqueStage` findings → `LearningsStore` (`data/<channel>/learnings/`) → `Config.learnings()` into every model Request next run.

## Build order (next)
1. ✅ `core/` frozen + verified.  2. Port 12 stages against core (parallel).  3. `ai/` + `services/` impls.  4. `infra/` + `app/` wiring.  5. Fixture render → parity vs old → delete old.
