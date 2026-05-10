# A/V sync invariants — the pipeline owns it

Set as a non-negotiable rule by the user 2026-05-07 after a Hindi
mythology Short shipped out of sync, with the speaker changing
mid-render and 13 authored beats fragmented into 18 word-level
panels.

> "no it is your responsibility to sync images with audio generated,
> not just for this but for everything we generate for all the
> channels"

Out-of-sync output is a release blocker, not a "ship and iterate"
item. Applies to every channel, every Short, every long-form, every
render stage.

## What "in sync" means concretely

Eight invariants the pipeline must enforce. If any is violated, the
render must fail loud OR self-correct, never silently ship a
degraded Short.

### 1. Word-level A→B caption alignment

Every spoken word in the synthesised audio has a Devanagari/Latin
caption appearing precisely as it is spoken, ±50ms. Drift past
that band = fail.

- Driver: `pipeline/beats.py:transcribe_words` (Whisper word
  timestamps) → `pipeline/captions` (PNG overlay timing).
- Existing gate: `audio_critic.py` L3 (paragraph-break silence
  alignment).
- Hindi addition: chunked Whisper with forced `language='hi'`
  per `hindutavaanimated/learnings/whisper_chunked_hindi_forced.md`
  + 1.5s word-clamp per
  `hindutavaanimated/learnings/whisper_hindi_undercount.md`.

### 2. Authored beats preserved

If `narrations/<slug>.json:beats[]` declares N beats with their
own `.narration` text, the rendered video has **at most N panels**.

- The beat splitter MAY merge adjacent short authored beats into
  one panel.
- It MUST NOT re-split a single authored beat into multiple panels
  based on Whisper word-timestamp drift.
- Observed violation 2026-05-07: krishna-govardhan-dharan authored
  13 beats; renderer produced 16-18 word-fragment panels. Result:
  "centerpiece" beat (Krishna lifting Govardhan) didn't survive the
  re-split as a single sustained panel.
- Fix location: `pipeline/beats.py:split_into_beats` must respect
  authored beat boundaries when present.

### 3. No phantom silences

TTS chunking, normalisation, or compose stages MUST NOT insert
audio gaps that aren't in the source narration's `\n\n` paragraph
structure.

- Cloud TTS chunks already include natural head/tail silences;
  direct ffmpeg concat is enough. Adding 0.4s × N inter-paragraph
  silences puts seconds of "phantom" audio the source-text→word-
  timestamp aligner can't account for, drifting captions vs
  visuals.
- Fixed 2026-05-07 in `pipeline/tts/cloudrun.py:_synth_cloudrun_chunked`.

### 4. Voice consistency across chunks

When client-side TTS chunking is used (necessary for IndicF5 /
IndicParler past their ~22-30s caps), all chunks of one render
MUST share a stable seed so the same speaker is generated for
every paragraph.

- Description-driven cloud TTS (Parler-TTS) without `seed` set is
  non-deterministic. Different seed per call → different speaker
  per chunk → "different voices in the same short".
- Fixed 2026-05-07: `_synth_cloudrun_chunked` derives a SHA256-
  stable seed from the narration text and passes it to every
  chunk.

### 5. Caption fidelity to canonical source

Captions show what's spoken AND must be the canonical source-text
form, not the phonetic respelling.

- Kokoro-tuned hyphen-respellings (`सात → साअत`) help that voice
  pronounce conjuncts but should NOT show up in captions.
- Captions show `सात`; TTS internal text uses `साअत` only when the
  provider needs it.
- Achieved by:
  (a) Skipping respellings for native-Hindi cloud models (IndicF5,
  IndicParler) — fixed 2026-05-07 via `skip_hindi_respellings`
  flag in `normalize_for_tts`.
  (b) For Kokoro path: caption rendering uses original-source text,
  not the respelled-for-TTS form.

### 6. No silent truncation

If TTS produces audio significantly shorter than expected from
word-count × WPM × 1.4 tolerance, the pipeline fails loud — does
not silently ship a Short with the closer dropped.

- 2026-05-07 violation: 869-char Hindi narration (~60s expected) →
  21.6s synthesised audio because IndicF5 / IndicParler silent-
  truncated past their internal caps. Closer + lesson + blessing
  silently missing.
- Mitigated by paragraph chunking. A truncation-guard assertion
  is still TODO.

### 7. Cache must invalidate on source change

Source narration change → wipe `prompts.json`, `caption_*.png`,
`beats.json`, `word_*.png` so they're re-derived from the current
source. Otherwise stale prompts persist (e.g. Latin "Like /
Comment / Subscribe" tokens leaking into image prompts after
narration was Devanagari-fied).

- Existing: `pipeline/render/shorts.py` per-slug voice fingerprint
  invalidates `narration.wav`.
- TODO: extend fingerprint to cover the full source-text hash
  and wipe prompt + caption artefacts when it changes.

### 8. xfade-aware panel hold_s in long-form animated renders

Long-form animated renders use xfade-concat for panel transitions
with ~1.2s crossfade per pair. That overlap silently shortens the
visible video — every concat boundary subtracts `crossfade_s` from
the visible duration. If `hold_s = total_dur / N`, the visible
video ends `(N-1) × crossfade_s` seconds before the audio, and
ffmpeg's `-shortest` mux silently truncates the audio tail.

- Observed violation 2026-05-08 on krishna-govardhan-leela-long:
  65 panels × 1.2s xfade × 583s narration → 76.8s of closer +
  lesson + CTA + blessing silently dropped at mux.
- Fix in `pipeline/render/long_form.py:_flatten_chapters_to_panels`:
  ```
  hold_s = (total_dur_s + (N-1) × crossfade_s) / N
  ```
  And the auto-pad/scale block targets
  `target_total_panel_s = dur + (N-1) × crossfade_s`, not
  `target == dur`.
- After fix: hold_s = 9.5s, total_seg = 617.5s, visible = 540.7s
  ≈ dur. Audio fits fully.

## Owner

The pipeline + the relevant skill (e.g. /make-hindutava-short).
NOT the user. Out-of-sync renders surface immediately during the
critic phase or a pre-compose gate, not after the user opens the
mp4.

## Memory

- [`memory/feedback_pipeline_owns_av_sync.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_pipeline_owns_av_sync.md) — base rule
- [`memory/feedback_crossfade_aware_hold_s.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_crossfade_aware_hold_s.md) — sub-invariant #8
