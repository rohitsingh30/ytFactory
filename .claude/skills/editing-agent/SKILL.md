---
name: editing-agent
description: Polish a finished ytFactory mp4 OR assemble a folder of raw mp4 clips, generated stills, or a mixed bag into a directed cinematic edit. Auto-detects mode (polish / assemble-clips / assemble-stills / assemble-mixed) from input. LLM planner emits an EDL (cuts, transitions, LUT, Ken Burns, audio duck, letterbox, music sync); ffmpeg + PySceneDetect execute. Runs as a Cloud Run service (cloud/editing-agent/) registered in the admin-tab orchestrator alongside render-worker-v2; falls back to laptop ffmpeg when cloud unavailable. Optional 8th stage between compose and upload via proposal.editing.enabled. Use when the user says "polish this short", "make this cinematic", "assemble these clips", "cut these stills into a teaser", "/editing-agent on aita04". For pure review use /critique-video. For format research use /clone-video-format.
learnings_consulted:
  - .claude/skills/editing-agent/learnings/_index.md
  - .claude/skills/critique-video/SKILL.md
  - docs/cloudrun_render_worker.md
  - docs/cloud_prerender_hook.md
  - docs/admin_panel_first.md
---

# /editing-agent — cinematic editor for any ytFactory artifact

Acts as a **director + colorist + editor + DP**, then hands the
edit-decision-list (EDL) to ffmpeg. Works on three artifact types:

1. A single rendered `.mp4` — re-cuts, color-grades, adds letterbox,
   ducks audio, smooths transitions. The "polish" pass.
2. A folder of `.mp4` clips — scene-detects each, picks the best
   moments, weaves them into a cinematic montage with grade + music.
3. A folder of images (`.png` / `.jpg` / `.webp`) — Ken Burns,
   parallax, crossfade, optional 2.5D pseudo-motion, color grade,
   music sync. Treats each still as a directed shot.
4. A mixed folder of clips + stills — types are auto-classified and
   woven into one timeline.

You wear three hats:

- **Director.** Watch / read every frame, pick the cinematic intent
  (mood, pace, lens, palette).
- **Editor.** Build the EDL — cuts, transitions, ramps, LUT, ducking,
  letterbox, music beat-sync.
- **Colorist.** Pick the LUT (`cinematic.cube` / `teal-orange.cube` /
  `noir.cube` / `warm-doc.cube`) and grade per shot.

ffmpeg + PySceneDetect are the executor. The LLM is the brain. Same
dispatcher pattern as `pipeline/llm/cli.py` — Claude CLI on laptop,
Azure OpenAI in cloud, no per-call spend. (`auto-editor` was scoped
out before v1 ships — its PyPI metadata declares `Requires-Dist:
pyav==13.1.*` but PyAV is published on PyPI as `av`, not `pyav`, and
pip's resolver can't bridge the alias. The LLM planner emits explicit
trim filters per shot so we don't need auto-editor's silence-cut
heuristic.)

## Architecture

This skill is the **authoring + dispatch layer**. The actual work runs
in two places:

```
                       /editing-agent (this skill)
                              │
                              │ 1. classify input → mode
                              │ 2. extract context (channel? bare?)
                              │ 3. call planner (LLM → EDL JSON)
                              │ 4. dispatch to executor
                              ▼
        ┌──────────────────────┴──────────────────────┐
        │                                             │
        ▼                                             ▼
   CLOUD (default)                              LAPTOP (fallback)
   cloud/editing-agent/                         pipeline/editing/executor.py
   Cloud Run JOB                                Same EDL → ffmpeg
   sibling of render-worker-v2                  Used when CLOUDRUN_EDITING_AGENT_URL
   Firestore: editing_jobs/<id>                 unset OR cloud is unhealthy
   Admin tab: green/yellow/red                  CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=1
                                                 hard-errors instead
```

The cloud JOB is **also wired as an optional 8th stage in
`render-worker-v2`** between `compose` and `upload`. Opt-in per render
via `proposal.editing.enabled = true` in the website's render form.
When opted in, the orchestrator's existing TIMELINE machinery streams
the editing-agent's progress to the UI for free — no new plumbing.

## How to run it

### 1. Confirm input + mode

Resolve the input path:

- If user passed an explicit path/slug, use it.
- Else: scan `~/Desktop`, `~/Downloads`, the channel's `<slug>.mp4`,
  and the most recently modified `.mp4` in CWD. State which one in
  one line.

Auto-detect the mode (do NOT ask):

| Input shape | Mode |
|---|---|
| 1 file, `.mp4` | `polish` |
| Folder, all `.mp4` | `assemble-clips` |
| Folder, all images (`.png`/`.jpg`/`.webp`) | `assemble-stills` |
| Folder, mixed | `assemble-mixed` |

If ambiguous (folder with 1 mp4 + 1 image), default to
`assemble-mixed` and state the call.

### 2. Resolve context (channel-aware vs bare)

