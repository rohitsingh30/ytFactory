---
name: HindutavaAnimated channel (hindutavaanimated)
description: Hindi devotional Hindu mythology channel — Amar Chitra Katha aesthetic, Cartesia Sneha narration. 2 episodes shipped (Mahabharat + Ramayan). Locked format + custom compose path.
type: project
originSessionId: 757022d5-5a62-421f-b1a1-80fe5002bcdf
---

Hindi devotional Hindu mythology channel, branded **HindutavaAnimated** on
YouTube. Local channel-dir `hindutavaanimated/` (don't rename — OAuth slot
keys off it). Channel scope EXPANDED 2026-05-03 from "Mahabharat-only" to
"any Hindu mythology" after the Hanuman/Ramayan story shipped well; the
ॐ icon was chosen specifically for this brand-flexibility.

## Status (2026-05-03)
- 2 episodes SHIPPED, both public:
  - **abhimanyu-chakravyuh** — https://youtu.be/Tf92NmOA0lw — "जब 16 साल के लड़के ने कौरवों के 7 महारथियों को धूल चटाई" — 41s, Mahabharat
  - **hanuman-sanjivani-parvat** — https://youtu.be/nIEq7zKUg9E — "हनुमान ने पूरा हिमालय उठा लिया | संजीवनी बूटी की कथा" — 44s, Ramayan, with **Suno-generated devotional BGM** under narration at 12% + LIKE/COMMENT/SUBSCRIBE closer panel
- 1 subscriber

## Owner / Auth
- Google account: rohit30.iitkgp@gmail.com
- OAuth token: `~/.config/ytfactory/youtube_token_hindutavaanimated.json`
- GCP project `save-contacts-auto` had to add this email as a Test User to clear the "create_contact_auto has not completed verification" 403. If this resurfaces, that's the fix.

## Locked format (don't relitigate)
- One dramatic Hindu mythology episode per Short — Mahabharat, Ramayan, or any Puraan
- Hook (3-4s) → setup → rising action → climax (visual centerpiece) → Hindi lesson punchline → CTA
- 40-60s total, ~12 beats
- Pure Hindi narrative, NO Sanskrit (rejected after karmanye-vadhikaraste mock)
- Captions: Devanagari word-level, AITA-style center-vertical
- Closer overlay: 3-row "LIKE / COMMENT / SUBSCRIBE" (introduced in hanuman; abhimanyu had SUBSCRIBE-only)
- BGM: optional Suno-generated mellow Hindu instrumental (bansuri/tanpura/tabla, ~70 BPM, raga Yaman feel) at 12% under narration with 1.5s fade-out

## Visual style
Amar Chitra Katha — bold black ink outlines, sindoor red / peacock blue /
marigold yellow / emerald green / antique gold. Locked in
`hindutavaanimated/config.yaml:image_style_prefix`. Z-Image-Turbo @
768×1344 / 4 steps / seed 108. Per-story character lock via cast.json.

