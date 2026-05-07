# Image generation stack

> **Cloud-first as of 2026-05-07.** Every channel that previously
> declared `image_provider: z_image_turbo` (15 YAMLs across all
> channels except mystoriesanimated/variants/tifu.yaml which uses
> mflux) now defaults to `cloudrun_flux2_klein` (FLUX.2 [klein] 4B
> on Cloud Run NVIDIA L4). Local mflux Z-Image-Turbo stays in code
> as **automatic render-level fallback** when the cloud service is
> unhealthy.

ytFactory has two tiers of image-gen providers:

- **Cloud on NVIDIA L4** (`asia-southeast1`, **`--min-instances=0`**
  scale-to-zero, pre-warmed via
  `cloud/warm_image_services.sh`) — production default
- **Local on M2 Max** (mflux MLX-native) — automatic fallback only

For ops + cost + rollout history see `docs/cloudrun_image.md`.
For the model-pick research (why FLUX.2 klein, what we eliminated)
see `docs/research/image_gen_2026.md`.

---

## Available providers

| Provider | Where | License | Notes |
|---|---|---|---|
| `sd_turbo` | local (PyTorch MPS) | OpenRAIL-M | Legacy. Dropped in May 2026 — see images.validate_provider_config for the smudgy-output story. |
| `sdxl_lightning` | local (PyTorch MPS) | OpenRAIL-M | SDXL-Lightning 4-step LoRA + IP-Adapter (the only path with character-lock today). Used as fallback. |
| `mflux` | local (mflux/MLX) | Apache 2.0 | Flux Schnell 12B 4-bit. Top prompt adherence at 12B; ~40GB peak RAM. Used by `mystoriesanimated/variants/tifu.yaml` only. |
| `z_image_turbo` | local (mflux/MLX) | Apache 2.0 | Z-Image-Turbo 6B. **Was the production default until 2026-05-07.** Now the local-fallback path for cloud failures. |
| `z_image_turbo_fal` | hosted (fal.ai) | Apache 2.0 (model) | Z-Image-Turbo via fal.ai paid API. ~$0.005/image. Network-only fallback path. |
| `cloudrun_flux2_klein` | **cloud L4** | Apache 2.0 | **The new English production default** (2026-05-07). FLUX.2 [klein] 4B (BFL Jan 2026). Sub-second inference. T2I + image-to-image + multi-reference editing. Auto-falls back to local `z_image_turbo` (mflux). |
| `cloudrun_z_image_turbo` | **cloud L4** | Apache 2.0 | Z-Image-Turbo 6B via diffusers on L4. Parity / risk-insurance lane next to FLUX.2 klein. **Cold-load reliability WIP** — see `memory/feedback_zimage_cloudrun_coldload_stall.md`, P3.5 todo. NOT on the production path today. |

---

## Cloud-first laptop-fallback policy (2026-05-07)

| Cloud provider | If Cloud Run is unreachable → falls back to | Reason |
|---|---|---|
| `cloudrun_flux2_klein` | local `z_image_turbo` (mflux) | mflux is the laptop's stable image option; FLUX.2 klein has no MLX equivalent |
| `cloudrun_z_image_turbo` | local `z_image_turbo` (mflux) | same model, MLX runtime; byte-different output, identical style |

The fallback is implemented in
`pipeline/images_cloudrun.py::_local_fallback` and triggers on
`CloudRunUnavailable` (5xx, timeout, network error after retry).

**Render-level circuit breaker** (the key divergence from the TTS
client): the FIRST cloud failure in a render trips a module-global
flag → all subsequent images in the same process skip cloud and go
straight to local mflux. Without this, a 30-image Short during a
cloud outage would pay 30 × `CLOUDRUN_IMAGE_TIMEOUT` (900 s × 30 =
7.5 hr) of timeouts. `reset_circuit_breaker()` is wired into all
four render entry points.

Set `CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` in tests / canary to
hard-error instead of falling back.

---

## Active provider per channel (cloud-first reality, 2026-05-07)

