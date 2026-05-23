# Render Telemetry — Single Source of Truth

**Status:** Living document. Update when you add a new event, artifact
kind, or diagnostic recipe.

**Owner:** anyone editing the render path.

**Purpose:** when a render produces a bad mp4 the agent (or human)
debugging it MUST start here, not at the final mp4 frames. Every stage
emits structured events to Cloud Logging, dumps its real inputs and
outputs to GCS, and writes a decision log to Firestore. If a question
about a render can't be answered from those three surfaces, the gap is
a telemetry bug — fix the telemetry first, *then* the underlying
issue.

This doc is the catalogue: what gets emitted, where it lives, how to
query it, and which playbook to follow.

---

## 1. The three surfaces

| Surface | Lifetime | Purpose | Query tool |
|---|---|---|---|
| **Cloud Logging** | 30 days | Real-time event stream — every stage, every LLM call, every retry, every decision. Each entry is a structured JSON record with `event`, `category`, `success`, `duration_ms`, `job_id`, `metadata`. | `gcloud logging read` / Logs Explorer |
| **GCS artifacts** | Bucket retention (default 90d) | Full inputs and outputs for every stage — script JSON, cast JSON, full LLM prompts and responses, refined image prompts, per-beat prompt assembly, timeline JSON, ffmpeg arg vectors, events.jsonl flush. | `gsutil ls gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/` |
| **Firestore `jobs/<job_id>`** | Forever | Compact summary — stage durations, artifact pointers, decision log, final mp4 URL. Stable schema; safe to grep across all historic jobs. | `gcloud firestore documents describe jobs/<job_id>` |

The three are **layered** — the same fact may appear on more than one
surface (e.g. an LLM retry shows up as a log entry **and** as a row in
`events_log` artifact **and** as an entry in Firestore
`decision_log[]`). That redundancy is on purpose: each surface has a
different failure mode (log retention expires, GCS gets garbage-
collected, Firestore rate-limits) and a different query shape.

---

## 2. Event taxonomy

Every telemetry call routes through
`pipeline.observability.telemetry.track()` and emits a JSON-structured
log entry with the fields below.

### 2.1 Common fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `event` | string | yes | Dot-separated namespace, e.g. `llm.call`, `image.gen`, `source.fetch_fallback`. |
| `category` | string | yes | Top-level bucket — one of `pipeline`, `llm`, `image`, `tts`, `asr`, `ffmpeg`, `http`, `control`, `decision`, `artifact`, `stage`. |
| `success` | bool | yes | Did the operation complete without raising? Retries map to multiple events, each with their own `success`. |
| `duration_ms` | int | when meaningful | Wall-clock duration. Omit for instantaneous decisions. |
| `job_id` | string | yes | From `RenderContext.job_id` if set, else explicit kwarg. Used as the join key across surfaces. |
| `metadata` | dict | yes | Stage-specific. See per-event sections below. |

### 2.2 Body capture

`pipeline.observability.bodies.track_io()` is the same as `track()`
but accepts `input_text` and `output_text`. It attaches the following
extra metadata fields:

| Field | Always | Body-mode only |
|---|---|---|
| `input_chars`, `output_chars` | ✓ | — |
| `input_sha256`, `output_sha256` | ✓ (first 16 hex) | — |
| `input_preview`, `output_preview` | — | ✓ (≤ `TEL_BODY_MAX_CHARS`) |
| `input_truncated`, `output_truncated` | — | ✓ |

**Body mode** is on in Cloud Run (`K_SERVICE` or `CLOUD_RUN_JOB`
present) and off on the laptop. Override via
`YTFACTORY_TELEMETRY_BODIES={0,1}`. **Cheap mode** (off) still emits
the hash and length — only the preview text is dropped, so identical
inputs are still recognisable.

Secrets (Bearer tokens, sk-keys, AIza-, ya29-, JWTs, api_key=) are
redacted **before** truncation and hashing.

