# History Recapped — production workflow

Reproducible scripts for the **History Recapped** channel (channel slug `historyrecapped`,
display name `History Recapped`). This channel ships **100% archival war footage**
with our narrator on top, word-by-word captions, and inline national-flag
emojis — *not* the sports format (animations + 1 footage cut at the climax).

The channel's `config.yaml` sets `render_style: footage_only` which routes
`workers/heavy/render_short.py` through `historyrecapped/scripts/render_footage_only.py`
instead of the standard image-gen path. Image-gen is skipped entirely.

## End-to-end recipe

For a new story (e.g. `pointe-du-hoc-1944`):

1. **Author the script** — write `historyrecapped/raw/<slug>.json` (story facts +
   metadata) and `historyrecapped/narrations/<slug>.json` (hook + narration text +
   3 title options). The narration MUST end with the channel closer pattern (see
   `historyrecapped/learnings/closer.md`):
   `"… LIKE to honor those who served. SUBSCRIBE for more such stories."`
   Do **not** use the old "COMMENT below — which battle next?" closer.

2. **Find HD archival source** — search YouTube for restored documentary on the
   topic. Download once with yt-dlp (cached at
   `sportstoriesanimated/footage/sources/<video_id>.mp4` — currently shared across
   channels).

3. **Pick 5-7 footage windows AT 1fps** — write `historyrecapped/shotlist/<slug>.json`:
   ```json
   {
     "slug": "<slug>",
     "source_url": "https://www.youtube.com/watch?v=<id>",
     "windows": [
       {"in_s": 11.0, "out_s": 18.0, "match_text": "<narration line this clip pivots on>"},
       …
     ]
   }
   ```
   **Critical:** before committing in_s/out_s, sample each candidate minute at
   1fps and read the resulting tile grid:
   ```bash
   ffmpeg -ss 440 -t 120 -i src.mp4 -vf "fps=1,scale=160:-1,tile=10x12" /tmp/m_440.png
   ```
   Picking from a coarser 60s-cell montage routinely lands on talking-head
   presenters or modern documentary text-overlay B-roll. Pointe du Hoc v0 had 2/7
   bad windows because they were chosen from a 60s scan.

4. **Render** — one command does TTS, whisper alignment, word PNG render, ffmpeg
   trim/concat/blurred-letterbox 9:16, narration mux with `tpad`, inline-flag
   composite, and caption burn with cut-aware clamping:
   ```bash
   .venv/bin/python historyrecapped/scripts/render_footage_only.py \
       --channel historyrecapped --slug <slug>
   ```
   Output: `historyrecapped/shorts/<slug>.mp4`. Total runtime ~1-2 min.

5. **Re-rendering after a narration edit** — word PNGs are keyed by index, not
   content hash, so a stale cache produces mismatched captions. Clear the cache:
   ```bash
   rm historyrecapped/cache/<slug>/{narration.wav,narration.voice.json,beats.json,prompts.json,word_*.png}
   ```
   then re-run the render. Footage clips (`clip_*.mp4`, `video.mp4`) are fine to keep.

6. **Upload** — slug-aware:
   ```bash
   .venv/bin/python historyrecapped/scripts/upload.py \
       --slug <slug> \
       --title "<chosen title from narrations/<slug>.json title_options>" \
       --privacy public
   ```
   Calls `pipeline.upload.upload_short` with `skip_critic=True` (manual builds
   don't have a critic.json on disk). Token cached at
   `~/.config/ytfactory/youtube_token_historyrecapped.json` after the one-time auth
   via `auth.py`. Record written to `historyrecapped/uploads/<slug>.json`.

## Branding

`branding.py` renders the 800×800 channel icon and 2560×1440 banner into
`historyrecapped/branding/`. PIL-only, no diffusion. Big Caslon
serif on khaki/sepia + grain + vignette + gold medal mark. Banner content
fits inside YouTube's mobile-safe 1546×423 centred band so nothing crops on
phones.

## Emoji rendering note

Apple Color Emoji.ttc is a sbix bitmap font; Pillow's freetype build on macOS
renders it blank when `embedded_color=True`. We sidestep by using **twemoji**
PNGs from `cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/` (CC-BY-4.0).
Codepoints in active use:
- `1f1eb-1f1f7` 🇫🇷 / `1f1ec-1f1e7` 🇬🇧 / `1f1e9-1f1ea` 🇩🇪
- `1f396` 🎖️ / `1f44d` 👍 / `1f514` 🔔

## Why not `make_shorts.py`?

The hybrid path runs image-gen for every animated beat (~30 min for 18-20 beats
on M2 Max). For a footage-only channel that's pure waste. `render_footage_only.py`
runs the same TTS + whisper + beat-split + word-PNG render in **~1-2 min total**
and gives full control over the visual track (we hand-cut footage windows
instead of generating illustrations).

## Legacy scripts

`build_100footage.sh`, `regen_audio_caps.py`, `final_v2.py` are pre-generalization
scripts kept for reference. They were per-slug (originally `battle-of-britain-few`)
and have been superseded by the generic `render_footage_only.py`. Don't add new
work to them — extend `render_footage_only.py` instead.

## Episodes shipped

| slug | title | URL |
| --- | --- | --- |
| `battle-of-britain-few` | The 2,937 men who saved Britain | https://youtube.com/shorts/bc7lopoWSY4 |
| `pointe-du-hoc-1944` | The most impossible mission of D-Day | https://youtube.com/shorts/qqOU32B4p3Q |
| `dunkirk-1940` | The 9 days that saved the British army | https://youtube.com/shorts/icUNo0DQudw |
| `pearl-harbor-1941` | What really happened at Pearl Harbor | https://youtube.com/shorts/NfaaobtwC_A |
| `omaha-beach-1944` | What really happened on Omaha Beach | https://youtube.com/shorts/3n_6oUFWZMo |
| `midway-1942` | The five minutes that won the Pacific war | https://youtube.com/shorts/W8_h-1UeGIE |
| `bastogne-1944` | The one word that saved Bastogne | https://youtube.com/shorts/Nk3YMq0BE2Y |
| `kursk-1943` | The largest tank battle in history | https://youtube.com/shorts/Ae-GM9kaiJE |
| `stalingrad-uranus-1942` | How the Red Army trapped 300,000 Germans at Stalingrad | https://youtube.com/shorts/OaVNwb01VlQ |
| `market-garden-1944` | Why Arnhem was a bridge too far | https://youtube.com/shorts/7OgyVExGvXI |
| `reichstag-1945` | The flag over the Reichstag — what really happened | https://youtube.com/shorts/r_Mrold1DNg |
| `guadalcanal-1942` | The first ground Japan ever lost | https://youtube.com/shorts/nk3NU3dL6RM |
