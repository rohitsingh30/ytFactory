# Image generation — research findings 2026-05-07

> **Status:** research complete. **Top recommendation:** deploy
> **`black-forest-labs/FLUX.2-klein-4B`** as the new cloud default,
> with **`Tongyi-MAI/Z-Image-Turbo`** as a secondary cloud service
> (parity-with-current-local + risk-mitigation lane). Both Apache 2.0,
> both diffusers-native, both fit comfortably on Cloud Run NVIDIA L4
> 24 GB with our existing GCS-Fuse persistent-weights infra.

This research was commissioned to find a FREE, fast, high-quality
text-to-image model to **replace `z_image_turbo` (mflux/MLX local)**
as the default for every channel and run on our existing Cloud Run
infra (`asia-southeast1`, NVIDIA L4, `gs://ytfactory-model-weights-v2`
bucket mounted at `/models/hf` via GCS Fuse — same pattern as the 6
TTS services).

The top 2 picks below are deployable on that stack today. The
disqualified models are listed at the bottom for reference (so the
next person doesn't re-investigate).

---

## Our use case (the constraint set)

A model has to satisfy ALL of these to be a viable default:

1. **License — Apache 2.0 / MIT** (commercial, redistributable,
   self-host, fine-tune). Anything with "non-commercial",
   "research-only", "<100M MAU", or "no training other models" is
   disqualified.
2. **Diffusers-native pipeline** so the cloud service is a thin
   FastAPI wrapper, no custom inference fork.
3. **Fits in 24 GB VRAM at bf16** on a single NVIDIA L4 (Cloud Run
   GPU = L4-only as of 2026-05). bf16 not int4/fp8 — quantization
   can come later for cost.
4. **≤8 sampling steps for end-to-end inference under ~5 s warm**
   on L4. Each Short renders ~30 images on the critical path; we
   cannot afford ~30 s/image like Qwen Image's 20B native path.
5. **Multi-aspect** (9:16 vertical Shorts + 16:9 long-form) without
   degradation.
6. **Style versatility.** We need the same model to handle: AITA
   sketch/cartoon, kids' nursery rhyme illustration, Hindu
   devotional, period history, photoreal cosmos/space, and sports
   cartoon faces. No model excels at all of these — we pick the one
   with the widest competent range.
7. **Weights ≤ ~30 GB** so GCS-Fuse cold-load over the bucket
   stays under ~2 minutes. Cold-start hit = first /readyz; we hide
   that by warming `/readyz` from the laptop before the render
   begins (same trick TTS uses).

Not required (but nice-to-have):
- **In-image text rendering.** None of our channels need legible
  text in frame; captions are baked separately by ffmpeg.
- **Negative prompt obedience.** Both top picks are
  guidance-distilled (`guidance_scale=0.0`–`1.0`), so negative
  prompts are ignored — we already use the `force_positive` trick.
- **Image-to-image / edit support.** Free upgrade if available
  (FLUX.2 klein has it, Z-Image-Turbo doesn't).

---

## TL;DR — top 2 to deploy, in order

### 🥇 1. **FLUX.2 [klein] 4B** — `black-forest-labs/FLUX.2-klein-4B`

- **License:** Apache 2.0 ✅ (released Jan 2026).
- **Architecture:** 4 B rectified flow transformer, step-distilled to
  4 inference steps. Uses 8 B Qwen3 text embedder under the hood.
- **Capabilities in one model:** text-to-image **AND** image-to-image
  single-reference editing **AND** multi-reference generation. (Bonus
  unlock: rhymetimejunction's recurring mascots Laddu/Jalebi/Tuk-Tuk/
  Dadi could finally lock with multi-ref editing — today they drift
  every frame because we have no character-lock on the mflux path.)
- **Speed on L4 bf16:** **~0.5–0.8 s per 1024×1024 warm image**
  (4 steps, BFL-published benchmark + Pruna optimization tutorial).
  ~5–10× faster than current Z-Image-Turbo mflux path on M2 Max,
  ~2–4× faster than Z-Image-Turbo bf16 on the same L4.
- **VRAM:** ~13 GB at bf16 (consumer RTX 3090/4070-class headroom on
  our 24 GB L4).
- **Weights size:** ~9 GB on HF (4 B params @ bf16 + Qwen3 text
  embedder shards + scheduler + tokenizer).
- **Cold-start over GCS-Fuse:** estimated ~30–60 s for the first
  `/readyz` (smaller than Z-Image-Turbo so likely faster).
- **Diffusers integration:**
  ```python
  from diffusers import Flux2KleinPipeline
  pipe = Flux2KleinPipeline.from_pretrained(
      "black-forest-labs/FLUX.2-klein-4B",
      torch_dtype=torch.bfloat16,
      local_files_only=True,
  ).to("cuda")
  image = pipe(
      prompt=...,
      height=1024, width=1024,
      guidance_scale=1.0,
      num_inference_steps=4,
      generator=torch.Generator("cuda").manual_seed(seed),
  ).images[0]
  ```
  (Requires diffusers from main / ≥0.36 — not in any released pin
  yet; cloud service installs from git like cloud/image-qwen does.)
- **Style range — caveats:**
  - **Photoreal:** Excellent (BFL pedigree).
  - **Stylized cartoon:** Strong on benchmark blogs (Apatero claims
    88 % win-rate vs Z-Image's 30 % on cartoon prompts; that
    benchmark is FLUX-leaning so treat as upper bound, not ground
    truth — Z-Image still produces production-quality stylized
    output for us today).
  - **Devotional / mythological / period:** Untested for our
    channels. **This is the canary risk.**
- **Why first:** sub-second inference is a step-change for our 30-
  images-per-Short pipeline (potential 5-7 min of wall-clock saved
  per Short). Multi-reference editing unlocks character-lock for
  rhymetimejunction. Apache 2.0 4 B variant is the *only* truly
  open variant in the FLUX.2 family — 9 B [klein] and the [dev]
  family are non-commercial.

### 🥈 2. **Z-Image-Turbo 6B** — `Tongyi-MAI/Z-Image-Turbo`

- **License:** Apache 2.0 ✅ (released late Nov 2025).
- **Architecture:** 6 B Single-Stream Diffusion Transformer (S3-DiT),
  step-distilled to 8 NFE. RL-tuned for aesthetic quality (the only
  one of the two with RL in the post-training stack — see Tongyi-MAI
  model zoo table).
- **Speed on L4 bf16:** **~2–3 s per 1024×1024 warm image** (8 steps).
  Q4 quantized variant goes to ~0.25–0.3 s but we'd ship bf16 to
  match parity with current local mflux.
- **VRAM:** ~12 GB at bf16. Q4 fits in 6 GB.
- **Weights size:** ~13 GB at bf16 on HF.
- **Cold-start over GCS-Fuse:** estimated ~60–120 s for the first
  `/readyz`.
- **Diffusers integration:**
  ```python
  from diffusers import ZImagePipeline  # diffusers ≥0.34 with merged Z-Image PRs
  pipe = ZImagePipeline.from_pretrained(
      "Tongyi-MAI/Z-Image-Turbo",
      torch_dtype=torch.bfloat16,
      low_cpu_mem_usage=False,
      local_files_only=True,
  ).to("cuda")
  image = pipe(
      prompt=..., height=1024, width=1024,
      guidance_scale=0.0,            # CFG-distilled — ignore guidance
      num_inference_steps=9,          # 8 DiT forwards = 9 steps
      generator=torch.Generator("cuda").manual_seed(seed),
  ).images[0]
  ```
- **Style range:** This is what we currently ship via mflux. Proven
  to handle every channel's style (AITA cartoon, kids, Hindu
  devotional, history, cosmos photoreal, sports). The
  `force_positive` token trick we developed for `z_image_turbo`
  (DESIGN.md §13) **transfers byte-identically** to the cloud
  service (same model checkpoint, just diffusers vs MLX runtime).
  Small per-pixel diff but no character/style drift.
- **Why second:** parity with what we ship today, lowest-risk
  rollout. If FLUX.2 klein 4 B's style range turns out to be a
  regression on devotional / sports / kids in canary, we can flip
  the affected channels to `cloudrun_z_image_turbo` instead of
  forcing a model swap on every channel. Also covers us if FLUX.2
  klein hits a license-clarification snafu (Apache 2.0 declared
  but the family is new).

---

## Why these two and not anything else

### Hard-disqualified (license)

| Model | License | Why out |
|---|---|---|
| **FLUX.2 [dev]** | FLUX Non-Commercial License | Top open-weights Elo (1161) but non-commercial — **blocks our use**. |
| **FLUX.2 [dev] Turbo** | FLUX NCL | Same — Elo 1165 but commercial-use forbidden. |
| **FLUX.2 [klein] 9B** | FLUX NCL | The bigger klein variant is *not* Apache 2.0; only the 4 B variants (klein 4 B + klein Base 4 B) are. |
| **HunyuanImage 3.0** (Tencent, 80 B MoE) | Tencent Hunyuan Community License | 13 B active params, strong arena scores BUT: <100 M MAU clause, EU/UK/South Korea exclusions, "no using outputs to train other models" clause. **Not Apache/MIT — disqualified per our gate #1.** Also 160 GB checkpoint blows our weights bucket budget. |
| **Wan 2.2 Image** (Alibaba) | Restrictive (research/non-commercial flavour) | Open-weights but commercial deployment requires Alibaba Cloud agreement — out. |
| **Stable Diffusion 3.5 Large** | Stability AI Community License | Free <$1 M revenue but technically not Apache/MIT and adds compliance overhead. Also 8 B params, slower than Z-Image-Turbo for similar Elo. |
| **SDXL family** | OpenRAIL-M | Permissive but the model is two generations behind Z-Image / FLUX.2 on quality. We already keep `sdxl_lightning` as a legacy fallback. |

### Hard-disqualified (cost on our use case)

| Model | License | Why out |
|---|---|---|
| **Qwen Image Max 2512** | Apache 2.0 | **Top open-weights Elo (1160)** AND Apache — passes license gate. But: 20 B params, **~25–30 s per image** on RTX 4060 Ti / similar L4-class hardware. We render ~30 images per Short → **~12-15 min just for image gen** per Short. Disqualified by gate #4 (≤5 s warm). Reconsider IF a step-distilled "Qwen-Image Turbo" lands. |
| **HiDream-I1 Full 17 B** | MIT | Apache-equivalent permissive ✅. But: requires Llama-3.1-8B as text encoder (gated on HF — needs token gymnastics at build time, see existing `cloud/image-hidream/Dockerfile` ARG hassle). 33 GB total weights footprint. Mid-pack quality. Not worth the operational cost for our workload. |
| **HunyuanImage 3.0 distilled** | Tencent CL | 8-step distilled variant exists but license still disqualifies. |

### Soft-disqualified (less compelling than top 2)

| Model | License | Why not picked |
|---|---|---|
| **SANA 1.5 (NVlabs)** | Apache 2.0 | 0.6 B / 1.6 B / 4.8 B variants, very VRAM-efficient (~10–14 GB), sub-second on L4. Linear DiT + Gemma text encoder + 32× compressed autoencoder. Fastest of the bunch. But quality benchmarks place it ~Z-Image tier on photoreal and **below** on stylized output. If we need a *third* lane (e.g. for ultra-fast preview rendering on the dashboard), SANA is the obvious pick. |
| **FLUX.1 [schnell]** | Apache 2.0 | Original FLUX schnell. Superseded by FLUX.2 [klein] 4 B on every axis (faster, better quality, smaller, supports edit). Keep on the mental shortlist only as a known-stable fallback if FLUX.2 klein has a launch-quality issue. |
| **PixArt-Σ / PixArt-α** | Apache 2.0 | Lightweight (0.6 B), Apache. Quality is dated — superseded by SANA on speed/efficiency and FLUX.2 klein on quality. |
| **Kolors** (Kuaishou) | Apache 2.0 | OK quality. No 2026 update, no distilled variant. Slower than Z-Image. |
| **HunyuanDiT** (Tencent) | Apache 2.0 | Original Hunyuan DiT, predates HunyuanImage 3.0. Mid-pack quality, slower than Z-Image. No reason to pick. |
| **Lumina-Image-2.0** | Apache 2.0 | 2 B, fast, decent quality. No edit support. Smaller community / less battle-tested than Z-Image or FLUX.2 klein. |

### Closed-source (excluded by gate #1 but for reference — these dominate the absolute leaderboard)

OpenAI **GPT Image 2 (high)** Elo 1338, OpenAI **GPT Image 1.5 (high)** 1272, Google **Nano Banana 2 / Gemini 3.1 Flash Image** 1261, Google **Nano Banana Pro / Gemini 3 Pro Image** 1219, ByteDance **Seedream 4.0** 1201. All API-only, paid per-image, no weights, no self-host. We considered cost/quality vs an open self-host: **at our render volume the GCP credit budget can't sustain paid-API image gen**, and the open-weights gap to GPT Image 2 is ~170 Elo points which is notable but not catastrophic for our use case (we're not selling images; we're rendering background visuals for narrative Shorts).

---

## Source data

**Artificial Analysis Image Arena leaderboard (blind-vote Elo, 2026-05-07):**
- Top open-weights overall: FLUX.2 [dev] Turbo 1165, FLUX.2 [dev] 1161, Qwen Image Max 2512 1160.
- Of these, **only Qwen Image Max 2512 is Apache 2.0** — and it's
  too slow for our throughput.
- FLUX.2 [klein] 4 B is not yet posted with a public Elo on the
  arena; BFL claims it "matches or exceeds models 5× its size"
  (i.e. ~Qwen-Image Max territory at sub-second latency). Treat
  as ~1100–1150 Elo until verified in canary.
- Z-Image-Turbo: ~1080–1100 Elo from third-party reviews
  (CodeSOTA, MagicHour). What we ship today.

**Speed claims on L4 24 GB bf16** (compiled from BFL blog,
Pruna tutorial, Qubrid benchmarks, Tongyi model card):

| Model | Warm 1024² inference | Cold load over GCS Fuse |
|---|---|---|
| FLUX.2 [klein] 4 B (4 steps) | **~0.5–0.8 s** | ~30–60 s |
| Z-Image-Turbo (8 steps, bf16) | ~2–3 s | ~60–120 s |
| Z-Image-Turbo (8 steps, Q4) | ~0.25–0.3 s | same |
| SANA 1.5 0.6 B | ~0.8–1.0 s | ~20–30 s |
| Qwen Image Max 2512 (50 steps) | ~25–30 s | ~120–180 s |
| HiDream-I1 Full | ~15–30 s | ~120–180 s |
| FLUX.1 schnell (4 steps) | ~3–5 s | ~60–120 s |

(All numbers warm-state. Cold first-image = warm + cold-load. We
hide the cold-load behind `/readyz` warmup from the laptop.)

---

## Canary results — 2026-05-07

### `mystoriesanimated/variants/aita_text.yaml` — `cloudrun_flux2_klein` ✅ PASS

End-to-end render of `aita-birth-pool` on the new cloud path:

| Metric | Result |
|---|---|
| FLUX cold-load | 5-7 min (varies; one-time tax per warm container) |
| Warm /generate (4-step, 768×1344 vertical) | **3.86s server-side / ~5s e2e** |
| Stage 3 total (7 images) | **33.4 s** |
| Stage 4 ffmpeg compose | 31.8 s |
| Final mp4 | 13.9s @ 1080×1920 30fps, 2.5 MB |
| Visual style fidelity vs prior local mflux | **indistinguishable** — same channel aesthetic, same character continuity, same composition quality |

Sample artifacts saved to `data/research/cloud_image/canary4/`
(mp4 + 3 representative beat PNGs).

**Speedup vs local mflux:** ~3-4× per image (3.86s vs ~12-15s).

**Production gotchas surfaced + fixed during canary:**

1. **`urllib.request.urlopen` doesn't notice TCP RST during long
   waits** — Cloud Run LB severs idle connections after ~5 min;
   urllib's blocking read sat forever even when server returned
   200 OK. Switched to `requests.Session()` (one per call, no
   connection pool reuse, retry-once on connection-reset).

2. **`min-instances=0` made the canary unreliable** — Cloud Run
   scaled to zero between requests; every render paid the 5-7 min
   cold-load. Bumped FLUX.2 klein to **`--min-instances=1`** so one
   container stays warm (~$80/mo at L4 24/7). Persisted in
   `cloud/image-flux2-klein/deploy.sh`.

3. **`CLOUDRUN_IMAGE_TIMEOUT` default too tight** — was 600s,
   exactly the cold-load envelope. Bumped default to 900s.

4. **Warmup wired but Cloud Run cycled containers anyway** — the
   `images.warmup("cloudrun_flux2_klein")` hook fires `/readyz` on
   a background thread at the top of `make_short`. With min=1 it's
   essentially a no-op (instance already warm), but on cold deploys
   it correctly hides the 5-7 min load.

---

## Recommended rollout

### Phase 1 — deploy FLUX.2 [klein] 4 B as `cloudrun_flux2_klein` cloud service

Same Cloud Run + GCS-Fuse pattern as TTS:

1. Stage `black-forest-labs/FLUX.2-klein-4B` to
   `gs://ytfactory-model-weights-v2/hub/models--black-forest-labs--FLUX.2-klein-4B/`
   via the existing `cloud/weights-staging/stage.py` job.
2. **(BLOCKING)** validate that
   `Flux2KleinPipeline.from_pretrained(..., local_files_only=True)`
   loads from the staged bucket — if `_walk_files` symlink-skipping
   breaks the diffusers cache layout, fix the staging job before
   building the service. (See plan.md Phase 0 for the validation
   recipe.)
3. New `cloud/image-flux2-klein/` service: nvidia/cuda:12.4
   runtime, diffusers from main, 4-step inference, `guidance_scale=1.0`,
   server signature mirrors `cloud/image-qwen/server.py` (inline
   PNG <5 MB else GCS upload, `/readyz` lazy-load + warm-time
   metric). Deploy script mirrors `cloud/tts-chatterbox/deploy.sh`
   (weights bucket mount, `--cpu=8 --cpu-boost --memory=24Gi
   --concurrency=1 --max-instances=2 --min-instances=0`).
4. New `pipeline/images_cloudrun.py` client with **render-level
   circuit breaker** (one failure → all subsequent images for the
   rest of the render go local mflux; reset hook fired at the top
   of every render entrypoint).
5. Wire `cloudrun_flux2_klein` provider into `pipeline/images.py`
   dispatcher.
6. Canary on `mystoriesanimated/variants/aita_text.yaml` with
   `CLOUDRUN_IMAGE_DISABLE_FALLBACK=1`. Compare visual style vs
   prior local-mflux render of same story.

### Phase 2 — deploy Z-Image-Turbo as `cloudrun_z_image_turbo` parallel service

Same scaffolding. Reuses the shared `pipeline/cloudrun_auth.py`
(extracted from the current `pipeline/tts/cloudrun.py` global token
cache — needs to become audience-keyed before the second image
service comes online; see plan.md Phase 2 for the auth refactor).

This second service is **insurance + parity**: any channel where
FLUX.2 klein style turns out to be a regression in canary can flip
to `cloudrun_z_image_turbo` instead of being forced back to local
mflux.

### Phase 3 — per-channel canary, then bulk YAML flip

Canary one channel-variant per major style family with fallback
disabled, pick the per-channel default among `cloudrun_flux2_klein`
/ `cloudrun_z_image_turbo` / `z_image_turbo` (local), bulk flip
the remaining YAMLs once we have a verdict.

### Out of scope for this iteration

- SANA 1.5 as a third lane (only worth it if we add an ultra-fast
  preview rendering path on the dashboard).
- Quantized FP8 / NVFP4 variants of FLUX.2 klein (Apache 2.0,
  same family, claimed 1.6×–2.7× faster — defer until bf16
  baseline is stable so quant artifacts can be measured against
  a clean reference).
- Image-to-image / multi-reference editing capabilities of FLUX.2
  klein — exposing them through the pipeline (channel mascot
  lock for rhymetimejunction; reference-image conditioning for
  AITA character continuity) is a follow-up after the T2I path
  is shipped and stable.
- Migrating `mflux` Flux Schnell (only used by
  `mystoriesanimated/variants/tifu.yaml`) to cloud — low priority,
  legacy fallback only.

---

## Confidence + caveats

- Z-Image-Turbo claims are high-confidence (we already run it
  daily, model card + Tongyi benchmarks + L4 third-party
  benchmarks all agree).
- FLUX.2 klein 4 B claims are **medium confidence** — model is 4
  months old, BFL-published benchmarks may be best-case, no
  extensive third-party blind-vote data yet. The Phase-1 canary
  is the actual decision point, not this document.
- All "win-rate" comparisons in marketing blog posts (Apatero,
  pxz.ai, sequencer.media) are unreliable — vendors run benchmarks
  that favour their preferred model. We rely on Artificial
  Analysis Elo (blind-vote, neutral) for overall quality and our
  own canary for use-case fit.
- "Free" here means open weights — Cloud Run GPU L4 still costs
  money. Estimated $20-40/mo at our current Short volume vs
  current ~$6/mo TTS spend.
