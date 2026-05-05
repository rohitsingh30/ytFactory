# ytFactory TTS — Cloud Run GPU service

Hosts TTS inference (F5-TTS in v1; Higgs Audio v2 + CosyVoice 2 +
Chatterbox in v2) on NVIDIA L4 GPU in `asia-southeast1` (Singapore).

## Why this exists

The local pipeline runs F5-TTS-MLX on M2 Max — fast for Shorts, slow
for long-form (1.5-9 hr per render). This service moves heavy TTS to a
real NVIDIA GPU at ~5-10× M2 Max wall-clock, freeing the laptop for
image generation + authoring during long renders. Local providers
remain in `pipeline/tts/` and are used as automatic fallback if this
service is unavailable.

## Layout

```
cloud/tts/
  Dockerfile          CUDA 12.4 + Python 3.12 + torch cu124 + f5-tts
  requirements.txt    Python deps (torch installed separately for cu124)
  server.py           FastAPI app — /healthz, /readyz, /synth
  handler.py          Model router (mirrors pipeline/audio.py::synthesize)
  models/
    __init__.py
    f5.py             F5-TTS PyTorch synth (v1)
    # Phase 3:
    # higgs.py        Higgs Audio v2 (lazy-imported)
    # cosyvoice.py    CosyVoice 2 (lazy-imported)
    # chatterbox.py   Chatterbox (lazy-imported)
  deploy.sh           build + push + deploy convenience script
  README.md           this file
```

## Quick deploy

```bash
cd cloud/tts
./deploy.sh           # tag = timestamp
./deploy.sh v2        # explicit tag
```

The deploy script reads `${GCP_PROJECT:-ytfactory-prod}` and
`${GCP_REGION:-asia-southeast1}` from env. Override only when running
against a non-prod project.

## API contract

`POST /synth` request:

```json
{
  "model": "f5",
  "text": "Hello world.",
  "ref_audio_b64": "<base64 of 24kHz mono WAV, 5-15s>",
  "ref_text": "Transcript of the reference audio.",
  "speed": 1.0,
  "seed": null,
  "output": "inline",
  "gcs_object_prefix": "cosmosdecoded/eddington-1919/chunk-007-"
}
```

Response (inline path):

```json
{
  "model": "f5",
  "duration_s": 12.4,
  "wall_s": 4.1,
  "rtf": 0.331,
  "sha256": "…",
  "output_inline": "<base64 wav>"
}
```

Response (gcs path, automatic for WAVs >5 MB):

```json
{
  "model": "f5",
  "duration_s": 38.2,
  "wall_s": 12.7,
  "rtf": 0.332,
  "sha256": "…",
  "output_gcs": "gs://ytfactory-tts-io/cosmosdecoded/eddington-1919/chunk-007-abc123.wav"
}
```

## Auth

Service is deployed `--no-allow-unauthenticated`. Callers must attach
a Google ID token for the service URL:

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" "${URL}/healthz"
```

The laptop-side `pipeline/tts/cloudrun.py` provider handles this
automatically using the gcloud Application Default Credentials.

## Cost

| Volume | GPU hr/mo | Cost/mo (₹) |
|---|---:|---:|
| Today (~13 videos) | ~1.7 | ~₹200 |
| Full throttle (~138 videos) | ~14 | ~₹1,250 |

Quota-of-5 GPUs is **free**; only active GPU-seconds are billed. Idle
cost = ₹0. See `docs/cloudrun_tts.md` for budget alert + auto-disable.

## Rollback

Single env-var on the laptop:

```bash
unset CLOUDRUN_TTS_URL
```

`pipeline/tts/cloudrun.py` raises a clear error → channel configs fall
back to their local `tts_provider` (`f5_tts`, `kokoro`, `chatterbox`).
No code change, no redeploy.
