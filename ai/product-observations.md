# Product Observations

Per-channel product surface + operator workflow. One paragraph per channel; does **not** restate the channel rules (those live in `pipeline/channels/<channel>.yaml` and its variants — read the YAML for rules). This doc captures the *product concept* each channel embodies + the operator paths into the system.

7 channels in scope per the locked operating principle (ADR-017): all 7 must be functional. 6 have YAMLs today; `scrollpulse` YAML pending (ADR-018).

---

## Channels

### MyStoriesAnimated
Reddit/AITA-class story Shorts. Auto-pull from r/AITA, r/TIFU, r/relationship_advice et al. via `source_adapter: reddit_video`; LLM rewrites the raw post into a hook-first 50-60s narration; warm hand-drawn watercolor + ink illustrated panels per beat via Z-Image-Turbo; Chatterbox TTS over the top. Niche variants (`pipeline/variants/mystoriesanimated/<niche>.yaml`) split the channel into AITA / TIFU / wiki-oddities / today-in-history sub-streams, each with its own source adapter and tonal preset. This is the highest-throughput channel and the canonical illustrated-panel path. Format: Shorts only.

### History Recapped
War / disaster / expedition history. Two product modes: **Shorts** (50-60s, archival YouTube footage via CriticalPast / Periscope Film, narrator over real newsreel) and **long-form sleep history** (60+ min, calmer narration, image-panel or footage-only). `source_adapter: historyrecapped_manual` — curator hand-authors `raw/<slug>.json`; the system does not auto-pull. Chatterbox TTS. The Shorts path explicitly bypasses image-gen via `historyrecapped/scripts/build_100footage.sh` because production renders are 100% archival, but the channel's image_provider is still set to `cloudrun_z_image_turbo` for the future hybrid path. Format: Shorts + long-form.

### HindutavaAnimated
Hindi mythology — Mahabharat / Ramayan / Puraan / Krishna leela. Pure Hindi narration (Sanskrit dropped 2026-05-03), Amar Chitra Katha comic-book art via Z-Image-Turbo, IndicF5 TTS with the hindi-female-iitm-anchor voice clone. `source_adapter: manual` — every script is hand-authored under `hindutavaanimated/narrations/<slug>.json` because there is no clean auto-source for curated scripture episodes. Two format variants: 50-60s Shorts and 10-15 min long-form. The IndicF5 ref_audio_text wiring is currently broken (ADR-016 in decision-log) so Hindi renders ship gibberish until fixed.

### Cosmos Decoded
Physics + space "how we knew?" videos. Every video pairs a specific physics prediction (theory) with the experiment that confirmed it (test) with the engineering consequence (impact). **Footage-only by design** — no AI image gen ever. Sources: NASA Image Library (PD), ESA/Hubble/JWST releases, Wikimedia, arXiv paper screenshots, LIGO/CERN open archives, period photography (Eddington 1919 plates, Cavendish notebooks). `source_adapter: cosmosdecoded_manual` — curator-driven; no auto-pull yet. Chatterbox TTS over the footage with word-by-word captions (Shorts) or sentence-level (long-form). Two formats: 50-60s Shorts (act-2 measurement isolated) + 25-40 min long-form (full three-act). Format: Shorts + long-form.

### SportsStoriesAnimated (slug: `sportsrecapped`)
Football moments + real broadcast cut-ins. Pipeline order differs from every other channel — `wiki_research` runs BEFORE `cast` (see `pipeline/channels/sportsrecapped.yaml` header). The channel USP is splicing actual match footage at the climax beat (`script.json` declares optional `footage[]` of `{match_text, url, in_s, out_s}` triples; `compose.compose_hybrid` does the splice). Cartoon imagery between footage cuts via Z-Image-Turbo. `source_adapter: sports_moments_manual` — hand-authored raw + dossier; pronunciation dictionary from the dossier feeds TTS so "Aguero" → "ah-GWAIR-oh" before Chatterbox sees it while captions keep the original spelling. Three sub-formats: 50-60s Shorts (head-to-head countdowns, tweet-reactions), 10-15 min mid-form football explainers (narrator-only over broadcast footage), 20-30 min long-form sports docs (with commentator/YouTuber talking-head clips). Format: Shorts + mid-form + long-form.

### Rhyme Time Junction
Bilingual Hinglish nursery rhymes for kids. Fundamentally different pipeline from every other channel: **audio is a sung Suno song, not TTS** (`audio_provider: external_song`; ingests a pre-existing `song.wav` rather than synthesizing). **Continuous animation, not slideshow** — each beat is a 3-5s animated clip via `motion_provider`. **Recurring mascots** (Laddu, Jalebi, Tuk-Tuk, Dadi) locked at channel-level cast; per-story `cast.json` only adds guest characters per rhyme. Captions are sung lyrics from `script.json` (Whisper output is too noisy on sung Hindi). Format: Shorts only. As of 2026-05-03 YAML mtime, the `external_song` audio mode is still flagged as not-yet-wired in `pipeline/audio.py` — verify before relying on this channel for an MVP render.

