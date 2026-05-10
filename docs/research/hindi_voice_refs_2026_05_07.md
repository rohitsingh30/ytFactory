# Hindi voice-clone reference research — 2026-05-07

Research delivered after the krishna-govardhan-dharan render shipped
with audibly flat Hindi narration despite stable seed + voice-clone
anchoring + per-chunk speed normalisation. User feedback:
"so so so monotonous … use indic parler man".

The root cause is **the ref WAV itself**, not the chunker. The current
ref is sourced from AI4Bharat IndicVoices-R "announcer-quality"
selection — a reading-task corpus, designed to be prosodically flat.
F5-family voice-clone TTS faithfully copies the ref's F0 contour
into every synthesised paragraph, so a flat ref produces flat output
regardless of how aggressively we vary chunk-level speed knobs.

## Current state (the bug)

- Ref: `pipeline/voice_refs/bench/hindutavaanimated__shorts__hindi_female_storyteller/ref.wav`
  (8.0s, 24 kHz mono, ~70.6 dB SNR, F0 ~200 Hz, female 30-45).
- Source: AI4Bharat IndicVoices-R, CC-BY-4.0.
- Bench score: highest of 4 Hindi cells in the 2026-05-07 voice matrix.
- Production result: flat, monotonous, audibly identical prosody
  across all 13 paragraph chunks of a dramatic Hindi mythology Short.

## Three Hindi narrator archetypes for HindutavaAnimated

Pick deliberately, do not blend within a single render:

1. **Kathavachak / pravachan** — Morari Bapu, Devkinandan Thakur,
   Jaya Kishori register. Wide pitch range, theatrical pauses,
   melismatic stress on dharma keywords. Fits dramatic Mahabharat /
   Ramayan Shorts.
2. **Doordarshan documentary narrator** — Harish Bhimani / Tom Alter
   cadence. Low-baseline, controlled, theatrical-but-not-sung. Fits
   the 12-beat hook → twist → lesson Short structure.
3. **Audiobook / radio-natak storyteller** — Suno Kahani / archive.org
   audiobook register. Conversational warmth, mid-pitch, natural
   breath, low theatricality. Fits calm long-form kathaa.

Channel bifurcation: **dramatic Shorts → kathavachak**;
**long-form kathaa → audiobook-storyteller**. The current ref is
closest to archetype #3 (mid-pitch announcer) which is exactly why
it falls flat on dramatic content.

## Open-license Hindi voice datasets (non-fluff survey)

| Dataset | License | Hindi hours | Quality | Verdict |
|---|---|---|---|---|
| AI4Bharat **IndicVoices-R** | CC-BY-4.0 | ~340h | TTS-grade; flat F0 contour by design | Current production. Filter by pitch-variance, not just SNR, for any future picks. |
| AI4Bharat **Rasa** | CC-BY-4.0 (announced) | ~1h neutral + 30 min × 6 Ekman emotions | Studio-grade + **emotion-labelled** (happy/sad/angry/fear/disgust/surprise) | **Highest-leverage candidate.** Verify Hindi shard release status. |
| SPRINGLab **IndicTTS-Hindi** | License-gated (`smtiitm@gmail.com` clearance required) | 1 male + 1 female pro voice artist, 11,825 utterances | **Cleanest single-speaker Hindi corpus.** | Studio-grade; legal gate to clear before commit. |
| Mozilla **Common Voice Hindi** | CC0 | ~20-30h | Phone mics, 50-60 dB SNR, regional accents | Aggressive SNR + pitch-variance filter required. |
| ARTPARK-IISc **Project Vaani** | CC-BY-4.0 | ~150,000h target across 773 districts | Field recordings, varied SNR | Regional accents (UP / Bihar / MP) match audience demographic. |
| **LibriVox Hindi** | Public Domain | <10 audiobooks; female-narrator works confirmed | Volunteer recordings | One-off mining for kathaa archetype. |
| archive.org **Premchand collections** | Mixed — text PD, recordings often `© HindiYugm` | Hours of Premchand-text narration | Verify per-item PD tag before pulling. | Suno Kahani recordings excluded (copyrighted). |
| archive.org **Gita Press Hindi** (e.g. `GitaHindi`) | PD-marked items | Multi-hour Gita / Ramayan recitations | Devotional register | Verify per-item PD tag. |

**Excluded (non-PD or restrictive):**
- Morari Bapu pravachans → CC BY-NC-ND 3.0
- Doordarshan Mahabharat 1988 → still in copyright until 2050
- All India Radio archives → Prasar Bharati commercial licenses
- Audible Hindi catalog → all rights reserved

## Anti-patterns — what produces a flat clone

- **Reading-task corpora as refs.** Trained-listener register = flat
  F0 by design. Current production bug.
- **Refs ending mid-phrase or on commas.** F5 destabilises → falls
  back to monotone. Always end on `.` `?` or `!`.
- **Sanskrit chanting as ref for prose.** Carries svara/anudatta tonal
  patterns into prose narration → robotic singing. Class-of-bug per
  `hindutavaanimated/learnings/sung_vs_prose_kand.md`.