If the user passed `--channel <slug>` OR the input path lives under a
channel dir (`<channel>/shorts/`, `<channel>/long_form/`, etc.):

- Load `<channel>/config.yaml` and `<channel>/learnings/channel.md`.
- Inherit aspect ratio (9:16 / 16:9), duration band, footage policy,
  music license rules, banned visual treatments.
- Refuse outputs that violate channel constraints (e.g. you cannot
  add stock B-roll to a HistoryRecapped Short — archive-only).

If bare (no channel context), default profile:

```
aspect: keep input
duration: keep input ±20%
lut: cinematic.cube
music: none (user must opt in)
captions: keep existing if present
```

### 3. Hat 1 — Director (read every frame)

For `polish`: dense-sample the mp4 with ffmpeg (`-vf fps=1/2` or
2 fps for ≤90s clips), read each frame, write a per-second beat sheet
(palette, composition, energy 1-10).

For `assemble-clips`: PySceneDetect per file → list of shots, then
sample 1 frame per shot.

For `assemble-stills`: read each image, classify
(wide / medium / close / detail / negative-space / text), tag mood.

Output a `director_notes` block — what story this footage tells, what
arc you're going to cut, the picked LUT, the picked pace.

### 4. Hat 2 — Editor + Colorist (build the EDL)

Call the LLM planner via the existing dispatcher:

```python
from pipeline.editing.planner import plan_edit
edl = plan_edit(
    input_paths=[...],
    mode="polish" | "assemble-clips" | "assemble-stills" | "assemble-mixed",
    director_notes=notes,          # from stage 3
    channel_profile=channel_cfg,   # None when bare
    target_duration=target,        # from channel band or kept
)
```

`pipeline/editing/planner.py` shells through `pipeline.llm.cli.call_llm`
— Claude CLI on laptop, Azure OpenAI in cloud, **no separate spend.**

The EDL schema (see `## Output schema` below) is closed-form JSON the
executor can ffmpeg-compile deterministically. NEVER let the LLM emit
free-form ffmpeg commands — the executor whitelists filter chains.

### 5. Quality gates (block on hit)

Before dispatch, mechanically enforce:

1. **Aspect-ratio match** — EDL output dimensions match channel
   aspect (or input aspect when bare).
2. **Duration budget** — final runtime within ±10% of `target_duration`.
3. **LUT whitelist** — `lut` field is one of the 4 shipped cubes; no
   path traversal.
4. **Music license check** — if `music.source` is set, must be
   `archive_pd` / `youtube_audio_library` / a path under
   `<channel>/music/`. Reject anything else.
5. **Filter whitelist** — every entry in `edl.shots[].filters[]` is in
   the executor's allow-list (`crop`, `scale`, `eq`, `lut3d`, `fade`,
   `xfade`, `zoompan`, `subtitles`, `volume`, `afade`, `loudnorm`).
6. **Footage-policy match** (channel mode only) — refuse stock B-roll
   on archive-only channels; refuse AI imagery on broadcast-only
   channels (`sportsrecapped`).
7. **Caption preservation** — if input had burned-in captions
   (detect via OCR sample), warn before re-grade can crush legibility.

### 6. Dispatch to executor

```python
from pipeline.editing.cloudrun import execute_edit, CloudRunUnavailable
try:
    out_mp4 = execute_edit(edl, output_dir=...)
except CloudRunUnavailable:
    from pipeline.editing.executor import execute_local
    out_mp4 = execute_local(edl, output_dir=...)
```

Cloud path POSTs the EDL + signed-URL inputs to the Cloud Run JOB
(`CLOUDRUN_EDITING_AGENT_URL`), polls Firestore `editing_jobs/<id>`
for status, downloads result from GCS.

Laptop path runs ffmpeg locally with the same EDL.

**Per-render circuit breaker** — first `CloudRunUnavailable` in a
batch trips a module-global flag → all subsequent edits skip cloud.
Same pattern as `pipeline/images_cloudrun.py`. Reset via
`pipeline.editing.cloudrun.reset_circuit_breaker()` at the top of
each render entrypoint.

### 7. Echo output path + post-mortem

Print the absolute path to the produced `.mp4`. Print a one-line
post-mortem the user can copy into the chat:

```
✓ /editing-agent polish on cosmos_eddington_1919.mp4
  → cosmos_eddington_1919__edited.mp4
  mode: polish | LUT: cinematic.cube | duration: 58.2s (was 61.4s)
  cuts: 7 | transitions: 4 xfade | grade: teal-orange shadow lift
  cloud: ✓ (cold-load 17s)
```

### 8. Self-learning hook

After the user runs `/critique-video` on the produced mp4:

1. If a regression is found, classify:
   - **ONE-OFF** (this LUT was wrong for this story) → fix in the
     EDL, re-run, append a 1-line note to
     `.claude/skills/editing-agent/learnings/_index.md`.
   - **CLASS-OF-BUG** (planner consistently picks teal-orange for
     warm-doc footage) → fix in `pipeline/editing/planner.py`
     (system prompt or post-validate), append to
     `.claude/skills/editing-agent/learnings/<topic>.md`, mirror to
     `~/.claude/projects/-Users-rohit-ytFactory/memory/skill_editing_agent_<topic>.md`
     per CLAUDE.md dual-save rule.
