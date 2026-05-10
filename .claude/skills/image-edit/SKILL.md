---
name: image-edit
description: Exploit FLUX.2 klein's image-to-image + multi-reference editing modes (the two modes we own on cloud and currently throw away — all 16 channel YAMLs only use plain T2I). Does three jobs the pipeline can't do today. (1) Cast-appearance lock — given a face image + a target shotlist, regenerate every shot with that exact face locked, fixing the long-running cast-drift bug documented in docs/cast_appearance_lock.md without retraining a LoRA. (2) Thumbnail variants — given an existing rendered shot, generate N stylistic variants (different palette, different framing, different overlay placement) for A/B testing thumbnails before publish. (3) Background swap — given an existing shot + a new background prompt, regenerate the same character/composition on a new background, useful for series-arc visual continuity ("Eklavya in the forest" → "Eklavya in Drona's ashram") without re-rolling the character. All three call cloud/image-flux2-klein's `/edit` endpoint (currently scaffolded but unused — this skill is what makes it real). Use when the user says "image-edit", "/image-edit", "lock the face", "regenerate this shot with that character", "thumbnail variants", "swap the background", "use flux image-to-image", "multi-reference edit", "cast lock with flux", or after a render where cast drift is visible per /critique-video. For pure T2I image gen (the existing default in every render pipeline) just use the channel's existing `image_provider: cloudrun_flux2_klein` route — this skill is for the EDITING modes, not basic generation.
---

# /image-edit — exploit FLUX.2 klein's image-to-image + multi-reference editing

## Why this skill exists

`docs/cloudrun_image.md` line 41:

> FLUX.2 klein bonus capability: T2I + image-to-image + multi-reference
> editing in one model

We've shipped 100+ Shorts with the cloud FLUX service. **None of them
have used image-to-image or multi-reference editing.** We're paying
for a 4B-parameter model and using ~⅓ of its surface area.

The three highest-leverage uses we already need:

1. **Cast-appearance lock** — every animated channel
   (`mystoriesanimated`, `hindutavaanimated`) has cast drift. Frame 3
   is a 30-yr-old man in a beard; frame 7 is a different 30-yr-old man
   in a beard. `docs/cast_appearance_lock.md` has been chasing this
   with character-prompt rigor, character cards, and seed reuse — none
   of which actually work because plain T2I doesn't condition on
   identity. **FLUX multi-reference editing does.**
2. **Thumbnail variants** — today every Short ships one thumbnail
   (frame 1 of the rendered video, sometimes a hand-crafted PNG). With
   image-to-image we can generate 5-10 stylistic variants of the same
   thumbnail in <30 s and pick the highest CTR via `/upload-via-playwright`'s
   thumbnail A/B feature.
3. **Background swap** — a series like "Eklavya through 5 chapters"
   needs the SAME Eklavya in 5 different settings. Plain T2I gives 5
   different Eklavyas. Multi-reference (face + new background prompt)
   gives 1 Eklavya in 5 places.

## How to run it

### 1. Pick the mode

```
/image-edit cast-lock      <reference-face.jpg> <shotlist.json>
/image-edit thumbnail      <rendered-shot.png>  --variants 6
/image-edit background     <subject-shot.png>   --bg "<new background prompt>"
```

Confirm in one line:

```
image-edit: mode=cast-lock  ref=hindutavaanimated/cast/eklavya_face_v1.jpg  shotlist=eklavya-archery/shotlist/eklavya-archery.json (12 shots)
```

### 2. Preflight

1. **Cloud service ready** — `curl /api/cloud/health | jq '.rows[] | select(.short==\"image-flux2-klein\")'` (or `/app/cloud` Health row for `image-flux2-klein`) must be
   green. If yellow (cold-loading), kick `POST /api/cloud/warm` (or the Warm button on the `/app/cloud` admin tab) and wait.
2. **Reference image** — at least 512×512, < 10 MB, PNG/JPG only.
   For cast-lock, must be a clean face shot (the model uses the face
   embedding as identity conditioning).
3. **Shotlist (cast-lock only)** — must be a valid `shotlist.json`
   per `pipeline/niche_schema.py`. Each shot's `prompt` field is the
   regen target.

### 3. Per-mode wiring

#### Cast-lock

For each shot in the shotlist:

