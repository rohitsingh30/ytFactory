# Studio website personality — design contract (2026-05-09)

The studio used to be a clinical Vercel-style admin dashboard: monogram
avatars, no social proof, no preview, a 4-step wizard with dropdowns.
This rewrite gave it a real creator's-command-center feel: real YT
avatars + banners, real subs/views, audition-before-commit catalogues,
collapsed wizard.

## Architecture

Three layers, each independently testable.

### Layer 1 — backend personality data

`/api/channels` now returns full personality: `avatar_url`, `banner_url`,
`subscribers`, `youtube_video_count`, `total_views`, `recent_videos[3]`,
`youtube_url`, `custom_url`. Pulled from the cached YT payload at
`data/research/youtube/<account>.json` (no live YT call per request).

Two refresh paths:

```bash
# Refresh stats + mirror brand images (one-shot)
.venv/bin/python -m pipeline.research.youtube

# Single channel
.venv/bin/python -m pipeline.research.youtube --account mystoriesanimated

# Skip image mirror (e.g., when offline)
.venv/bin/python -m pipeline.research.youtube --no-assets
```

The refresh:
1. Calls `channels.list?part=snippet,statistics,contentDetails,brandingSettings`
   — `brandingSettings` is the same quota cost as the existing call, so
   adding the avatar + banner URLs is free.
2. Mirrors the avatar (≈800px) + banner (≈2560×1440) into
   `data/research/channel_assets/<account>/{avatar.jpg,banner.jpg}` —
   served by `/api/channels/{key}/{avatar,banner}.jpg` so the UI never
   hot-links yt3.ggpht.com (CORS / link-rot).
3. Auth-less channels (rhymetimejunction, scrollpulse today) silently skip;
   the endpoint returns 404 and the FE falls back to a per-niche
   gradient + monogram chip — no broken UI.

New companion endpoint: `/api/channels/{key}/inspiration` returns the
3 most-recent shorts on this channel for the InspirationDrawer (audition
the channel's actual voice/visuals before tweaking knobs).

New music catalogue: `/api/music/catalog` enumerates curated beds under
`<channel>/{music,branding/music,songs}/`. Streams via
`/api/music/sample/{channel}/{filename}` — same caching contract as the
voice-sample endpoint.

### Layer 2 — UI primitives

Composable, reusable, all under `web-next/components/app/`:

| Primitive | What it does |
|---|---|
| `ChannelAvatar` | Round YT-style avatar (`/api/channels/<key>/avatar.jpg`) with monogram fallback when 404 |
| `ChannelBanner` | Wide 16:5 / 16:7 banner; mirrored YT banner with per-channel gradient fallback |
| `ChannelStatStrip` | subs · videos · views inline (auto-hides null cells) |
| `ChannelHeroCard` | Banner + avatar punch-out + tagline + stats + 3 latest thumbs + provider chips. The canonical channel face. |
| `AudioSampleButton` | Round play/pause with auto-mutex via `audio-bus.ts` (only one plays at a time across the page) |
| `PreviewableTile` | Generic "audition-then-commit" tile — used for voices, music beds |
| `InspirationDrawer` | Slide-out side panel with 3 inline-playable YT iframes for the picked channel |
| `channel-meta.ts` | Single source for tones, monograms, labels, `fmtCount` (1.2K / 4.5M) |

`ChannelIcon` is now a thin re-export of `ChannelAvatar` — every
existing call-site picks up the personality treatment for free.

### Layer 3 — pages

| Page | Change |
|---|---|
| `/app/create` | **4 steps → 2.** Step 1 = ChannelHeroCard gallery with inline variant chips (channel + variant in one click). Step 2 = knob deck + live "looks like" preview pane with InspirationDrawer + `?channel=<key>` deep link |
| `/app/channels` | New language filter; ChannelHeroCard grid replaces thin tiles |
| `/app/channels/[channel]` | Hero (banner + avatar + StatStrip + Open-on-YT) + Latest 6 Shorts + Recent Renders + Pipeline Defaults + Override Defaults editor |
| `/app` (Dashboard) | Channel section uses ChannelHeroCard (3-4 cols) — every monogram now has a face |
| `/app/library`, `/app/queue` | Pick up real avatars via the `ChannelIcon → ChannelAvatar` shim |
| `/` (Landing) | `ChannelBand` rebuilt: real avatars + sub counts in a 4-col grid (was a wordmark rail). Falls back to wordmarks if `/api/channels` is cold. |

## Key fix that surfaced (CLASS-OF-BUG)

`pipeline/research/youtube.py` had been writing/reading from
`pipeline/data/research/youtube/` (wrong path: `parent.parent` from
`pipeline/research/youtube.py` = `pipeline/`, not the project root)
while every other consumer (`pipeline.research.aggregator`,
`web/server.py`, `pipeline.customization`) read from the canonical
`data/research/youtube/`. Result: `fetch_account()` "succeeded" but the
data never reached the website.

**Fix**: import `PROJECT_ROOT`/`RESEARCH_DIR` from the canonical
`pipeline.paths` module instead of recomputing path math locally.
`pipeline/research/channel_assets.py` follows the same pattern.

**Memory note**: any new module under `pipeline/research/` (or any
≥2-deep package) that needs project paths MUST import from
`pipeline.paths`, never recompute via `Path(__file__).parent.*`.

## Refreshing data

```bash
# After auth-ing a new channel (e.g., rhymetimejunction):
.venv/bin/python -m pipeline.research.youtube --account rhymetimejunction

# After adding a new curated music bed:
# Just drop the file into <channel>/music/ — /api/music/catalog re-scans on each request.

# After editing a channel's tagline / language in pipeline/customization.py CHANNEL_REGISTRY:
# Restart the control plane — the registry is module-level.
```

## Operational gotcha — never `npm run dev` after a `npm run build`

Next 14 dev mode and prod mode write incompatible chunks into the same
`.next/` directory. If a `npm run build` ran in the same checkout
(leaves `.next/BUILD_ID` + production chunks behind) and then dev
starts, webpack-runtime tries to load chunks like `./579.js` that prod
emitted but dev didn't, surfacing as:

```
Server Error: Cannot find module './579.js'
Require stack: …/web-next/.next/server/webpack-runtime.js
```

The repo's `scripts/predev-guard.mjs` already wipes `.next/` on
`npm run dev` start; it doesn't help if dev was started, then build
ran in another shell, then dev was reused. **Workflow rule**: always
`rm -rf web-next/.next` between flipping modes. Use either dev OR build
in a given shell — not both.

## Rollback

The personality fields are all optional + nullable on the FE. Reverting
the backend (delete `data/research/channel_assets/`, set every
`avatar_url`/`banner_url` to `None`) gracefully degrades every UI to
monograms + gradients — no broken pages, no stack traces.