### 2.3 Event namespaces

| Namespace | Source | Captured fields |
|---|---|---|
| `stage.start` / `stage.end` / `stage.failed` | `@stage_envelope` decorator | `stage`, `channel`, `slug`, `run_id`, `format`, `artifact_kind`, `artifact_uri`, `error` (on failed) |
| `llm.call` | `pipeline/llm/cli.py` (all 3 backends) | `model`, `backend`, `input_tokens`, `output_tokens`, `finish_reason`, `prompt_sha256`, `response_sha256`, `retry`, `temperature`, `max_tokens`, `response_format` |
| `llm.retry` | `cli.py` retry branches | `reason` (one of `max_tokens_swap`, `response_format_swap`, `reasoning_effort_drop`, `max_tokens_double`, `content_filter`), `attempt` |
| `llm.module.<name>` | `@_obs.traced` LLM modules | Module-specific metadata + body via `track_io` |
| `image.gen` | `pipeline/images/images.py` | `final_prompt_sha256`, `final_prompt_preview`, `negative_prompt`, `seed`, `width`, `height`, `steps`, `cfg`, `model`, `cache_hit` |
| `image.cache.hit` / `image.cache.miss` / `image.cache.store` | `pipeline/images/image_cache.py` | `cache_key`, `prompt_sha256`, `path` |
| `image.refiner.batch` | `pipeline/images/prompt_refiner.py` | `batch_size`, `model`, `success_count`, `fallback_count`, `fallback_reasons[]` |
| `image.refiner.fallback` | per-beat fallback | `beat_index`, `reason` (`truncated`, `dict_returned`, `missing_field`, `length_violation`) |
| `prompts.author_gate` | `_author_prompts_for_engine` | `prompt_count`, `refined`, `fallback_used` |
| `prompts.resolve_for_beat` | `_resolve_prompt_for_beat` | `beat_index`, `source` (`refined`, `legacy_build_full_prompt`, `fallback`), `cast_chars`, `scene_chars`, `final_chars` |
| `source.fetch_attempt` | `_fetch_source` | `kind`, `ref`, `backend` |
| `source.fetch_ok` / `source.fetch_fallback` | same | `status_code`, `body_chars`, `fallback_reason` |
| `http.call` | per-service HTTP client | `service`, `method`, `url`, `status_code`, `request_sha256`, `response_sha256` |
| `tts.chunk` | `pipeline/render/audio/tts_*` | `chunk_index`, `text_sha256`, `text_preview`, `voice`, `audio_seconds` |
| `tts.server` | cloud TTS servers | `text_sha256`, `text_preview`, `voice`, `gpu_seconds`, `audio_seconds` |
| `asr.chunk` | ASR client | `chunk_index`, `audio_seconds`, `word_count`, `gpu_seconds` |
| `asr.server` | `cloud/asr-whisper/server.py` | `audio_seconds`, `word_count`, `model`, `language` |
| `ffmpeg.call` | `pipeline/render/shared/ffmpeg_helpers.py` | `purpose`, `args[]`, `exit_code`, `stderr_tail`, `output_bytes`, `duration_ms` |
| `engine.pick` | `pipeline/render/engine.py::pick_engine` | `chosen` (`render_short` / `render_long`), `kind`, `format` |
| `timeline.beat_map` | `asr_anchors`, `asr_beats` | `beat_count`, `total_seconds`, `unanchored_count` |
| `compose.run` | `compose/*` | `inputs[]`, `output_path`, `output_bytes` |
| `music.pick` / `music.duck` | `music/*` | `track_id`, `mood`, `duck_curve_id` |
| `overlay.render` | `overlays/*` | `kind` (word/sentence/chap/lower/anchor/closer), `count`, `total_chars` |
| `cache.hit` / `cache.miss` | various | `cache_key`, `path` |
| `artifact.upload` | `emit_artifact()` | `kind`, `uri`, `size_bytes` |
| `decision.<scope>` | freeform decision events | `scope` (e.g. `source`, `refiner`, `engine`, `compose`), `chosen`, `alternatives[]`, `reason` |
| `control.<scope>` | `control/core/*` | scope-specific |

