---
name: Hindi TTS prosody — hyphen-forced syllables + tight pause budget
description: For Kokoro Hindi voices, mispronunciation is fixed via hyphen-respelling in _HINDI_TATSAMA_RESPELLINGS, and prosody works at 5% pause budget concentrated at paragraph boundaries
type: feedback
originSessionId: 757022d5-5a62-421f-b1a1-80fe5002bcdf
---
Validated 2026-05-03 on the abhimanyu-chakravyuh v3 render. Two non-obvious
techniques the user confirmed sound right:

**1. Hyphen-forced syllable separation** beats vowel-elongation respellings
for Kokoro Hindi (hf_alpha, hm_psi, etc).

  - **Why**: bare vowel-elongation forms like `अभीमन्यू` still get re-segmented
    by Kokoro at the wrong syllable boundary ("अभी मन्योर"). A literal ASCII
    hyphen (`अभि-मन्यु`) survives the phoneme pass and locks the syllable
    break. Same trick: `योद्धा → योद-धा`, `महारथियों → महा-रथियों`,
    `सब्सक्राइब → सब-स्क्राइब`, `गर्भ → गर्-भ`.
  - **How to apply**: when adding a new Mahabharat/devotional proper noun
    or tatsama word to ``pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS``,
    DEFAULT to the hyphen-forced form, not vowel-respelling. Confirm via
    a token round-trip synth (single token to Kokoro, manual listen).
    Final consonants need explicit halant (`बाण → बाण्`, `धनुष → धनुष्`)
    to prevent schwa absorption. Anusvara/chandrabindu (ं/ँ) get dropped
    silently — respell explicitly: `साँस → सान्स`, `तेरहवाँ → तेरहवान`.

**2. Pause budget ≤5% of audio, concentrated at paragraph boundaries** —
not scattered after every sentence.

  - **Why**: user's "too many breaks" complaint mapped to evenly-spaced
    inter-sentence pauses (~0.4s each × 17 sentences = 9% of audio). Even
    when individually justified by kathaa cadence, that pattern reads as
    choppy. Concentrating the same total budget at 4-6 paragraph
    transitions (and keeping mid-paragraph at 0.05-0.15s) reads as
    natural breath. Gravitas comes from per-sentence speed drops on
    weight words (0.78-0.82x), NOT from dead air.
  - **How to apply**: in script.json's `narration_prosody`, mark
    paragraph boundaries with `paragraph_end: true` and budget those at
    0.4-0.6s; non-boundary `post_pause_s` ≤0.15s. The source `narration`
    string MUST have matching `\n\n` breaks at those points so the
    pipeline's per-paragraph stitcher and the audio_critic's L3 lens
    both see the structure. Total pause budget enforced by L20 lens
    (≤5% of synthesised duration).

**Closer rule for Hindi devotional channels** (L18): lesson, blessing
(e.g. "जय श्री कृष्णा।"), and CTA must be three discrete paragraphs —
em-dash joins fuse them on hm_psi into one breathless run. CTA itself
should be comma-period joined ("...पसंद आई हो, तो कमेंट करें।") rather
than em-dash.
