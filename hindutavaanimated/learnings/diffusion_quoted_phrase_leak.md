---
name: Z-Image-Turbo renders quoted phrases in prompts as literal on-image text
description: When a prompt contains a quoted descriptive phrase (e.g. "this is where you must go") OR mentions "negative space for caption", the diffusion model renders that text into the image — even when the prompt also says NO TEXT. Class-of-bug for any non-Latin channel.
type: feedback
---

## Symptoms (hanuman-sanjivani-parvat v1, 2026-05-03)

**Beat 04** (Sushena pointing to Drona Parvat). Prompt ended with:
> Composition like a 'this is where you must go' map-panel. NO TEXT.

Render included **"this is where you must go."** in legible English at the
top of the image, despite the explicit "NO TEXT" instruction.

**Beat 10** (lesson — Hanuman heroic stance). Prompt said:
> Composition leaves the lower-third of the frame as warm golden negative
> space so a Devanagari caption can sit there. … NO TEXT.

Render included **gibberish Devanagari** ("स्वाद्यांच्या वी वस्ली") in the
lower third — Z-Image-Turbo tried to fill the "where the caption goes"
region with caption-like glyphs because the prompt described the space's
purpose.

## Pattern (the class-of-bug)

Diffusion models trained on image-text pairs treat any **descriptive
mention of text** as a text-render request, regardless of negation:
- Quoted phrases (`'this is where you must go'`) → rendered as English
- "negative space for [caption / title / text]" → rendered as Devanagari/script glyphs (real or hallucinated)
- "where text will go" / "title sits here" → same

The "NO TEXT" guardrail does NOT override this behaviour because the
diffusion model already locked onto the text-region intent before
processing the negative.

## Rules for prompt authoring on Hindi/non-Latin channels

1. **Never quote phrases inside prompts**, even when describing composition style.
   Replace `Composition like a 'X' map-panel.` with `Iconographic map-style composition.`

2. **Never reference the caption / title / text overlay's location.**
   Replace `lower-third negative space for the caption` with descriptive
   visual language only: `lower-third dominated by warm gold and saffron tones`.

3. **Trust compose to overlay text post-diffusion.** All text (captions,
   closer panels, titles) must live in the compose pipeline (`pipeline.captions`),
   never in the diffusion prompt. The diffusion image should be visually
   complete WITHOUT any text and WITHOUT any region "reserved for text."

4. **Add explicit anti-text language to global_negatives:**
   ```
   "no Devanagari text in image",
   "no Latin letters in image",
   "no quoted phrases rendered as text"
   ```

5. **Prompt-lint check (TODO)**: scan prompts.json for any of:
   - `'…'` quoted strings
   - words "caption", "title", "text", "writing" in proximity to "where", "for", "negative space"
   - Reject the prompt with a hint message.

## Detection on future renders

If an image has on-image text where there shouldn't be, and the prompt
contains either a quoted phrase OR mentions caption/text positioning, that's
this class-of-bug. Fix: rephrase the prompt to remove the trigger, then
re-render the affected beat (~5-10 min on a warm pipe).

Both v1 leaks (English "this is where you must go" + gibberish Devanagari)
were eliminated by removing the trigger phrases — beats 04 and 10 v2
rendered cleanly.