---

## 3. GCS artifact layout

Bucket: `gs://ytfactory-prod-v3-artifacts/`

All artifacts live under `jobs/<job_id>/<kind>/` (plus an index for
list-shaped kinds). Layout enforced by
`pipeline/render/artifacts.py::_gcs_blob_path()`.

| Kind | Path | When emitted | Notes |
|---|---|---|---|
| `source` | `jobs/<job>/source/source.json` | After `_fetch_source` (success or fallback) | Raw fetched body + URL + status + which backend served it |
| `script` | `jobs/<job>/script/script.json` | After rewrite stage | Full narration JSON |
| `cast` | `jobs/<job>/cast/cast.json` | After cast stage | Structured cast (Q70 schema) |
| `prompts_raw` | `jobs/<job>/prompts_raw/prompts.json` | Before refiner | What the refiner is asked to refine |
| `prompts_refined` | `jobs/<job>/prompts_refined/prompts.json` | After refiner | What the image-gen actually gets |
| `refiner_io` | `jobs/<job>/refiner_io/refiner_io.json` | Inside `refine_prompts_batch` | Full request to LLM + raw LLM response — bug B forensics |
| `image_meta` | `jobs/<job>/image_meta/<NNNNN>.json` | Per panel | Final prompt + seed + cfg + cache_hit + bytes |
| `tts_chunks` | `jobs/<job>/tts_chunks/<NNNNN>.json` | Per chunk | text + voice + audio_seconds |
| `asr_alignment` | `jobs/<job>/asr_alignment/asr.json` | After ASR | word-by-word alignment |
| `timeline` | `jobs/<job>/timeline/timeline.json` | After beat mapping | beat→time bounds |
| `music` | `jobs/<job>/music/music.json` | After music stage | track, mood, duck curve |
| `compose` | `jobs/<job>/compose/compose.json` | After compose | ffmpeg arg vector + output stats |
| `render_plan` | `jobs/<job>/render_plan/plan.json` | After engine pick | RenderSpec dict |
| `decision_log` | `jobs/<job>/decision_log/decision_log.json` | At upload | All `decision.*` events for this job |
| `events_log` | `jobs/<job>/events_log/events.jsonl` | At upload | EventsBuffer flush — every event this run emitted |

**Every** stage uploads at least one artifact via the
`@stage_envelope` decorator. If a stage adds none, that's a telemetry
bug.

---

## 4. Firestore schema

Doc: `jobs/<job_id>`.

```json
{
  "job_id": "...",
  "status": "succeeded" | "failed" | "running",
  "stages": {
    "rewrite":         { "duration_ms": ..., "success": true, "artifact_uri": "gs://.../script/script.json" },
    "cast":            { ... },
    "images":          { ... },
    "tts":             { ... },
    "asr":             { ... },
    "compose":         { ... },
    "upload":          { ... },
    "editing_agent":   { ... }
  },
  "artifacts": {
    "script":   "gs://.../script/script.json",
    "cast":     "gs://.../cast/cast.json",
    "events_log": "gs://.../events_log/events.jsonl",
    ...
  },
  "decision_log": [
    { "scope": "source", "chosen": "anon", "fallback": true, "reason": "403", "ts": "..." },
    { "scope": "refiner", "chosen": "fallback", "reason": "dict_returned", "ts": "..." },
    ...
  ],
  "final_video_uri": "gs://.../short.mp4"
}
```

`decision_log[]` is the **fast-path diagnostic**: a single Firestore
read tells you every interesting decision the pipeline made without
having to spelunk into Cloud Logging.

---

## 5. Diagnostic playbook

When a render produces an unexpected mp4, work through these recipes
**in order**. Stop at the first one that points at the bug.

All recipes assume these shell vars are set:

