---
name: Sanitise "asshole" → "a hole" pre-TTS for AITA shorts
description: Replace any spoken "asshole"/"assholes" + AITA-class acronyms with "a hole" form before TTS, so YouTube's profanity classifier doesn't yellow-icon AITA Shorts.
type: feedback
originSessionId: 5e765d5b-0fe6-4e45-ab49-d0138cfa29f4
---
The narrated word **"asshole"** must NEVER reach Kokoro/F5-TTS for AITA channels. It's the YouTube auto-classifier's profanity-flag trigger and yellow-icons every AITA Short for monetization. Replace with "a hole" pre-TTS — Kokoro pronounces it "ay-hole", Whisper transcribes the two words back, and the karaoke captions match the audio.

**Why:** User feedback 2026-05-03. The first two Shorts of this channel shipped with the literal "asshole" in narration; the next round sanitises.

**How to apply:**

Two layers in `pipeline/audio.py`, both inside `normalize_for_tts` (the single point all TTS providers funnel through):

1. **`_ACRONYM_PHRASES`** dict expansions for AITA-class acronyms now use "a hole":
   - `"AITA"` → `"am I the a hole"`
   - `"WIBTA"` → `"would I be the a hole"`
   - `"YTA"` → `"you're the a hole"`
   - `"NTA"` → `"not the a hole"`
   - `"NAH"` → `"no a holes here"`

2. **`_RE_PROFANITY_ASSHOLE`** word-bounded regex catches any standalone "asshole" / "assholes" still in the narration after acronym expansion (e.g. when the LLM rewriter wrote it directly). Preserves case-bucket (ALL-CAPS → "A HOLE", Title → "A hole", lower → "a hole"). Plural-aware ("assholes" → "a holes").

**Cache invalidation:** any change to this substitution logic invalidates cached `narration.wav` files because the spoken text is different. The voice fingerprint in `make_shorts.py:_voice_fingerprint` should bust automatically when the audio.py module reloads, but if you change just the regex without changing the fingerprint inputs, force-delete `data/cache/<slug>/narration.wav` to force re-synth.

**Do not regress:** if a future critic recommendation says "use the literal slur for impact", reject it — monetization > impact. The captions in the rendered Short ALSO say "a hole" (because Whisper transcribes the spoken audio), which is the same on-screen sanitisation YouTube creators use commonly.

**Future:** if YouTube's classifier tightens further, add a static "beep" splice — overwrite the audio sample range for any Whisper-transcribed "asshole" tokens with a sine-tone or silence. The current approach is the cheaper first-line defense.
