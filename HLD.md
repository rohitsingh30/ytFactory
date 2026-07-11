# ytFactory — High-Level Design (HLD)

The anchor document. One page to understand the whole system, the design
patterns it uses, and where the real (small) cleanup targets are.

> **Honest status (verified 2026-07-11):** the system is architecturally
> sound. The large refactor in `ai/refactor-plan.md` already landed
> (duplicate control modules gone, legacy `render()` gone, `prod-v2`
> swept, stale script dirs gone). The perceived "hundreds of folders" is
> ~250 `pipeline/voice_refs/**` **audio-data** dirs, not code. Real code
> is ~40 dirs; only 4 are single-module. Don't rewrite — finish trims.

---

## 1 · The one-sentence system

> A prompt becomes a published YouTube video, fully automated, for 7
> channels, on one render engine — laptop decides *what*, cloud does the
> *rendering*.

---

## 2 · Three runtime planes (the HLD)

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  CONTROL PLANE (laptop /     │        │  EXECUTION PLANE (Google      │
│  web-server / web-next)      │ writes │  Cloud, serverless, $0 idle)  │
│                              │ Fire-  │                               │
│  control/routes/  (FastAPI)  │ store  │  render-worker-v2 (Cloud Run  │
│  control/core/    (state)    │───────▶│  JOB) — 7-stage pipeline      │
│   • jobs / queue / scheduler │  job   │        │                      │
│   • rate_limit / cloud_run   │  doc   │        ▼ HTTP                 │
│                              │        │  GPU SERVICES (Cloud Run)     │
│  Never renders. State only.  │◀───────│   image-z-turbo · tts-*·asr   │
└─────────────────────────────┘  status └──────────────────────────────┘
        │                                          │
        │ reads proposal                           │ writes artifacts
        ▼                                          ▼
   User (browser wizard /app/create)      GCS gs://…/jobs/<id>/short.mp4
                                          Firestore jobs/<id> + decision_log
                                          Cloud Logging stage.start/end
```

**Boundary rule (the key HLD invariant):** the two planes never call each
other directly. They communicate through **Firestore (state)** + **GCS
(artifacts)**. That decoupling is why idle cost is `$0` (GPU services
`min-instances=0`) and why the same code runs identically local or cloud.

| Plane | Owns | Entry point |
|---|---|---|
| Control | job lifecycle, scheduling, rate limits, UI | `control/core/jobs.py::create_job` → `cloud_run.py::trigger_render_job` |
| Worker | the 7-stage render, per-render Cloud Run Job | `cloud/render-worker-v2/entrypoint.py::_main_from_firestore` |
| GPU services | image-gen, TTS, ASR (stateless HTTP) | `cloud/<svc>/` behind `CLOUDRUN_*_URL` |

---

## 3 · The render pipeline (7 stages)

```
rewrite → cast → images → tts → asr → compose → upload
(LLM)    (LLM)  (z-turbo) (TTS) (Whisper) (engine)  (YouTube)
                     └──── images ‖ tts overlap when both cloud-bound ────┘
```

Declared once in `entrypoint.py::STAGES`. Each stage: emits telemetry,
dumps real I/O to GCS, and **fails loud** (raises rather than emitting a
placeholder). The `compose` stage is where the render engine runs.

---

## 4 · The render engine — the core LLD (patterns in use)

This is already good low-level design. Name the patterns explicitly:

```
render_via_engines(spec)              # Facade — one public entry
   └─ pick_engine(spec)               # Strategy selector (only branch on kind)
        └─ render_short | render_long
             └─ get_plugin(slot,name) # Registry lookup, per slot
```

| Pattern | Where | Why it's right |
|---|---|---|
| **Facade** | `video.py::render_via_engines` | One entry; callers never see engines/plugins |
| **Strategy** | `engine.py::pick_engine` | short vs long chosen by `spec.kind` — the *only* kind-branch |
| **Registry** | `contracts.py::_REGISTRY` + `register_plugin`/`get_plugin` | Add a capability = 1 file + 1 register call; zero engine edits |
| **Protocol (structural typing)** | `contracts.py` 6 `Protocol` slots | Plugins need no base class; type-checker enforces contract |
| **Immutable spec** | `spec.py::RenderSpec` (built once by `build_spec`) | One source of truth for a render; never mutated after build |

**Six plugin slots** (orthogonal — mix freely):
`audio · timeline · visualize · overlays(list) · music · compose`
Impls live in `pipeline/render/<slot>/*.py`, self-registering at import.

> The design principle to keep: **engines are branchless.** Any new
> visual mode / caption style / music policy is a new plugin file, never
> an `if` in the engine. This is the pattern to *protect*, not replace.

---

## 5 · Where the code actually lives (the map you were missing)

```
control/           Control plane
  core/            State modules (jobs, queue, scheduler, rate_limit, cloud_run, auth)
  routes/          FastAPI routers (one per surface)
cloud/             Cloud Run services + render-worker-v2 (each has deploy.sh)
pipeline/          The render library (channel-agnostic)
  render/          Engine + 6 plugin slot dirs + spec.py + contracts.py  ← core
  llm/             rewrite / cast / prompts + 3-backend dispatcher
  images/ tts/ asr/  Provider clients that call the GPU services
  channels/        <channel>.yaml — SOURCE OF TRUTH for channel rules
  variants/        niche overlays on top of a channel
  voice_refs/      ⚠ AUDIO DATA (not code) — this is the "folder sprawl"
web/ web-next/     Prod FastAPI server + Next.js wizard/admin UI
scripts/           Laptop CLI entry points
tests/             pytest (269 files)
ai/                Knowledge base (architecture, decision-log, refactor-plan, audits)
```

**If you're ever lost:** the whole render is 6 files —
`entrypoint.py` (stages) → `video.py` → `engine.py` → `short_engine.py`
→ `contracts.py` → `channels/<channel>.yaml`. See `CODE_FLOW.md`.

---

## 6 · Real cleanup targets (small, safe, verified today)

Not a rewrite — a trim list. Each is verified against current callers.

| Target | Evidence | Action | Risk |
|---|---|---|---|
| `control/render_routes.py` | 0 importers; both servers use `control/routes/render_routes.py` | delete | none |
| `pipeline/voice_refs/**` sprawl | ~250 audio-data dirs polluting the code tree | move under `data/voice_refs/` (update `paths.py` seam) OR just accept it's data | low |
| 4 single-module dirs (`auth`,`render/qa`,`publish`,`text`) | thin but each has a caller | leave as-is unless a natural merge appears | none |
| `cloud/_bench/**`, `cloud/_shared/redeploy_for_otel.sh` | parked fixtures / 0 callers (see `ai/vestigial-audit.md`) | delete if truly unused | low |
| Remaining items in `ai/refactor-plan.md` Phase 3/4 | some were killed mid-flight | reconcile doc against reality, close or drop | low |

**Recommended sequence:** (1) delete the confirmed dead file, (2) decide
on `voice_refs` relocation, (3) reconcile `ai/refactor-plan.md` status to
reality so the docs stop implying work that's already done. No engine or
plugin changes — that layer is the good part.

---

## 7 · What NOT to do

- ❌ Don't rewrite the engine/plugin layer — it's textbook Registry+Strategy.
- ❌ Don't collapse the 3 planes — the decoupling is what gives `$0` idle.
- ❌ Don't touch channel behaviour via code — rules live in the YAML.
- ❌ Don't big-bang refactor before the video — finish the small trims.
