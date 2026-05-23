# Performance Concerns

Hot paths and cost levers, with where to look in code. Latency is
explicitly deferred per onboarding-qa Q24 ("we can look into latency
later") — the priority is reliability — but the cost levers are
still active because every render today burns L4 GPU time, Azure
OpenAI tokens, and GCS egress, succeeded or failed.

Sources: code reads on 2026-05-22. The 5 cost rules in `CLAUDE.md`
under "Cost guardrails" are the canonical list; this doc verifies
each against current `deploy.sh` files.

---

## 1. Cost-rule audit (verifies CLAUDE.md guardrails 1-5)

### Rule 1 — `min-instances=0` on every GPU service

| Service | min-instances | Verified |
|---|---|---|
| `cloud/asr-whisper/deploy.sh:61` | 0 | OK |
| `cloud/image-z-image-turbo/deploy.sh:86` | 0 | OK |
| `cloud/tts-chatterbox/deploy.sh:57` | 0 | OK |
| `cloud/tts-indicf5/deploy.sh:49` | 0 | OK |

CPU services intentionally diverge:
- `cloud/web-server/deploy.sh:102` → `min-instances=1` (web traffic
  warm-start; not a GPU service).
- `cloud/web-next/deploy.sh:119` → `min-instances=1` (same rationale).

All GPU services honor Rule 1. **PASS.**

### Rule 2 — `max-instances=1` on every GPU service

| Service | max-instances | Verified |
|---|---|---|
| `cloud/asr-whisper/deploy.sh:60` | 1 | OK |
| `cloud/image-z-image-turbo/deploy.sh:85` | 1 | OK |
| `cloud/tts-chatterbox/deploy.sh:56` | 1 | OK |
| `cloud/tts-indicf5/deploy.sh:48` | 1 | OK |

Both tts-chatterbox and tts-indicf5 deploy.sh comments confirm "was
2 pre-2026-05-17 cost audit" — the audit landed and is preserved.

CPU services higher (correctly):
- `cobalt-api/deploy.sh:47` → max-instances=4.
- `editing-agent/deploy.sh:49` → max-instances=4.
- `clone-video-worker/deploy.sh:135` → max-instances=4.

**PASS.**

### Rule 3 — `--region=asia-southeast1` on every `gcloud builds submit`

All deploy.sh files use `REGION="${GCP_REGION:-asia-southeast1}"`
and pass `--region="${REGION}"` to `gcloud builds submit` AND to
`gcloud run deploy` / `gcloud run jobs deploy`. Confirmed at:
- `cloud/render-worker-v2/deploy.sh:21, 46, 99`
- `cloud/asr-whisper/deploy.sh:20, 41, 50`
- `cloud/image-z-image-turbo/deploy.sh:22, 41, 75`
- `cloud/tts-chatterbox/deploy.sh:21, 32, 46`
- `cloud/tts-indicf5/deploy.sh:19, 29, 39`

**PASS.**

### Rule 4 — Double-checked `threading.Lock()` around `_model()`/`_pipe()`

| Service | Lock site | Pattern |
|---|---|---|
| `cloud/image-z-image-turbo/server.py:73` | `_PIPE_LOCK = threading.Lock()` | `_pipe()` at line 77 |
| `cloud/tts-chatterbox/server.py:225` | `_MODEL_LOCK = threading.Lock()` | comment at line 235 explicitly cites the 22 GiB OOM scenario |
| `cloud/asr-whisper/server.py:87` | `_MODEL_LOCK = threading.Lock()` | OK |
| `cloud/tts-indicf5/server.py:74` | `_INDICF5_LOCK = threading.Lock()` | OK |

All 4 GPU services hold the lock. **PASS.**

### Rule 5 — Don't bake huge weights into Docker images

- `cloud/image-z-image-turbo/deploy.sh:69` comment confirms
  "min-instances=0 since weights load fast from local SSD now" — the
  exception case the rule names ("z-image-turbo is the exception
  justified by gcsfuse cold-load latency").
- No other service appears to bake weights into the image; the
  Dockerfiles (sampled `cloud/render-worker-v2/Dockerfile` and
  per-service ones) do not COPY large weight blobs.

**PASS.**

---

## 2. GPU cold-start cost

**Pattern:** All GPU services have `min-instances=0`. Every render
triggers cold-start on whichever GPU service hadn't been called
recently. Cold-start cost on L4 nvidia-l4 with the lock pattern is
~30-90s per service (load weights from `local-ssd` for z-image-turbo;
load from gcsfuse mount for the TTS services).

**Per-render impact:**
- One short render hits chatterbox once (chunked TTS) + image-z-image-turbo
  multiple times + asr-whisper once. If all three are cold → ~3 × ~45s =
  ~135s of cold-start.
- One 30-min long-form hits the same set but TTS is chunked — 5+
  chunks. Per onboarding-qa Q47, `tts_chunked` synthesizes ~380-char
  chunks joined with 0.4s silence. If chatterbox is cold for the
  first chunk only (warm for subsequent), the cost is ~45s
  amortised over 30 min — small. But each separate render starts
  from cold again.

**Hot path:** Cold-start dominates the first chunk's latency. Warm
endpoints (`POST /api/cloud/warm` per the `cloud/warm_tts_services.sh`
+ `cloud/warm_image_services.sh` scripts) exist; trigger from the
control plane on render-job-create to pre-warm before stage execution
arrives.

**Lever:** `cloud/warm_tts_services.sh` and
`cloud/warm_image_services.sh` are the existing warming utilities;
verify they're invoked at the right point in the queue lifecycle
(when a job moves from `queued` → `running`).

---

## 3. LLM token spend by stage

Source of truth: `pipeline/llm/cli.py:186-244`
(`_DEFAULT_MAX_TOKENS_BY_STAGE`):

| Stage | max_tokens | Notes (with code line) |
|---|---|---|
| `rewrite_long_form` | 64000 | line 220 — bumped 32k → 64k on 2026-05-13 |
| `rewrite` (short) | 8192 | line 225 |
| `cast` | 8192 | line 226 |
| `prompts` | 8192 | line 227 |
| `critic` | 8192 | line 228 |
| `audio_critic` | 8192 | line 229 |
| `imitate_analyze` | 8192 | line 230 |
| `imitate_apply` | 8192 | line 231 |
| `prompt_refine` | 8192 | line 238 — Haiku-tier, batched across beats |
| (fallback) | 4096 | line 244 |

**Per-render LLM spend at gpt-5.3-chat (Azure):**
- Short render: rewrite (~6k tokens) + cast (~3k) + prompts (~5k)
  + prompt_refine (~4k) ≈ ~18k output tokens.
- Long-form render: rewrite_long_form outline (~5k) + N section
  bodies (~6k each × 10 = ~60k) + cast (~3k) + prompts (~10k) +
  prompt_refine (~6k) ≈ ~84k output tokens — and that's just OUTPUT.
  Reasoning-token usage is INVISIBLE but billed. Per `cli.py:288`:
  high effort = 15-20k reasoning per call, medium = 5-8k, low = 1-3k,
  minimal = 0-200.

**Critical knob:** `_DEFAULT_REASONING_EFFORT_BY_STAGE` at
`pipeline/llm/cli.py:304-320` — currently:
- `rewrite_long_form`: minimal (line 318)
- `critic`: medium (line 319)
- everything else: minimal (fallback at line 321)

Per onboarding-qa O4 (improvement-opportunities), the minimal setting
for rewrite_long_form is the root cause of length-instruction-following
failures. Flipping back to medium DOUBLES the reasoning-token spend
per section body — at 10 sections per long-form, that's an extra
50-80k reasoning tokens per render. Cost trade is correct for MVP
(quality > spend) but should be re-evaluated once shipping reliably.

**Lever:** Per-stage env override `YTFACTORY_REASONING_EFFORT_<STAGE>`
(line 332) lets the operator pin without redeploy.

---

## 4. Z-Image-Turbo image-gen latency

**Path:** `cloud/image-z-image-turbo/server.py` (the cloudrun service);
called from `pipeline/render/visualize/ai_beat_slideshow.py` (per-beat)
and `pipeline/render/visualize/longform_panels.py` (per-panel).

**Latency profile (per onboarding-qa Q67 research):**
- Z-Image-Turbo 6B S3-DiT, CFG-distilled.
- 1024×1024 at 8-12 steps → ~3-6s per image on L4 (estimate;
  measure under production load).
- Concurrency = 1 on the deploy (line 84 of
  `image-z-image-turbo/deploy.sh`), max-instances = 1 (line 85).
  So per-render the image-gen workload is fully serialised — one
  image at a time.

**Per-render impact:**
- Short with 30 beats → 30 × ~4s = ~120s purely in image-gen
  (assuming warm). Plus cold-start (~30-60s) if first call.
- Long-form with 60 panels → 60 × ~4s = ~240s.

**Lever:** Steps are configurable via `spec.extra["image_steps"]`
(default 4 per `longform_panels.py:94`). Drop steps → faster +
lower quality. Z-Image-Turbo is CFG-distilled so step count below
4 degrades fast; above 12 has diminishing return.

**Lever 2:** Onboarding-qa improvement O18 (batch-and-select for
character consistency) would MULTIPLY image-gen calls per beat by
~32x. Major spend implication; needs cost vs quality A/B before
landing.

---

## 5. TTS chunked long-form

**Path:** `pipeline/audio/` facade → `pipeline.tts.cloudrun` →
chatterbox (or indicf5 for Hindi). Long-form uses `tts_chunked`
(onboarding-qa Q47): ~380-char chunks joined with 0.4s silence.

**Per-render impact:**
- 30-min long-form ≈ 4500 narration words ≈ ~25000 chars ≈ ~65 chunks
  if `tts_chunked` keeps the ~380-char target.
- Each chunk is a fresh HTTP call to the cloudrun TTS service. If
  the service is warm, each call is ~RTF (real-time factor) × text
  duration. Chatterbox RTF varies; assume 0.3 × text duration as a
  rough budget = ~9 min of TTS time for 30 min of audio.
- With concurrency=2 (deploy.sh:55 of tts-chatterbox), two chunks can
  be in-flight per instance — but max-instances=1, so the parallelism
  cap is 2 across the whole render.

**Cold-start risk:** Each fresh render starts cold (min-instances=0).
First chunk eats the cold-start cost; subsequent ride warm.

**Lever:** None at the cost side without breaking Rule 2. The right
lever is `tts_chunked` quality (chunk boundary placement, silence
duration) — but no current concern flagged there.

---

## 6. ASR-whisper

**Path:** `cloud/asr-whisper/server.py`. Used post-TTS to align word
timings for caption overlays.

**Per-render impact:**
- One call per render. Whisper RTF ≈ 0.1-0.2 on L4 (faster-whisper).
- 30 min of audio → ~3-6 min of ASR time.
- Cold-start same shape as other GPU services.

**No active concern.** ASR isn't on the critical-path bottleneck.

---

## 7. Render-worker-v2 Cloud Run JOB resources

**Where:** `cloud/render-worker-v2/deploy.sh:102` —
`--memory=8Gi --cpu=4 --task-timeout=3600 --max-retries=0`.

**Per-render impact:**
- 8GB RAM, 4 vCPU, 60-min cap. The worker is CPU-bound only on
  ffmpeg compose (the GPU work delegates to cloudrun services).
- `--max-retries=0` means a failed task is GIVE-UP, not auto-retry.
  Correct for the current model (the Firestore queue layer handles
  user-driven retry); but combined with the coarse-grain whole-render
  gates means one failed gate burns 30+ min of cloudrun GPU spend
  with no recovery.

**Lever:** Granular-retry framework (improvement-opportunities O22)
would address this — instead of `--max-retries=0` at the JOB level,
have the JOB itself retry its FAILING PIECE without restarting the
whole render.

---

## 8. Long-form rewrite parallel fan-out — token spend amplification

**Where:** `pipeline/llm/rewrite_long_form.py:1109`
(`ThreadPoolExecutor(max_workers=5)`). Each section body call is
one independent LLM call with full `max_tokens=64000` budget
available (though typical use is far less).

**Per-render impact:**
- 10 sections × 1 attempt = 10 LLM calls in flight up to 5 at a time.
- Per-call cost: ~6k output tokens × $0.075/1k (gpt-5.3 output) ≈
  $0.45/section × 10 = $4.50 just on section bodies. Plus reasoning
  tokens (invisible but billed).
- Retries (`_SECTION_BODY_MAX_RETRIES=2` at line 1014) DOUBLE the
  worst-case spend if 2 sections retry once each.

**Lever:** None obvious without compromising parallelism. The
improvement-opportunities O6 (iterative-extend retry) reuses the
prior draft so the retry call is SHORTER (only the missing words to
add), reducing per-retry cost.

---

## 9. Hot paths to monitor

Even without active latency optimisation:

1. **Image-gen serial path** — 60 panels × 4s = 4 minutes of pure
   image-gen on long-form. If any cloud incident spikes this to 10s,
   the render goes from 4 min to 10 min image-gen alone.
2. **TTS chunk roundtrip** — 65 chunks × HTTP overhead. If concurrency
   drops to 1 (rare cold), each chunk is fully sequential.
3. **Outline LLM call** — single call, blocking; if Azure has a slow
   minute, the whole fan-out delays by that amount.
4. **ASR-whisper alignment** — single call, blocking; runs AFTER TTS
   finishes. Cold-start of asr-whisper isn't amortised against
   anything.

---

## 10. Cost-per-render rough estimate (back-of-envelope)

For a 30-min long-form on the current pipeline:

| Stage | Estimated cost | Notes |
|---|---|---|
| Azure OpenAI (LLM) | ~$5-8 | 84k output + reasoning |
| Z-Image-Turbo (60 panels) | ~$0.50-1 | L4 × 4 min |
| Chatterbox TTS (chunked) | ~$0.30 | L4 × 9 min × duty-cycle |
| ASR-whisper | ~$0.20 | L4 × 6 min |
| render-worker-v2 JOB | ~$0.50 | 8GB CPU × 60 min |
| GCS egress + storage | ~$0.10 | mp4 upload + artifacts |
| **Total** | **~$7-10** | Per successful render |

For SHORTS (50-60s): ~$0.50-1 total. The cost concentration is
long-form, specifically the LLM stage.

**Failed renders burn the full cost up to the failing stage.** Per
the documented `a734babb` failure (killed at rewrite stage) — the
LLM spend was incurred (~$5) for zero usable output. Per `7743ca76`
— full $7-10 burned for an mp4 that exited the worker as "failed."

**Lever:** Granular-retry (O22) + tighter early gates (O2/O3) so
failure is detected at outline (cheap) not at section-fan-out
(expensive).

---

## Summary

The five cost guardrails are honoured in the current `deploy.sh`
files. Image-gen serial path is the single largest within-render
latency component. LLM tokens are the single largest within-render
cost component, and the cost of FAILED renders is significant
because gates fire late and JOB has `--max-retries=0`. Per
onboarding-qa Q24 latency is deferred but cost is implicit — every
day the MVP doesn't ship cleanly, the daily spend continues with
zero shippable output.
