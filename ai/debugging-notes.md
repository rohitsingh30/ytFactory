# Debugging Notes

Triage recipes for the common failure shapes. The render-detail UI page is the primary debug surface (Q39 — user-confirmed: "read the error there, retry from UI").

This doc is the lookup table you reach for when a render fails. For *why* the gates fire and the philosophy that governs them, see `ai/decision-log.md` (especially ADR-003 — gates as repair triggers).

---

## The primary debug surface

**`/app/render/[jobId]`** in the web-next UI (`web-next/app/app/render/[jobId]/page.tsx`).

This page reads the Firestore job doc and surfaces:
- Per-stage status (rewrite / cast / images / tts / asr / compose / upload).
- The error message from the failed stage.
- Retry button (re-fires the Cloud Run job).
- Links into Cloud Logging for the full subprocess stdout/stderr.

Operator workflow: read the error here, hit retry, escalate to Cloud Logging only when the surfaced message is opaque.

---

## The traceback extractor

When the render-worker subprocess crashes, the operator-visible error is built by `pipeline/render/video.py::_extract_last_traceback`. Read it once; it's the most important diagnostic primitive in the system.

**File:** `pipeline/render/video.py:642`.

**What it does:** Walks the subprocess log top-to-bottom, finds every `Traceback (most recent call last):` header, and returns the **last NON-telemetry** traceback (capped at 80 lines). "Non-telemetry" filtering is at `_is_telemetry_traceback` (line 623), which classifies a traceback as telemetry-noise when its first `File "..."` frame is in `opentelemetry/(exporter|sdk/(metrics|_logs|trace)/export)/`.

**Why this exists:** Pre-2026-05-13 (commit 8a4f7e15), the extractor returned the LAST traceback unconditionally. The OTel Cloud Monitoring exporter (`opentelemetry-exporter-gcp-monitoring 1.12.0a0`) logs `400 "Points must be written in order"` via `logger.error(..., exc_info=ex)` — these are LOGGED but NON-FATAL. The exporter's `try/except` catches them internally and returns `MetricExportResult.FAILURE`. The subprocess keeps running, but the traceback is in the log. The operator-surface tail-25 was 100% OTel noise, ZERO actionable diagnostic.

**Fallback chain:**
1. Last non-telemetry traceback (capped 80 lines) — the normal case.
2. Last telemetry traceback when EVERY traceback is telemetry — a diagnostic clue itself (subprocess died from SIGNAL / OOM-kill / non-exception path).
3. Trailing 25 lines when no traceback exists at all.

**When debugging:** if you see "Subprocess error: <opentelemetry frames>" in the operator surface, the extractor's telemetry filter is misclassifying something. Check that the failing frame's *first* `File "..."` line really is in `opentelemetry/` — the entry frame is what gets classified, not deep frames.

---

## Cloud Logging access pattern

When the UI error is too thin or the render-worker subprocess fails before it can write back to Firestore:

```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="ytfactory-render-worker-v2"' \
  --project=ytfactory-prod-v3 \
  --limit=200 \
  --format="value(textPayload)"
```

For a specific job ID, filter on `labels.execution_name` or the Firestore `jobs/<id>` doc which records the Cloud Run execution name.

GCS artifacts land at `gs://ytfactory-prod-v3-artifacts/jobs/<id>/short.mp4` (or `long.mp4`).

---

## `audit_data/` — historical reference

The `audit_data/` directory contains 153 per-render summary blobs (`<channel>_summary.json` + per-job UUID dirs). Per Q53 these describe **pre-fix renders** and "the data you chose earlier might be old, we have made changes in model since that." Treat as **historical reference only** — useful for spotting patterns across 100+ renders, NOT as live signal for the current pipeline state. Open question Q-013 tracks whether this directory is still being written to.

---

## Failure recipes

Each recipe is shaped: **symptom → which stage → file:line → fix path**.

### Recipe 1: Rewrite gate fired — `LongFormContractError`

**Symptom:** Render fails at the rewrite stage with `LongFormContractError`. Error message mentions section word counts or total narration words below threshold.

**Stage:** `rewrite` (long-form).

**Where:** `pipeline/critic_long_form.py` — the envelope validator. Specifically:
- `HARD_FLOOR_FRAC = 0.50` at line 222 (total under 50% of expected).
- `HARD_SECTION_FLOOR_FRAC` at ~line 370 (per-section under % of mean).

**Diagnosis:**
1. Check the section word counts in the rewrite output. If one section is dramatically short (e.g. 14% of mean), the per-section floor fired.
2. If the total is short but sections are balanced, the total floor fired — the outline LLM under-allocated.
3. If sections are individually fine but writeback duration still fails, see Recipe 3.

