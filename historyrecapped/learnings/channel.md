---
name: History Recapped channel
description: Documentary war-history channel shipping 100% archival footage with our narrator on top. 12 episodes live as of 2026-05-04 (2 prior + 10-pack from 2026-05-04 batch). Channel slug + display name both `historyrecapped` / "History Recapped" — every path/account/yaml uses the channel name.
type: project
originSessionId: d8b4c272-4f1a-4370-83ff-3d002b51108d
---
History Recapped — documentary war-history Shorts. **Slug `historyrecapped` everywhere** (`historyrecapped/config.yaml`, `historyrecapped/{raw,narrations,cache,shorts,uploads,branding}/`, OAuth account=historyrecapped, niche key historyrecapped). YouTube display name "History Recapped".

**Why:** User pivoted on 2026-05-03 from a Hindi Mahabharat concept → war history with sports's footage-cut-in pipeline → 100% archival footage. Sports-style hybrid (illustrated plates + 1 cut at climax) was rejected as "no suspense format in this type of shorts". The right format is pure archival timeline (5-7 footage windows concat'd) with our Cartesia narrator + word captions + inline national-flag emojis. See `format.md` + `closer.md` for the rules. Footage scrub + caption-emoji density rules: see `footage_scrub_watermarks.md` (channel-local) and `/docs/shorts_caption_emoji_density.md` (cross-channel). Footage scrub + caption-emoji density rules: see `footage_scrub_watermarks.md` (channel-local) and `/docs/shorts_caption_emoji_density.md` (cross-channel).

**How to apply:**
- Channel YAML: `historyrecapped/config.yaml`. Display name `History Recapped`. `render_style: footage_only` routes through `scripts/historyrecapped/render_footage_only.py` instead of image-gen. TTS (Shorts): F5-TTS-MLX cloning a 9.5s ref clip of Cartesia/Theo at `pipeline/voice_refs/theo.wav`, speed 0.95 (migrated 2026-05-04 from cartesia/Theo after $5 prepay exhausted in <1h; Cartesia retained as commented fallback in YAML). Long-form sleep mode stays on Kokoro/bf_isabella + atempo 0.6. Image gen config (`z_image_turbo` 768x1344) is legacy from the abandoned hybrid format — kept in the YAML so a future hybrid Short doesn't break, but production renders skip image-gen entirely. Closer panel: `SUBSCRIBE for more deep dives / LIKE if you learned something`. Upload account `historyrecapped`, category 27 (Education).
- Production scripts (slug-aware, run from project root):
  - `scripts/historyrecapped/render_footage_only.py --channel historyrecapped --slug <slug>` — generic 100% footage renderer. Reads `historyrecapped/narrations/<slug>.json` + `historyrecapped/shotlist/<slug>.json` + `historyrecapped/config.yaml`; writes `historyrecapped/shorts/<slug>.mp4` in ~1-2 min total. Pipeline: TTS → whisper → word PNGs → ffmpeg trim windows → concat → narration mux → caption burn with cut-aware clamping.
  - `historyrecapped/scripts/upload.py --slug <slug> --title "..." --privacy public` — uploads via `pipeline.upload.upload_short`, writes record to `historyrecapped/uploads/<slug>.json`.
  - `historyrecapped/scripts/{auth.py, branding.py}` — one-time OAuth + brand asset rerender. Legacy `build_100footage.sh, regen_audio_caps.py, final_v2.py` are pre-generalization and only kept for reference; the new `render_footage_only.py` does all three steps.
