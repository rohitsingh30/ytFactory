---
name: History Recapped long-form support-ask pattern (REVISED 2026-05-04)
description: Long-form sleep videos drop exactly TWO soft like/subscribe asks — one at ~3-4 min in, one as the closer. Both inline in narration only; footage rolls continuously underneath; no animated screens, no cuts, no music dip. Earlier "every 15-20 min" rule is RETIRED.
type: feedback
---

Every History Recapped long-form sleep video must include exactly **two** soft support asks: one early in the first 4 minutes (when most listeners are still awake), and the closer.

**Why:** User explicit requirement (2026-05-03 original) for periodic asks; revised 2026-05-04 against the Sleepy Time History reference (`@SleepyTimeHistory`, 148K subs, 2.1M views on the early-humans episode). That channel does exactly two asks per 2hr+ video — one soft early ask, one closer — and out-monetizes the over-asking style. Pulling listeners out of the calm fugue every 18 minutes worsens watch-time retention more than it helps subscriptions; the listener is asleep by ask #3 anyway, so it's all noise, no upside. The user explicitly rejected the animated-ask-screen design — pausing the video and cutting to a CTA card breaks the calm-footage spell and risks waking the viewer. Footage must keep rolling. Asks are voice-only.

**How to apply:**

1. **Cadence (REVISED):** **TWO asks per video, not five.** One soft early ask at ~3-4 min in (right after the 90-second hook + first chapter setup, while listeners are still awake enough to act). Then nothing until the closer. The closer doubles as the final ask. The earlier "every 15-20 min" rule is RETIRED.

2. **Implementation: inline narration only.** The ask is a soft sentence (or two) embedded directly in the narration text at the right paragraph boundary. The chunked TTS pipeline renders it with the same voice / atempo / silence joiners as the rest. Footage rolls continuously underneath — NO animated ask screen, NO video cut, NO music dip. The ask just floats by as part of the audio.

3. **Soft-voice line** (gentle, sleep-consistent — never urgent or salesy):
   > "We put a lot of effort into bringing you these stories. If you've enjoyed listening, a quiet like helps the channel, and subscribing tells us to make more. Thank you for staying with us."

   Vary the wording slightly between asks so it doesn't feel mechanical (the same exact line 5 times in 90 min reads as a loop). Keep all variants in the same gentle register.

4. **Final closer.** The last ask is the closer — don't add a separate "thanks for watching" outro on top. Final-position version of the line:
   > "We put a lot of effort into bringing you this. If it helped you rest, a quiet like helps the channel, and subscribing tells us to make more like this. Sleep well."

5. **What we explicitly do NOT do:**
   - Animated ask screen overlay or cut-in
   - Pause the main timeline
   - Dim or duck the video brightness during the ask
   - Burn caption text on screen
   - Change the music bed dynamics

   Why: every one of those breaks the "set it and let it run" sleep-content register. The narrator's voice IS the only signal we send during the ask.

The Shorts channel's "LIKE to honor those who served. SUBSCRIBE for more such stories." closer does NOT carry over — that cadence is punchy/documentary and doesn't fit a sleep voice. The long-form mode uses its own gentle, support-framed inline asks above.