**Case study — `a734babb` (2026-05-20 15:02):** Section 7 contained 51 words; mean section was 363 words (14% of mean); narration delivered 3634 words (81% of 4500 target). The per-section floor at line ~370 fired. Killed before any mp4 produced. Root cause: the parallel section-body fan-out at `pipeline/llm/rewrite_long_form.py:1017` (`_generate_all_section_bodies`, `_SECTION_BODY_MAX_WORKERS=5`) has no cross-section awareness; one section LLM under-delivered while the others over-delivered. See ADR-003 + ADR-007 for the fix shape (gate-as-repair-trigger + iterative-extend retry).

**Fix path (post-ADR-003 + ADR-004 + ADR-007):** The gate should not kill the render; it should trigger iterative-extend on the failing section only. Until those ADRs land, the operator-side workaround is: retry the render from `/app/render/[jobId]`. The outline is non-deterministic; a re-roll usually allocates differently.

---

### Recipe 2: Writeback verification failed — duration too short

**Symptom:** Render fails at the very end. mp4 was successfully produced and uploaded to GCS, but the Cloud Run job exits non-zero. Error mentions "duration X seconds < Y seconds required."

**Stage:** `compose` / writeback verification.

**Where:** `cloud/render-worker-v2/entrypoint.py:~2400` — the 80% duration floor.

**Diagnosis:** The mp4 itself is real (h264+aac, valid streams). The gate fired because total duration is under 80% of the configured target. Root cause is upstream — the rewrite stage produced narration shorter than target, which produced shorter TTS, which produced a shorter mp4.

**Case study — `7743ca76` (2026-05-21 19:48):** mp4 duration 1232s, required 1440s (80% of 1800s target). 59.7 MB h264+aac, real and watchable. Worker exited 1.

**Fix path:** ADR-010 — drop the 80% duration floor; keep only mp4-stream-validity sanity checks. Length policing belongs at the rewrite gate (ADR-004), not at writeback. Until ADR-010 lands, the operator workaround is: download the mp4 from GCS (`gs://ytfactory-prod-v3-artifacts/jobs/<id>/`), verify it's watchable, manually mark the job complete in Firestore, push to YouTube via `/upload-via-playwright`.

---

### Recipe 3: Image-gen failure threshold tripped

**Symptom:** Render fails at the `images` stage with `RenderFailedError` referencing per-beat failure ratio.

**Stage:** `images`.

**Where:** `pipeline/render/ai_beat_slideshow.py:90` — `_PER_BEAT_FAILURE_THRESHOLD = 0.10`. Above 10% of beats failing image-gen, the stage raises.

**Diagnosis:**
1. Provider availability — was `cloudrun_z_image_turbo` healthy at the time? Check `/app/cloud` health section.
2. Cold-start — z-image-turbo runs `min-instances=0` (cost guardrail rule 1). A wave of cold-start timeouts can spike per-beat failures. Mitigation: `cloud/warm_image_services.sh` pre-warms the service before a render.
3. Prompt refiner output — verify `pipeline/images/prompt_refiner.py` produced sensible refined prompts. Per ADR-008 the refiner is still klein-calibrated; on z-turbo it can emit 4-10-word fragments that z-turbo handles poorly. Look for floating-object product photos in the output (MEMORY.md "Z-Image-Turbo verb-led prompts").
4. Character description — if `spec.extra["character_description"]` was missing from `cast.json`, beat prompts have no character spec to prepend; consistency-related rejections cascade.

**Fix path (post-ADR-008 + ADR-009 + ADR-003):** z-turbo-native refiner + verbatim character spec prepend + gate becomes post-retry kill (per-beat retry with stronger prompt, not first-fail kill). Until those land, operator workaround is retry from UI; if it keeps tripping, manually warm z-image-turbo and retry.

---

### Recipe 4: TTS produces gibberish noise (Hindi)

**Symptom:** Render completes end-to-end, but the audio sounds like noise / static / gibberish. Only on HindutavaAnimated channel.

**Stage:** `tts`.

**Where:** `pipeline/tts/cloudrun.py:929` — `_synth_cloudrun_indicf5`. `ref_audio_text` is not actually forwarded to the IndicF5 model (verified Q71).

**Diagnosis:** Inspect the request payload sent to IndicF5. If `ref_audio_text` is missing from the JSON body, the bug is confirmed. The model voice-clones from `ref_audio` alone without the paired text, which is the documented failure mode for IndicF5.

**Fix path:** ADR-016 — targeted one-line wiring fix. Regression test asserting `ref_audio_text` is in the payload.

