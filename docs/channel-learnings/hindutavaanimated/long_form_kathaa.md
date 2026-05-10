---
name: HindutavaAnimated long-form kathaa format
description: 50-70 min Hindu scripture kathaa videos (Mahabharat / Ramayan / Gita / Puraan) authored via /make-katha — Kokoro hf_alpha Hindi narration over footage-only visuals (Wikimedia, archive.org, museum open-access, CC0 stock). NO AI image gen. Uses historyrecapped/scripts/render_footage_only.py with --aspect 16:9. Channel publishes BOTH 50-60s Shorts AND 50-70 min kathaa to the same YouTube channel.
type: project
---

HindutavaAnimated publishes BOTH 50-60s mythology Shorts AND
50-70 min long-form scripture kathaa to the **same YouTube
channel** (rohit30.iitkgp@gmail.com / HindutavaAnimated). The
kathaa mode is a NEW format added 2026-05-05; it lives inside the
existing `hindutavaanimated/` dir under a `kathaa:` config block
— there is no separate channel.

**Why:** The Shorts channel has 4 episodes shipped and 1 viral
signal (abhimanyu-chakravyuh). Long-form devotional / scripture
kathaa is a high-RPM long-watch-time monetization niche on
YouTube — channels narrating Gita / Ramayan / Mahabharat
chapters in pure Hindi over temple imagery monetize on
completion >40% of 50-90 min videos. Same scripture-narrative
premise as the Shorts, fundamentally different format: 16:9
horizontal, soft kathaa-vyaas voice, footage-only visuals, 6-10
chapters per video, two embedded support asks.

**How to apply:**

- **Authoring:** `/make-katha` (skill at
  `.claude/skills/make-katha/SKILL.md`). Stage-1 spec locks
  text + section + tone + sanskrit-policy + duration. Stage-2
  builds chapter dossier with mool_shloka / prose_summary /
  bhavarth / sources / footage_queries. Stage-3 sources footage
  from Wikimedia / archive.org / Pexels / Pixabay / museum
  open-access / broadcaster official channels (NEVER YouTube
  re-uploaders). Stage-4 writes the continuous kathaa.

- **Layout** (single dir, single channel):
  - `hindutavaanimated/config.yaml` — needs a NEW `kathaa:`
    block (see below) alongside the existing Shorts settings.
  - `hindutavaanimated/narrations/<slug>.json` — same dir for
    both Shorts and kathaa; kathaa slugs end in `-katha-<YYYYMM>`
    (e.g. `gita-adhyay-2-sankhya-yog-katha-202605`).
  - `hindutavaanimated/shotlist/<slug>.json` — kathaa shotlists
    use the existing `windows: [{in_s, out_s, kind, source_url,
    chapter, _license_note, _design_notes}]` schema and
    reference `hindutavaanimated/footage/sources/*.mp4`.
  - `hindutavaanimated/cache/<slug>/tts_chunks/` — Kokoro
    chunked synth lands here; resumable across runs.
  - `hindutavaanimated/footage/sources/` — downloaded source
    files (Wikimedia stills, archive.org reels, Pexels mp4s).
  - `hindutavaanimated/music/` — optional ambient bed
    (bansuri / tanpura / temple chant at -28 dB).
  - `hindutavaanimated/long_form/<slug>.mp4` — final 60-min
    output.

- **Format vs the Shorts** (decided 2026-05-05):
  - 50-70 min (vs 50-60s)
  - 1920×1080 horizontal (vs 1080×1920 Shorts)
  - Kathaa-vyaas register (vs punchy Shorts hook)
  - NO captions by default — devotional viewers often have eyes
    closed (mirrors `historyrecapped/learnings/long_form_channel.md`
    sleep-mode rule). Opt-in captions via `caption_mode:
    devanagari` if needed.
  - Continuous LONG footage windows 30-90s each (vs 5-7s Shorts
    cuts) — jarring cuts wake a viewer using this for
    sleep / meditation
  - Optional ambient music bed at -28 dB under narration
  - **Two embedded support asks** — one in Mangalaacharan
    (~3-4 min in), one in Upsanhar (closer). Inline narration
    only, no animated screens. Mirrors
    `historyrecapped/learnings/long_form_support_asks.md`
    REVISED 2026-05-04.
  - Long-form YouTube upload category 22 (People & Blogs) per
    existing channel default; can move to 27 (Education) if
    channel pivots.

- **Voice (locked 2026-05-05):** Kokoro `hf_alpha` (Hindi
  female) at speed 1.0. F5-TTS-MLX (the production English
  default elsewhere) does NOT speak Hindi at the configured
  model — Kokoro is the only Hindi-capable provider in the
  repo. The channel's existing `_HINDI_TATSAMA_RESPELLINGS`
  table in `pipeline/audio.py` handles common conjuncts;
  per-story proper nouns go in
  `narrations/<slug>.json:pronunciation_dict`.

