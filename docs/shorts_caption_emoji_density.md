---
name: Shorts captions must sprinkle emojis at one per 3-5 spoken words
description: Cross-channel rule — burned word-captions on every Shorts pipeline (history, sports, AITA, mythology) target one emoji every 3-5 words. Long-form sleep is exempt.
type: feedback
---
Every Shorts channel that burns word-by-word captions onto the rendered
video targets a density of **one emoji per 3-5 spoken words** in the
caption track. Not on every word (visual noise, kills readability), not
just at the start (no rhythm). Decided 2026-05-04.

**Why:** Emoji-rich captions out-perform plain text on Shorts retention
and re-watch — they give the eye a syncopated visual beat that pairs
with the audio cadence, and they preview the *vibe* of the line a beat
before the spoken word lands. Sleep / history / sports / mythology all
benefit; the emoji set differs per channel but the density rule is the
same.

**Scope:**
- Applies to: every channel that uses `pipeline.compose.prerender_word_captions`
  to render burned captions (historyrecapped Shorts, sportstoriesanimated,
  mystoriesanimated, hindutavaanimated, rhymetimejunction).
- Exempt: History Recapped long-form sleep videos. Per
  `historyrecapped/learnings/long_form_channel.md` they ship with no
  burned captions at all — that rule wins.

**How to apply:**
- The emoji-injection logic already exists per channel
  (`historyrecapped/emoji/inline_words/`, `pipeline.captions`'s twemoji
  side-paste). The change is the density target, not the mechanism.
- Aim for one emoji every 3-5 spoken words. Pick emojis that match the
  noun/verb at that beat-word — never random decoration. If no good match
  exists in the local set, leave that span plain rather than forcing a
  weak match (better gap than wrong-glyph).
- Validate via `/critique-video` on the first render after the change —
  watch for caption-blocks that are visually overloaded (multiple emojis
  adjacent, or emojis stacked on consecutive words) or that visually
  hide a word behind a pictogram.
