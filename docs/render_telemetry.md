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

### Recipe A — "23 identical panels" (image diversity)

1. `gsutil cp gs://ytfactory-prod-v3-artifacts/jobs/<job>/decision_log/decision_log.json -`
   → look for `scope=refiner chosen=fallback`. If present, the
     refiner failed and every prompt went through the legacy path —
     skip to step 4.
2. `gsutil cp gs://.../jobs/<job>/refiner_io/refiner_io.json -`
   → inspect the raw LLM response. If it's a dict instead of a
     list[N], bug B. If it's truncated, the token cap fired.
3. `gsutil cp gs://.../jobs/<job>/prompts_refined/prompts.json -`
   → if this file exists with N distinct prompts, refiner ran; the
     bug is downstream (image cache, server-side prompt collapse, or
     LoRA dominance). Move to step 4.
4. `gsutil cp gs://.../jobs/<job>/image_meta/00000.json -`
   → inspect `final_prompt_preview`. This is the EXACT string the
     z-image-turbo server received. If the cast prefix is 80%+ of the
     prompt, that's the legacy cast-dominance bug; verify with
     `cat 00007.json` etc — if they're all near-identical, the
     legacy path is winning.
5. If the final prompts ARE distinct but the panels still look
   identical, query the `image.cache.hit` events — a hash collision
   would surface here.

### Recipe B — "source fetch fell back"

1. Look for `event=source.fetch_fallback` in Cloud Logging:
   `gcloud logging read 'jsonPayload.event="source.fetch_fallback" AND jsonPayload.job_id="<job>"' --limit=10`.
2. `metadata.fallback_reason` tells you what went wrong (403, 404,
   timeout, parser error).
3. `gsutil cp gs://.../source/source.json -` to see what body was
   ACTUALLY used by the downstream rewrite.

### Recipe C — "LLM returned the wrong shape"

1. Filter Cloud Logging by `jsonPayload.event="llm.call" AND
   jsonPayload.job_id="<job>" AND jsonPayload.metadata.success=false`.
2. Read `metadata.retry`, `metadata.reason`. Common values:
   `max_tokens_swap`, `response_format_swap`, `content_filter`,
   `max_tokens_double` (token cap retry).
3. For the actual prompt + response, the matching event also has
   `metadata.prompt_sha256` and `metadata.response_sha256`. Pull the
   refiner_io / prompts_refined artifact for the full text.

### Recipe D — "ffmpeg failed"

1. `gcloud logging read 'jsonPayload.event="ffmpeg.call" AND jsonPayload.success=false AND jsonPayload.job_id="<job>"'`.
2. `metadata.stderr_tail` (last 4kB) + `metadata.args[]` reproduce the
   exact ffmpeg invocation.
3. `metadata.purpose` identifies which compose / overlay / music
   helper made the call.

### Recipe E — "TTS sounded wrong"

1. `gsutil ls gs://.../jobs/<job>/tts_chunks/` → enumerate chunks.
2. `gsutil cp gs://.../tts_chunks/<NNNNN>.json -` to see the text +
   voice the TTS server received.
3. Cross-check against `event=tts.server` log entries for
   `audio_seconds` / WPM anomalies.

### Recipe F — "ASR misaligned beats"

1. `gsutil cp gs://.../jobs/<job>/asr_alignment/asr.json -` for the
   word-by-word alignment.
2. `gsutil cp gs://.../jobs/<job>/timeline/timeline.json -` for the
   beat→time bounds the renderer ended up using.
3. Look for `event=timeline.beat_map` entries with
   `metadata.unanchored_count > 0` — those are beats that fell back
   to fixed timing.

### Recipe G — "wrong engine ran"

1. `gcloud logging read 'jsonPayload.event="engine.pick" AND jsonPayload.job_id="<job>"'`.
2. `metadata.chosen` is the engine name; `metadata.kind` /
   `metadata.format` are the inputs to the picker.

### Recipe H — "everything looks fine but the mp4 is empty"

1. `gsutil cp gs://.../jobs/<job>/events_log/events.jsonl - | jq -r '.event' | sort | uniq -c`
   → which stages emitted zero events.
2. Cross-check against `jobs/<job>.stages` in Firestore for which
   stages were even run.
3. A stage that ran but emitted nothing is a telemetry bug — fix
   that first.

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
| `cloud/render-worker-v2` | entrypoint.py (13 stages), writeback.py | wiring |
| `cloud/image-z-image-turbo` | server.py | wiring |
| `cloud/tts-chatterbox`, `cloud/tts-indicf5` | server.py | wiring |
| `cloud/asr-whisper` | server.py | wiring |
| `cloud/editing-agent` | server.py | wiring |
| `cloud/clone-video-worker` | server.py | wiring |
| `pipeline/llm` | cli.py + 14 modules | bodies pending |
| `pipeline/images` | images.py, prompt_refiner.py, image_cache.py, images_cloudrun.py | wiring |
| `pipeline/sources` | reddit_api, youtube_video, wikipedia, today_in_history | wiring |
| `pipeline/research` | wiki.py, channel_assets.py | wiring |
| `pipeline/render/audio` | tts_single, tts_chunked | wiring |
| `pipeline/render/timeline` | asr_anchors, asr_beats | wiring |
| `pipeline/render/visualize` | ai_beat_slideshow, longform_panels, footage_filler | wiring |
| `pipeline/render/compose` | beat_slideshow_mux, section_video_mux | wiring |
| `pipeline/render/music` | single_bed, ducked_loop, section_mood | wiring |
| `pipeline/render/overlays` | word_caption_pngs, sentence_caption_ass, chapter_card, lower_third, anchored_footage, closer_panel | wiring |
| `pipeline/render/shared` | ffmpeg_helpers, trim_letterbox | wiring |
| `control/core` | jobs, scheduler, queue, reconciler, storage, sim_worker, cloud_run | wiring |

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
