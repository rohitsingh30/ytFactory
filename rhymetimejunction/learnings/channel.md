---
name: Rhyme Time Junction channel
description: New bilingual Hinglish nursery-rhyme channel with continuous animation (not slideshow), recurring mascot family, singing-voice only
type: project
originSessionId: eee5c9ed-b6d4-47c9-859e-7790613cd4bc
---
**Channel name:** Rhyme Time Junction
**Format:** Bilingual Hinglish nursery rhymes, ~90-120s standalone or ~6min compilations
**Animation:** Continuous (image-to-video) — NOT slideshow like other channels. Engine TBD (Wan 2.2 local vs Kling paid A/B).
**Voice:** SINGING ONLY. Kokoro is banned for this channel — it can't sing. External tool (Suno v4.5 likely) generates the song WAV; pipeline ingests it.
**Quality bar:** High (user explicitly called this out — do not downgrade silently).

**Mascot family v0** (locked, reuse forever):
- **Laddu** — boy ~5, round, mustard kurta + jeans, curious/bouncy
- **Jalebi** — girl ~7, twin braids, magenta frock, bossy big-sis
- **Tuk-Tuk** — pet (pug or parrot TBD), comic relief
- **Dadi** — grandma, sets up each rhyme
- Palette: marigold + magenta + teal + cream (warm Indian, not neon kids-TV)

**Pilot:** Hathi Raja Kahan Chale — standalone 90-120s, derisk singing voice + continuous animation on ONE rhyme before scaling to compilations.

**Why:** Diaspora bilingual lane is open (CocoMelon owns English, ChuChu owns Hindi); recurring mascots are the moat for kids' content; AITA-style flat 2D + continuous motion is differentiated.

**How to apply:** v0 manifest exists at `channels/rhymetimejunction.yaml` (authored 2026-05-03). Compilation strategy comes AFTER the standalone Hathi Raja pilot ships clean.

**Concrete pipeline gaps that block first render** (in priority order):
1. **`audio_provider: external_song` mode** — `pipeline/audio.py` must learn to skip TTS when the channel declares this; expects `<slug>/song.wav` to exist; runs ASR on the song for word timestamps; aligns to the canonical `lyrics: [...]` block in script.json (Whisper hallucinates on sung Hindi). Until this is wired, first render needs a temp Cartesia Arushi placeholder narration so the pipeline doesn't crash.
2. **Multi-mascot scene composition** — current `pipeline/cast.py` emits one narrator + supporting; rhyme scenes need 2-3 mascots in frame simultaneously, prompt-engineered per beat. v0 stuffs all mascots into `character_description` as a single block; per-mascot seeded sub-prompts are cleaner future work.
3. **Animation engine spike** — `motion_provider: animatediff_lcm` is the v0 (existing wiring); 3-way A/B vs LTX-Video 2B + Wan 2.2 5B I2V is pending. Spike script to author at `scripts/animation_engine_spike.py`. Defer until v0 renders + we have a baseline to compare.
4. **`/make-rhyme` skill** — author script.json with `lyrics: [...]` blocks, melody hint, beat-to-verse mapping. Doesn't exist yet.
5. **Upload account setup** — `upload.py auth --account rhymetimejunction` once the YouTube channel exists; `made_for_kids: true` is non-negotiable.

**Spoken bridge voice locked:** Cartesia Arushi `95d51f79-c397-46f9-b49a-23763d3eaa2d` (Hinglish female, see reference_cartesia_voice_ids.md).

**v0 ships SPOKEN, not sung (decision 2026-05-03):** user explicitly opted out of the manual Suno step ("dude I ain't doing shit, use api of cartesia right"). YAML reverted from `audio_provider: external_song` back to standard `tts_provider: cartesia` + Arushi. The Hathi Raja narration came out at 31s (Cartesia speaking 92 words at speech tempo, no melody) — this is ~3× shorter than the 110s the script targeted because there's no song duration. Visuals will run at the spoken tempo for v0; sung upgrade is a future task.

**Sung upgrade paths (later):**
- Suno via piapi.ai unofficial API (~$10/mo, TOS-fragile, fully automated)
- Cartesia spoken + ffmpeg music-bed mix-in from public-domain children's instrumentals
- Manual Suno (the originally-designed path) — `audio_provider: external_song` is wired and ready

**Cast.json class-of-bug discovered first render:** `pipeline/cast.py` auto-authoring overrode the channel-level `character_description` in the YAML. The four mascots got re-imagined as a bear cub (Laddu) + mouse (Jalebi) + pug (Tuk-Tuk consistent) + dadi (consistent) — animal mascots instead of the human-kid-with-pug brief. Channel YAML's `character_description` block isn't being honored as the mascot lock for per-story cast. Fix: cast.py should TREAT a channel YAML's `character_description` block as authoritative when provided and skip auto-authoring; or have a `cast_lock: true` flag the YAML can set.

**Beats.py doesn't read `lyrics: [...]`:** the existing beats path splits on Whisper ASR output of the spoken `narration` field; the rhyme-channel-only `lyrics: [...]` block is ignored. For v0 this is fine (12 auto-split beats vs 7 authored). For sung-Suno path it'll matter — beats need to align to verse boundaries from `lyrics`, not Whisper noise on sung Hindi.

