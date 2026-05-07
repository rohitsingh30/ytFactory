# Pipeline latency + execution-location map (2026-05-07)

> Living reference for **where each pipeline stage runs**, **how long
> it takes**, and **what each channel's end-to-end render looks like**
> after the cloud TTS + cloud image gen migrations of 2026-05-06 and
> 2026-05-07.

This doc replaces casual "render takes ~X minutes" memory with
honest, measured numbers per stage. If you're tuning parallelism,
costing a new channel, or debugging a slow render, start here.

---

## 1. The three execution surfaces

ytFactory work happens in three places:

| Surface | What runs there | Hardware | Lifecycle |
|---|---|---|---|
| **Laptop** (Apple Silicon M2 Max, 32 GB unified memory) | Whisper transcription, ffmpeg compose, PIL caption pre-render, YouTube upload, Playwright cross-engagement, render orchestrator (`pipeline.render.shorts.make_short`), local mflux/sdxl/kokoro fallbacks | 1× Apple GPU (Metal/MLX), 12 perf + 4 efficiency cores | Always-on while user is active; no cron |
| **Cloud Run** (`asia-southeast1`, NVIDIA L4 GPU, scale-to-zero) | 6 TTS services (`ytfactory-tts-{chatterbox,f5,higgs,cosyvoice,indicparler,indicf5}`), 1 production image service (`ytfactory-image-flux2-klein`), 1 standby image service (`ytfactory-image-z-image-turbo`), 1 weights-staging Job | 1× L4 (24 GB VRAM), 8 vCPU, 24-32 GiB RAM per service | `--min-instances=0` for all (idle ≈ $0); cold-load tax 2-7 min hidden via pre-warm script |
| **External APIs / SaaS** | Azure OpenAI (LLM script + prompt authoring), Anthropic Claude (`claude` CLI for prose-heavy authoring + cast), Reddit/Wikipedia/YouTube source pulls, YouTube Data API (upload + analytics), SunoAPI (rhymetimejunction songs), GCS bucket reads/writes | n/a | Per-call billing or per-token |

Plus **Cloud Run control plane** (`web/server.py` running at
`https://ytfactory-control-...run.app`) which hosts the dashboard
and queues render leases for the laptop agent — but it's not on the
render hot path, so latency-irrelevant for this doc.

---

## 2. Where each pipeline stage runs

The 8 stages of `make_short` (Shorts path) and the 6-7 stages of
`render` (footage_only / long_form) decompose like this:

| Stage | What it does | Where it runs | Why there |
|---|---|---|---|
| **0. Source mining** | pull raw text (Reddit top, Wikipedia oddities, today-in-history, YouTube transcript) | laptop | HTTP-bound, no GPU |
| **1. Script authoring** | hook-first 50-80 word narration, title options, source-url passthrough | **External: Azure OpenAI or Claude** (subprocess to `claude` CLI) | LLM, paid per-token |
| **2. Cast authoring** (per-story narrator + supporting characters) | character descriptions for the diffusion prompts — required by AITA-style cast-router channels | **External: Claude CLI** | LLM, prose-heavy, claude > Azure for character prose |
| **3. TTS narration** | text → ~10-90 s WAV (Shorts) or ~25-60 min WAV (long-form) | **Cloud Run L4 (asia-southeast1)** + auto-fallback to local F5-MLX or local Kokoro hf_alpha | GPU-bound, ~10× speedup vs M2 Max MPS |
| **4. ASR (whisper word timestamps)** | align spoken WAV back to text for caption timing + beat boundaries | **laptop (whisper-mlx)** | MLX-native, no cloud whisper service |
| **5. Beat split + LLM prompt authoring** | split narration into 8-20 visual beats, then Claude CLI authors a diffusion prompt per beat | laptop (split logic) + **External: Claude CLI** (prompt authoring) | Mix of CPU + LLM |
| **6. Image generation** | one PNG per beat at 768×1344 (Shorts) / 1344×768 (long-form image_panels) | **Cloud Run L4** `cloudrun_flux2_klein` + auto-fallback to local mflux Z-Image-Turbo | GPU-bound, 3-4× speedup vs local mflux |
| **7. Caption pre-render** | per-word PNG overlays via PIL (yellow italic, channel-specific font) | laptop (PIL) | CPU-cheap, parallel-friendly |
| **8. ffmpeg compose** | slideshow ken-burns or footage cut, audio mux, caption overlay (drawtext or libass ASS), final encode | laptop (ffmpeg + libass) | Always local; ffmpeg on M2 Max is fast for short renders |
| **9. Critic** | Claude CLI watches the rendered mp4, scores 0-10, optionally re-runs from stage 5 | **External: Claude CLI** (with vision) | LLM-vision, paid per-token |
| **10. Upload** | YouTube Data API: video.insert + thumbnail set + privacy=public | laptop (google-api-python-client) | Per-channel OAuth tokens cached on laptop |
| **11. Cross-engagement** | subscribe + like-newest of every other channel from the just-uploaded channel's account | laptop (Playwright + Chrome profile per account) | Browser automation can't run on Cloud Run cleanly |