```python
import requests, base64, subprocess

token = subprocess.check_output(["gcloud", "auth", "print-identity-token"], text=True).strip()
ref_b64 = base64.b64encode(open(ref_face, "rb").read()).decode()

for shot in shotlist["shots"]:
    body = {
        "prompt": shot["prompt"],
        "reference_images": [{"image_b64": ref_b64, "weight": 0.8, "role": "identity"}],
        "width": 1024, "height": 1024, "steps": 4,  # klein 4-step regime
        "guidance": 2.5,
    }
    r = requests.post(
        f"{flux_url}/edit",
        headers={"Authorization": f"Bearer {token}"},
        json=body, timeout=120,
    )
    out_path = shot_path_for(shot)
    open(out_path, "wb").write(base64.b64decode(r.json()["image_b64"]))
```

The `/edit` endpoint must exist on `cloud/image-flux2-klein/server.py`.
If it doesn't (likely — only scaffolded), this skill's first task on
first run is to add it (operator-confirmed). Pattern:

```python
# cloud/image-flux2-klein/server.py
@app.post("/edit")
async def edit(body: EditRequest):
    pipe = _load_pipe()  # already memoized
    pil_refs = [pil_from_b64(r.image_b64) for r in body.reference_images]
    out = pipe(
        prompt=body.prompt,
        image=pil_refs,                       # multi-ref input
        strength=1.0 - body.reference_images[0].weight,  # higher weight = stick closer to ref
        num_inference_steps=body.steps,
        guidance_scale=body.guidance,
        width=body.width, height=body.height,
    ).images[0]
    return {"image_b64": pil_to_b64(out)}
```

#### Thumbnail variants

```python
for i in range(n_variants):
    body = {
        "prompt": original_prompt + f", thumbnail variant {i+1}",
        "reference_images": [{"image_b64": original_b64, "weight": 0.4, "role": "style"}],
        "width": 1080, "height": 1920,  # 9:16 thumbnail
        "steps": 4, "guidance": 3.0,
        "seed": 1000 + i,  # varied seeds for diversity
    }
    # ... POST /edit, save to <slug>/thumbnails/v<i>.png
```

#### Background swap

```python
body = {
    "prompt": new_bg_prompt + " <subject preserved>",
    "reference_images": [
        {"image_b64": subject_b64, "weight": 0.7, "role": "identity"},
    ],
    "width": 1024, "height": 1024, "steps": 4, "guidance": 2.5,
}
```

### 4. Output

Write outputs to the channel's standard image path conventions
(`<channel>/<niche>/<slug>/images/<shot-id>.png` for cast-lock;
`<channel>/<niche>/<slug>/thumbnails/v<i>.png` for thumbnails;
`<channel>/<niche>/<slug>/images/<shot-id>_bg<n>.png` for background
swap).

Print a summary:

```
image-edit cast-lock: 12 shots regenerated  total=14.3s  avg=1.19s/shot
  out: hindutavaanimated/eklavya_archery/images/  (12 png)
  preview: hindutavaanimated/eklavya_archery/images/_contact_sheet.jpg
```

### 5. QC: face-similarity check (cast-lock only)

After regen, score each output against the reference face using
`facenet_pytorch.MTCNN` + `InceptionResnetV1` cosine similarity. Flag
any shot below 0.65 cosine — that shot didn't actually lock to the
reference and needs operator review (try higher `weight`, retry, or
mark as known-tradeoff in the shotlist).

```
QC: 12 shots  10 ✓  2 ⚠ below threshold
  shot 7 (cosine 0.58) — Eklavya from behind; identity hard to enforce
  shot 11 (cosine 0.61) — extreme close-up on hand, no face visible
```

### 6. Self-learning

- Append per-run results to `data/_bench/image_edit.jsonl` —
  ref-image, mode, n_outputs, avg_cosine, wall_s, cost.
- If face-similarity is consistently below 0.7 for a given character,
  recommend (a) better reference image, (b) finer prompt scaffolding,
  or (c) consider training a tiny LoRA (out of scope for this skill).
- Document one-shot fixes in `<channel>/learnings/cast_lock_<character>.md`.

### 7. Auto-invocation

- **After `/critique-video` flags cast drift** — the critic suggests
  `/image-edit cast-lock` with the offending shot list pre-resolved.
- **Manually** — for thumbnail variants and background swaps.

## Cross-references

- `docs/cast_appearance_lock.md` — the long-running drift problem this
  skill finally fixes the right way.
- `cloud/image-flux2-klein/server.py` — needs the `/edit` endpoint
  (this skill adds it on first run if missing).
- `docs/cloudrun_image.md` — confirms FLUX.2 klein supports the modes.
- `/critique-video` — surfaces cast drift; this skill fixes it.
- `/upload-via-playwright` — consumes the thumbnail variants for A/B.
