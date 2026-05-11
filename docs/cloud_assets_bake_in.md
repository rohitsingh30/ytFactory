# Cloud asset bake-in pattern (`data/<subdir>/` → Cloud Run image)

> **Established 2026-05-11** during the music-beds + Suno song-samples
> deploy. The 5 new procedural music beds + 2 Suno preview MP3s would
> have shipped as empty dirs without the matching ignore-file
> re-includes — three places need the same coordinated change.

## TL;DR

Three places need the same edit when baking a new shared `data/<subdir>/`
into a Cloud Run image:

1. **`Dockerfile`** — explicit `COPY data/<subdir>/ /workspace/data/<subdir>/`.
2. **`.dockerignore`** — `!data/<subdir>/` + `!data/<subdir>/**` re-include.
3. **`.gcloudignore`** — same two `!` lines.

Skipping (3) is the silent-failure trap: cloudbuild strips the dir
from the build context, the COPY succeeds copying *an empty
directory*, the container starts cleanly, and the route serves 404 on
every asset request with no error logged anywhere.

## Why the blanket `data/*` exclude exists

`data/` holds per-channel state (cache/, scratch/, telemetry/, research
dumps, intermediate renders) that's easily multi-GB. Shipping it all
to cloud would balloon the build context, slow every deploy, and bake
laptop-only artefacts into prod images. The blanket `data/*` exclude
in `.dockerignore` + `.gcloudignore` is intentional and not negotiable.

The escape hatch is per-subdir `!`-include exceptions, applied to the
small static assets the cloud services genuinely need.

## Concrete pattern

`.dockerignore`:

```gitignore
# Heavy data — never ship to Cloud Run (with surgical re-includes below).
data/*
workers/
sources/
agent/
shared/
tests/
docs/

# Re-include the mirrored channel-asset subdir (avatars/banners) so
# /api/channels surfaces real channel faces.
!data/research/
data/research/*
!data/research/channel_assets/
!data/research/channel_assets/**

# Procedural music beds + Suno song-vocal previews — small static assets
# the create-page Customize step relies on for /api/music/sample/* and
# /api/songs/sample/*. ~5 MB total; reproducible via
# scripts/build_music_beds.py + scripts/build_song_samples.py.
!data/music/
!data/music/**
!data/song_samples/
!data/song_samples/**
```

`.gcloudignore` mirrors the same `!` block.

`cloud/web-server/Dockerfile`:

```dockerfile
COPY data/research/channel_assets/ /workspace/data/research/channel_assets/

# Curated music beds + Suno-generated song-vocal previews are read
# by /api/music/sample/* and /api/songs/sample/* respectively. ~5 MB
# total. Without these, the create-page Music-bed catalogue and the
# SongPicker Female/Male preview tiles 404 in cloud.
COPY data/music/         /workspace/data/music/
COPY data/song_samples/  /workspace/data/song_samples/
```

`cloud/render-worker-v2/Dockerfile` (only needs `data/music/` for the
Stage 5.25 music-bed mixer):

```dockerfile
COPY data/music/    /workspace/data/music/
```

## Verification — distinguish 404 (missing file) from 401 (auth)

After every deploy, smoke-test the asset URL with `curl -sI`:

```bash
$ curl -sI https://ytfactory-web-...run.app/api/songs/sample/female.mp3
HTTP/2 401   ← auth gate caught it; route reached the handler; FILE PRESENT
HTTP/2 404   ← route reached but FastAPI couldn't find the file in container
HTTP/2 502   ← route handler crashed (separate problem, not asset bake-in)
```

The 401 vs 404 distinction is the cheapest cloud-side asset health
check. If you get 404 after a deploy that "added" an asset, the
ignore-file re-include is the first thing to check.

## Sibling examples in the repo

| asset dir                       | service that bakes it       | route                                    |
|---------------------------------|------------------------------|------------------------------------------|
| `data/research/channel_assets/` | `ytfactory-web`              | `/api/channels/{ch}/avatar.jpg`          |
| `data/music/`                   | `ytfactory-web` + render-worker | `/api/music/sample/{shared}/{key}.mp3`   |
| `data/song_samples/`            | `ytfactory-web`              | `/api/songs/sample/{filename}`           |

Add a row to this table whenever you onboard a new asset dir — the
table doubles as the smoke-check matrix after deploys.

## When NOT to bake an asset into the image

- **Per-render artefacts** (cache/, scratch/, intermediate/, telemetry/) —
  these belong on GCS at runtime, not in the image. See
  `docs/youtube_stats_refresh.md` for the per-account research-cache
  pattern (GCS read at runtime, never baked).
- **Anything > 50 MB** — pushes deploy times into 5+ min territory and
  adds noise to every redeploy. Mount via GCS Fuse or fetch on first
  request instead.
- **Anything user-generated or per-account** — must live on GCS so it
  survives image rebuilds and stays consistent across replicas.

## Cross-references

- `feedback_cloud_dockerignore_data_reinclude.md` — terse memory pointer
- `feedback_dual_deploy_after_niche_schema_change.md` — cloud-deploy companion rule
- `cloud/web-server/Dockerfile` — reference implementation
- `.dockerignore` + `.gcloudignore` — current re-include lines
