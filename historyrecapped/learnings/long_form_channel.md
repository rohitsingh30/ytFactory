---
name: History Recapped long-form sleep mode
description: 60-120 min HD-archival history sleep videos shipped to the SAME History Recapped YouTube channel as the 50-60s Shorts. Soft Kokoro/af_nicole voice at natural speed (~105 wpm), 16:9 horizontal, no captions, inline like/sub asks every ~18 min, archival footage rolling continuously. v0.1 in render as of 2026-05-04.
type: project
---

History Recapped publishes BOTH 50-60s Shorts AND 60-120 min long-form sleep videos to the **same YouTube channel** (rsinghtomar54@gmail.com / @HistoryRecapped). The long-form mode lives inside the existing `historyrecapped/` dir under a `long_form:` config block — there is no separate `sleephistory` channel.

**Why:** User identified the sleep-history niche on YouTube as a high-RPM long-watch-time monetization play (2026-05-03). Channels like Stories of the Forgotten and Calm History monetize on completion >40% of 1-2 hour videos with calm narration over archival visuals. Same archival-footage premise as the History Recapped Shorts, fundamentally different format: long-form 16:9, soft voice, no captions, no animated ask screens, periodic inline CTAs every ~18 min.

**How to apply:**

- **Layout (single dir, single channel).** Everything under `historyrecapped/`:
  - `config.yaml` has both top-level Shorts settings AND a `long_form:` block (Kokoro voice, ask cadence, 16:9 output)
  - `narrations/<slug>.json` — same dir for both modes; long-form slugs end in `-sleep` (e.g. `pacific-war-1941-1942-sleep.json`)
  - `shotlist/<slug>.json` — same dir, long-form shotlists are `clips: [{source, in_s, out_s}]` and reference `footage/long_sources/*.mp4`
  - `cache/<slug>/tts_chunks/` — long-form caches pile up here (resumable, see `feedback_cartesia_402_resumable.md`)
  - `footage/long_sources/` — HD source MP4s (DroneScapes-class compilations + archive.org PD)
  - `music/` — royalty-free ambient beds (currently the renderer falls back to a synthetic ambient drone if no `<music_bed_default>.wav` exists in this dir)
  - `shorts/<slug>.mp4` — final outputs land here regardless of mode (dir name is misleading; it's the output dir for both Shorts AND long-form)
  - Renderers split: `scripts/historyrecapped/render_footage_only.py` for Shorts, `scripts/historyrecapped/render_long_form.py` for long-form sleep mode

- **Format vs Shorts.**
  - 60-120 min (vs 50-60s)
  - 1920×1080 horizontal (vs 1080×1920 Shorts)
  - Soft narrator (vs Cartesia/Theo punchy doc cadence)
  - NO captions (eyes closed)
  - Continuous LONG footage windows 30-50 min each (vs 5-7s Shorts cuts) — jarring cuts wake the viewer
  - Ambient music bed at -28 dB under narration (Shorts has none)
  - Periodic support asks every ~18 min, **inline narration only — no animated screens, no video cut, no music dip** (Shorts uses end-only closer panel)
  - Long-form YouTube upload category 27 (Education), NOT a Short

- **Voice (locked 2026-05-04).** Kokoro local TTS, voice `af_nicole` at speed 1.0 (continuous; no atempo post-pass). Yields ~105 wpm — the right sleep-narration zone. Free, unlimited, runs on M2 Max at ~0.4× realtime per chunk. Cartesia/Sarah is a fallback for when paid credits are restored — see the commented-out block in `historyrecapped/config.yaml` `long_form:`. The 3-way A/B (British Lady / Sarah / Sneha at +atempo 0.85) is cached at `historyrecapped/cache/_voice_samples/` as historical reference.

- **Periodic support asks.** See `long_form_support_asks.md`. Inline narration sentences, NOT animated screens. Cadence ~18 min. Vary wording slightly so the line doesn't read as a loop. Final ask doubles as the closer; no separate "thanks for watching" outro.

- **Source strategy.** See `long_form_sources.md`. The realistic HD-2hr-free path is DroneScapes-class YouTube re-uploaders (1080p restored) with private upload + ContentID dispute fallback. archive.org PD caps at 480p which the user rejected as "not HD." Paid stock (Pond5, Storyblocks) is the only zero-risk HD path.

- **Render command.**
  ```bash
  .venv/bin/python scripts/historyrecapped/render_long_form.py \
      --channel historyrecapped --slug <long-form-slug>
  ```
  Stages: chunked Kokoro TTS → trim source clips with blurred 16:9 letterbox → concat → ambient music bed (synthetic placeholder if no wav in `historyrecapped/music/`) → final mux. Pipeline is fully resumable; cached TTS chunks are skipped on re-run. Use `--tts-only` for iterative narration testing without burning ffmpeg time on the video stage.

- **Word count math (validated 2026-05-04).** Kokoro/af_nicole at speed 1.0 = ~105 wpm. So:
  - 50 min target → ~5,250 words narration
  - 80 min target → ~8,400 words
  - 2 hr target → ~12,600 words
  - 2.5 hr target → ~15,750 words
  
  Char count in JSON ≈ word count × 5.8 (English avg). The first 2hr authoring attempt produced ~5000 words → ~50 min, not 2hr. **Always count words explicitly before kicking off render** — don't trust intuition on length scaling.

- **First episode (2026-05-04).** `pacific-war-1941-1942-sleep` — Pearl Harbor through Midway, ~50 min v0.1. Source: DroneScapes RxcnJNnOiaw 1080p 2hr. v0.1 status: in render or shipped depending on when this is read. Authoring includes 3 inline support asks + closer.

- **Status as of 2026-05-04:** voice + source + render pipeline validated. v0.1 (~50 min HD) rendering or rendered. No OAuth, no uploads yet — long-form uploads need a separate flow from the Shorts upload.py since long-form has no shotlist closer panel and no caption burn.