- **Source strategy:** Wikimedia Commons + archive.org PD +
  Pexels / Pixabay CC0 + museum open-access (Met, Indian
  Museum, British Museum) + broadcaster official channels
  (Gita Press / BAPS / ISKCON). NEVER YouTube re-uploaders
  (memory: `long_form_sources.md`).

- **Sanskrit policy switch:** default is `hindi-only` (matches
  the Shorts locked rule that pure-Sanskrit was rejected after
  the karmanye-vadhikaraste mock). For Gita / Upanishad text
  where the verse itself is the point, opt in to
  `shloka-then-translation` — read the Devanagari shloka, then
  "इसका भाव यह है कि…" + Hindi anuvad. Bare Sanskrit without
  Hindi translation is rejected by quality gate §6.12.

- **Render command:**
  ```bash
  caffeinate -i .venv/bin/python -u \
    historyrecapped/scripts/render_footage_only.py \
    --channel hindutavaanimated --slug <slug> --aspect 16:9
  ```
  `caffeinate -i` + `python -u` per
  `feedback_long_form_render_caffeinate.md`.
  
  **NOTE:** `--aspect 16:9` is a renderer extension flagged
  shared with `/make-top10`. Single unified patch to land
  before first kathaa render — `render_footage_only.py` is
  currently hard-coded `[bg]scale=1080:1920` (9:16 only).

- **Why footage-only, not the F5-TTS sleep renderer:**
  `render_long_form.py` REJECTS any `tts_provider != f5_tts`
  (memory: `feedback_long_form_strict_f5.md`). Kokoro is the
  only Hindi voice we have. So kathaa rides on
  `render_footage_only.py`, which reads the channel's
  `tts_provider` from `config.yaml` and uses it.

- **Word-count math (calibrate against first run):** Kokoro
  hf_alpha at speed 1.0 lands ~110 wpm in measured Hindi
  kathaa register. So:
    - 50 min → ~5,500 Hindi words
    - 60 min → ~6,600 words
    - 70 min → ~7,700 words

  Always count words explicitly before kicking off the render
  — historyrecapped's first 2-hour authoring attempt produced
  ~5,000 words and rendered ~50 min instead of ~120 min.

- **Whisper Hindi caveat:** if `caption_mode: devanagari` is
  enabled, force chunked-Hindi Whisper per
  `whisper_chunked_hindi_forced.md` — auto-language detection
  on Hindi guesses Telugu and word-coverage drops to 50%.
  Default `caption_mode: none` sidesteps this entirely.

## YAML diff to add to `hindutavaanimated/config.yaml`

```yaml
# ---- Long-form kathaa mode (NEW 2026-05-05) ---------------------------
# Companion format to the Shorts. Authored via /make-katha; rendered
# via historyrecapped/scripts/render_footage_only.py --aspect 16:9.
# Same Kokoro hf_alpha voice; 16:9 horizontal; no captions by default;
# 6-10 chapters per video; 2 embedded asks.
kathaa:
  aspect: "16:9"
  output_resolution: [1920, 1080]
  duration_band: [50, 70]      # minutes
  caption_mode: none           # 'none' | 'devanagari'
  tts_speed: 1.0
  music_bed_db: -28            # ambient bed under narration
  closer_hold_s: 1.5
  upload_category_id: "22"     # People & Blogs (move to 27 Education if pivoting)
  description_template: |
    {script.metadata.invocation}

    🕉️ कथा: {script.metadata.episode}
    📿 अध्याय / कांड: {script.metadata.section}

    {chapter_timestamps_block}

    📖 स्रोत:
    {sources_block}

    ━━━━━━━━━━━━━━━━━━━━
    🙏 {script.blessing_close}
    🔔 SUBSCRIBE for more such kathaa
    ━━━━━━━━━━━━━━━━━━━━

    #hindukatha #bhagavadgita #ramayan #mahabharat #hindumythology #devotional #spiritualhindi #meditation
```

## Status (2026-05-05)

- Skill scaffolded (`.claude/skills/make-katha/SKILL.md`).
- `kathaa:` config block NOT YET added to
  `hindutavaanimated/config.yaml` — user must add the YAML diff
  above before first run (skill enforces via quality gate §6.11).
- `render_footage_only.py --aspect 16:9` extension NOT YET
  landed — same one-filter swap as `/make-top10` requested. Land
  the unified patch before the first kathaa renders.
- No episodes shipped yet. First candidate suggestions:
  - `gita-adhyay-2-sankhya-yog-katha` — most popular Gita
    chapter, the foundational dialogue.
  - `ramayan-sundarkand-katha` — devotional standalone, high
    sentiment recall.
  - `mahabharat-bhishma-parva-shanti-pravachan-katha` —
    Bhishma's instruction, scriptural depth.
