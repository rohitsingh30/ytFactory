# Canonical channel layout

Every YouTube channel in this repo MUST match this structure. The single
source of truth in code is [`pipeline/paths.py`](../pipeline/paths.py)
(`RenderPaths` dataclass + `Subdir` enum); every disk path in the
pipeline derives from it. **Never hardcode a subdir name.**

## Layout

```
<channel>/
  config.yaml                       # always present (channel-wide defaults)
  variants/<v>.yaml                 # opt — overlay YAMLs (style overlays)

  raw/[<niche>/]<slug>.json         # tracked — pulled stories
  narrations/[<niche>/]<slug>.json  # tracked — LLM-authored narration
  cast/[<niche>/]<slug>.json        # gitignored — narrator/character roles
  shotlist/[<niche>/]<slug>.json    # tracked — footage timeline
  uploads/[<niche>/]<slug>.json     # tracked — YouTube upload record
  uploads/[<niche>/]<slug>.x.json   # tracked — X upload sidecar
  shorts/[<niche>/]<slug>.mp4       # gitignored — final 9:16 mp4
  shorts/[<niche>/]<slug>.thumb.png # gitignored — auto-thumb
  long_form/<slug>.mp4              # gitignored — final 16:9 mp4
  cache/<slug>/...                  # gitignored — per-slug regen artifacts
  scratch/                          # gitignored — ephemeral build dirs
  critiques/<slug>/                 # frames gitignored, JSONs/MDs tracked

  branding/                         # *.png gitignored — channel art sources
  scripts/                          # tracked — channel-specific Python tools
  learnings/                        # tracked — channel docs

  footage/                          # opt — for footage-only channels
    sources/                          # gitignored, large — Shorts mp4s
    long_sources/                     # gitignored, large — long-form mp4s
    transcripts/                      # gitignored — Whisper transcripts of footage
  footage_plan/<slug>.json          # opt — sports — per-doc footage plan (tracked)
  music/                            # opt — royalty-free background tracks
  songs/<slug>.wav                  # opt — sung audio (rhymetimejunction; gitignored)
  emoji/                            # opt — twemoji cache (gitignored)
```

The `[<niche>/]` segment between channel root and per-slug subdir is
present iff the channel uses niches. **A channel uses niches everywhere
or nowhere; no mixed.** (Sports is the documented exception — it has
both parent-channel content at the channel root AND a `ranked/` niche.)

## Niche rule

Niche dirs nest **between channel root and the per-slug subdir name**:

```
mystoriesanimated/reddit_amitheasshole/narrations/<slug>.json   ✅ canonical
mystoriesanimated/reddit_amitheasshole/uploads/<slug>.json      ✅ canonical
mystoriesanimated/uploads/reddit_amitheasshole/<slug>.json      ❌ legacy (Gen 2 — migrated 2026-05-05)
mystoriesanimated/narrations/<slug>.json                        ❌ legacy (Gen 1 — migrated 2026-05-05)
```

Channel-wide subdirs (`config.yaml`, `variants/`, `learnings/`,
`scripts/`, `branding/`, `music/`, `songs/`, `emoji/`, `footage/`,
`footage_plan/`) **never** nest under a niche — multiple niches share
them.

[`pipeline/niches.py:NICHE_CHANNEL`](../pipeline/niches.py) is the
source of truth for `(variant_yaml → channel, niche)` mapping. Every
variant YAML in `<channel>/variants/` should appear in NICHE_CHANNEL;
unregistered variants fall back to a flat layout with a loud warning.

## Channels using niches

| Channel               | Niche dirs                                                                                                                  |
|-----------------------|-----------------------------------------------------------------------------------------------------------------------------|
| mystoriesanimated     | reddit_amitheasshole, reddit_tifu, reddit_maliciouscompliance, today_in_history, wiki_oddities, wiki_misconceptions, aita_cooking |
| sportstoriesanimated  | ranked + parent (mixed-niche exception: parent docs at root, `ranked/` niched) |

## Channels that are flat

airecap, cosmosdecoded, hindutavaanimated, historyrecapped,
rhymetimejunction.

## Cross-channel state under `data/`

Only genuinely cross-channel things live under `data/`:

