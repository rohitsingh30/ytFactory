---
name: SportsStoriesAnimated character consistency requires wiki dossier + per-character seed
description: Per-channel rule — sports shorts must ground supporting[] in real Wikipedia event data and lock per-character seeds so faces stay consistent across beats
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
For SportsStoriesAnimated, characters drifted between beats (cartoon Aguero looked like a different person every shot) on the v1 Aguero 93:20 render. User flagged this and asked for a Wikipedia "deep dive" on each event before rendering.

**Why:** z_image_turbo regenerates from text every beat with no cross-beat anchor; the channel-wide locked seed + a generic "compact Argentine forward in sky-blue #16" description isn't enough to keep faces stable. The story-specific cast must come from the actual event: who was on the pitch, what they looked like in *that* match (era-specific kit, hair, body type), and how their names are pronounced.

**How to apply:**
- Run `pipeline/wiki_research.py` (LLM via claude CLI) on every sports story BEFORE cast.py + script.py — output dossier at `data/intermediate/<channel>/dossier/<slug>.json` with `{ people: [{ name, role, visual: {...era-specific kit, body, hair, beard, era}, pronunciation_phonetic, seed }], match: {...}, key_moments: [...] }`.
- The dossier feeds cast.py so `supporting[]` is grounded in real people for that event, with each entry getting its own per-character seed (deterministic hash of name) for cross-beat anchoring.
- prompts.py / images.py must use the per-character seed when a beat features that character — not the channel-wide seed. Channel seed only applies to beats with no named character (pure-scene shots).
- The pronunciation_phonetic field flows into pre-TTS text substitution (see feedback_pronunciation_pretts.md) so Kokoro doesn't mangle names like "Aguero" / "Dzeko" / "Mbappé".
- This fix is sports-channel-specific in its driver (Wikipedia events have clean dossiers); per-character seed lock is a class-of-bug fix that applies to any channel with named recurring characters in supporting[].