### Long-form variants

Long-form (`pipeline.render.long_form` for sleep history,
`pipeline.render.sports_doc` for sports docs, footage_only for
kathaa) skip stages 4-7 in favour of:

| Stage | What it does | Where |
|---|---|---|
| **5'. Footage prep** (footage_only) | yt-dlp download from archive.org URLs in the shotlist, scrub watermarks, fps unification | laptop (yt-dlp + ffmpeg) |
| **6'. Image-panels render mode** (long_form image_panels OR kathaa) | Z-Image-Turbo painterly stills with ken-burns (40-300 panels) | Currently still **local mflux** (cloud Z-Image is the P3.5 follow-up) |
| **8'. Long-form ffmpeg compose** | `_trim_clip_letterbox` aspect short-circuit (stream-copy when src aspect == out aspect), libass ASS captions for 600-800 caption rows | laptop |

---

## 3. Per-stage latency table

Numbers are **measured** (warm-state) where possible, **estimated**
otherwise. Variance is typical (±50%). All latencies are wall-clock
on the laptop's perspective unless noted.

| Stage | Cold | Warm | Notes |
|---|---|---|---|
| **0. Source mining** | n/a | 5-30 s | HTTP, depends on Reddit/Wiki throttling |
| **1. Script authoring** | n/a | 5-15 s | Azure OpenAI gpt-5.3-chat, ~80 tokens |
| **2. Cast authoring** | n/a | 30-60 s | Claude CLI, prose-heavy, multi-character |
| **3a. TTS chunked Shorts** (cloudrun_chatterbox, ~30 s WAV) | +60-120 s (cold container; pre-warm via `cloud/warm_image_services.sh` doesn't apply — that's image-only) | **~25-50 s** (RTF 0.21-0.24 cloud) | Falls back to local F5-MLX (~2× slower, ~50-90 s) on cloud failure |
| **3b. TTS chunked long-form** (cloudrun_chatterbox, ~25-60 min WAV) | +60-120 s | **~7-15 min** for a 60-min sleep video | Local F5-MLX would be 1.5-9 hours and crashes WindowServer on Low Power Mode |
| **4. Whisper word timestamps** | +5 s (mlx_whisper weight load) | ~0.3 RTF on M2 Max → ~10 s for a 30 s WAV; ~3 min for a 60 min WAV | Always laptop; no cloud whisper service |
| **5. Beat split + prompt authoring** | n/a | 30-90 s for 8 beats (Claude CLI: one call per prompt) | Bottleneck for short Shorts; long-form has bigger but fewer prompts |
| **6a. Image gen — Shorts** (cloudrun_flux2_klein, 8 beats × 768×1344 4-step) | +5-7 min cold-load | **~33 s** for 8 images (4-5 s/image warm) | Falls back to local mflux Z-Image (~85-105 s for 8 images, ~3× slower). Pre-warm via `cloud/warm_image_services.sh` to skip cold-load. |
| **6b. Image gen — long-form image_panels** (40-300 panels) | n/a (still local mflux) | **~10-30 min for 40 panels** local mflux ZImage; cloud Z-Image is P3.5 follow-up | Long-form image_panels mode is rare; most long-form uses footage_only |
| **6c. Footage download** (footage_only) | n/a | 30 s - 5 min depending on shotlist source clip count + size | yt-dlp local |
| **7. Caption pre-render** | n/a | 0.2-2 s | PIL local, parallel |
| **8a. ffmpeg compose Shorts** | n/a | **25-60 s** | Slideshow Ken Burns + per-word overlays; M2 Max libx264 |
| **8b. ffmpeg compose long-form** (60-90 min sleep) | n/a | 30-90 min | libass ASS path keeps RAM under 2 GB even with 800 caption rows |
| **9. Critic (Shorts)** | n/a | 30-60 s | Claude CLI vision; samples mp4 at 1 fps, scores |
| **10. Upload** | n/a | 10-30 s | YouTube Data API video.insert; metered chunk upload |
| **11. Cross-engagement** | n/a | 30-60 s per other channel × N other channels = ~3-5 min total | Playwright + per-account Chrome profile |

