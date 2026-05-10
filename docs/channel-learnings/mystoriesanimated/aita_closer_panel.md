---
name: AITA closer panel — like/comment split, not "vote"
description: For AITA-format Shorts, the closing CTA panel should map LIKE to YTA and COMMENT to NTA (or the reverse). Do not use "vote in the comments" framing.
type: feedback
originSessionId: ab2936df-9d5e-427a-971e-5bc41f6b4817
---
For AITA Shorts, the closer panel must split the two verdicts across **like** and **comment** — not ask viewers to "vote" or "comment 1 vs 2".

Specifically: **LIKE = asshole (YTA), COMMENT = not asshole (NTA).**

**Why:** "Vote in the comments / 1 NTA / 2 YTA" is weak — it asks for one engagement signal (a comment) and viewers ignore numbered choices. Splitting the verdict across two distinct UI actions captures *both* algo-positive engagement signals from the same audience: people who agree the OP is YTA tap the like (zero-friction), people who think NTA leave a comment (high-value engagement). The asymmetric friction also creates investment — comments mean more than likes on the platform.

**How to apply:** When rendering the closing panel for any AITA-format channel (aita_text, aita_animated, aita_cooking, etc.), use two pills labeled `LIKE if YTA` (red) and `COMMENT if NTA` (green), with a short prompt above. Never use "vote" framing or comment-with-number CTAs.
