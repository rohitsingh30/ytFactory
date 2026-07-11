# ytFactory — Actual Code Flow (what calls what)

Grounded in the real code, not the docs. Every arrow below is a real
function call you can open and read. File:line references are current
as of this trace.

---

## 30-second mental model

```
Browser prompt
   │
   ▼
control/core/jobs.py        ← writes Firestore jobs/<id>, fires the Cloud Run Job
   │  (gcloud run jobs execute ytfactory-render-worker-v2)
   ▼
cloud/render-worker-v2/entrypoint.py::_main_from_firestore(job_id)
   │  reads the job doc, builds a RenderSpec, then runs 7 STAGES in order
   ▼
STAGE "compose" calls the render engine:
pipeline/render/video.py::render_via_engines(spec, script, …)
   │
   ▼
pipeline/render/engine.py::pick_engine(spec)   → render_short OR render_long
   │
   ▼
pipeline/render/short_engine.py::render_short(…)
   │  resolves 6 plugins from the registry and calls them
   ▼
gs://…/jobs/<id>/short.mp4   → YouTube
```

Two things to hold in your head:
1. **The worker is a 7-stage loop.** One list, run top to bottom.
2. **The render step is branchless.** It never says `if visual_mode ==`.
   It looks up plugins in a dict and calls them.

---

## 1 · Where a job is born

**File:** `control/core/jobs.py`
- The browser wizard confirms a proposal → `_enqueue_render_job(...)`.
- It does exactly two things, in this order (order matters — see below):
  1. `create_job()` — writes `jobs/<id>` to Firestore (the source of truth).
  2. `trigger_render_job(<id>)` — `gcloud run jobs execute ytfactory-render-worker-v2`.

The Job execution gets the job id via env `YTFACTORY_JOB_ID`. It does NOT
receive the proposal directly — it reads it back from Firestore. That's
why create must happen before trigger.

---

## 2 · The worker: a 7-stage loop

**File:** `cloud/render-worker-v2/entrypoint.py`
**Entry:** `_main_from_firestore(job_id)`  (line ~3086)

What it does, in order:
1. Installs a SIGTERM handler (Cloud Run kills the task at timeout).
2. Reads `jobs/<id>` from Firestore (3-attempt retry for write-propagation race).
3. Builds the **RenderSpec** from `proposal + channel YAML + variant YAML`
   via `pipeline.render.spec.build_spec`. This is the resolved "what am I
   about to render" — persisted back to Firestore so the dashboard shows it.
4. Runs the stage list:

```python
# entrypoint.py:567
STAGES = [
    ("rewrite",  "Rewriting script"),        # LLM writes the script
    ("cast",     "Casting voice & visuals"),  # LLM locks character look
    ("images",   "Generating images"),        # Z-Image-Turbo panels
    ("tts",      "Synthesizing narration"),   # Chatterbox / IndicF5
    ("asr",      "Aligning captions"),         # Whisper word timing
    ("compose",  "Composing video"),           # ← the render engine runs here
    ("upload",   "Uploading to GCS"),          # short.mp4 → bucket → YouTube
]
```

Each stage:
- Emits `stage.start` / `stage.end` / `stage.failed` to Cloud Logging.
- Dumps its real input/output to `gs://…/jobs/<id>/` (so you debug from
  artifacts, never by guessing from the final frame).
- **Fails loud.** If it can't produce real output it raises — no
  placeholder MP4 that looks successful.

Key stage functions (real):
- `_stage_rewrite_real()` (line ~1250) → calls `pipeline.llm.rewrite.rewrite()` → `script.json`
- `_stage_cast_real()` (line ~1461) → per-character appearance lock → `cast.json`
- The `images` / `tts` / `asr` / `compose` work is driven by the render engine (below).

> Note: `images` and `tts` conceptually overlap — the engine kicks off
> image-gen on a worker thread while TTS+ASR run, when both are
> cloud-bound. See `pipeline/stage_overlap.py`.

---

## 3 · The render engine (the `compose` stage's real work)

**Public entry:** `pipeline/render/video.py::render_via_engines(spec, …)` (line 81)

```python
# video.py:123
from pipeline.render.engine import pick_engine
engine_fn = pick_engine(spec)      # returns render_short OR render_long
return engine_fn(spec, script, work_dir, out_path, …)
```

**Dispatcher:** `pipeline/render/engine.py::pick_engine(spec)`
- `spec.kind == SHORT`  → `short_engine.render_short`
- `spec.kind in {LONG_FORM, SPORTS_DOC, FOOTAGE_ONLY}` → `long_engine.render_long`

That's the **only** branch on kind in the whole render path.

---

## 4 · Inside the short engine — 6 plugins, zero branches

**File:** `pipeline/render/short_engine.py::render_short(...)` (line 127)

Step 1 — resolve which plugin name goes in each slot (from the spec):
```python
# short_engine.py:167
audio_name     = _audio_plugin_name(spec)        # from audio_mode + voice_provider
timeline_name  = _timeline_plugin_name(spec)     # engine default: asr_beats
visualize_name = _visualize_plugin_name(spec)    # from spec.visual_mode
music_name     = _music_plugin_name(spec)        # from spec.music_policy
compose_name   = _compose_plugin_name(spec)      # engine default: beat_slideshow
```

