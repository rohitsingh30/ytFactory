# History Recapped — production workflow

Reproducible scripts for the **History Recapped** channel (channel slug `historyrecapped`,
display name `History Recapped`). This channel ships **100% archival war footage**
with our Cartesia narrator on top, word-by-word captions, and inline national-flag
emojis — *not* the sports format (animations + 1 footage cut at the climax).

The scripts here bypass `make_shorts.py` entirely because image-gen would be
~30 minutes of waste for a footage-only short. They drive the underlying pipeline
modules directly (`pipeline.audio`, `pipeline.beats`, `pipeline.compose`, `pipeline.upload`).

## End-to-end recipe

For a new story (e.g. `pointe-du-hoc-1944`):

1. **Author the script** — `historyrecapped/raw/<slug>.json` and
   `historyrecapped/narrations/<slug>.json`. The script must end with a
   sentence that matches the closer-CTA regex (e.g. ends in `?` or contains
   `comment your`/`comment below`). Closer pattern we ship with:
   `"LIKE if you learned something. SUBSCRIBE for more deep dives like this one. Could you have done it at <X>?"`

2. **Find HD archival source** — search YouTube for restored documentary on the
   topic. Download once, cache in
   `sportstoriesanimated/footage/sources/<video_id>.mp4`
   (the cache is currently shared across channels — see
   `feedback_footage_cache_shared`). Run whisper on the source audio to find
   visual sequences by narration cue (`scramble`, `dogfight`, `bombers`, etc.).

3. **Build silent video** — `build_100footage.sh`. Edit the `WINDOWS=(...)` array
   to your in/out times; the script trims each window with the blurred-letterbox
   9:16 filter (same as `pipeline/footage.py`), uniform 30fps, concats them, and
   writes a silent `.mp4` to `/tmp/wh_100footage/video.mp4`. Total length should
   match (or slightly exceed) the narration length you'll get in step 4.

4. **Generate audio + captions** — `regen_audio_caps.py`. Calls
   `pipeline.audio.synthesize` (Cartesia/Theo), `pipeline.beats.transcribe_words`
   (whisper-mlx), `pipeline.beats.split_into_beats`,
   `pipeline.compose.prerender_word_captions`. ~25 seconds end-to-end. Writes
   `narration.wav`, `beats.json`, and 150+ `word_NNNN.png` files into
   `historyrecapped/cache/<slug>/`. Sources `.env` for
   `CARTESIA_API_KEY`.

5. **Compose final video** — `final_v2.py`. Re-muxes narration onto the silent
   video (with `tpad` if narration runs longer), composites inline national-flag
   emojis with relevant words (FR/UK/DE flags + 🎖️ medal + 👍 LIKE + 🔔 SUBSCRIBE),
   and burns word-by-word captions. **Cut-aware clamping** ends each word's
   display 100ms before the next scene cut so captions never bleed across visual
   transitions. The script's `SCENE_CUTS` array MUST match the cumulative cuts
   defined in `build_100footage.sh`. Output:
   `historyrecapped/shorts/<slug>-100footage-v2.mp4`.

6. **Upload** — `upload_history_recapped.py` (in `scripts/`, not here). Calls
   `pipeline.upload.upload_short` with `skip_critic=True` and
   `privacy_override="public"`. Token cached at
   `~/.config/ytfactory/youtube_token_historyrecapped.json` after the one-time auth
   via `auth_history_recapped.py`.

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
Cached at `/tmp/wh_emoji/`. Codepoints in active use:
- `1f1eb-1f1f7` 🇫🇷 / `1f1ec-1f1e7` 🇬🇧 / `1f1e9-1f1ea` 🇩🇪
- `1f396` 🎖️ / `1f44d` 👍 / `1f514` 🔔

## Why not `make_shorts.py`?

The hybrid path runs image-gen for every animated beat (~30 min for 18-20 beats
on M2 Max). For a footage-only channel that's pure waste. The direct pipeline-module
chain runs the same TTS + whisper + beat-split + word-PNG render in **~25 seconds**
and gives full control over the visual track (we hand-cut footage windows
instead of generating illustrations).

The hybrid path is still fine for channels where animations are the format
(SportsStoriesAnimated, MyStoriesAnimated, AITA-class). History Recapped pivoted
*away* from that during initial dev — see
`memory/feedback_history_recapped_format.md`.