- Channel layout (everything under `historyrecapped/`): `config.yaml`, `raw/<slug>.json` (story facts + metadata), `narrations/<slug>.json` (hook + narration text + title options), `shotlist/<slug>.json` (source_url + windows[]: in_s/out_s/match_text), `cache/<slug>/{narration.wav,beats.json,prompts.json,word_*.png,clip_*.mp4,video.mp4}`, `shorts/<slug>.mp4`, `uploads/<slug>.json`, `branding/{icon_800.png,banner_2560x1440.png}`, `learnings/`.
- Window selection MUST be verified at 1fps before rendering — picking from 60s montage cells frequently lands on talking-head presenters or text-overlay title cards. Sample each candidate region with `ffmpeg -ss N -t 60 -i src.mp4 -vf fps=1,tile=10x6 grid.png` and read the grid before committing in_s/out_s. Pointe du Hoc v0 had 2 of 7 windows on talking heads; v1 fixed after 1fps inspection.
- Branding assets at `historyrecapped/branding/`. Big Caslon serif on khaki/sepia + grain + vignette + gold medal mark with star. Banner content lives inside YouTube's mobile-safe 1546×423 centred band. Re-render with `historyrecapped/scripts/branding.py`.
- Closer pattern: see `closer.md` — "LIKE to honor those who served. SUBSCRIBE for more such stories." (NOT the earlier sports-borrowed "COMMENT which battle next?").
- Footage source caching: `scripts/historyrecapped/render_footage_only.py` uses `<channel>/footage/sources/` (per-channel) — NOT the shared `sportstoriesanimated/footage/sources/` that `pipeline/footage.py:DEFAULT_CACHE_DIR` still defaults to. Download new sources directly into `historyrecapped/footage/sources/` so the renderer doesn't re-download via the legacy default.
- Source quality classes (ranked, learned from the 2026-05-04 10-pack):
  - **Best — CriticalPast / Periscope Film YouTube reuploads** (e.g. `MHcy4dF8P00` Pearl Harbor, `5uIjqtpcMmM` Stalingrad surrender, `t9tGRRa2kr8` Guadalcanal, `FmfVaVDGx-Q` Reichstag, `lU9MgGceeJg` Bastogne stock reel). 2-10 min each. Pure period newsreel, no presenter, sometimes a small watermark. **Default to these.**
  - **Good — colorized HD restorations of period film** (e.g. `zBXanMHgoLs` Omaha 4K colorized). 20-30 min, all archival but a single source.
  - **Mixed — full documentaries with chapter cards** (e.g. `GEJPw5gkkpU` Midway 28min, `FbAi7UAG-rU` Kursk 17min). Mostly archival but periodic title cards / officer-portrait insert shots; verify at 1fps to skip them. Midway windows at 440s + 700s landed on chapter title cards in the 5s overview — only 1fps zoom caught it.
  - **Bad — animated-map "history" channels** (e.g. WPhistory's `jfPvkPz2W64` Stalingrad). 80% red-pincer animated maps, NOT real footage. Skip.
  - **Bad — host-on-camera documentary remixes** (e.g. `izy1f7ozNlY` Guadalcanal). Recurring presenter shot every ~45-60s, hard to sequence around. Skip.
- **Episodes shipped (12 total, all public, category 27 Education):**
  - 2026-05-03 — `battle-of-britain-few` → https://youtube.com/shorts/bc7lopoWSY4
  - 2026-05-03 — `pointe-du-hoc-1944` → https://youtube.com/shorts/qqOU32B4p3Q (title "The most impossible mission of D-Day")
  - 2026-05-04 batch (10):
    - `dunkirk-1940` → https://youtube.com/shorts/icUNo0DQudw
    - `pearl-harbor-1941` → https://youtube.com/shorts/NfaaobtwC_A
    - `omaha-beach-1944` → https://youtube.com/shorts/3n_6oUFWZMo
    - `midway-1942` → https://youtube.com/shorts/W8_h-1UeGIE
    - `bastogne-1944` → https://youtube.com/shorts/Nk3YMq0BE2Y
    - `kursk-1943` → https://youtube.com/shorts/Ae-GM9kaiJE
    - `stalingrad-uranus-1942` → https://youtube.com/shorts/OaVNwb01VlQ
    - `market-garden-1944` → https://youtube.com/shorts/7OgyVExGvXI
    - `reichstag-1945` → https://youtube.com/shorts/r_Mrold1DNg
    - `guadalcanal-1942` → https://youtube.com/shorts/nk3NU3dL6RM
- OAuth: account `historyrecapped` token at `~/.config/ytfactory/youtube_token_historyrecapped.json`. Owning Google account: rsinghtomar54@gmail.com. Authed via `historyrecapped/scripts/auth.py` (interactive flow, prints URL, user pastes into Chrome). GCP project `save-contacts-auto`, fixed callback port 8089 — same OAuth client used by every other channel.
- First upload caveat: brand-new YouTube channels get an extra slow processing pass from YouTube's anti-spam system (30-60min for a 55s Short). Don't delete + reupload.