```bash
export PROJECT=ytfactory-prod-v3
export BUCKET=ytfactory-prod-v3-artifacts
export JOB=24c5887a-d111-4b22-9e2f-eaa0a0a0a0a0   # full job id (uuid)
```

If you only have the short prefix (e.g. `24c5887a`), look it up:

```bash
gsutil ls gs://$BUCKET/jobs/ | grep 24c5887a
```

### Recipe A — "N identical panels" (image diversity)

**Job 24c5887a worked example** — TIFU short rendered 23
near-identical brunette/yellow-tee/bedroom panels despite a 4-character
cast and a varied script.

```bash
# Step 1 — was there a refiner fallback? (the fastest signal)
gsutil cat gs://$BUCKET/jobs/$JOB/decision_log/decision_log.json \
  | jq '.entries[] | select(.scope=="refiner")'
#   → {"scope":"refiner","chosen":"fallback","reason":"dict_returned",...}
#   means refiner failed and every prompt went through the legacy path.
#   Skip to step 4.

# Step 2 — inspect the raw refiner LLM call
gsutil cat gs://$BUCKET/jobs/$JOB/refiner_io/refiner_io.json | jq .
#   → .request.prompt[:500]        — what we sent
#   → .response.raw_text[:500]     — what the model returned (often a
#                                    dict {"prompt_0":"..."} not list[N])
#   → .response.parse_error        — why the parser rejected it
#   This is bug B forensics.

# Step 3 — if refiner ran successfully, did its output flow through?
gsutil cat gs://$BUCKET/jobs/$JOB/prompts_refined/prompts.json | jq 'length'
#   N distinct refined prompts here = refiner is healthy. Bug is
#   downstream. Move to step 4.

# Step 4 — read what z-image-turbo ACTUALLY received per panel
for i in 00000 00007 00014; do
  echo "=== panel $i ==="
  gsutil cat gs://$BUCKET/jobs/$JOB/image_meta/$i.json \
    | jq '{final_prompt_preview, seed, cfg, cache_hit}'
done
#   If `final_prompt_preview` for all panels starts with the same
#   200-char cast prefix and only varies in the last 10%, the legacy
#   cast-dominance path is winning (bug C).

# Step 5 — distinct prompts but identical pixels → cache collision
gcloud logging read \
  "jsonPayload.event=\"image.cache.hit\" AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=50 --format=json \
  | jq '.[] | {prompt_sha256: .jsonPayload.metadata.prompt_sha256, path: .jsonPayload.metadata.path}'
#   If multiple distinct panel indices all resolved to the same
#   cache path, you've found a hash-collision or key-truncation bug.
```

### Recipe B — "source fetch fell back"

**Worked example** — bug A on job 24c5887a: Reddit returned 403, the
adapter silently swapped to the anonymous JSON endpoint, downstream
rewrite never knew the body was the lower-fidelity sanitized text.

```bash
# Step 1 — Cloud Logging: every fallback decision
gcloud logging read \
  "jsonPayload.event=\"source.fetch_fallback\" AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=10 --format=json \
  | jq '.[].jsonPayload.metadata'
#   → {"kind":"reddit","ref":"r/tifu/comments/...","fallback_reason":"403",
#      "backend_attempted":"oauth","backend_used":"json_anon"}

# Step 2 — what body did the rewrite stage ACTUALLY see?
gsutil cat gs://$BUCKET/jobs/$JOB/source/source.json \
  | jq '{backend_used, status_code, body_chars, body_sha256, body: .body[:500]}'

# Step 3 — Firestore decision_log mirror (one read, no log query)
gcloud firestore documents describe jobs/$JOB \
  --project=$PROJECT --format=json \
  | jq '.fields.decision_log.arrayValue.values[].mapValue.fields | select(.scope.stringValue=="source")'
```

### Recipe C — "LLM returned the wrong shape"

Most common cause of bad renders. Hits the refiner, the rewrite, and
the cast author.

