---
name: History Recapped long-form periodic support-ask pattern
description: Long-form sleep videos drop soft like/subscribe asks every ~15-20 min — INLINE in the narration only. Footage rolls continuously underneath; no animated ask screens, no video pauses, no cuts. The TTS handles the asks as part of the same chunk pipeline.
type: feedback
---

Every History Recapped long-form sleep video must include **periodic support asks** dropped in at regular intervals (default ~15-20 min). The end-of-video closer alone is not enough — by minute 60 of a 90-min video, most of the audience is already asleep and will never see a final CTA.

**Why:** User explicit requirement (2026-05-03). For long-form sleep content the engagement window is the first 20-30 min when viewers are still semi-awake; that's when the like/sub action lands. The user explicitly rejected the original animated-ask-screen design — pausing the video and cutting to a CTA card breaks the calm-footage spell and risks waking the viewer. Footage must keep rolling. The ask is voice-only.

**How to apply:**

1. **Cadence:** every ~15-20 min, including a final one as the closer. For a 90-min video that's ~5 asks (at roughly 0:18, 0:36, 0:54, 1:12, 1:30). For an 80-min video, ~4 asks.

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