| Path              | What                                                            |
|-------------------|-----------------------------------------------------------------|
| `data/cache/`     | ML model weight cache (Kokoro, F5, Whisper) — shared across renders |
| `data/research/`  | YouTube research aggregate (cross-channel competitor analysis)  |
| `data/telemetry/` | Per-render telemetry JSONL                                      |
| `data/_bench/`    | TTS A/B benchmark output (gitignored)                           |

Anything per-render (raw, narration, cast, shotlist, upload record,
mp4, thumb, image cache, scratch, critique) lives under the channel
root, NOT under `data/`. The legacy `data/intermediate/<channel>/...`,
`data/critiques/<slug>/`, and `data/shorts/<slug>.mp4` locations were
decommissioned in the 2026-05-05 layout cleanup.

## Use `RenderPaths` — never hardcode

```python
from pipeline.paths import RenderPaths, Subdir

# Flat channel
p = RenderPaths.for_channel("historyrecapped")
p.narration_for("battle-of-britain-few")
# → /Users/rohit/ytFactory/historyrecapped/narrations/battle-of-britain-few.json

# Niched channel
p = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
p.upload_record_for("amitheasshole-aita-for-something")
# → /Users/rohit/ytFactory/mystoriesanimated/reddit_amitheasshole/uploads/amitheasshole-aita-for-something.json

# Resolve from a YAML path (uses NICHE_CHANNEL):
p = RenderPaths.from_channel_yaml("mystoriesanimated/variants/aita_animated.yaml")
# → RenderPaths(channel="mystoriesanimated", niche="reddit_amitheasshole", ...)

# Resolve from a compound channel_dir string (legacy callers):
p = RenderPaths.from_channel_dir("mystoriesanimated/reddit_amitheasshole")

# Bulk-create the dirs you'll write to
p.ensure_dirs(Subdir.NARRATIONS, Subdir.UPLOADS, Subdir.CACHE)
```

`RenderPaths.from_channel_yaml` recognises FOUR layouts (added the
two central-config patterns 2026-05-10 after `cake-orch-v6` crashed
all the way at the end of a 20-min render with `cannot resolve
channel from yaml path`):

| pattern | example | resolves to |
|---|---|---|
| `<channel>/config.yaml` | `mystoriesanimated/config.yaml` | `mystoriesanimated` (flat) |
| `<channel>/variants/<v>.yaml` (in `NICHE_CHANNEL`) | `mystoriesanimated/variants/aita_animated.yaml` | `mystoriesanimated/reddit_amitheasshole` |
| `pipeline/channels/<slug>.yaml` (central, post-2026-05-10) | `pipeline/channels/mystoriesanimated.yaml` | `mystoriesanimated` |
| `pipeline/variants/<slug>/<v>.yaml` (central) | `pipeline/variants/mystoriesanimated/aita.yaml` | `mystoriesanimated` |

The cloud worker's `_channel_yaml_for()` returns the third form
(via `pipeline.channels._channel_yaml_path`); the renderer subprocess
then passes that path to `RenderPaths.from_channel_yaml`. Pre-fix
the resolver only knew the first two patterns and the renderer
crashed AFTER all 22 images + the mp4 had landed on disk. The
regression test in `tests/test_layout_parity.py::test_central_layout_yaml_resolves`
asserts every pattern resolves to the same channel root.

## Adding a new channel

1. `mkdir <channel>/` and write `<channel>/config.yaml`.
2. Decide flat-or-niched. If niched, create the niche dirs and add
   entries to `pipeline/niches.py:NICHE_CHANNEL`.
3. The renderers + dashboard + scheduler will auto-discover the channel
   on next run — no other registration needed.
4. The parity test ([`tests/test_layout_parity.py`](../tests/test_layout_parity.py))
   asserts the channel has at minimum a `config.yaml`. Per-slug subdirs
   are created lazily on first render.

## Adding a new niche

1. Create the niche dir under the channel: `mkdir <channel>/<niche>/`.
2. Add an entry to `NICHE_CHANNEL`:
   ```python
   "<niche_key>": ("<channel>/<niche>", "<channel>/variants/<variant>.yaml"),
   ```
3. The variant YAML in `<channel>/variants/<variant>.yaml` declares the
   niche's source adapter, TTS provider, visual style, etc.
4. Multiple variant YAMLs can map to the same niche dir if they share
   content (style overlays). Different content → different niche.
