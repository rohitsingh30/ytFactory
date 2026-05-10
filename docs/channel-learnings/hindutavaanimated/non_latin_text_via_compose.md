---
name: Non-Latin text in image must be overlaid by compose, never baked into the diffusion prompt
description: Z-Image-Turbo / Flux Schnell cannot reliably render Devanagari (and other Indic) conjuncts; they produce broken text (e.g. "जय कुष्णा" instead of "जय श्री कृष्णा"). Force text overlay post-diffusion.
type: feedback
originSessionId: 757022d5-5a62-421f-b1a1-80fe5002bcdf
---
Validated 2026-05-03 on the abhimanyu-chakravyuh closer beat. The prompt
asked for "जय श्री कृष्णा in gold leaf above the hands" baked into the
diffusion image. Z-Image-Turbo rendered:
- "जय" correctly
- The श्री was OMITTED entirely
- कृ→कु conjunct collapse
- Final result was "जय कुष्णा" — gibberish to a Hindi reader, embarrassing
  on a devotional channel

**Why:** diffusion models tokenise text as image-space patches and don't
understand Indic conjuncts (द्ध, क्ष, ज्ञ, र्भ, श्र). They reliably handle
basic Latin glyphs (because the training distribution has billions of
Latin-text images) but fail on every other script, even single tokens.
The single Devanagari "ॐ" is a special case — it's iconographic enough
that it's overrepresented as a graphic in training data.

**How to apply:** for any image prompt that targets a non-Latin channel
(Hindi, Tamil, Bengali, Arabic, Hebrew, CJK), the prompt MUST include:
> NO LETTERS, NO TEXT, NO WRITING anywhere in the image — [script name] script
> must NEVER be rendered by the diffusion model.

Then overlay the text via `pipeline.captions.render_closer_panel()` or
ffmpeg post-pass. The captions module's `_find_font` (post-2026-05-03)
auto-picks a script-appropriate font when the text contains Devanagari
characters; extend the same probe for other scripts as channels are added.

This reinforces the existing principle in
`feedback_compose_owns_closer_render.md` ("compose owns closer panel
render") — the principle was authored for the AITA-LIKE/COMMENT panel
that needed to ride on top of any beat-10 image, but the SAME principle
applies (more strongly) when the panel text is in a non-Latin script,
because there diffusion can't even hit the text correctly in the
first place.

**Single-glyph exception:** ॐ rendered cleanly in icon_03_om_lotus
(channel branding). Single iconic Unicode glyphs sometimes survive
because the training distribution treats them as graphics. For
multi-character Devanagari strings, never trust diffusion.