**Class-of-bug visuals** (don't re-discover):
- Diffusion can't render N≥4 distinct figures cleanly → reduce ensembles to "1-2 named characters + silhouette wall" (kaurava_seven cast, kaurava silhouettes in hanuman beat 02 with Ravana)
- Diffusion can't render "broken/torn/shattered" applied to clothing/objects → use "wreckage scattered around" instead
- Diffusion treats QUOTED PHRASES in prompts as text-to-render targets (beat 04 hanuman v1 rendered "this is where you must go." in English; beat 10 v1 rendered gibberish Devanagari because the prompt mentioned "negative space for caption to sit"). **Never quote descriptive composition language; never reference "where text goes."**
- Single iconic Unicode glyph (ॐ) renders cleanly because training data has it as a graphic; multi-character Devanagari/Indic does not.

## TTS — Cartesia Sonic-2 + Sneha
- Voice id `6b02ffe5-e3cb-48c0-a023-c72f85953375` at speed 0.85 (`slow` mapping)
- Devanagari conjuncts handled natively. The `_HINDI_TATSAMA_RESPELLINGS` table in pipeline/audio.py is a no-op for Cartesia (kept for the kokoro fallback path).
- Requires `CARTESIA_API_KEY` (in .env)
- **Kokoro fallback**: flip `tts_provider: kokoro` + `tts_voice: hm_psi` for free/offline rendering

## Compose path (custom harness, NOT make_shorts.py)
Channel uses `source_adapter: manual` — make_shorts.py would re-author
prompts via LLM and re-render audio, both wasteful for hand-curated
episodes. The actual compose flow is `/tmp/compose_episode.py`-style
(slug-aware, BGM-aware):

1. Render images via `pipeline.images.generate()` directly from `prompts/<slug>.json` (cast tokens like `{narrator:hanuman}` inlined from `cast/<slug>.json`)
2. Synthesize audio via `pipeline.audio._synth_cartesia()` (or `_synth_kokoro` fallback)
3. Optionally generate BGM via `pipeline.audio.synth_via_sunoapi()` with instrumental-bhajan style prompt
4. Run `pipeline.beats.transcribe_words()` for Whisper word timestamps
5. **CLAMP word durations to ≤1.5s** before passing to compose — Whisper-mlx-4bit hallucinates 20+ second word durations on dense Hindi (caused the "stuck on hogi" bug in hanuman v1). See `whisper_hindi_word_clamp.md`.
6. **Time-proportional beat allocation** by script word count (NOT Whisper word indices — Whisper undercounts Hindi by 2x). Apply min-beat-duration 2.5s + redistribute so the centerpiece holds long enough.
7. Call `pipeline.compose.compose()` with image_paths + Beat[] + audio for word-level captions + Ken Burns + ffmpeg stitching
8. Post-compose ffmpeg pass: mix BGM at 12% under narration with 1.5s fade-out, then overlay the closer panel on the last 4s

**TODO for productionising this flow:** wire `--use-existing-cache` flag into `make_shorts.py` so the website button works without bypassing the orchestrator. The custom harness is fine for authoring; the standard pipeline should consume the same artefacts.

## Class-of-bug fixes shipped this session

| File | Fix | Why |
|---|---|---|
| `pipeline/audio.py:_SENTENCE_SPLIT_RE` | Added `।॥` to terminator chars | Hindi sentences end in danda, not period |
| `pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS` | 18-entry hyphen-respelling table | Kokoro Hindi voices collapse Devanagari conjuncts; hyphen forces syllable break (`अभि-मन्यु`) |
| `pipeline/audio.py:_HINDI_NUMERAL_RESPELLINGS` | `सात→साअत`, `सोलह→सोलाह` | Numeral homophones (`सात`/`साथ`) destroyed stakes |
| `pipeline/audio_critic.py:_tokenise` | `[a-z0-9']+` → `[\w']+` UNICODE | Silently dropped Devanagari → wpm=0 → pacing lenses bypassed |
| `pipeline/audio_critic.py` lenses L16-L21 | Hindi-aware regression checks | Future Hindi renders auto-check tatsama / numeral / closer-paragraph / danda / over-pause / tokeniser |
| `pipeline/beats.py:_norm_token` | `[^a-z0-9']+` → `[^\w']+` UNICODE | Word-caption alignment was empty for Hindi |
| `pipeline/captions.py:_find_font` | Devanagari-script probe → Devanagari Sangam MN | Caption renderer was Latin-only |
| `pipeline/captions.py:_split_closer_format` | Strip pattern `,.!? ` → `, ` only | Was eating author-intended `!` emphasis |

## Branding assets (in `hindutavaanimated/branding/` after reorg)
- **`icon_03_om_lotus.png`** ⭐ (1024×1024) — gold ॐ on peacock-petal lotus, **PICKED** as the channel icon (universal, brand-flexible across all Hindu content)
- **`banner_01_kurukshetra.png`** ⭐ (1344×768) — battlefield panorama with negative space for title, **PICKED** as the banner — needs ffmpeg lanczos-upscale to ≥2048×1152 before upload
- icon_01_krishna_mukut, icon_02_chakravyuh_wheel, banner_02_krishna_chariot — alternates kept for future use

## Episode candidates queued
Mahabharat: Eklavya guru-dakshina, Karna kavach-kundal daan, Bheeshma iccha-mrityu, Draupadi cheer-haran, Ashwatthama ka shraap.
Ramayan: Lakshman-rekha, Hanuman ke Sita-mata se milne (Ashok Vatika), Ravan ke das mukh, Shabri ke ber.
Krishna: Kaaliya naag-mardan, Govardhan parvat, Putana vadh, Makhan chor leelas.
For each, copy the hanuman triple (narrations/cast/prompts) as template.

## Open / TODO
1. Upload `icon_03_om_lotus.png` + upscaled `banner_01_kurukshetra.png` to YouTube Studio (manual — no API endpoint in our pipeline)
2. Wire `--use-existing-cache` into make_shorts.py so the website renders work for manual-source channels
3. **Whisper Hindi transcription regression**: whisper-mlx-4bit misses ~50% of dense Hindi narration words (44s audio came back as 65 words). For now we clamp + script-proportional, but a chunked Whisper pass (split audio at silence points, transcribe each chunk) would recover the lost captions in the CTA section. See `whisper_hindi_undercount.md`.
4. Author 3-5 more episodes for catalog depth before promoting the channel
