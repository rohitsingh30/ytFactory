# Z-Image-Turbo quality playbook

Research-backed additive recommendations for the in-flight refiner refactor
(`/ai/improvement-opportunities.md` O8). Every claim cites a URL; every
recommendation is ADDITIVE — none change current test outputs.

Audited surfaces:
- `pipeline/images/images_cloudrun.py` (HTTP client → Cloud Run service)
- `pipeline/images/prompt_refiner.py` (`REFINER_VERSION = "v3-scene-anchor"`)
- `cloud/image-z-image-turbo/server.py` (FastAPI wrapper around `ZImagePipeline`)
- `cloud/image-z-image-turbo/{Dockerfile,requirements.txt,deploy.sh}` (L4, diffusers 0.38.0)
- `tests/test_prompt_refiner.py` + `tests/test_pipeline_images_capabilities.py`

---

## Model identity (verified)

| Field | Value | Source |
|---|---|---|
| Repo | `Tongyi-MAI/Z-Image-Turbo` | [HF model card](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) |
| Org | Alibaba Tongyi Lab (Tongyi-MAI) | [GitHub README](https://github.com/Tongyi-MAI/Z-Image) |
| Params | 6 B | "An efficient 6B-parameter foundation generative model" — [arXiv 2511.22699](https://arxiv.org/abs/2511.22699) |
| Architecture | **Scalable Single-Stream DiT (S3-DiT)** — *not* "Sparse Scaling DiT" as our docstring guesses | "We adopt a Scalable Single-Stream DiT (S3-DiT) architecture. … text, visual semantic tokens, and image VAE tokens are concatenated at the sequence level" — [Z-Image diffusers docs](https://huggingface.co/docs/diffusers/api/pipelines/z_image) |
| Distillation | **Decoupled-DMD** (Decoupled Distribution Matching Distillation) + DMDR (RL post-train) | "Decoupled-DMD is the core few-step distillation algorithm that empowers the 8-step Z-Image model" — [HF model card § Distillation](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo), papers [arXiv 2511.22677](https://arxiv.org/abs/2511.22677), [arXiv 2511.13649](https://arxiv.org/abs/2511.13649) |
| Text encoder | **Qwen3-4B** (hidden=2560, 36 layers, RoPE θ=1,000,000, max positions 40,960) | [Z-Image text encoder doc](https://github.com/fblissjr/ComfyUI-QwenImageWanBridge/blob/main/nodes/docs/z_image_encoder.md) and HF Discussion #4 |
| Max prompt tokens (default) | **512** (diffusers default); **1024** supported locally | "the text maximum length is set as 512 tokens" — [official PROMPTING GUIDE discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8) |
| Trained resolutions (in-domain) | **1024 / 1280 / 1536** tiers; canonical 9:16 is **720×1280** and **864×1536**; canonical 16:9 is **1280×720** and **1536×864** | Tongyi-MAI staff (QJerry): "use more as long as both width & height are divided by 16 (8×vae + 2×patch) and not exceeded 256 pixels fluctuation of 1024 resolution grid (like 768 ~ 1280)" — [Discussion #28](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/28); aspect bucket table — [SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions](https://github.com/SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions) |
| VAE downsample | 8× | Discussion #28 (QJerry) |
| Patch size | 2× | Discussion #28 (QJerry) |
| Scheduler | `FlowMatchEulerDiscreteScheduler` (registered in `__init__`) | [diffusers Z-Image API doc](https://huggingface.co/docs/diffusers/api/pipelines/z_image) |
| Attention | SDPA default; pluggable via `pipe.transformer.set_attention_backend(...)` | "Optionally, set the attention backend to flash-attn 2 or 3, default is SDPA in PyTorch" — [diffusers Z-Image API doc](https://huggingface.co/docs/diffusers/api/pipelines/z_image) |
| License | Apache-2.0 | [HF model card](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) |
| Release date | Nov 2025 | [ComfyUI Wiki release note](https://comfyui-wiki.com/en/news/2025-11-27-alibaba-z-image-turbo-release) |

**Corrections to our existing docstrings:**
- `pipeline/images/images_cloudrun.py` and CLAUDE.md call the architecture
  "S3-DiT (Sparse Scaling DiT)". The official name is
  **Scalable Single-Stream DiT** ([diffusers doc verbatim](https://huggingface.co/docs/diffusers/api/pipelines/z_image)).
- CLAUDE.md claims "Resolution: 1920×1080 default". Actual code default is
  **768×1344** (`pipeline/images/images.py:694-695`,
  `cloud/image-z-image-turbo/server.py:194-200`). The 1920×1080 is the
  *output frame* size after ffmpeg upscale in compose
  (`pipeline/images/animation.py:72`).

---

## Inference parameters (recommended)

| Param | Our current | Recommended | Citation |
|---|---|---|---|
| `guidance_scale` | `0.0` (clamped via validator) | **`0.0`** — keep | "Guidance should be 0 for the Turbo models" — [diffusers API doc](https://huggingface.co/docs/diffusers/api/pipelines/z_image); "this model does not use negative prompts at all" — [Discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8) |
| `num_inference_steps` | 9 (range 4-12) | **9** (range 4-12 OK) — keep | "num_inference_steps=9 … results in 8 DiT forwards" — [HF model card example](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) |
| `negative_prompt` | not sent | **not sent** — keep | "Ignored when not using guidance (i.e., ignored if `guidance_scale` is less than `1`)" — [diffusers Z-Image API param doc](https://huggingface.co/docs/diffusers/api/pipelines/z_image); `apply_cfg = self.do_classifier_free_guidance and current_guidance_scale > 0` — [pipeline_z_image.py](https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/z_image/pipeline_z_image.py) |
| `cfg_normalization` | not sent | not sent (no-op at CFG=0) | "if new_pos_norm > max_new_norm: pred = pred * (max_new_norm / new_pos_norm)" — applied only when `apply_cfg=True` |
| `cfg_truncation` | not sent | not sent (no-op at CFG=0) | "if _precomputed_t_norms[i] > self._cfg_truncation: current_guidance_scale = 0.0" — already-zero stays zero |
| `max_sequence_length` | **NOT PASSED → defaults to 512** | **PASS `1024`** | "Locally, you can set `max_sequence_length=1024` to accommodate longer prompts" — [Tongyi-MAI official Discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8) |
| Resolution (9:16) | 768×1344 (off-domain by ~64px on long side) | **720×1280** or **864×1536** (both in-domain) | QJerry "not exceeded 256 pixels fluctuation of 1024 grid (like 768 ~ 1280)" — Discussion #28; canonical buckets — [SaTaNoob table](https://github.com/SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions) |
| Attention backend | default (PyTorch SDPA, no flash kernel) | **`_native_flash`** (PyTorch SDPA's FlashAttention path) on L4 | "25% faster — saved about 52 seconds on a 50-step Full HD generation" on Blackwell with `_native_flash` — [Discussion #139](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/139); FA-3 requires Hopper SM90+ which L4 (Ada SM89) does not have — [diffusers attention_backends doc](https://huggingface.co/docs/diffusers/main/en/optimization/attention_backends) ("FlashAttention-3 requires Ampere GPUs at a minimum" — note: in practice FA-3 is Hopper-only) |

---

## Prompt-shape rules (research-backed)

Each rule below cross-checks ≥2 sources.

### R1. Long, structured, positive — 80-250 words

> "Z-Image-Turbo works best with long and detailed prompts. You may consider
> first manually writing the prompt and then feeding it to an LLM to enhance it."
> — [Tongyi-MAI official Discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8)

> "80–250 words of clear, structured description is a sweet spot, and long
> and precise is good, while long and poetic is often worse."
> — [Fliki community guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)

**Our refiner already targets this** (`_REFINER_SYSTEM`: "Aim for ~120-180
words of combined output across the three fields"). Status: aligned —
**do not change**.

### R2. Positive constraints only — every "no X" loses

> "Write positive constraints as presence, not absence — 'Clean studio
> background, plain seamless backdrop, no props' beats 'no clutter'."
> — [Fliki guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)

> "negative_prompt … Ignored when not using guidance"
> — [diffusers Z-Image API param doc](https://huggingface.co/docs/diffusers/api/pipelines/z_image)

**Our refiner enforces this** (Rule 7: "Use POSITIVE constructions exclusively"
and the 2026-05-24 anti-text suffix rewrite documented at
`images_cloudrun.py:489-518`). Status: aligned — **do not change**.

### R3. Subject at the prompt start — left-positional bias is real

> "Put the subject and any text you want rendered at the very start. This
> reflects Z-Image's 512-token effective attention cap."
> — [Fliki guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)

The Qwen3-4B encoder uses RoPE with θ=1,000,000 — long-distance fall-off in
practice ([encoder doc](https://github.com/fblissjr/ComfyUI-QwenImageWanBridge/blob/main/nodes/docs/z_image_encoder.md)).
Our refiner already injects shot tokens at the **start** of `refined_visual`
(Rule 2: "Inject EXACTLY ONE shot_type from the rotation … at the START of
refined_visual"). Status: aligned — **do not change**.

### R4. 3-5 visual concepts maximum

> "Limit yourself to 3 to 5 strong visual concepts per prompt, as past that
> attention drifts and you get contradictions."
> — [Fliki guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)

**Latent risk**: our refiner currently stacks `scene_anchor` +
`character_description` + shot_type + lighting token + style_block + mood
+ anti-text suffix. That's 6-7 concept slots, brushing the limit.
See "Open questions" §3 below.

### R5. Token weighting `(token:1.5)`, `BREAK`, `AND` syntax — UNSUPPORTED

> "Doesn't Work: Token weighting syntax like `(token:1.5)`"
> — [Illuminatianon community guide](https://gist.github.com/illuminatianon/c42f8e57f1e3ebf037dd58043da9de32)

**Implication**: don't add Stable-Diffusion-style emphasis syntax to the
refiner output. Our current refiner doesn't, so this is a future-proofing
note, not a fix.

### R6. Pair ONE camera phrase with ONE film/lighting phrase

> "the guide emphasizes pairing **one camera phrase** and **one film/lighting
> phrase**: 'point-and-shoot film camera,' 'Fujifilm GFX100 II medium format
> camera, 110mm f/2 lens' … 'Kodak Portra 400 tones, warm skin rendition'"
> — [Fliki guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)

Our refiner injects ONE shot_type (camera) and ONE lighting token per beat.
Status: aligned — **do not change**. (See O3 below for *optional* camera-lens
vocabulary upgrade.)

---

## Character continuity techniques (ranked for our L4 + 60-min envelope)

| Technique | Feasible on our stack? | Citation |
|---|---|---|
| **(a) Deterministic seed locking** (we do this) | ✅ already shipping (`image_seed + seed_offset`) | per render_worker code |
| **(b) IP-Adapter face/style conditioning** | ❌ **does not exist** for Z-Image | "Z-Image has no IP-adapter. The closest thing is z-image edit, but it has not been released." — [search result quoting community Q&A](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) |
| **(c) ControlNet pose/segmentation** | ⚠️ exists (`alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union`) but **diffusers Python integration is NOT documented** — ComfyUI-only as of the searched tutorials | "covers only ComfyUI implementation" — [Next Diffusion ControlNet tutorial](https://www.nextdiffusion.ai/tutorials/consistent-z-image-turbo-images-controlnet-comfyui-t2i); model — [stable-diffusion-art article](https://stable-diffusion-art.com/z-image-controlnet-union/) |
| **(d) LoRA finetune** | ⚠️ trainers exist (Next Diffusion, AI Toolkit) but Z-Image's **fused QKV** breaks naive diffusers LoRA loads — needs a custom loader | "LoRAs … shipped with separate to_q, to_k, to_v projections … Z-Image Turbo's native architecture stores attention as a single fused QKV matrix. Loading these LoRAs without conversion means the attention weights never reach the model." — [search-result summary citing capitan01R/Comfyui-ZiT-Lora-loader](https://github.com/capitan01R/Comfyui-ZiT-Lora-loader) |
| **(e) Verbatim character-token strings** (we do this) | ✅ already shipping (`character_description` prepended at compose time, `build_full_prompt` in `pipeline/images/images.py`) | per refiner doc rule 6 |
| **(f) `prompt_embeds` shared across panels** | ✅ technically supported by `ZImagePipeline.__call__(prompt_embeds=...)` but [UNVERIFIED — needs A/B test] whether reusing a per-character embedding tensor improves continuity. | [diffusers API](https://huggingface.co/docs/diffusers/api/pipelines/z_image) |

**Conclusion**: (a) + (e) are the realistic levers. (c) and (d) are
out-of-scope for the in-flight refactor — they need their own design doc
(see "Open questions" §1 below). IP-Adapter (b) doesn't exist.

---

## Recommended refactor additions (ADDITIVE — no regressions)

Three high-confidence, low-blast-radius wins. All can be shipped behind
env-flag kill-switches to keep the existing test surface green.

### O-Z1. Pass `max_sequence_length=1024` to the diffusers `__call__`

**Where**: `cloud/image-z-image-turbo/server.py`, in the `pipe(...)` call
(~line 276) AND `pipeline/images/images_cloudrun.py` `_generate_cloudrun`
payload (~line 442) so the wire schema permits it.

**Sketch**:
```python
# server.py — add a field on GenerateIn
class GenerateIn(BaseModel):
    ...
    max_sequence_length: int = Field(1024, ge=64, le=1024)

# in generate() — pass it through
result = pipe(
    prompt=req.prompt,
    height=h, width=w,
    guidance_scale=req.guidance_scale,
    num_inference_steps=req.steps,
    generator=generator,
    max_sequence_length=req.max_sequence_length,  # NEW
)
```

**Why**: our refiner targets 120-180 words of dense visual detail PLUS the
character + era + anti-text suffix. Worst-case a long render packet runs
~220 words ≈ 280-330 tokens (well inside 512). But the *long-form*
character-prepend can push it past 512 in edge cases (long
`character_description` + long `scene_anchor` + 180-word refined fields).
At 512 the encoder silently truncates the SUFFIX — which is exactly where
our anti-text safety clause now lives (`images_cloudrun.py:520-524`).
Doubling to 1024 buys 5× the headroom for no quality penalty.

**Evidence**:
> "Locally, you can set `max_sequence_length=1024` to accommodate longer prompts."
> — [Tongyi-MAI official Discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8)

**Cost**: ~0 — Qwen3-4B already runs to 40,960 positions; 512→1024 is just
a 2× context-window allocation in the text encoder, < 100 ms on L4. No VRAM
implications for the DiT (latent size doesn't change).

**Test impact**: zero — no existing test pins this. The current path
implicitly uses 512 (the pipeline default); explicitly passing 1024 is a
silent compatibility upgrade.

**Composes with in-flight refactor**: yes — orthogonal to refiner prompt
content.

---

### O-Z2. Move 9:16 dimension from 768×1344 → 720×1280 (canonical bucket)

**Where**: `cloud/image-z-image-turbo/server.py:194-200` `_ASPECT_DIMS`,
and `pipeline/images/images.py:694-695` (`generate()` default).

**Sketch**:
```python
# server.py
_ASPECT_DIMS = {
    "9:16": (720, 1280),     # was (768, 1344) — 720x1280 is in the
                             # canonical "1024-tier" training bucket
    "16:9": (1280, 720),     # was (1344, 768)
    "1:1":  (1024, 1024),
    "4:3":  (1152, 864),     # was (1152, 896) — 1152x864 is canonical
    "3:4":  (864, 1152),     # was (896, 1152) — symmetric
}

# Optional second tier for HD output (relevant once compose targets
# 1080p natively):
_ASPECT_DIMS_HD = {
    "9:16": (864, 1536),
    "16:9": (1536, 864),
    "1:1":  (1280, 1280),
}
```

**Why**: per Tongyi-MAI staff Discussion #28:

> "You could use more as long as both width & height are divided by 16
> (8×vae + 2×patch) and not exceeded 256 pixels fluctuation of 1024
> resolution grid (like 768 ~ 1280)."
> — [Discussion #28 (QJerry, Tongyi-MAI)](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/28)

768×1344 is **outside the in-domain band** on the long side (1344 - 1024 =
320 > 256-pixel envelope around the 1024 grid). The canonical 9:16 bucket
the model was trained on is 720×1280 ([SaTaNoob table](https://github.com/SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions),
cross-checked against the [Tongyi-MAI HF Space app.py](https://huggingface.co/spaces/Tongyi-MAI/Z-Image-Turbo/blob/main/app.py)
linked from Discussion #28). Generating at an in-domain bucket is
guaranteed-best instruction following; off-domain is a guess.

Pixel-budget comparison:
- 768×1344 = 1,032,192 px (off-domain on long side)
- 720×1280 = 921,600 px (in-domain, 11% fewer pixels → ~10-12% faster)
- 864×1536 = 1,327,104 px (in-domain, 28% more pixels → ~25% slower but
  higher fidelity)

**Recommend default = 720×1280** (in-domain + 10% faster); optionally
expose 864×1536 as an "HD" tier for cinematic beats once compose can
consume non-1080p sources without uglification.

**Test impact**: `tests/test_pipeline_images_capabilities.py:21` pins
`width=1024, height=1024, steps=8` for the capabilities sanity check —
that's the 1:1 path, NOT 9:16. **No existing test pins 768 or 1344**,
verified via `grep -n "1344\|width=\|height=" tests/`. Safe to change.

**Composes with in-flight refactor**: yes — refiner is dimension-agnostic.

---

### O-Z3. Enable `_native_flash` attention backend on the L4 server

**Where**: `cloud/image-z-image-turbo/server.py` `_pipe()` (after
`enable_model_cpu_offload`).

**Sketch**:
```python
# server.py — after pipe.enable_attention_slicing()
try:
    pipe.transformer.set_attention_backend("_native_flash")
    logger.info("attention backend set to _native_flash (SDPA FlashAttention)")
except Exception as e:
    logger.info("attention backend stays SDPA-default: %s", e)
```

**Why**: PyTorch's SDPA has multiple kernel backends. The default uses an
auto-dispatch heuristic that doesn't always pick the FlashAttention kernel
on Ada Lovelace; explicitly selecting `_native_flash` guarantees the
FA-style path is used.

**Evidence**:
> "25% faster — saved about 52 seconds on a 50-step Full HD generation"
> using `with attention_backend("_native_flash"):` on Blackwell.
> — [Discussion #139](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/139)

> Diffusers backend names: `_native_flash` = "PyTorch's FlashAttention"
> — [diffusers attention_backends doc](https://huggingface.co/docs/diffusers/main/en/optimization/attention_backends)

The Blackwell number is upper-bound. L4 (SM89, Ada Lovelace) doesn't have
the Hopper async FA-3 kernel, but it does have the standard FA-2 SDPA
kernel which the PyTorch backend selects when `_native_flash` is forced.
Real-world L4 lift [UNVERIFIED — needs A/B test] but the **floor is zero**
(at worst it's the same SDPA path the default already takes).

**Cost**: ~0 — no extra package install (we don't need `flash-attn`, the
PyTorch built-in path is sufficient). ~10 ms one-time cost on cold-load to
walk the module tree.

**Test impact**: zero — no test pins the attention backend; `set_attention_backend`
fails open via the `try/except` guard.

**Composes with in-flight refactor**: yes — independent of prompt path.

---

## What we should NOT change (with reasons)

| Surface | Why it's already optimal |
|---|---|
| `guidance_scale=0.0` clamped via validator | "this model does not use negative prompts at all" — [Discussion #8 (Tongyi-MAI)](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8). The validator clamp also catches JSON float-rounding bugs (`0.0000001 != 0.0`). |
| `num_inference_steps` clamp `[4, 12]` default 9 | "num_inference_steps=9 … results in 8 DiT forwards" — [HF model card](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo). Above 12 you waste wall-time with no quality improvement on a distilled model; below 4 collapses to a noise blob. |
| Refiner system prompt — positive-only, 80-250 word target, shot-rotation injection at start, lighting token, anti-text prefix | All four are independently verified by the Fliki community guide, the illuminatianon gist, and the official Tongyi-MAI Discussion #8. Don't add `(token:1.5)` weights, don't add BREAK syntax — they're confirmed unsupported. |
| Anti-text SUFFIX (2026-05-24 rewrite, verb-led) | The diagnosis was correct: prepended noun tags were dominating short scenes (job 845bdb0d, 45/77 floating tees). Verb-led suffix is the right shape. Switching to negative_prompt won't help because CFG=0 ignores it. |
| `enable_model_cpu_offload` + VAE slicing + attention slicing | Required to fit the 22 GiB L4 envelope with concurrency=1. Documented in the server's docstring with the precise VRAM accounting. |
| Per-call `requests.Session()` (no connection pooling) | Documented anti-pattern for Cloud Run LB severance behaviour at the top of `images_cloudrun.py`. Don't try to share sessions. |
| `enable_model_cpu_offload` over manual `.to("cuda")` | The pipeline manages its own device placement; mixing with `.to` causes the 22 GiB OOM that bit on 2026-05-16. |

---

## Open questions / experiments to run

These need A/B testing on real renders before adopting.

### Q1. ControlNet Union for character continuity

The `alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union` model exists but every
published workflow is ComfyUI-based ([Next Diffusion tutorial](https://www.nextdiffusion.ai/tutorials/consistent-z-image-turbo-images-controlnet-comfyui-t2i)
covers only ComfyUI). Diffusers integration would need a custom
`ZImagePipeline` subclass that accepts a control image — non-trivial, not
documented upstream. **Experiment**: spike a one-panel proof-of-concept
loading the Fun-ControlNet weights as a `ZImageTransformer2DModel` adapter
in diffusers Python; if the API is clean, design a follow-up O-ID for
adding pose-controlled hero shots to the long-form panel-stills path.

### Q2. `prompt_embeds` reuse for character continuity

`ZImagePipeline.__call__` accepts a pre-computed `prompt_embeds` argument
([diffusers API](https://huggingface.co/docs/diffusers/api/pipelines/z_image)).
**Hypothesis**: encoding the `character_description` ONCE and concatenating
it with per-panel scene embeddings could yield more stable identity than
re-tokenising the full string each panel. **Experiment**: render 10 panels
of the same protagonist with (a) verbatim string prepend (current) vs
(b) cached embed prepend, score via face-embedding similarity in
`/critique-video`.

### Q3. Concept-budget audit on the refiner

The Fliki guide warns "3-5 strong visual concepts per prompt, past that
attention drifts." Our stack today: shot_type + character_desc + scene_anchor
+ era_anchor + lighting + style + mood + anti-text = 8 concepts in the
combined prompt. **Experiment**: instrument the refiner to count concept
slots actually emitted (heuristic: count noun phrases per slot), correlate
against `/critique-video` "drift" findings.

### Q4. FlashAttention-2 (`flash` backend) via `flash-attn` package

If the `_native_flash` PyTorch backend underperforms on L4 (insufficient
A/B lift), the next step is to install `flash-attn==2.x` in the Dockerfile
and use `set_attention_backend("flash")`. **Experiment**: add `flash-attn`
to a canary requirements.txt, redeploy as `ytfactory-image-z-image-turbo-fa2`,
benchmark wall-clock vs production for 50 panels.

### Q5. HD resolution tier for cinematic beats (864×1536 / 1536×864)

Once compose's resampling chain can consume non-1080p sources without
introducing ringing, the 1280-tier (864×1536 for 9:16) is in-domain and
yields ~25% more pixels of real model detail. **Experiment**: render
the same 5-beat sequence at 720×1280 and 864×1536, blind-score
hands/faces/cloth-detail fidelity.

### Q6. Z-Image-De-Turbo for hero panels

A community de-distilled variant ([zimageturbo.org/z-image-de-turbo](https://zimageturbo.org/z-image-de-turbo))
restores CFG and supports proper negative_prompt at the cost of 28-50
steps. **Hypothesis**: rendering hero panels (first beat, last beat) on
de-Turbo at 30 steps may justify the 3-4× wall-time hit. **Experiment**:
spin up a parallel `ytfactory-image-z-image-base` service, render
{panel-1, panel-N} only on it, splice into the Turbo-rendered body.

---

## Source ledger

Authoritative (Tongyi-MAI staff or official channels):
- [Z-Image-Turbo HF model card](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo)
- [Z-Image GitHub README](https://github.com/Tongyi-MAI/Z-Image)
- [Tongyi-MAI PROMPTING GUIDE Discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8)
- [Discussion #28 (latent/resolution, QJerry)](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/28)
- [Discussion #139 (Flash SDPA speedup)](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/139)
- [arXiv 2511.22699 (Z-Image paper)](https://arxiv.org/abs/2511.22699)
- [arXiv 2511.22677 (Decoupled-DMD)](https://arxiv.org/abs/2511.22677)

Diffusers/library:
- [Diffusers Z-Image pipeline doc](https://huggingface.co/docs/diffusers/api/pipelines/z_image)
- [Diffusers attention_backends doc](https://huggingface.co/docs/diffusers/main/en/optimization/attention_backends)
- [pipeline_z_image.py source](https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/z_image/pipeline_z_image.py)

Community (cross-checks):
- [Fliki prompting guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)
- [Illuminatianon gist](https://gist.github.com/illuminatianon/c42f8e57f1e3ebf037dd58043da9de32)
- [SaTaNoob resolution buckets](https://github.com/SaTaNoob/ComfyUI-Z-Image-Turbo-Resolutions)
- [Next Diffusion ControlNet tutorial](https://www.nextdiffusion.ai/tutorials/consistent-z-image-turbo-images-controlnet-comfyui-t2i)
- [Z-Image text encoder analysis](https://github.com/fblissjr/ComfyUI-QwenImageWanBridge/blob/main/nodes/docs/z_image_encoder.md)
- [stable-learn Z-Image tutorial](https://stable-learn.com/en/z-image-turbo-tutorial/)
- [RunDiffusion overview](https://www.rundiffusion.com/z-image)
- [ComfyUI Wiki release note](https://comfyui-wiki.com/en/news/2025-11-27-alibaba-z-image-turbo-release)
- [capitan01R/Comfyui-ZiT-Lora-loader (LoRA QKV fix)](https://github.com/capitan01R/Comfyui-ZiT-Lora-loader)
