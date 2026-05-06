# ytfactory-tts-cosyvoice (Cloud Run GPU)

CosyVoice 2 (FunAudioLLM, Apache 2.0) hosted on Cloud Run L4 GPU in
`asia-southeast1`. **Standalone service** — does not share an image
with the main `ytfactory-tts` service that hosts F5 / Higgs /
Chatterbox.

## Why a separate service

CosyVoice's dep tree pins:

- `torch 2.3.1+cu121`
- `openai-whisper` from source
- `deepspeed`, `lightning`, `gradio`
- `tensorrt` (optional; we disable `load_trt` at runtime)

The main service runs `torch 2.4.1+cu124` for F5/Higgs/Chatterbox.
After 5 build iterations trying to coexist, isolating CosyVoice in
its own container with its own native deps was the cleaner path. See
the comments at the top of [`Dockerfile`](./Dockerfile) for the full
rationale.

## Files

| File | Purpose |
| --- | --- |
| `Dockerfile` | CUDA 12.1 devel base + torch cu121 + CosyVoice repo + weights |
| `requirements.txt` | Our pins (FastAPI, GCS). CosyVoice's own pins live in `/opt/CosyVoice/requirements.txt` and are layered after ours. |
| `server.py` | FastAPI: `/healthz`, `/readyz`, `/synth` — same contract as `cloud/tts/server.py` |
| `handler.py` | Single-model router (only `"cosyvoice"`) |
| `models/cosyvoice.py` | Singleton wrapper around `cosyvoice.cli.cosyvoice.CosyVoice2` |
| `deploy.sh` | `gcloud builds submit` + `gcloud run deploy` |

## Build + deploy

```bash
cd cloud/tts-cosyvoice
./deploy.sh v1
```

The default `--max-instances=2` leaves 3 of the 5-GPU pool free for
the main `ytfactory-tts` service.

## Endpoints

- `GET /healthz` — liveness, no GPU touch.
- `GET /readyz` — warms the CosyVoice singleton (~5-8 s on L4).
- `POST /synth` — body: `{model:"cosyvoice", text, ref_audio_b64,
  ref_text, speed, seed, output:"inline"|"gcs"}`. Returns either
  `output_inline` (base64 WAV ≤5 MB) or `output_gcs` (`gs://` URI).

`ref_text` is **required** — CosyVoice's zero-shot mode needs the
reference transcript.

## Laptop side

Set `CLOUDRUN_TTS_COSYVOICE_URL` in `.env`:

```
export CLOUDRUN_TTS_COSYVOICE_URL=https://ytfactory-tts-cosyvoice-XXXXXXX.asia-southeast1.run.app
```

If unset, `pipeline/tts/cloudrun.py::_synth_cloudrun_cosyvoice`
falls back to `CLOUDRUN_TTS_URL` (which today returns 400 because the
main service doesn't ship cosyvoice yet — useful for catching
mis-configuration during rollout).

See [`docs/cloudrun_tts.md`](../../docs/cloudrun_tts.md) for the
operational runbook (auth, smoke tests, cost, rollback).
