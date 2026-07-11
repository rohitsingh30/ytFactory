# ytFactory — Explainer Video Script & Outline

**Audience:** Mixed technical / PM · **Target length:** 8–12 min
**Format:** Talking-head or voiceover over screen-share. Diagrams from `design-deck/` (8 PNGs) carry the visuals.

Legend: 🎙️ = narration · 🖥️ = what's on screen · ⏱️ = running time target

---

## 0 · Cold open / hook  (0:00–0:30)  ⏱️ ~30s

🖥️ Play 8–10s of a finished Short (any `<channel>/shorts/*.mp4`), then cut to the `design-deck/01_system_context.png`.

🎙️
> "This is a fully-automated YouTube studio. You type one prompt — 'make an AITA short' — and a few minutes later a finished, captioned, 1080×1920 video is uploaded to the right channel. No human touches the edit. One render engine drives **seven** different channels. Here's how it works — top to bottom."

**PM takeaway to say out loud:** idea → published video, zero manual editing, seven channels on one codebase.

---

## 1 · What it does — the product  (0:30–1:45)  ⏱️ ~75s

🖥️ `README.md` channel table (or a slide of it).

🎙️
> "ytFactory turns an idea into an uploaded MP4. The pipeline is channel-agnostic: the same engine renders Reddit story shorts, Hindi mythology, physics explainers, football recaps, war history — seven channels today."

Hit these points (from README):
- **Any channel, any visual mode:** AI image-gen / motion video / archival footage — chosen *per render*, not per channel.
- **Any audio mode:** TTS narration or a sung Suno song — also per render.
- **Source of truth is a YAML** per channel (`pipeline/channels/<channel>.yaml`), not code. Non-engineers can tune a channel's rules.

**PM framing:** "Adding a channel is a config task, not an engineering project. That's the whole design bet."

---

## 2 · High-Level Design — the two planes  (1:45–3:30)  ⏱️ ~1m45s

🖥️ `design-deck/01_system_context.png`

🎙️ Walk the diagram left→right:
1. **User** confirms a proposal in a Next.js wizard (`/app/create`).
2. **Laptop = control plane.** It queues jobs, schedules channels round-robin, reconciles state. It does *not* do the heavy render.
3. **Google Cloud = execution plane.** Serverless, `$0 when idle` (min-instances=0). A Cloud Run **Job** — `render-worker-v2` — runs *one execution per render*.
4. The job calls **GPU-backed Cloud Run services**: image-gen (Z-Image-Turbo), TTS (Chatterbox / IndicF5), ASR (Whisper).
5. **State** lives in Firestore (`jobs/<id>`) + artifacts in GCS. Every stage emits telemetry to Cloud Logging.

🎙️ Key line:
> "The laptop decides *what* to render; the cloud does the *rendering*. They talk through Firestore and a GCS bucket — never a direct connection. That's why idle cost is basically zero."

**Two execution modes** (bottom bar of the diagram) — mention briefly, don't dwell:
- **Cloud push:** `jobs.py → trigger_render_job() → gcloud run jobs execute`.
- **Laptop lease (MLX):** the laptop can lease a task and run inference locally. Same engine either way.

---

## 3 · The render pipeline — 7 stages  (3:30–5:15)  ⏱️ ~1m45s

🖥️ `design-deck/03_worker_pipeline.png`

🎙️ Walk the 7 stages. Keep each to one sentence:
1. **rewrite** — one LLM call fetches *real* source material (Reddit/Wiki/NASA) and writes an engaging script. "The LLM is an editor on real material, not an author from nothing."
2. **cast** — a smaller LLM call locks character appearance (age, hair, clothing, props) so faces stay consistent across image panels.
3. **images** — Z-Image-Turbo generates the visual panels (parallel, ThreadPool fan-out ×4).
4. **tts** — narration synthesized (Chatterbox for English, IndicF5 for Hindi). *Runs in parallel with images.*
5. **asr** — Whisper aligns the narration word-by-word so captions land on the exact timestamp.
6. **compose** — final MP4 mux: visuals + captions + audio + music.
7. **upload** — pushed to YouTube; artifact lands at `gs://…/jobs/<id>/short.mp4`.

🎙️ Two design invariants worth stating (right side of the diagram):
- **Fail loud, never placeholder.** If a stage can't produce real output, it *raises*. No solid-color video pretending to be a success.
- **Everything is observable.** Every stage dumps its real input/output to GCS and emits `stage.start / end / failed`. You debug from artifacts + logs, never by guessing from the final frame.

**PM framing:** "The parallelism between images and TTS is why an 8-panel short renders in minutes, not serially."

---

## 4 · Low-Level Design — the plug-and-play engine  (5:15–7:30)  ⏱️ ~2m15s

**This is the core engineering idea. Slow down here.**

🖥️ `design-deck/04_plug_and_play.png` alongside the code.

🎙️ The problem it solved (from `contracts.py` history):
> "Before this, there were four separate renderers — shorts, sports, long-form, footage — about 8,300 lines with the same bugs fixed in multiple places. We collapsed them into **two engines** and **six plugin slots**."

### 4a · One entry point, dispatch by spec  (~40s)
🖥️ `pipeline/render/engine.py` — show `pick_engine`.

🎙️
> "Every render goes through one function — `render_via_engines(spec)`. It calls `pick_engine`, which routes to `render_short` or `render_long` based on `spec.kind`. That's the *only* branch on kind in the whole system."

### 4b · Six plugin slots, zero branches  (~50s)
🖥️ `design-deck/04_plug_and_play.png` — the six rows: **audio, timeline, visualize, overlays, music, compose.**

