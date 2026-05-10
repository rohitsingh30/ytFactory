---
name: Spoken closer is a 3-part CTA — natural-English question + comment invite + subscribe ask
description: Narrated closer = "Am I wrong?"-style question (NEVER AITA acronym) + comment invite + short conversational subscribe ask. Never robotic.
type: feedback
originSessionId: eee5c9ed-b6d4-47c9-859e-7790613cd4bc
---
The spoken closer of every AITA Short verbally asks viewers to subscribe — in addition to the question and the comment invite. The like icon is already embedded into the last beat's cartoon image, but the audio drives the strongest CTA action.

**Why:** User feedback 2026-05-03. Two preferences combined:
1. Narrator should verbally ask for subscribe (earlier design left this to the visual panel; user now wants it spoken)
2. Narrator must NEVER pronounce AITA-class verdict acronyms (AITA, WIBTA, YTA, NTA, NAH, ESH) or the literal phrase "am I the asshole" — the visual panel handles those, audio uses natural English instead

**How to apply (wired in `pipeline/rewrite.py:_closer_block`):**

Closer block in the rewrite system prompt requires three components, 2-3 short sentences max:
1. **Verdict question in plain English** — "Am I wrong here?", "Was I out of line?", "Am I the one in the wrong?". NEVER "AITA?", "WIBTA?", "Am I the asshole?".
2. **Comment invite** — natural language ("tell me what you would have done", "drop your verdict below").
3. **Subscribe ask** — short, conversational, NEVER YouTube-preset ("smash that subscribe button", "don't forget to subscribe", "hit the bell"). ~6 words, casual register.

Good audio closers:
- "Am I wrong here? Drop your verdict below — and stick around if you want more stories like this one."
- "Was I out of line? You decide. Subscribe for one of these every day."
- "Tell me what you would have done. Subscribe and I'll see you on the next one."

Bad (acronym-banned):
- "AITA? Drop your verdict below."  — verdict acronym is banned in spoken narration
- "Am I the asshole? Comment below." — literal phrase banned

Bad (robotic):
- "Smash that like button and subscribe."
- "Don't forget to subscribe and hit the bell."

**Defensive layer:** `pipeline/audio.normalize_for_tts._strip_verdict_acronym_sentences` removes any sentence containing AITA/WIBTA/YTA/NTA/NAH/ESH from spoken text before TTS, so re-renders of pre-2026-05-03 scripts also stay clean.

**Do not regress:** if a future critic finding says "the subscribe ask sounds awkward", iterate on the phrasing — don't drop the ask. Keep it human, keep it short, keep it. If a critic finds the audio still pronouncing a verdict acronym, the strip rule has a hole — fix the regex, not the symptom.