2. Update `MEMORY.md` index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate (stage 5) that blocks emit on detection.

## Output schema (EDL)

```json
{
  "version": 1,
  "mode": "polish | assemble-clips | assemble-stills | assemble-mixed",
  "channel": "<slug or null>",
  "aspect": "9:16 | 16:9 | 1:1",
  "fps": 30,
  "target_duration_s": 58.0,
  "lut": "cinematic.cube",
  "letterbox": { "enabled": true, "ratio": 2.39 },
  "audio": {
    "duck_speech_db": -3,
    "loudnorm_lufs": -14,
    "music": null
  },
  "music": {
    "source": "archive_pd | youtube_audio_library | path",
    "path": "<channel>/music/cinematic_swell.mp3",
    "fade_in_s": 1.0,
    "fade_out_s": 2.0,
    "duck_under_speech": true
  },
  "shots": [
    {
      "idx": 0,
      "input_ref": "clip_03.mp4#scene_2 | image_07.png",
      "in_s": 4.20,
      "out_s": 6.80,
      "filters": [
        { "type": "crop",    "args": "iw:iw/2.39" },
        { "type": "scale",   "args": "1080:608" },
        { "type": "lut3d",   "args": "file=luts/cinematic.cube" },
        { "type": "zoompan", "args": "z='zoom+0.0008':d=125:s=1080x1920" }
      ],
      "transition_in":  { "type": "fade",  "duration_s": 0.3 },
      "transition_out": { "type": "xfade", "duration_s": 0.5, "to_idx": 1 }
    }
  ],
  "captions": {
    "preserve_burned": true,
    "add_overlay": false
  },
  "director_notes": "warm cosmos doc, slow swell, hold every shot ≥2s"
}
```

The executor compiles this to a single ffmpeg invocation (or a
2-pass loudnorm + filter_complex chain). **No free-form ffmpeg from
the LLM ever.**

## Important rules

- **No free-form ffmpeg from the LLM.** Filter whitelist is enforced
  in stage 5. New filters require a code change in
  `pipeline/editing/executor.py::ALLOWED_FILTERS`.
- **No copyrighted music.** Stage 5 check 4 blocks anything outside
  archive PD / YouTube Audio Library / `<channel>/music/`.
- **Channel context is sticky.** When `--channel` is set or the input
  lives under a channel dir, channel constraints are LITERAL — copied
  from `<channel>/learnings/channel.md` verbatim, never paraphrased.
- **Always use `.venv/bin/python`** for any helper invocations.
- **LLM via the dispatcher only** — `pipeline/llm/cli.py::call_llm`,
  never `anthropic.Anthropic` directly. (memory:
  `feedback_llm_via_claude_cli.md`)
- **Cloud first, laptop fallback.** Default behavior tries Cloud Run.
  Set `CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=1` for canary testing
  to hard-error instead.
- **Per-render circuit breaker.** First cloud failure trips local
  fallback for the whole render — protects the 30+ shot critical
  path the same way `pipeline/images_cloudrun.py` does. Reset is the
  caller's responsibility (renderer entrypoints).
- **Stays out of the renderer's lane.** This skill never runs stages
  1-7 (rewrite / cast / images / tts / asr / compose / upload). It
  consumes their output OR runs as the optional 8th stage between
  compose and upload.
- **Admin-tab visibility is mandatory.** The new service registers in
  `pipeline/cloud/services.py` so the `/app/cloud` admin tab shows
  green/yellow/red live (per `docs/admin_panel_first.md`). No
  separate `cloud-health` skill — that pattern was retired 2026-05-10.

## Cloud pre-render hook

Before any render that opts into the editing stage, the orchestrator
must call:

```python
from pipeline.cloud.warm import warm_async
warm_async(channel, include_editing=True)
```

`include_editing=True` adds the editing-agent service URL to the
parallel pre-warm fan-out so the ~17s cold-load happens behind the
existing TTS + image warm rather than serially after compose. Health
of the editing-agent service is visible in the `/app/cloud` admin
tab alongside TTS and image services.

## Learnings from prior runs

(empty on day 1; the self-learning hook in stage 8 appends here)

## Why this skill is separate from /critique-video and /clone-video-format

`/critique-video` REVIEWS a finished mp4 (viewer + engineer hats) and
emits text feedback — never edits the file. `/clone-video-format`
RESEARCHES the format DNA of an external video and emits a
fingerprint JSON — never edits anything. `/editing-agent` is the only
skill that produces a NEW mp4 from existing artifacts. The schema
(EDL with closed-form filter whitelist) and the executor (ffmpeg +
PySceneDetect inside a Cloud Run service) are incompatible
with both review and research; forcing it into either skill would
fork their schemas and break their critique loops.