- **<5s refs** — model under-determined, falls back to mean.
  **>15s refs** — IndicF5 truncates and the cut may land mid-breath.
- **Phone-mic reverb / room tone (>200ms RT60).** F5 clones the
  room — every chunk sounds boxy.
- **Pitch-flat speakers (<30 Hz F0 std-dev within the clip).**
  Even at 70 dB SNR the synthesis inherits the flatness. **Filter
  candidates by pitch-variance, not just SNR.**
- **Cross-script pollution** (Hindi ref + English/transliterated
  transcript). F5 alignment breaks.
- **Multiple speakers in the ref window.** Always solo-speaker.

## Single biggest leverage move

**Switch from one mid-pitch IndicVoices-R announcer ref to a 3-ref
rotation, keyed by beat type, picked from emotionally-labelled or
audiobook-storyteller sources.**

| Voice name | Use cases | Pull from |
|---|---|---|
| `hindi-female-storyteller-dramatic` | mythology Shorts hook + twist + centerpiece | AI4Bharat Rasa `surprise`/`angry` clip when Hindi shard ships; fallback IndicTTS-Hindi female studio `?`-ending utterance |
| `hindi-female-storyteller-calm` | Shorts setup + lesson; all kathaa long-form | LibriVox Premchand mid-paragraph clip OR IndicTTS-Hindi statement-ending utterance |
| `hindi-female-storyteller-devotional` | kathaa invocations / opening shloka glosses / dedications | archive.org PD-marked Bhagavad Gita Hindi-translation passage (the prose, not the Sanskrit shloka) |

Per-chunk dispatch: extend the script JSON's beat schema with a
`beat_register: dramatic | calm | devotional` annotation, then
extend `pipeline/voice_catalog.resolve_voice()` to honour it. F5 is
zero-shot — switching the ref WAV between cloud calls is free and
is the canonical way to inject prosodic range without fine-tuning.

## Concrete next-action sequence

1. **Confirm Rasa Hindi shard release** on Hugging Face / GitHub.
2. **Pull 5 candidate clips per archetype** (15 total) from the
   sources above, with full provenance:
   - `pipeline/voice_refs/<name>/ref.wav` (5-15s, 24 kHz mono PCM_16,
     ≥65 dB SNR, ≥30 Hz F0 std-dev within the clip, ends on
     `.`/`?`/`!`)
   - `pipeline/voice_refs/<name>/ref.txt` (verbatim Devanagari
     transcript)
   - `pipeline/voice_refs/<name>/_source.txt` (URL + license)
3. **Bench all 15** through the existing 2026-05-07 matrix harness
   against an abhimanyu-style dramatic Short and a calm kathaa
   passage.
4. **Commit the 3 winners** under `pipeline/voice_refs/<name>/` and
   add catalog entries.
5. **Extend the script schema + resolver** to dispatch refs
   per-chunk by `beat_register`.
6. **Re-render krishna-govardhan-dharan** to validate the flatness
   complaint resolves.

Why this beats single-ref alternatives: a single "more dynamic" ref
over-performs on calm beats; a single "calmer" ref reproduces
today's flatness. Rotation gives the synth a different prosodic
target per beat without ever touching the model itself.

## Sources

- [AI4Bharat IndicVoices-R (HF)](https://huggingface.co/datasets/ai4bharat/indicvoices_r)
- [AI4Bharat Rasa (HF)](https://huggingface.co/datasets/ai4bharat/Rasa) / [paper](https://arxiv.org/html/2407.14056) / [releases](https://github.com/AI4Bharat/Rasa/releases)
- [SPRINGLab IndicTTS-Hindi (HF)](https://huggingface.co/datasets/SPRINGLab/IndicTTS-Hindi) / [IIT-M Indic TTS DB](https://www.iitm.ac.in/donlab/indictts/database)
- [Mozilla Common Voice Hindi](https://commonvoice.mozilla.org/hi)
- [ARTPARK-IISc Project Vaani](https://huggingface.co/datasets/ARTPARK-IISc/Vaani) / [Vaani v1 access](https://vaani.iisc.ac.in/dataset/Version1)
- [LibriVox public domain policy](https://librivox.org/pages/public-domain/)
- [archive.org — Do Sakhiyan / Premchand / Sonali Ekka](https://archive.org/details/dosakhiyan_2512_librivox)
- [archive.org — Bhagvad Gita As It Is, Hindi](https://archive.org/details/GitaHindi)
- [archive.org — Mahabharat by Shri Rajendra Das Ji](https://archive.org/details/ByShriRajendraDasJi)
- [F5-TTS ref-audio guidance #965](https://github.com/SWivid/F5-TTS/issues/965)
- [AI4Bharat IndicF5 model card](https://huggingface.co/ai4bharat/IndicF5)
- [99pandit — Top 10 Katha Vachak (archetype reference)](https://99pandit.com/blog/top-10-katha-vachak-in-india/)

## Memory

[`memory/feedback_voice_ref_must_have_pitch_variance.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_voice_ref_must_have_pitch_variance.md)