---

## 4. End-to-end channel workflows

### 4.1 mystoriesanimated (AITA, AITA cliffhanger, wiki_oddities, today_in_history, tifu, etc.)

**Format:** 50-60 s vertical Shorts (1080×1920), Reddit/Wikipedia source.

```
[laptop]    pull_stories.py: Reddit/Wiki HTTP        ──  5-30 s
[external]  Azure OpenAI: rewrite + title options    ── 5-15 s
[external]  Claude CLI: cast authoring               ── 30-60 s
            ──────────────────────────────────────────────────
[cloud]     /synth (cloudrun_chatterbox warm)        ── 25-50 s
[laptop]    whisper_mlx: word timestamps             ── 10-20 s
[laptop+ex] beat split + Claude CLI prompts × 8     ── 30-90 s
            ──────────────────────────────────────────────────
[cloud]     /generate × 8 (cloudrun_flux2_klein)     ── ~33 s warm / +5-7 min cold-load tax
[laptop]    PIL caption pre-render (parallel)        ── 1-2 s
[laptop]    ffmpeg slideshow compose                 ── 25-60 s
            ──────────────────────────────────────────────────
[external]  Claude CLI critic (1 fps sample)         ── 30-60 s
[laptop]    YouTube upload                            ── 10-30 s
[laptop]    Cross-engagement (Playwright × N chans)  ── 3-5 min

TOTAL warm cloud, ~3-5 min/Short end-to-end
TOTAL cold cloud, ~10-12 min/Short (cold-load front-loaded by pre-warm)
```

### 4.2 historyrecapped (Shorts: war battles + long-form: sleep history)

**Shorts format:** 50-60 s vertical, 100% archival footage from
archive.org. Same as mystoriesanimated but stages 6a → 6c (footage
download replaces image gen, except for thumbnail).

**Long-form sleep format:** 60-120 min, 16:9 1920×1080, soft Sarah
voice over warm-firelight-graded archival footage.

```
[laptop]    /make-sleep-history skill: author narration  ── 5-15 min interactive (humans + Claude)
            ──────────────────────────────────────────────────
[cloud]     /synth chunked (cloudrun_chatterbox)         ── 7-15 min for 60 min WAV
[laptop]    whisper_mlx alignment (chunked)              ── 2-5 min for 60 min WAV
[laptop]    yt-dlp footage download (archive.org)        ── 5-15 min depending on clip count
[laptop]    long-form ffmpeg compose                     ── 30-90 min (libass ASS path)
            ──────────────────────────────────────────────────
[laptop]    YouTube upload                                ── 1-3 min for ~500 MB mp4
[laptop]    Cross-engagement                              ── 3-5 min

TOTAL: 50 min - 2.5 hr per long-form sleep video
```

### 4.3 hindutavaanimated (Hindu mythology shorts + kathaa)

**Shorts:** 50-60 s vertical Hindi narration via `cloudrun_indicf5`
(IndicF5 voice clone), Z-Image-Turbo style devotional images via
cloud FLUX.2 klein.

**Kathaa:** 50-70 min long-form Hindi narration over slow
ken-burns devotional photo + temple footage. Footage-only path.

Differences from mystoriesanimated Shorts pipeline:
- Stage 3 TTS provider: `cloudrun_indicf5` (Hindi voice clone) — RTF
  similar to chatterbox at ~0.20-0.30 (cloud L4)
- Auto-fallback: local Kokoro `hf_alpha` (preset Hindi female) — flatter prosody but ships in Hindi

### 4.4 cosmosdecoded (physics/space "How We Knew" deep dives + matched short)

**Long-form format:** 25-40 min, 16:9, footage-only from NASA / NTRS
/ ESA / CERN / LIGO / Wikimedia / arXiv / LoC / Smithsonian. Same
shape as historyrecapped long-form sleep but with cosmosdecoded
voice + branding.

