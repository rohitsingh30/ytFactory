# cloud/_bench/

Cloud Run service scaffolds that are **not on the production path**.
Kept around for revival — re-run `<dir>/deploy.sh` to push to
`ytfactory-prod-v2` when one of these is needed.

## What's here

| Dir | Status | Why parked |
|---|---|---|
| `tts-f5/` | bench | English F5-TTS; superseded on prod by `tts-chatterbox` (cloud) |
| `tts-higgs/` | bench | Higgs Audio v2 3B; emotional/multilingual but cold-load thrash (see [`docs/cloudrun_higgs.md`](../../docs/cloudrun_higgs.md)) |
| `tts-cosyvoice/` | bench | CosyVoice 2; voice-clone alt to Chatterbox |
| `tts-indicparler/` | bench | Indic Parler-TTS for 21 langs; superseded for Hindi by `tts-indicf5` (IndicF5 wins all Hindi cells per voice-matrix bench 2026-05-08) |
| `image-z-image-turbo/` | WIP | Z-Image-Turbo 6B parity lane; cold-load reliability work pending (see memory: `feedback_zimage_cloudrun_coldload_stall.md`) |
| `image-hidream/` | scaffold | HiDream-I1 Full 17B; never deployed |
| `image-qwen/` | scaffold | Qwen-Image; photoreal + faces + in-image text. Never deployed |

## Live production services live one level up at `cloud/<svc>/`

The production matrix as of the 2026-05-09 cutover:

- `cloud/tts-chatterbox/` — English production TTS
- `cloud/tts-indicf5/` — Hindi production TTS
- `cloud/image-flux2-klein/` — production image gen (FLUX.2 [klein] 4B)
- `cloud/render-worker-v2/` — render JOB
- `cloud/web-server/` — orchestrator (`ytfactory-web`)
- `cloud/clone-video-worker/` — video clone JOB
- `cloud/cobalt-api/` — cobalt downloader
- `cloud/weights-staging/` — weights staging JOB
- `cloud/web-next/` — Next.js frontend (deploy pending)

## Reviving a bench service

1. `cd cloud/_bench/<svc>/`
2. Inspect/edit `deploy.sh` (defaults already target `ytfactory-prod-v2`)
3. `./deploy.sh`
4. Move the dir back up to `cloud/<svc>/` once it's on the production path
