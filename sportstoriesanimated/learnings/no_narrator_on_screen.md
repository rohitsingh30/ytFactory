---
name: SportsStoriesAnimated — narrator is voice-over only, NEVER appears on screen
description: Sports shorts must depict only the real people from the story (dossier supporting[]) or pure scene/object shots — never invent an analyst character
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
For SportsStoriesAnimated, the cartoon scenes must show ONLY:
1. A specific named person from the dossier (Aguero, Dzeko, Balotelli, Mancini, etc. — real people involved in the actual event), OR
2. A pure scene / object shot (empty stadium, scoreboard, trophy in case, ball on the spot, fans in the stands).

There must be **no recurring narrator character** on screen. The narrator is third-person voice-over only. The user explicitly flagged this on the v3 Aguero render: "you are using random face at the start, it should be only faces of relevant people in the story."

**Why:** Sports stories aren't first-person AITA-style narration. There's no "OP" telling their story. The story is the match. Inventing a fictional analyst persona and putting them on screen between actual players is jarring — it's a "random guy" the viewer has no relationship to. AITA's pattern (narrator IS the on-screen character) is the wrong template for this channel.

**How to apply:**
- `cast.cast_from_dossier` should set the narrator description to a voice-only sentinel (e.g. `"VOICE-OVER ONLY — never depict on screen, this is the narrator's voice not a visual character"`), NOT a visual character description like the one currently in `_SPORTS_NARRATOR_DEFAULT`.
- The channel YAML gets `narrator_visual_mode: voice_only` (vs default `"on_screen"` for AITA channels).
- `pipeline/prompts.py` (per-beat scene/key_visual author) must read the channel mode and, when `voice_only`, instruct the LLM: "DO NOT place a narrator on screen. Every beat depicts either a specific named character from supporting[] (use their name + kit + #) OR a pure object/scene shot from the match. The phrase 'the character' / 'the narrator' / 'a man watching' is FORBIDDEN."
- Critic should flag any sports-channel scene string containing "the narrator" / "the character" without a specific player name.