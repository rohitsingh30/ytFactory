"""Heuristic gate on raw stories — is this story illustratable?

Reddit's top-of-day mix includes plenty of stories that are mostly
internal monologue, mostly dialogue, or so abstract there's nothing
to draw. We catch those before TTS to avoid wasting Flux cycles on
unrenderable input.

Score 0..1. Threshold defaults to 0.4. Below → drop the story.
"""

from __future__ import annotations

import re

from pipeline import observability as _obs


# Concrete-noun seeds. Stories that mention specific objects/places
# render better than stories that are pure feelings/abstractions.
# Bias toward the AITA-style domestic-conflict vocabulary.
_CONCRETE_NOUNS = {
    # rooms / places
    "kitchen", "bedroom", "bathroom", "living", "garage", "yard", "garden",
    "restaurant", "table", "couch", "sofa", "bed", "door", "window", "car",
    "house", "apartment", "office", "store", "shop", "park", "street",
    "hospital", "school", "wedding", "funeral", "party", "dinner", "lunch",
    "breakfast", "airport", "train", "bus", "hotel", "pool",
    # objects / props
    "phone", "computer", "laptop", "book", "letter", "envelope", "package",
    "box", "bag", "purse", "wallet", "key", "ring", "necklace", "watch",
    "shirt", "dress", "jacket", "shoes", "hat", "tie",
    "cake", "pizza", "bottle", "glass", "plate", "fork", "knife", "spoon",
    "steak", "salad", "wine", "beer", "coffee", "tea", "milk", "bread",
    "money", "cash", "check", "bill", "receipt", "card", "stamp",
    "dog", "cat", "bird", "horse", "fish", "puppy", "kitten",
    "baby", "stroller", "toy", "doll", "bike", "scooter",
    # people roles (these tend to recur in AITA prompts)
    "husband", "wife", "boyfriend", "girlfriend", "partner",
    "mother", "father", "mom", "dad", "sister", "brother",
    "daughter", "son", "uncle", "aunt", "cousin", "grandma", "grandpa",
    "friend", "neighbor", "boss", "coworker", "stranger",
    "mil", "fil", "sil", "bil", "dil", "stepmom", "stepdad",
}

# Verbs that imply physical action (something to draw).
_ACTION_VERBS = {
    "walked", "ran", "drove", "flew", "left", "arrived", "came",
    "threw", "broke", "smashed", "tossed", "kicked", "punched", "slapped",
    "grabbed", "took", "picked", "dropped", "spilled", "poured",
    "cooked", "baked", "burned", "ate", "drank", "swallowed",
    "yelled", "screamed", "shouted", "laughed", "cried", "sobbed",
    "called", "texted", "messaged", "emailed",
    "showed", "wore", "carried", "held", "pointed", "wrote",
    "bought", "sold", "paid", "stole", "found", "lost",
    "kissed", "hugged", "pushed", "pulled", "shoved",
    "married", "divorced", "fired", "quit", "moved",
}

_DIALOGUE_RE = re.compile(r'"[^"]+"')
_NUMBER_RE = re.compile(r"\b\d+\b|\b\$\d+|\b\d+\s*(dollars?|bucks?)\b", re.IGNORECASE)


@_obs.traced("llm.visualizability.score_visualizability", category="llm")
def score_visualizability(text: str) -> tuple[float, list[str]]:
    """Return (score 0..1, list of reason strings).

    Heuristics:
    - Length: 200-3000 chars is the sweet spot for an AITA Short.
    - Concrete nouns: more specific nouns = more visual material.
    - Action verbs: physical action = drawable scenes.
    - Numbers / quantities: anchor a story in specifics.
    - Dialogue penalty: stories that are >50% dialogue inside quotes
      are largely "two people talking", which renders weakly.
    """
    if not text or not text.strip():
        return 0.0, ["empty"]

    reasons: list[str] = []
    score_parts: list[float] = []

    # 1. Length sanity. Too short = no detail; too long = needs heavy
    # condensation that loses specifics.
    n_chars = len(text)
    if n_chars < 200:
        score_parts.append(0.0)
        reasons.append(f"too short ({n_chars} chars)")
    elif n_chars > 5000:
        score_parts.append(0.3)
        reasons.append(f"long ({n_chars} chars) — risk of losing specifics")
    elif 250 <= n_chars <= 3000:
        score_parts.append(1.0)
    else:
        score_parts.append(0.6)

    words = re.findall(r"[a-zA-Z']+", text.lower())
    n_words = max(1, len(words))

    # 2. Concrete nouns per ~100 words. AITA stories typically have
    # 5-15 concrete nouns per ~200 words; below ~3 is suspect.
    n_concrete = sum(1 for w in words if w in _CONCRETE_NOUNS)
    concrete_per_100 = (n_concrete / n_words) * 100
    if concrete_per_100 < 1.0:
        score_parts.append(0.1)
        reasons.append(
            f"few concrete nouns ({n_concrete} in ~{n_words} words)"
        )
    elif concrete_per_100 < 2.0:
        score_parts.append(0.5)
    else:
        score_parts.append(1.0)

    # 3. Action verbs.
    n_actions = sum(1 for w in words if w in _ACTION_VERBS)
    actions_per_100 = (n_actions / n_words) * 100
    if actions_per_100 < 0.5:
        score_parts.append(0.2)
        reasons.append(f"few action verbs ({n_actions})")
    elif actions_per_100 < 1.5:
        score_parts.append(0.7)
    else:
        score_parts.append(1.0)

    # 4. Numbers / quantities — strong anchor for inciting wedge.
    n_numbers = len(_NUMBER_RE.findall(text))
    if n_numbers == 0:
        score_parts.append(0.4)
        reasons.append("no concrete numbers / dollar amounts / quantities")
    else:
        score_parts.append(1.0)

    # 5. Dialogue-heaviness penalty. Stories that are mostly two
    # people talking ("...", "...") render weakly — diffusion can't
    # show conversation.
    quoted_chars = sum(len(m) for m in _DIALOGUE_RE.findall(text))
    dialogue_share = quoted_chars / n_chars
    if dialogue_share > 0.5:
        score_parts.append(0.1)
        reasons.append(
            f"dialogue-heavy ({dialogue_share:.0%} of text in quotes)"
        )
    elif dialogue_share > 0.3:
        score_parts.append(0.6)
    else:
        score_parts.append(1.0)

    score = sum(score_parts) / len(score_parts)
    return score, reasons