### scrollpulse (YAML pending — ADR-018)
Brain-rot Reddit + gameplay-overlay Shorts. Auto-pulled Reddit thread renders into a top-60% authentic-looking thread card with TTS; bottom-40% is a pre-rendered Subway Surfers / Minecraft parkour gameplay loop. Same niche split as MyStoriesAnimated (AITA, AskReddit, TIFU, relationship_advice, MaliciousCompliance, pettyrevenge, Showerthoughts, etc.) — round-robin across subreddits. Renders via `scrollpulse/scripts/render_split_screen.py` per the `/make-reddit-thread` skill. Format: Shorts only. YAML doesn't exist yet at `pipeline/channels/scrollpulse.yaml`; UI references it in `web-next/app/app/create/page.tsx:567-575` (`FALLBACK_CHANNEL_KEYS`).

---

## Architecture observations

**Two production tracks is the wrong framing** (Q16-Q17). There is no channel/visual-mode 1:1. Each render picks one of 3 visual modes — AI image-gen (Z-Image-Turbo), motion video (image-to-video), archival footage (curated YouTube/NASA/Wiki) — and any channel can in principle use any mode. The dominant mode per channel today is captured above, but the split is per-render, not per-channel.

**One image provider across all 6 YAMLs:** every channel sets `image_provider: cloudrun_z_image_turbo`. Klein references in code comments and the prompt refiner docstring (`pipeline/images/prompt_refiner.py:1`) are stale — production never uses klein. See ADR-008.

**TTS provider is mostly Chatterbox.** 5 of 6 channels use `cloudrun_chatterbox`; HindutavaAnimated uses `cloudrun_indicf5` (Hindi); rhymetimejunction's TTS line is set but unused (audio comes from Suno). See `voice_refs/` for the reference clips.

**Source-adapter divergence** is the per-channel content-pipeline signature: `reddit_video` (auto-pull) vs `*_manual` (curator hand-authors raw JSON). Niche-level multi-source mixing is on the roadmap (ADR-013).

---

## Operator surfaces (`web-next/`)

The UI is the primary entry point — every render is operator-triggered (Q36). The launchd daemons (`control/com.ytfactory.*.plist`) handle uploads + critic gates, NOT render initiation. Path layout under `web-next/app/app/`:

- **`/app/create`** — the 3-step render wizard (`web-next/app/app/create/page.tsx`, 2598 LoC).
  1. **Mode**: 3 cards. "Channel-focused" (works). "Clone a video" (works, sub-route at `/app/create/clone/`). "AI-generated" (disabled / coming soon — `/app/create/chat/` exists but is gated off).
  2. **Channel**: picks from the channel registry (currently the 6 YAMLs + the scrollpulse fallback key).
  3. **Customize**: form (short/long), niche, topic (manual or "Auto-generate" via `discoverApi` — which fetches from native source adapters per Q40-Q41, with LLM brainstorm as fallback), voice (filterable by gender/tone/use-case), audio mode (voice/song), visual source, music bed, advanced fields.
  Submit → `renderApi.enqueue(...)` → routes to `/app/render/<jobId>`.

- **`/app/queue`** — Firestore-backed job queue view (`web-next/app/app/queue/page.tsx`).

- **`/app/render/[jobId]`** — primary debug surface per Q39 (`web-next/app/app/render/[jobId]/page.tsx`). Surfaces stage status, error messages, retry button. The operator reads the error here, hits retry, doesn't drop to Cloud Logging.

- **`/app/channels/[channel]`** — per-channel inspection (`web-next/app/app/channels/[channel]/`).

- **`/app/cloud`** — Cloud Run service health + cost + deploys. Sub-views: `cost-section.tsx`, `deploys-section.tsx`, `health-section.tsx`.

- **`/app/telemetry`** — observability dashboard. Sub-views: jobs, links, llm-costs, overview, renders, services, stage-latency, timeline, errors. This is the operator's instrumentation surface (separate from the developer's GCP console).

- **`/app/admin`** — operator-side admin panel.

- **`/app/settings`** — settings.

---

## MVP definition

Per Q28: **all 7 channels reliably producing finished mp4s end-to-end without intervention.** Per Q26: zero successful end-to-end automated renders have shipped to date — this isn't a regression to repair, it's a first-stable-state to achieve. Everything else (latency, audience scale, cost optimization, new channels beyond scrollpulse) is post-MVP.