Step 2 — fetch the impls from the registry:
```python
# short_engine.py:189
audio_plugin     = get_plugin("audio",     audio_name)
timeline_plugin  = get_plugin("timeline",  timeline_name)
visualize_plugin = get_plugin("visualize", visualize_name)
music_plugin     = get_plugin("music",     music_name)
compose_plugin   = get_plugin("compose",   compose_name)
```

Step 3 — call them in order (this IS the render):
```
audio_plugin.synth(spec, script, work_dir)         → AudioResult   (TTS)   short_engine.py:406
timeline_plugin.build(spec, script, audio)         → Timeline      (ASR)   :411
visualize_plugin.produce(spec, timeline, work_dir) → visuals       (images):417
overlays: for each active producer .produce(...)   → caption/lower-third PNGs :491+
music_plugin.compose(spec, audio.duration_s)       → music.wav             :247
compose_plugin  (FinalMux)                         → final short.mp4
```

Overlays are **list-valued**: captions + lower-thirds + chapter cards +
anchored footage + closer panel are all the same shape and get
concatenated (`short_engine.py:491–538`).

---

## 5 · The registry — the 15 lines that make it plug-and-play

**File:** `pipeline/render/contracts.py` (line 526)

```python
_REGISTRY: dict[str, dict[str, Any]] = {}          # {slot: {name: impl}}

def register_plugin(slot, name, impl):             # plugins call this at import
    _REGISTRY.setdefault(slot, {})[name] = impl

def get_plugin(slot, name):                        # engine calls this to dispatch
    impls = _REGISTRY.get(slot)
    if impls is None or name not in impls:
        raise PluginNotFound(f"{name} not in {slot}; available: {…}")
    return impls[name]
```

Each plugin module registers itself at import time. Example:
```python
# pipeline/render/visualize/ai_beat_slideshow.py
register_plugin("visualize", "ai_beat_slideshow", AiBeatSlideshow())
```

**The payoff:** a new visual style = one new file in `pipeline/render/visualize/`
+ one `register_plugin(...)` call. No engine change, no contract change,
no spec change.

Slot dirs (open these to see the impls):
```
pipeline/render/audio/      → tts_single, tts_chunked, audio_from_fixture
pipeline/render/timeline/   → asr_beats, asr_anchors, timeline_from_fixture
pipeline/render/visualize/  → ai_beat_slideshow, longform_panels, footage_windows,
                              archival_shotlist, footage_filler, visuals_from_fixture
pipeline/render/overlays/   → word_caption_pngs, sentence_caption_ass, lower_third,
                              chapter_card, closer_panel, anchored_footage, noop
pipeline/render/music/      → ducked_loop, single_bed, section_mood, none
pipeline/render/compose/    → beat_slideshow, section_video
```

---

## 6 · Where it runs — local code, cloud execution

**All execution is on cloud.** This repo is the *code you read and
narrate* — the exact same source that is deployed to the Cloud Run Job
and GPU services. There is no separate "cloud version." The local files
in `pipeline/` and `cloud/render-worker-v2/` ARE what runs in production.

So for the video, the flow is: **show the code here → point at the cloud
where it executes.**

### 6a · Show the code (on screen, local repo)
Walk these files in order — this is the whole flow, top to bottom:
1. `control/core/jobs.py` — job is born
2. `cloud/render-worker-v2/entrypoint.py:567` — the 7-stage `STAGES` list
3. `pipeline/render/video.py:81` — `render_via_engines`
4. `pipeline/render/engine.py` — `pick_engine`
5. `pipeline/render/short_engine.py:189` — the 6 `get_plugin` calls
6. `pipeline/render/contracts.py:526` — the registry

### 6b · Show it running (cloud)
Fire one real render and watch the stages stream through Cloud Logging:
```bash
python scripts/trigger_one_render.py        # fires one real CLOUD render
gcloud logging read 'resource.labels.job_name="ytfactory-render-worker-v2"' \
  --project=ytfactory-prod-v3 --freshness=10m
```
You'll watch `stage.start`/`stage.end` walk
rewrite→cast→images→tts→asr→compose→upload, then the artifact appears at
`gs://ytfactory-prod-v3-artifacts/jobs/<id>/short.mp4`.

### 6c · (Optional) prove the same code runs locally
If you want a deterministic on-camera proof that this exact engine path
works without waiting on the cloud, the smoke test runs
`render_via_engines → pick_engine → render_short` with fixture plugins
(no GPU, no network) and produces a real MP4:
```bash
.venv/bin/pytest tests/test_render_engines_smoke.py -q     # 30 passed
```
Same production code path, GPU calls swapped for fixtures.

---

## One-line summary per file

| File | Role in the flow |
|---|---|
| `control/core/jobs.py` | Writes Firestore job, fires the Cloud Run Job |
| `cloud/render-worker-v2/entrypoint.py` | The 7-stage worker loop; `_main_from_firestore` |
| `pipeline/render/spec.py` | `build_spec` — resolves proposal+YAML → RenderSpec |
| `pipeline/render/video.py` | `render_via_engines` — public render entry |
| `pipeline/render/engine.py` | `pick_engine` — short vs long (only kind branch) |
| `pipeline/render/short_engine.py` | Resolves + calls the 6 plugins in order |
| `pipeline/render/contracts.py` | The registry + 6 Protocol slot definitions |
| `pipeline/render/<slot>/*.py` | The actual plugin impls (self-registering) |
| `pipeline/channels/<channel>.yaml` | Channel rules (source of truth, not code) |