**Paired short:** 50-60 s vertical, isolates the act-2
"measurement" moment. Same render shape as mystoriesanimated Shorts.

### 4.5 sportstoriesanimated (ranked countdowns + sports docs)

**Ranked countdowns:** 50-60 s vertical, FLUX.2 klein cartoon-style
faces of athletes/clubs.

**Sports docs (long-form_doc):** 30-60 min, footage-only from sports
broadcasts / archive.org PD entries. Anchor-aligned timeline.

### 4.6 airecap (AI news shorts)

50-60 s vertical, FLUX.2 klein photoreal/diagram-style images of AI
products + people. Source: handpicked weekly AI news bullet points.

### 4.7 rhymetimejunction (kids' Hinglish nursery rhymes)

**Two output paths:**
- **Slideshow path:** FLUX.2 klein illustrations (flat cartoon
  mascots Laddu/Jalebi/Tuk-Tuk/Dadi) + ken-burns + spoken Hinglish
  narration via `cloudrun_chatterbox`.
- **Sung-song path:** Suno API generates the actual sung MP3 over
  the LLM-authored Hindi-English bilingual lyrics. Costs ~$0.10/song
  via sunoapi.org wrapper. No TTS needed, no laptop audio compute.

---

## 5. Cost per render (back-of-envelope, 2026-05-07)

Cloud Run scale-to-zero pricing (asia-southeast1, NVIDIA L4):

| Component | Per-second | Per-Short (warm) | Per-Short (cold + Short) |
|---|---|---|---|
| L4 GPU | ~$0.00023 | ~$0.04 (33 s warm gen) | ~$0.10 (cold-load 5-7 min + 33 s gen) |
| TTS (cloudrun_chatterbox) GPU + idle | included in TTS service | ~$0.01 (25-50 s synth) | ~$0.03 (cold-load + synth) |
| Azure OpenAI (LLM) | per-token | ~$0.005-0.02 / Short | same |
| Claude CLI (cast + beat prompts + critic) | per-token | ~$0.05-0.15 / Short | same |
| YouTube Data API | free (within quota) | $0 | $0 |
| **Net per-Short (warm)** | — | **~$0.10-0.20** | **~$0.18-0.35** (cold) |

At 5-15 Shorts/day = **~$15-100/mo**.

Long-form sleep video cost (60-min): ~$2-4 per video (TTS chunks
dominate). At 1-3/week ≈ **~$10-50/mo**.

**Total monthly steady-state: ~$30-150/mo.** Fits inside a $300 GCP
free credit pool with comfortable margin.

---

## 6. Where the latency budget really goes (pareto)

Top 5 contributors to wall-clock latency on a typical Shorts render
(warm state, no cold-load):

1. **Cross-engagement: 3-5 min** — Playwright × N other channels.
   Largest single line item; roughly half the pipeline's wall-clock.
   Could be parallelized but currently sequential per browser
   profile. **Improvement candidate.**
2. **Image gen: 33-60 s** — already cloud, already 3-4× faster than
   local. Hard to improve without different model or batch /
   parallel calls (max-instances=2 caps parallelism at 2).
3. **ffmpeg compose: 25-60 s** — local CPU + libx264. Could be
   GPU-accelerated via VideoToolbox but quality regression risk.
4. **TTS synth: 25-50 s** — cloud chatterbox is already at RTF
   0.21-0.24. Lower bound is the WAV duration itself.
5. **Beat-prompt authoring: 30-90 s** — Claude CLI sequential
   per-prompt calls. Could be parallelized; currently bounded by
   Claude CLI's own latency.

Bottom 5 (already negligible):
- Caption pre-render: 1-2 s
- Whisper alignment: 10-20 s
- Source mining: 5-30 s
- LLM script: 5-15 s
- YouTube upload: 10-30 s

---

## 7. What's still NOT in the cloud (intentionally)

| Stage | Why local |
|---|---|
| Whisper word timestamps | mlx_whisper is faster on M2 Max than any cloud whisper service we've benchmarked, AND we'd pay for the upload+download round-trip of a multi-MB WAV |
| ffmpeg compose | Local M2 Max ffmpeg is fast enough for Shorts (25-60 s); Cloud Run for ffmpeg adds ~30 s of mp4 upload+download with no compute win |
| YouTube upload | YouTube Data API requires per-account OAuth tokens; tokens live in laptop browser sessions, not portable to Cloud Run cleanly |
| Cross-engagement | Playwright + per-account Chrome profiles. Cloud Run sandboxing makes this awkward and we'd need to stash N Chrome profiles in GCS. |
| Claude CLI cast/critic | The CLI runs locally and authenticates with the user's Anthropic account; running it from Cloud Run would need the account's session token in GCS Secret Manager. Not worth it for the savings. |