---

### Recipe 5: Captions missing / boxes for Devanagari

**Symptom:** Render completes, mp4 plays, but no captions overlay the audio. Or captions render as empty boxes (the Devanagari case).

**Stage:** `compose` / overlays.

**Where:** Multi-fix per Q73 / ADR-012:
- `spec.caption_style` may not be wired end-to-end through to the overlay producer + compose mux.
- Caption import failures warn-but-don't-fail today.
- Devanagari fonts are not in the worker Dockerfile.

**Diagnosis:**
1. If captions overlay correctly in English but show boxes for Hindi, it's the Devanagari font miss.
2. If captions are silently absent, check whether `caption_style` made it from the channel YAML through to the overlay producer.
3. Check stderr for "caption import failed" — a current warn-only path.

**Fix path:** ADR-012 — hard-fail (not warn) on caption import failure, install Devanagari fonts in Dockerfile, wire `spec.caption_style` end-to-end, add a post-render caption density gate. Until ADR-012 lands, operator workaround: re-render after manually verifying caption_style in cast.json.

---

### Recipe 6: Render never starts (Cloud Run cold-start / timeout)

**Symptom:** UI shows job enqueued but no progress. Per Q9, this is the #3 reliability pain.

**Stage:** Pre-stage; job didn't reach `entrypoint.py::_main_from_firestore`.

**Where:** `cloud/render-worker-v2/` Cloud Run job. Check `gcloud run jobs executions list` for the job name.

**Diagnosis:**
1. Cloud Run job execution was created but the container never started — check the execution's events tab for image-pull or quota errors.
2. Container started but timed out on weight load — z-image-turbo, chatterbox, indicf5 all need warm-up. The `cloud/warm_*.sh` scripts mitigate this.
3. ADC credential failure — `cloud/_shared/auth_setup.sh` must be sourced; if `deploy.sh` skipped it, the job can't reach Firestore.

**Fix path:** Read the Cloud Run execution logs (the `gcloud logging read` command in the section above, scoped by execution_name). Re-deploy if image-pull failed. Pre-warm services before a render via `cloud/warm_image_services.sh` + `cloud/warm_tts_services.sh`.

---

### Recipe 7: Render "succeeds" but ships a broken mp4 (silent quality failure)

**Symptom:** Job marked complete. mp4 in GCS. But on inspection: wrong captions, frozen frames, missing audio, mismatched character, generic placeholder image.

**Stage:** Any (this is the class).

**Where:** This is the silent-fallback class — MEMORY.md "Silent-fallback unshippable output." It's the #2 reliability pain (Q9).

**Diagnosis:** No single file. The pattern: a stage couldn't produce its real output and silently returned a placeholder (solid-color mp4, empty caption list, default character, etc.) instead of raising. The downstream stages run on the placeholder, the mp4 ships, the gate-that-would-catch-this doesn't exist or is too coarse.

**Fix path:** Per CLAUDE.md "Working principles" — if a stage can't produce real output, **raise**. Don't fall back. The retry-then-raise model from MEMORY.md is locked. The longer-term answer is per-stage quality gates (ADR-012 caption density gate; future per-beat image quality gate per Q66; future ASR alignment confidence gate).

Reproduce by `/critique-video` on the suspected mp4 to surface the specific layer that silently failed.

---

## Diagnosis hierarchy when nothing obvious fires

1. **Read the operator-surface error first.** It's already been through `_extract_last_traceback` and represents the curated diagnostic.
2. **If the error is opaque, hit Cloud Logging** for the full subprocess stream.
3. **If the subprocess looks clean but the job is stuck**, the Cloud Run job execution itself failed — check `gcloud run jobs executions list`.
4. **If the job completed but the mp4 is wrong**, it's silent fallback (recipe 7) — there's no exception, just bad output. Run `/critique-video`.
5. **If a daemon is involved** (cloud-critic, upload-next), check `~/Library/Logs/ytfactory/<daemon>.log` or `/tmp/upload_next.log`.

---

## Anti-patterns when debugging

- **Don't trust `audit_data/`** for live signal — it's pre-fix-era (Q-013).
- **Don't trust README.md** — flagged as stale per the charter; cite YAML + file:line instead.
- **Don't `gcloud run jobs update` env vars on render-worker-v2** — they'll be wiped on next deploy (ADR-022 / MEMORY.md "Render-worker env truth"). Edit `cloud/render-worker-v2/deploy.sh:90` and redeploy.
- **Don't add silent fallbacks** to fix a flaky stage — raise instead. The downstream gate exists to catch silent failures only because earlier silent fallbacks shipped unshippable mp4s.
