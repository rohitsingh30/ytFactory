---
name: History Recapped long-form prose style — second-person novelistic, mythbust per chapter
description: Long-form sleep narration is a novel told softly to a single listener — second-person, sensory-led, one mythbust per chapter. NOT encyclopedia voice, NOT documentary voice-over, NOT a Wikipedia rewrite. Reverse-engineered from Sleepy Time History's "Early Humans" episode (2.1M views).
type: feedback
---

The voice + cadence of our long-form sleep narration is locked (Sarah F5-clone, ~95-110 wpm). The **prose style** is what carries the listener for 60-120 minutes — and it's the part most likely to drift if an LLM-authored script defaults to "documentary mode."

**Why:** The LLM-default for "write me a 90-minute history script" is encyclopedia or doc-narrator register — third-person, fact-dense, transition-heavy, lecture-shaped. That voice works for short clips and Wikipedia. It does *not* work for sleep content; viewers complete on the prose-quality leg of the show, not on the facts. Sleepy Time History's chapter prose reads like a novelist's chapter, not a Wikipedia article.

**How to apply.** Author every long-form narration JSON to honor these rules. Run a final pass before render to grep for the anti-patterns and rewrite.

### Rule 1 — Second-person, present-tense default

The dominant grammatical voice is *you, you are, you wake, you walk, you watch*. The listener inhabits the scene. Slip into past tense + third person only for explicit historical attribution sentences:

> ✅ *"You wake before the others. The fire has burned down to a low orange glow, and the air outside the shelter is sharp with frost."*
> ✅ *"Your ancestors 40,000 years ago didn't have breakfast — not in the way you think about it."*
> ❌ *"Early humans woke at dawn and ate when food was available."* (third person, past tense — kills immersion)
> ❌ *"It is widely believed by anthropologists that..."* (lecture register)

### Rule 2 — Sensory-led sentences

Every paragraph should land at least one specific sense detail in its first or second sentence. Not "it was cold" — *"the cold settles into the small bones of your hands first, then your face."* Not "the camp was loud" — *"someone is singing softly across the fire, and a baby cries briefly, then settles."*

The five sense palette per chapter:
- **Sight**: light direction, color temperature, movement at the edge of vision
- **Sound**: human voices distant, wind through specific material, fire crackle, animal calls
- **Smell**: wood smoke, wet wool, leather, river silt, blood (rare, only when story-justified)
- **Touch**: temperature, fabric texture, weight in the hands
- **Taste**: only at the meal beats — bitter root, fat, smoke

### Rule 3 — One mythbust per chapter

Every chapter (~10-15 min beat) opens with the cliché the listener already carries, then dismantles it. This is the structural promise that keeps the runtime from feeling padded. Patterns:

- *"Here's what nobody tells you about prehistoric life. The hunt gets all the glory..."* → then explains gathering provided 60-80% of calories.
- *"If you imagine early humans as exhausted half-starved figures trudging endlessly across the landscape, you're not alone. That image has been drilled into us for decades..."* → then explains they had ~3-4 hours of leisure daily.
- *"Hollywood gives us the over-the-top charge — the whistles, the bayonets, the smoke. But that was 1% of the war..."* → then explains the boredom-and-mud reality.

Author every chapter intro to follow this template:
1. Name the cliché (1-2 sentences)
2. Acknowledge the listener also believes it (1 sentence — *"you're not alone"* / *"we've all seen it"*)
3. Pivot (*"But here's the truth..."* / *"What actually happened..."*)
4. Land the surprising fact

### Rule 4 — Sentences vary in length

Sleep narration that runs all-medium-length sentences becomes hypnotic in the wrong way — listeners notice the meter and snap awake. Mix:
- Short punch sentences for emphasis: *"It's quiet. Then it isn't."*
- Medium sensory sentences for transport: *"The air carries the scent of wood smoke and earth."*
- Long winding sentences for reflection: *"This wasn't because our ancestors were particularly fond of vegetables — it was simple evolutionary math, the same math that any species running on a tight calorie budget eventually settles on."*

Target distribution per chapter: ~15% short (≤8 words), ~60% medium (9-22 words), ~25% long (≥23 words). Long sentences must be parseable on a single breath; if the comma-count exceeds 4, split it.

### Rule 5 — No documentary connectives

Strip the LLM-default transitions. Replace with sensory or temporal anchors.

| ❌ Don't say | ✅ Do say |
|---|---|
| *In conclusion,* | *The fire is dying down now.* |
| *To understand X, we must first...* | *Picture this:* |
| *It is important to note that* | (just say the thing) |
| *As mentioned earlier* | (don't reference earlier — listener is half-asleep) |
| *Studies have shown that* | *Archaeologists today think* |
| *Furthermore* | (delete; start a new sentence) |
| *In summary* | (delete entire sentence) |

### Rule 6 — Numbers are spoken, not cited

The audience can't verify a citation while falling asleep. Numbers should land as story-rhythm, not statistics:

> ✅ *"A band like yours might have twenty, maybe thirty people. Rarely more than fifty."*
> ❌ *"Hunter-gatherer band sizes typically ranged from 25-50 individuals according to ethnographic studies."*

When a number is required for accuracy (battle dates, troop counts), state it once, then return to prose. Never list more than two numbers in the same sentence.

### Rule 7 — Repeat anchor phrases across chapters

Sleepy Time History reuses *"40,000 years ago"* and *"your ancestors"* as rhythmic anchors throughout the early-humans episode. The repetition builds a low cognitive ostinato — the listener doesn't have to track context, they sink into the loop.

For each long-form episode, pick 2-3 anchor phrases at authoring time and repeat them at every chapter open + 1-2 times mid-chapter. Examples:
- Western Front 1914-1918: *"the trench"*, *"1916"*, *"your section"*
- Early humans: *"40,000 years ago"*, *"your band"*, *"the fire"*

### Authoring workflow

1. Outline the 8-12 chapter beats first (each with a one-line cliché-to-bust)
2. Write the 6-beat hook per `long_form_hook_template.md`
3. Author each chapter at ~700-900 words (≈ 7-10 min audio at our cadence)
4. Embed exactly two soft asks per `long_form_support_asks.md`: one ~3 min in, one as the final closer. No others.
5. Final pass: grep for documentary-connective tokens (`Furthermore`, `In conclusion`, `It is important to note`, `Studies have shown`) — rewrite.
6. Final pass 2: scan first sentence of every paragraph — at least 50% should hit a sense detail. If not, rewrite.

### Where this fits

This document supplements the structural specs in `long_form_channel.md` (format) and `long_form_hook_template.md` (the first 90s). Together those three docs are the long-form authoring spec. Renderer config + visual grade is separate (`long_form_visual_signature.md`).