---

## 8. Cold-start tax matrix

The **first** render after a long idle pays cold-load on each cloud
service it touches. Subsequent renders inside the warm window
(typically 15 min) pay 0.

| Service | Cold-load (one-time) | Warm window after last call | Pre-warm available? |
|---|---|---|---|
| `cloudrun_chatterbox` | ~3-5 min (chatterbox `assign=True` patch active) | ~15 min idle before scale-to-zero | Yes — `curl /readyz` |
| `cloudrun_f5` | ~30-60 s (small model) | ~15 min | Yes |
| `cloudrun_higgs` | ~5-8 min (12 GB Higgs) | ~15 min | Yes |
| `cloudrun_cosyvoice` | ~3-5 min | ~15 min | Yes |
| `cloudrun_indicparler` | ~2-3 min | ~15 min | Yes |
| `cloudrun_indicf5` | ~30-60 s | ~15 min | Yes |
| `cloudrun_flux2_klein` | **~5-7 min** | ~15 min | **Yes — `cloud/warm_image_services.sh`** |
| `cloudrun_z_image_turbo` | 15-25 min unreliable (P3.5) | n/a (not on production path) | n/a |

**The pre-warm script `cloud/warm_image_services.sh`** triggers
`/readyz` on the image services in parallel. Run 5-10 min before a
batch render window. For TTS, hit `/readyz` directly:

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" \
  "https://ytfactory-tts-chatterbox-767262167641.asia-southeast1.run.app/readyz"
```

Or set up Cloud Scheduler crons (free tier covers 3 jobs/mo) — see
`docs/cloudrun_image.md` Cost analysis section for the recipe.

---

## 9. Future improvements (not committed)

- **Render-level concurrency = 2** — dispatch 2 image-gen calls in
  parallel within a single render to use both `max-instances=2`
  containers. Halves stage-6 wall-clock at no extra cost.
- **Z-Image cloud cold-load fix (P3.5)** — FastAPI lifespan load so
  the request path never blocks on cold-load. Then we can A/B
  Z-Image vs FLUX.2 klein per channel.
- **Cross-engagement parallelism** — drive multiple Chrome profiles
  concurrently. Biggest single win, ~2-3 min off every Short.
- **Quantized FP8/NVFP4 FLUX.2 klein** — 1.6×–2.7× faster per
  BFL/NVIDIA collab. Defer until bf16 baseline is stable.
- **Beat-prompt authoring parallelism** — 8 Claude CLI calls in
  parallel instead of sequential. ~30-60 s off every Short.
- **TTS-via-cloud for the Higgs / indicparler / cosyvoice paths
  using requests instead of urllib** — `pipeline/tts/cloudrun.py`
  still uses urllib; same TCP-RST class of bug as the image client
  surfaced 2026-05-07 (see `memory/feedback_urllib_cloudrun_stale_tcp.md`).
  P3.5+ follow-up.

---

## 10. Quick-reference cheat sheets

### Pre-warm before a render

```bash
# Image services (FLUX only by default; add zimage if you're A/B testing)
./cloud/warm_image_services.sh
./cloud/warm_image_services.sh flux zimage   # both, parallel

# TTS chatterbox (the production English default)
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" \
  "$CLOUDRUN_TTS_CHATTERBOX_URL/readyz" &
```

### Disable cloud fallback for canary work

```bash
export CLOUDRUN_IMAGE_DISABLE_FALLBACK=1
export CLOUDRUN_TTS_DISABLE_FALLBACK=1
./scripts/make_shorts.py --channel … --script …
```

### Where to look first when a render is slow

1. Check pre-warm: `gcloud run services list --filter='metadata.name~ytfactory' --format='value(metadata.name,status.latestReadyRevisionName)'`
2. Check Cloud Run logs: `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="ytfactory-image-flux2-klein"' --freshness=10m`
3. Check `[image-time]` lines in stdout — the renderer emits per-image timing
4. Check `pipeline.images_cloudrun` circuit-breaker state — if tripped, all images went to local mflux this render