```bash
# Step 1 — all failed LLM calls for this job, with retry reasons
gcloud logging read \
  "jsonPayload.event=\"llm.call\" AND jsonPayload.job_id=\"$JOB\" AND jsonPayload.success=false" \
  --project=$PROJECT --limit=20 --format=json \
  | jq '.[].jsonPayload.metadata | {module: .module, backend: .backend, retry: .retry, reason: .reason, prompt_sha256: .prompt_sha256}'

# Step 2 — pair with the matching retry events to see the full chain
gcloud logging read \
  "jsonPayload.event=\"llm.retry\" AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=20 --format=json \
  | jq '.[].jsonPayload.metadata'
#   Common reason values:
#     max_tokens_swap         — we doubled max_tokens and retried
#     response_format_swap    — we dropped JSON mode and retried
#     reasoning_effort_drop   — gpt-5 reasoning was downgraded
#     content_filter          — Azure refused; we reframed
#     max_tokens_double       — token cap fired twice

# Step 3 — for the actual prompt+response, pull the module's artifact
gsutil cat gs://$BUCKET/jobs/$JOB/refiner_io/refiner_io.json | jq .
#   (or prompts_raw, script, cast — depending on the module hit)
```

### Recipe D — "ffmpeg failed"

```bash
# Step 1 — every failed ffmpeg invocation for this job
gcloud logging read \
  "jsonPayload.event=\"ffmpeg.call\" AND jsonPayload.success=false AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=10 --format=json \
  | jq '.[].jsonPayload.metadata | {purpose, exit_code, stderr_tail, args}'
#   → metadata.purpose       identifies the caller (compose, overlay,
#                            music, trim_letterbox, …)
#   → metadata.stderr_tail   last 4kB of ffmpeg stderr
#   → metadata.args          the full argv — paste into a local shell
#                            to reproduce

# Step 2 — was the compose stage even reached?
gcloud firestore documents describe jobs/$JOB \
  --project=$PROJECT --format=json \
  | jq '.fields.stages.mapValue.fields.compose'
```

### Recipe E — "TTS sounded wrong" (mispronounced word, wrong voice, wrong pace)

```bash
# Step 1 — list every TTS chunk this run produced
gsutil ls gs://$BUCKET/jobs/$JOB/tts_chunks/
#   → 00000.json … 00042.json (one per chunk)

# Step 2 — read text + voice + audio_seconds per chunk
for f in $(gsutil ls gs://$BUCKET/jobs/$JOB/tts_chunks/); do
  gsutil cat $f | jq '{chunk_index, voice, audio_seconds, text_chars, text_preview}'
done

# Step 3 — server-side timing/WPM anomalies
gcloud logging read \
  "jsonPayload.event=\"tts.server\" AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=50 --format=json \
  | jq '.[].jsonPayload.metadata | {chunk_index, voice, text_chars, audio_seconds, gpu_seconds, wpm: ((.text_chars / 5) / (.audio_seconds / 60))}'
#   Acceptable narration WPM: 140–185. Outside that band = pronunciation
#   bug or wrong voice profile selected.
```

### Recipe F — "ASR misaligned beats / captions drift"

```bash
# Step 1 — per-word alignment (the raw faster-whisper output)
gsutil cat gs://$BUCKET/jobs/$JOB/asr_alignment/asr.json \
  | jq '.words[:20]'

# Step 2 — how the renderer mapped beats to seconds
gsutil cat gs://$BUCKET/jobs/$JOB/timeline/timeline.json \
  | jq '.beats[] | {idx, text_preview, start, end, anchored, anchor_source}'

# Step 3 — count unanchored beats (those that fell back to fixed timing)
gcloud logging read \
  "jsonPayload.event=\"timeline.beat_map\" AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=5 --format=json \
  | jq '.[].jsonPayload.metadata | {beat_count, unanchored_count, total_seconds}'
#   Any unanchored_count > 0 is a probable cause of caption drift.
```

