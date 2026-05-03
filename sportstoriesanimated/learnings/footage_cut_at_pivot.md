---
name: Sports footage cut-in lands at the narrative pivot, not the punchline
description: Cut to broadcast footage at the beat that introduces the climactic action ("Last kick"), not the beat describing the result ("Aguero scores") — and trim the goal-description narration so the broadcast commentary owns that moment
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
For SportsStoriesAnimated, the cartoon-to-broadcast cut should land at the **narrative pivot** — the beat where the climactic action *begins* — not at the beat that describes the punchline. The broadcast commentary takes over storytelling from that point through the call peak + crowd reaction.

User direction (2026-05-02 on the v7 Aguero render): "we should ideally cut at last kick of the game and then actual footage from there." Prior versions cut at "Aguero scores" — too late; the cartoon already showed the strike, then the broadcast started, creating redundancy.

**Why:** The footage cut is the dopamine payoff. Pivoting on the buildup beat ("Last kick of the season") spikes viewer pulse just as the visual switches to real action with original commentary. Cutting after the action has happened wastes the cut.

**How to apply:**
- The script JSON's `footage[].match_text` should match the **buildup beat**, not the result beat. Examples:
  - ✅ "Last kick" / "He doesn't even hesitate" / "Watch this"
  - ❌ "Aguero scores" / "It's in the net" / "GOAL"
- The footage window (in_s/out_s) typically covers 5-10s: 1-2s buildup + action + 2-3s after the audio peak (the iconic call). Find the call timestamp via numpy RMS sweep on the clip's audio.
- **Trim the script narration that overlaps the footage window.** Goal-description beats ("Balotelly to Aggwairo. One touch. Aggwairo scores.") become redundant once the broadcast plays — the commentary tells those moments. Either remove them from the script entirely (preferred) or accept the narration-duck during the footage (works but awkward).
- Resume narration AFTER the footage with a payoff/aftermath line ("Forty-four years of pain. Ended by one touch.") rather than re-describing the goal.
- Documentation in PIPELINE.md "Choosing optimal footage cut-in points" with the four craft rules (pivot, window size, trim overlapping narration, audio peak detection script).