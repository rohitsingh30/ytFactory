---
name: Never pronounce AITA-class verdict acronyms in narration
description: AITA / WIBTA / YTA / NTA / NAH / ESH and the phrase "am I the asshole" are banned in spoken narration; visual closer panel still displays them
type: feedback
originSessionId: eee5c9ed-b6d4-47c9-859e-7790613cd4bc
---
**Rule:** AITA-class verdict acronyms (AITA, WIBTA, YTA, NTA, NAH, ESH) and the literal English phrase "am I the asshole" / "am I the asshole?" must NEVER appear in the spoken narration of any Short. The hook uses natural-English framing ("Am I wrong for…", "Was I out of line for…"); the closer asks for the verdict in plain English ("Am I wrong here?", "Was I out of line?"). The visual closer panel — rendered separately from `cfg["closer_format"]` via `pipeline/compose.render_closer_panel` — still displays "LIKE if YTA / COMMENT if NTA" on screen as the engagement ask.

**Why:** User feedback 2026-05-03 — letter-spelled AITA sounds robotic, and the prior expansion to "am I the a hole" sounded off-tone for the channel. The acronyms now live entirely in pixels, never in audio.

**How to apply — TWO-LAYER enforcement:**

1. **LLM authoring layer** (`pipeline/rewrite.py` + `pipeline/script_check.py`):
   - `_SHARED_CRAFT_RULES` forbids these tokens in narration
   - `_BASE_PROMPT` opening rule says hook uses "Am I wrong for…" framing, never AITA frame
   - `_closer_block` instructs the LLM to ask the verdict in plain English; lists every acronym + the literal asshole phrase in the BAD examples
   - `_cliffhanger_closer_block` BAD examples updated likewise
   - `script_check.py` `_CTA_PATTERNS` no longer requires AITA tokens; `has_wrong_frame` replaces `has_aita_frame`

2. **Audio defensive layer** (`pipeline/audio.py`):
   - `_strip_verdict_acronym_sentences(text)` deletes any sentence containing AITA/WIBTA/YTA/NTA/NAH/ESH before TTS
   - Runs FIRST in `normalize_for_tts` so the downstream all-caps + acronym passes never see them
   - Whole-sentence removal (not just the token) — bare-token deletion would leave broken fragments like " for refusing to host?"
   - The verdict acronyms are also REMOVED from `_ACRONYM_PHRASES` and `_AITA_ACRONYMS_CI` so even if the strip is bypassed they no longer expand

**What stays unchanged:**
- Visual closer panel (compose.render_closer_panel) reads `cfg["closer_format"]` and renders the on-screen engagement ask — keep "LIKE if YTA / COMMENT if NTA" panels exactly as they are
- Reddit-class abbreviations MIL/FIL/SIL/BIL/DIL/OOP/TIFU/TIL stay allowed and read letter-by-letter — those are fine, just the verdict acronyms are out

**Don't regress:** if a critic flags "audio still says A-I-T-A", the regex `_RE_VERDICT_ACRONYM` has a hole or the rewriter prompt drifted — fix at the LLM level first, audio strip is the safety net not the primary fix.
