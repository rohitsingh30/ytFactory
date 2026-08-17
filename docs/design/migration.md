# ytFactory — Migration: new design vs existing

**Source of truth for the rebuild path.** The new design lives in `pipeline/core/`
(spine + contracts + model protocols). Stages + model impls are ported from the old
`pipeline/` against these frozen contracts, then the old tree is deleted.

## New vs existing
| Aspect | Existing | New (locked) |
|---|---|---|
| Orchestration | 7-stage **monolith** in `cloud/render-worker-v2/entrypoint.py` (~3k LoC), in-memory dicts | 13 discrete `Stage` classes + `Orchestrator`; typed artifacts via `Context` |
| Casting | one LLM call (`cast.json`) | **Director** (`CastLock` + `Style` lock) **+ Scene** (per-beat `Camera`/`Mood`/blocking) |
| Scene direction | flat scene text per beat | full `Scene` contract — director-grade, bounded enums |
| Prompt building | inline `build_full_prompt` + refiner (silent fallback) | explicit `PromptModel` → typed `ImageModelPrompt` |
| Cross-cutting | import-time globals (telemetry/config) | injected via `Context` (DIP) |
| Stage I/O | implicit dicts, drift-prone | typed dataclass contracts, enforced |
| Module size | god-modules: `spec.py` 1.3k, `long_form_lib.py` 2.3k, `compose.py` 1.6k, `entrypoint.py` 3k | ≤~400 LoC/module, split by responsibility |
| Extensibility | plugin registry (render only) | uniform `Model`/`Service` interface per stage |

## Where each stage ports FROM (old → new)
| Stage | ported from |
|---|---|
| SourceStage | `pipeline/sources/*` + `pipeline/social/*` |
| ScriptStage | `pipeline/llm/rewrite*.py` |
| DirectorStage | `pipeline/llm/cast.py` (+ style) |
| SceneStage | new (was implicit in prompt building) |
| ChunkStage | `pipeline/images/prompt_refiner.py` |
| AudioStage | `pipeline/tts/*`, `pipeline/audio/*` |
| AlignStage | `pipeline/asr_cloudrun.py` |
| Overlay/Music/Compose | `pipeline/images/images.py` + `pipeline/render/{visualize,overlays,music,compose}/*` + `pipeline/compose.py` — Compose renders chunk visuals (image-gen \| footage retrieval) **and** composes all |
| UploadStage | `pipeline/upload/*` + `thumbnails.py` + `publish/*` |
| CritiqueStage | `pipeline/critique/*` + `pipeline/llm/audio_critic.py` |

## Build phases
1. ✅ **Spine + contracts** — `pipeline/core/` (done, verified).
2. **Port stages** (parallel agents, one per stage) against `pipeline.core` — split
   god-modules, extract hardcodes to `Config`, de-slop while porting.
3. **Wire** — `Orchestrator` runs the stage list; fixture smoke render passes.
4. **Cutover + delete old** `pipeline/` tree.

## Status (2026-07-12)
- `pipeline/core/` complete + verified: 10 model protocols, all contracts, spine.
- Old code untouched (reference). Deleted so far: dead `images/animation.py`.