### Recipe G — "wrong engine ran" (short instead of long, or vice versa)

```bash
gcloud logging read \
  "jsonPayload.event=\"engine.pick\" AND jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=5 --format=json \
  | jq '.[].jsonPayload.metadata'
#   → {"chosen":"render_short","kind":"reddit_tifu","format":"9x16",
#      "spec_path":"gs://.../render_plan/plan.json"}

# To see the full RenderSpec that was dispatched:
gsutil cat gs://$BUCKET/jobs/$JOB/render_plan/plan.json | jq .
```

### Recipe H — "everything looks fine but the mp4 is empty / broken"

```bash
# Step 1 — which stages emitted events at all
gsutil cat gs://$BUCKET/jobs/$JOB/events_log/events.jsonl \
  | jq -r '.event' | sort | uniq -c | sort -rn
#   Expected stages: rewrite, cast, prompts.author_gate, image.gen,
#   tts.chunk, asr.chunk, timeline.beat_map, ffmpeg.call, compose.run,
#   artifact.upload. A stage with 0 events is a telemetry bug — fix
#   that BEFORE diagnosing the render issue.

# Step 2 — Firestore stage summary
gcloud firestore documents describe jobs/$JOB \
  --project=$PROJECT --format=json \
  | jq '.fields.stages.mapValue.fields | to_entries[] | {stage: .key, duration_ms: .value.mapValue.fields.duration_ms.integerValue, success: .value.mapValue.fields.success.booleanValue}'

# Step 3 — full event timeline (last resort, when 1+2 don't pinpoint it)
gcloud logging read \
  "jsonPayload.job_id=\"$JOB\"" \
  --project=$PROJECT --limit=500 --format=json --order=asc \
  | jq -r '.[].jsonPayload | "\(.timestamp) \(.event) success=\(.success) \(.metadata.purpose // .metadata.stage // "")"' \
  | less
```

---

## 6. Worked example: job `24c5887a`

(post-mortem of the 23-identical-panels render, 2026-05-23 ~11:45)

### Symptom

23 nearly-identical brunette/yellow-tee/bedroom panels in a TIFU
short. Cast was correctly diverse (4 distinct characters per cast
JSON); prompts were varied at the author step. So why?

### Telemetry walkthrough (what we **wish** we'd had)

1. Recipe A step 1 → `decision_log` would have shown
   `scope=refiner chosen=fallback reason=dict_returned` for the
   refiner batch.
2. Recipe A step 2 → `refiner_io.json` would have shown the raw LLM
   response as `{"prompt_0": "...", "prompt_1": "..."}` (dict, not
   list of 13), confirming bug B.
3. Recipe A step 4 → `image_meta/00000.json` would have shown the
   `final_prompt_preview` was 90% cast prefix and 10% scene — bug C.
4. Recipe B → `source.fetch_fallback fallback_reason=403` would have
   surfaced bug A even though it didn't directly cause the
   image-identity problem.

All three bugs are in the issue tracker; **none** are fixed by this
doc. The doc's job is to make the next instance of "23 identical
panels" diagnosable in 30 seconds instead of 4 hours.

---

## 7. Coverage matrix

Authoritative list of files that emit telemetry. Source of truth is
`coverage_matrix` in the session DB during the rollout; this section
is a snapshot. Update on every PR that touches the pipeline.

