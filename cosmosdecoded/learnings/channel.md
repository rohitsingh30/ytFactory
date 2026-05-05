---
name: Cosmos Decoded channel
description: Footage-only physics + space "how did we know?" channel. Every video pairs a physics prediction with the specific experiment, mission, or observation that confirmed it. Shorts (50-60s) + long-form (25-40 min). Scaffolded 2026-05-05; not yet shipping.
type: project
---

Cosmos Decoded — physics + space "how did we know?" channel. **Slug `cosmosdecoded` everywhere** (`cosmosdecoded/config.yaml`, `cosmosdecoded/{raw,narrations,cache,shorts,uploads,branding,footage,shotlist,scripts,music,long_form}/`, OAuth account TBD = `cosmosdecoded`, niche key `cosmosdecoded`). YouTube display name "Cosmos Decoded".

**Why:** User picked space + physics from the 10-channel slate (`docs/proposed_channels_2026_05_05.md` #9, refined 2026-05-05). The original brief was "SpaceMissionDecoded — NASA mission deep-dives". Refined wedge broadens to cover CERN / LIGO / lab-physics so the source pool is much bigger. The wedge: every video answers **"how did we know?"** by pairing a specific physics prediction with the specific observation, experiment, or mission that confirmed it. Three-act structure prediction → experiment → consequence. The differentiator vs. Scott Manley (mission ops) / Astrum (faceless astronomy) / PBS Space Time (theory-only) / Veritasium (face-led) is **document-led storytelling** — actual papers, lab notebooks, telegram exchanges, plate scans on screen.

**How to apply:**

- Channel YAML: `cosmosdecoded/config.yaml`. Display name `Cosmos Decoded`. `render_style: footage_only` routes through `scripts/historyrecapped/render_footage_only.py`. TTS (Shorts): Kokoro `am_michael` at 1.0 (zero-API local). Long-form: F5-TTS-MLX `sarah.wav` at speed 1.0 + atempo 1.0 (calm-explainer, NOT sleep-mode; sleep-mode 0.95+0.85 is explicitly rejected for this channel — audience came for engaged narration). Closer panel: `SUBSCRIBE for more decoders / LIKE if this changed how you see physics`. Upload category 28 (Science & Technology) — NOT 27 (Education); Sci/Tech has higher RPM in the physics audience demo.

- Production scripts (slug-aware, run from project root):
  - `scripts/historyrecapped/render_footage_only.py --channel cosmosdecoded --slug <slug>` for Shorts (50-60s, 9:16). Reads `cosmosdecoded/narrations/<slug>.json` + `cosmosdecoded/shotlist/<slug>.json` + `cosmosdecoded/config.yaml`. Pipeline: TTS → whisper → word PNGs → ffmpeg trim windows → concat → narration mux → caption burn with cut-aware clamping. ~1-2 min total.
  - `scripts/historyrecapped/render_long_form.py --channel cosmosdecoded --slug <slug>` for long-form (25-40 min, 16:9). Same renderer as HistoryRecapped sleep-mode but with `long_form.tts_speed: 1.0` (engaged) + cool-blue grade instead of warm-firelight + sentence captions in white (not yellow italic).
  - `cosmosdecoded/scripts/{auth.py, branding.py, upload.py}` — TO BUILD. Copy from `historyrecapped/scripts/` and rename slug. OAuth account = `cosmosdecoded`.

- Channel layout (everything under `cosmosdecoded/`):
  - `config.yaml` — channel rules + Shorts + long-form blocks
  - `raw/<slug>.json` — physics fact + paper citation + key date + named scientists + outcome
  - `narrations/<slug>.json` — hook + narration text + chapters (long-form) + title options
  - `shotlist/<slug>.json` — source_url + windows[]: in_s/out_s/match_text per beat
  - `cache/<slug>/{narration.wav,beats.json,word_*.png,clip_*.mp4,video.mp4}` — render artifacts
  - `shorts/<slug>.mp4`, `long_form/<slug>.mp4` — final outputs
  - `uploads/<slug>.json` — upload record (mirrors to GCS via `pipeline.upload`)
  - `branding/{icon_800.png,banner_2560x1440.png}` — channel art
  - `footage/{sources,long_sources}/` — per-channel cache (NOT shared with sportstoriesanimated)
  - `learnings/` — channel-specific rules

- **Source palette (NO AI image gen — every visual must trace to one of these):**
  - **NASA Image and Video Library** (`images.nasa.gov`) — PD, fully open, every mission
  - **NASA Technical Reports Server (NTRS)** — PD mission reports, engineering memos
  - **National Archives Record Group 255** — NASA AV holdings, PD
  - **JPL + JSC mission control footage** — PD via NASA channel + archive.org
  - **ESA / Hubble / JWST releases** — CC-BY-SA-IGO (cite ESA/Hubble per beat)
  - **CERN open-access photo + film archive** — CC-BY (LHC, ATLAS, CMS, accelerators)
  - **LIGO Open Science Center** — gravitational wave detector imagery + data plots
  - **Wikimedia Commons** — physicist portraits, observatory photos, instrument photographs (CC-BY-SA, attribution required per beat in description)
  - **Library of Congress** — historical scientific photography (Eddington plates, Cavendish notes, period observatory imagery)
  - **arXiv preprint PDFs** — paper page screenshots as fair-use editorial commentary, kept ≤8s on screen per beat
  - **Smithsonian National Air & Space photos** — mostly PD or CC
  - **Stock b-roll** — Pexels + Pixabay + Storyblocks for laboratory / observatory / night-sky / equation-on-chalkboard cutaways
  - **NEVER** — YouTube re-uploaders of news clips (ContentID risk), animated-physics channels (we want real photographs), AI-generated stills (channel rule)

- **Window selection MUST be verified at 1fps before rendering** — same rule as HistoryRecapped. NASA mission-doc compilations frequently include presenter chyrons, talking-head producers, or modern reenactment shots. Sample candidate regions with `ffmpeg -ss N -t 60 -i src.mp4 -vf fps=1,tile=10x6 grid.png` and read the grid before committing in_s/out_s.

- **The "How We Knew" three-act template** (every long-form, condensed for Shorts):
  1. **Prediction** (3-5 min long / 10s Shorts) — what the theory said before the test. Show the paper page or chalkboard equation. Name the predicting physicist.
  2. **Experiment** (15-20 min long / 30s Shorts) — the actual test. Photographs of the apparatus, the team, the location. Telegram exchanges or lab notebooks if available. The single moment of measurement.
  3. **Consequence** (5-10 min long / 15s Shorts) — what changed in physics or engineering after. The follow-up paper or mission. The Nobel (if any). The technology it enabled.

- **Anchor candidates** (footage-friendly, all-PD source pool, validate format):
  - `eddington-1919-eclipse` — How We Knew Light Bends. LoC eclipse plates + Cambridge archive + Royal Society 1920 paper PDF + Príncipe / Sobral coastal stock.
  - `pound-rebka-1959` — How We Knew Time Slows Near Mass. Harvard Jefferson Lab tower photos + Phys Rev Letters paper scan + Mössbauer apparatus diagrams.
  - `mars-polar-lander-1999` — The Last 13 Minutes. NASA images.nasa.gov + NTRS failure report + JPL mission control PD footage. (This was the original `spacemissiondecoded` anchor; carries over.)
  - `ligo-2015-gw150914` — How We Knew Gravitational Waves Exist. LIGO Open Science strain plot + Hanford / Livingston site photos + Phys Rev Letters Sept 2015 paper.
  - `eht-2019-m87` — How We Knew Black Holes Have Shadows. Event Horizon Telescope site photos + ApJ Letters image release + 2019 press conference PD photos.
  - `super-kamiokande-1998` — How We Knew Neutrinos Have Mass. Kamiokande tank photos (Wikimedia + university release) + 1998 Phys Rev Letters paper.
  - `cassini-grand-finale-2017` — How We Knew Saturn's Rings Are Young. NASA Cassini imagery + JPL grand-finale animation (PD) + Science 2018 results paper.

- **Risks (per `docs/proposed_channels_2026_05_05.md` #9):**
  - Audience is technical and unforgiving on physics errors. Hard rule: every claim must trace to a specific paper / mission report / press release. Never editorialise on contested theory.
  - Saturation moderate (Cool Worlds, History of the Universe, Anton Petrov, Astrum). The "how we knew" angle + document-led signature is the wedge — stay disciplined; do NOT drift into generic astronomy news or speculation videos.
  - Animation budget required for orbit / trajectory / particle-collision diagrams — but use NASA's existing PD animations (Cassini Grand Finale, Voyager trajectory, JWST commissioning) rather than build our own. Any custom motion graphics must be flagged for production review.
  - Some recent-mission imagery has ContentID exposure if a news org owns exclusive rights — stick to NASA / ESA / CERN / LIGO direct releases (all open-licensed) and skip news-channel re-uploads.

- **Long-form vs Shorts source amortisation:** the "how we knew" structure means every long-form's act-2 measurement moment ports straight to a Shorts. Build the long-form first, then strip the experiment beat into a 50-60s 9:16 cut. One narration-research session powers ~4-6 outputs (1 long + 3-5 Shorts).

- OAuth: account `cosmosdecoded` (placeholder until user creates the YouTube channel + OAuths via `cosmosdecoded/scripts/auth.py` once written). Token will live at `~/.config/ytfactory/youtube_token_cosmosdecoded.json`. GCP project `save-contacts-auto`, fixed callback port 8089 — same OAuth client used by every other channel (`reference_oauth_setup`).

- **Cross-engagement** is automatic — once OAuthed, `pipeline/cross_engage.py` discovers `cosmosdecoded` from the token cache and every owned channel auto-likes / auto-subscribes / opens a muted Playwright tab on each upload (`project_cross_channel_engagement`).

- **Upload throttle** — per-account ≥1h gap between consecutive publishes is enforced by `pipeline/upload.compute_throttled_publish_at` (`docs/upload_throttle.md`). No per-channel config needed.

## Status (2026-05-05)
- ✅ Channel directory scaffolded
- ✅ `config.yaml` written (Shorts + long-form blocks)
- ✅ `learnings/channel.md` (this file)
- ✅ YouTube channel created (Channel ID `UCIlAta6E57BlV2cPX0OuLEA`, "Cosmos Decoded")
- ✅ OAuth complete — token at `~/.config/ytfactory/youtube_token_cosmosdecoded.json`; cross-subscribed with all 4 sibling channels
- ✅ `cosmosdecoded/scripts/auth.py` (one-shot OAuth helper, mirror of historyrecapped's)
- ✅ `cosmosdecoded/scripts/branding.py` + branding assets (`branding/icon_800.png` 433KB + `banner_2560x1440.png` 2.3MB) — observatory navy + gold orbital mark + "How did we know?" tagline
- ✅ `/make-cosmos-decoder` skill at `.claude/skills/make-cosmos-decoder/SKILL.md` (paired long-form + Short authoring)
- ⏳ First anchor narration (suggest `eddington-1919-eclipse` — purest "how we knew" story, all-PD source pool)
- ⏳ Format validation render before promoting upload privacy from `private` to `public`
- ⏳ `cosmosdecoded/scripts/upload.py` — copy of historyrecapped's; build when first render is ready to ship
