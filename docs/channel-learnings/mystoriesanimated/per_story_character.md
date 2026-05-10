---
name: Lock aesthetic at channel, vary character per story
description: Channel YAML keeps style/aesthetic only; the character is authored per-story by an LLM to match the narrator persona.
type: project
originSessionId: f516de52-311a-490c-9cd2-584e7ebeb2f4
---
ytFactory's character lock model (DESIGN.md §14 Principle #2) was channel-locked: every Short on the AITA channel rendered the same round-headed cartoon girl in a yellow shirt. Critiques on aita02_v3, aita02_v4, and aita03 all flagged the same problem: the locked character mismatches the narrator (a 60yo MIL talking about her DIL, an adult man, etc.), so the cartoon kid grinning through a grandma's monologue is jarring.

**Why:** strong channel identity is supposed to come from *aesthetic* (line style, color palette, art tradition) — not from rendering literally the same character on every story. The narrator should match the story.

**How to apply:**
- `channels/aita_animated.yaml` retains `image_style_prefix` (the crayon look) but `character_description` moves to per-story `data/intermediate/<channel>/cast/<slug>.json`.
- `pipeline/cast.py` authors the narrator (age/gender/wardrobe) per story via the claude CLI given the source story + channel aesthetic.
- `make_shorts.py` reads `cast.json` first; falls back to `channel_cfg["character_description"]` for backwards compat.
- v1 is single-narrator-per-story (no supporting characters rendered). Schema leaves room for `supporting: []` later.
- The `image_style_prefix` should also drop the trailing "Plain pale beige background" — that's why every aita02/aita03 scene is a beige void; it overrides any setting the per-beat prompt tries to introduce.