| Channel / variant | Provider | Notes |
|---|---|---|
| airecap | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| cosmosdecoded | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| historyrecapped | `cloudrun_flux2_klein` | both top-level + nested image_panels variant flipped 2026-05-07 |
| hindutavaanimated | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated (parent) | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/aita_animated.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/aita_cliffhanger_animated.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/aita_cliffhanger_part2_animated.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/aita_cliffhanger_part2_text.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/aita_cliffhanger_text.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/aita_text.yaml | `cloudrun_flux2_klein` | **canary channel** (Phase 6 verified) |
| mystoriesanimated/variants/today_in_history.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/wiki_oddities.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| mystoriesanimated/variants/tifu.yaml | `mflux` | left on Flux Schnell (legacy, low priority for cloud migration) |
| rhymetimejunction | `cloudrun_flux2_klein` | flipped 2026-05-07; nursery-rhyme illustration |
| sportstoriesanimated | `cloudrun_flux2_klein` | flipped 2026-05-07 |
| sportstoriesanimated/variants/ranked.yaml | `cloudrun_flux2_klein` | flipped 2026-05-07 |

Total: **16 sites** flipped from `z_image_turbo` (local mflux) to
`cloudrun_flux2_klein` in a single sed pass.

---

## Per-image timing (canary 2026-05-07)

Measured against `mystoriesanimated/variants/aita_text.yaml`
rendering `aita-birth-pool` story:

| Provider | Per-image warm | Stage-3 total (7 images) | Notes |
|---|---|---|---|
| `cloudrun_flux2_klein` (cloud, this canary) | **3.86 s server / ~5 s e2e** | **33.4 s** | 4-step, 768×1344 vertical |
| `z_image_turbo` (local mflux, prior baseline) | ~12-15 s | ~85-105 s | 8 NFE, same dims |

**Speedup: ~3-4× per image, ~3× per Short stage 3.**

Cold-load (one-time on `--min-instances=1` boot): 5-7 min for
FLUX.2 klein. Hidden behind:
- Cloud Run's `--min-instances=1` (the warm container survives idle)
- `images.warmup("cloudrun_flux2_klein")` in render entry points
  (fires `/readyz` on a background thread at the top of `make_short`,
  so cold-loads happen during TTS+ASR if a fresh container is
  spawned)

---

## Visual fidelity vs prior local output

Side-by-side eyeballed during canary (`data/research/cloud_image/`):

- **Photoreal:** FLUX.2 klein > Z-Image-Turbo on most prompts
  (richer light, fewer anatomy glitches).
- **Stylized cartoon (AITA hand-drawn ink doodle):**
  indistinguishable from prior local output. Channel `image_style_prefix`
  and `force_positive` tokens carry across cleanly.
- **Character continuity across beats:** preserved (cast-router
  routes per-beat to the right narrator/supporting character; FLUX.2
  klein renders the same character consistently when the prompt
  re-mentions the cast description).
- **Like+subscribe icons in last beat:** rendered correctly via
  the existing `compose._append_last_beat_icons` path; no special
  cloud handling needed.

---

## Disqualified / not-shipped

See `docs/research/image_gen_2026.md` "Hard-disqualified" + "Soft-
disqualified" tables for the full elimination matrix:

- **FLUX.2 [dev] / [dev] Turbo** — top open-weights Elo BUT
  non-commercial license.
- **HunyuanImage 3.0** — Tencent CL with <100M MAU + EU/UK/SK
  exclusions; not Apache.
- **Qwen Image Max 2512** — Apache 2.0 + great Elo BUT 25-30 s/image
  on L4 = too slow for our 30-image-per-Short throughput.
- **HiDream-I1 Full** — gated Llama text encoder; ops cost too high.
- **Wan 2.2** — restrictive commercial use.
- **SANA 1.5 / FLUX.1 schnell / SDXL family** — quality below
  FLUX.2 klein, not worth the slot.

---

## Rollback

```bash
cd /Users/rohit/ytFactory
for f in $(grep -rlE "^[[:space:]]*image_provider: cloudrun_flux2_klein[[:space:]]*$" --include='*.yaml' .); do
  sed -i '' -E 's/^([[:space:]]*)image_provider: cloudrun_flux2_klein[[:space:]]*$/\1image_provider: z_image_turbo/' "$f"
done
```

Restores local mflux Z-Image-Turbo across all 16 sites in a single
sed. No service redeploy needed; the channel YAML is the only
surface that determines the dispatch path.
