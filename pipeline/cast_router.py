"""Stage 6.4 — pick the right character description per beat.

Without this, ``make_shorts.py`` prepended ``cast.narrator.description``
to every beat unconditionally, which is why supporting characters
(daughter, Ryan, Ryan's mom, etc.) never appeared correctly even though
``cast.json:supporting[]`` had rich descriptions for all of them.

Critic finding 2026-05-02 on ``amitheasshole-aita-for-telling-my-
daughter-i-am-disgusted-by``: 22 of 24 beats showed only the narrator
even though the narration named four other people. The story played as
a 60-second monologue. Fix: when a beat's narration / scene / key_visual
references a supporting character's name or alias, prepend THAT
character's description instead of the narrator's.

The matching pattern mirrors ``make_shorts.py:_seed_for_beat`` so the
same beat that gets a supporting character's seed for face-consistency
also gets that supporting character's description for body/clothing
consistency. The two fixes share the same alias scan — keep them in
sync.
"""

from __future__ import annotations

import re
from typing import Iterable


def _word_bounded(name: str, haystack: str) -> bool:
    """Return True if `name` appears in `haystack` as a word/phrase.

    Cheap version — checks the substring sits between non-letter chars.
    Mirrors the matching used by ``_seed_for_beat`` in make_shorts.py so
    seed-routing and description-routing pick the same character on the
    same beat.
    """
    n = name.lower().strip()
    if not n:
        return False
    h = haystack.lower()
    idx = h.find(n)
    while idx != -1:
        left_ok = idx == 0 or not h[idx - 1].isalpha()
        right_ok = (
            idx + len(n) == len(h)
            or not h[idx + len(n)].isalpha()
        )
        if left_ok and right_ok:
            return True
        idx = h.find(n, idx + 1)
    return False


def route_character_description(
    *,
    beat_text: str,
    scene: str,
    key_visual: str,
    narrator_desc: str | None,
    supporting_full: Iterable[dict] | None,
) -> tuple[str | None, str | None]:
    """Return (description, matched_character_name) for one beat.

    The scan order is:

      1. Scene / key_visual first — those are the LLM-authored visual
         intent for THIS beat; they should win over the spoken narration
         if they explicitly name a different character. Example: beat is
         spoken in first person ("I scrolled through them") but the
         scene description is "a young woman holding up a smartphone"
         (the daughter) — render the daughter, not the narrator.
      2. Beat narration text as a fallback. This catches beats whose
         scene happens to omit the name even though the spoken text
         clearly references the character ("Ryan thanked me").
      3. If nothing matches, fall back to the narrator description.

    Returns ``(narrator_desc, None)`` when no supporting character is
    referenced. Returns ``(supporting_desc, name)`` when one is — the
    narrator description is dropped so the diffusion attention isn't
    fighting two simultaneous appearance specs (the same reason
    images.build_full_prompt prepends one description, not many).

    A beat that names BOTH narrator-relative ("I") and a supporting
    character ("Ryan thanked me") still routes to the supporting
    character: the narrator is the audio voice, the supporting character
    is what we want VISUALLY in frame for that beat.
    """
    sup = list(supporting_full or [])
    if not sup:
        return (narrator_desc, None)

    haystacks = (scene or "", key_visual or "", beat_text or "")
    for hs in haystacks:
        if not hs.strip():
            continue
        for entry in sup:
            desc = (entry.get("description") or "").strip()
            if not desc:
                continue
            names: list[str] = []
            primary = (entry.get("name") or "").strip()
            if primary:
                names.append(primary)
            for a in entry.get("aliases") or []:
                a = (a or "").strip()
                if a:
                    names.append(a)
            for n in names:
                # Avoid one-letter false matches (e.g. "I" alias).
                if len(n) < 2:
                    continue
                if _word_bounded(n, hs):
                    return (desc, primary or n)

    return (narrator_desc, None)


# Tokens whose presence in a beat's scene/key_visual implies the
# narrator's body should be on screen. When these are present AND no
# supporting character was matched, we keep the narrator description.
# When NEITHER is present (purely abstract beat: "a wall calendar with
# pink and blue dots"), the caller may choose to pass description=None
# to render a clean object scene without forcing a character into it.
_NARRATOR_BODY_HINT_RE = re.compile(
    r"\b(?:the\s+character|narrator|"
    # First-person and possessive — implies the narrator is the actor
    r"\bi\b|\bmy\b|\bme\b|\bmyself\b|\bwe\b|\bour\b)\b",
    flags=re.IGNORECASE,
)


def is_object_only_beat(scene: str, key_visual: str, beat_text: str) -> bool:
    """Return True when the beat is purely about an object/scene with no
    person needed in frame.

    Heuristic: scene + key_visual contain none of the narrator-body
    hint tokens AND no supporting-character names were matched (caller
    determines that). Examples that should return True:

        "a wall calendar with pink and blue dots alternating"
        "three tall wine bottles standing in a row"
        "a gold ring on a wooden floor between two pairs of feet"

    Used to suppress the unconditional character prepend on those
    beats, since prepending forces a person into a frame that didn't
    need one and the diffusion model often complies by stuffing the
    narrator into otherwise clean object scenes.
    """
    combined = f"{scene} {key_visual}".strip()
    if not combined:
        return False
    return not _NARRATOR_BODY_HINT_RE.search(combined)