🎙️
> "The engine has **no** `if visual_mode == …` logic. It looks at the spec, asks the registry for the matching plugin, and calls it. Each slot is a Python *Protocol* — structural typing — so a plugin author just exposes a method with the right shape; no base class to inherit."

Call out the axes are **orthogonal** — mix freely:
- `spec.visual_mode` → `ai_beat_slideshow` / `longform_panels` / `footage_windows` / …
- `spec.audio_mode` → `tts_single` / `tts_chunked`
- `spec.music_policy` → `ducked_loop` / `single_bed` / `none`
- **overlays are list-valued** — captions + lower-thirds + chapter cards all compose together.

### 4c · The registry — 15 lines that make it extensible  (~45s)
🖥️ `pipeline/render/contracts.py` — show `register_plugin` / `get_plugin` (around line 529).

🎙️
> "The mechanism is dead simple: a dict keyed by (slot, name). Each plugin module calls `register_plugin('visualize', 'ai_beat_slideshow', …)` at import time. The engine calls `get_plugin(slot, name)`. If it's missing, it raises with the list of what *is* available."

🎙️ **The payoff line:**
> "Adding a whole new visual style is **one new file plus one `register_plugin` call**. No engine change, no contract change, no spec change. That's the difference between the old 8k-line mess and this."

**PM framing:** "New capability = one file. That's why seven channels don't mean seven codebases."

---

## 5 · Demo  (7:30–10:30)  ⏱️ ~3m

**Framing:** everything executes on cloud. This repo is the *same code*
that's deployed. So the demo = **show the code on screen → point at the
cloud where it runs.**

### Part 1 — the code (screen-share, ~1 min)
Scroll these six files in order — that's the whole flow:
1. `cloud/render-worker-v2/entrypoint.py:567` — the 7-stage `STAGES` list
2. `pipeline/render/video.py:81` — `render_via_engines`
3. `pipeline/render/engine.py` — `pick_engine` (short vs long)
4. `pipeline/render/short_engine.py:189` — the six `get_plugin` calls
5. `pipeline/render/contracts.py:526` — the registry (`register_plugin`/`get_plugin`)

🎙️ "This is the exact source deployed to the Cloud Run Job. Nothing
special runs in the cloud — this is what runs."

### Part 2 — it running on cloud (~2 min)
🖥️ Terminal + Cloud Logging + GCS bucket.
1. `python scripts/trigger_one_render.py` → prints a `job_id`.
2. Tail Cloud Logging: watch `stage.start`/`stage.end` walk the 7 stages
   you just showed in the code.
3. Artifact appears at `gs://ytfactory-prod-v3-artifacts/jobs/<id>/short.mp4`.
4. Play the finished short.

🎙️ Narrate the stages as they light up — they map 1:1 to Section 3's diagram.

### (Optional) deterministic backup — same code, local, no wait
If the cloud render is too slow for the cut, show that the identical
engine path runs locally with fixture plugins and makes a real MP4:
```
.venv/bin/pytest tests/test_render_engines_smoke.py -q     # 30 passed
```
🎙️ "Same `render_via_engines → pick_engine → render_short` path — GPU
calls swapped for fixtures. Proves the flow without waiting on the cloud."

---

## 6 · Wrap  (10:30–11:30)  ⏱️ ~1m

🖥️ Back to `design-deck/01_system_context.png`.

🎙️ Recap the three ideas that make it work:
1. **Two planes:** cheap laptop control plane + serverless cloud execution → `$0` idle.
2. **7 stages, fail-loud, fully observable** → you always know exactly where a render broke.
3. **Two engines, six plugin slots** → a new channel or style is config + one file, not a rewrite.

🎙️ Close:
> "Seven channels, one engine, one prompt to publish. That's ytFactory."

---

## Appendix — file cheat-sheet (for you, not on screen)

| Concept | Show this file |
|---|---|
| System context | `design-deck/01_system_context.png` |
| 7-stage pipeline | `design-deck/03_worker_pipeline.png` |
| Plugin slots | `design-deck/04_plug_and_play.png` |
| Engine dispatch | `pipeline/render/engine.py` → `pick_engine` |
| Registry (LLD core) | `pipeline/render/contracts.py:526–590` → `register_plugin` / `get_plugin` |
| Public entry | `pipeline/render/video.py` → `render_via_engines` |
| Channel rules | `pipeline/channels/<channel>.yaml` |
| Demo A trigger | `scripts/trigger_one_render.py` |
| Demo B test | `tests/test_render_engines_smoke.py` |

Other diagrams available if you want to go deeper: `02_job_lifecycle`, `05_cloud_topology`, `06_state_telemetry`, `07_post_render`, `08_channels_config`.

## Demo prep — MUST fix before recording a live demo

Local `ffmpeg` (Homebrew custom tap `homebrew-ffmpeg/ffmpeg` 8.1.1) is missing chained runtime libs
(libxcb, harfbuzz, libass, libvpx, …). Symptom: any ffmpeg call dies with `Abort trap: 6`, which fails
~150 tests (all ffmpeg-dependent) — the code itself is fine (4860 tests pass).

Fastest fix — install the full dependency set in one pass:
```
brew install $(brew deps homebrew-ffmpeg/ffmpeg/ffmpeg)
# then verify:
ffmpeg -y -loglevel error -f lavfi -t 1 -i sine=frequency=440:sample_rate=24000 -ac 1 -c:a pcm_s16le /tmp/t.wav && echo OK
```
Then re-run `tests/test_render_engines_smoke.py` to confirm the demo path is green.
(A live *cloud* render — Demo Option A — does NOT need local ffmpeg; it runs in the Cloud Run Job.)