| Layer | Files | Status |
|---|---|---|
| `cloud/render-worker-v2` | entrypoint.py (13 stages), writeback.py | ✅ complete (phase 1) |
| `cloud/image-z-image-turbo` | server.py | ✅ complete (phase 3) |
| `cloud/tts-chatterbox`, `cloud/tts-indicf5` | server.py | ✅ complete (phase 4) |
| `cloud/asr-whisper` | server.py | ✅ complete (phase 4) |
| `cloud/editing-agent` | server.py | ✅ complete (phase 5) |
| `cloud/clone-video-worker` | server.py | ✅ complete (phase 3) |
| `pipeline/llm` | cli.py + 14 modules | ✅ complete (phase 2) |
| `pipeline/images` | images.py, prompt_refiner.py, image_cache.py, images_cloudrun.py | ✅ complete (phases 2-3) |
| `pipeline/sources` | reddit_api, youtube_video, wikipedia, today_in_history | ✅ complete (phase 6) |
| `pipeline/research` | wiki.py, channel_assets.py | ✅ complete (phase 6) |
| `pipeline/render/audio` | tts_single, tts_chunked | ✅ complete (phase 4) |
| `pipeline/render/timeline` | asr_anchors, asr_beats | ✅ complete (phase 5) |
| `pipeline/render/visualize` | ai_beat_slideshow, longform_panels, footage_filler | ✅ complete (phases 2-3) |
| `pipeline/render/compose` | beat_slideshow_mux, section_video_mux | ✅ complete (phase 5) |
| `pipeline/render/music` | single_bed, ducked_loop, section_mood | ✅ complete (phase 5) |
| `pipeline/render/overlays` | word_caption_pngs, sentence_caption_ass, chapter_card, lower_third, anchored_footage, closer_panel | ✅ complete (phase 5) |
| `pipeline/render/shared` | ffmpeg_helpers, trim_letterbox | ✅ complete (phase 5) |
| `control/core` | jobs, scheduler, queue, reconciler, storage, sim_worker, cloud_run | ✅ complete (phase 8) |

**100% coverage** = every file in the matrix has at least one
`track()` / `track_io()` call **at a meaningful decision point**, not
just at the file top-level. Audit periodically with
`grep -rn 'observability\|_obs\.\|obs\.timed' <path>`.

---

## 8. How to add a new event

1. Decide which namespace it belongs to. If it's a new namespace, add
   it to §2.3.
2. Decide if it needs body capture (use `track_io`) or structural
   only (use `track`).
3. Pick an `artifact_kind` if the event has a payload bigger than the
   inline char cap — add it to `pipeline/render/artifacts.py::KNOWN_KINDS`.
4. Add a recipe entry in §5 if a future operator might need to query
   this event during a post-mortem.
5. Don't forget the `try/except` — telemetry never breaks the
   pipeline.

---

## 9. Environment flags

| Flag | Default | Purpose |
|---|---|---|
| `YTFACTORY_TELEMETRY_BODIES` | on in cloud, off on laptop | Master switch for body capture (preview text only — hash + length always emit). |
| `YTFACTORY_TEL_BODY_MAX_CHARS` | `4000` | Per-side body cap. |
| `YTFACTORY_EVENTS_BUFFER_MAX` | `20000` | EventsBuffer ring size. |
| `YTFACTORY_BUCKET` | `ytfactory-prod-v3-artifacts` | Artifact bucket. Unset → all `emit_artifact` calls are no-ops (laptop safe). |
| `YTFACTORY_JOB_ID` | (set by worker entrypoint) | Used as the join key. |

---

## 10. Iron rules

1. **No silent failure.** Every stage that produces a file MUST
   either succeed and emit an artifact, or fail loudly. A stage that
   returns a placeholder mp4 / empty caption list / silent audio is a
   bug.
2. **No body without redaction.** `track_io()` redacts secrets BEFORE
   truncation. Never bypass this by calling `track()` with a raw
   prompt in `metadata`.
3. **No telemetry that breaks the pipeline.** Every `track` /
   `emit_artifact` / `EventsBuffer.flush_to_disk` call is wrapped in
   `try/except`. If you add a new helper, do the same.
4. **No new event without a recipe.** If a future operator can't
   query your event meaningfully, the event is dead weight. Add §5
   entry.
5. **Pull the artifacts before diagnosing.** When the user reports a
   bad render, the first action is `gsutil ls
   gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/`. Reading frames
   from the final mp4 to guess what went wrong is now an
   anti-pattern.
