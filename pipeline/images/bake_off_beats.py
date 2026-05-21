"""Hard-coded representative beats for the 4-way image-model bake-off.

Each beat reproduces a real failure mode surfaced by the 2026-05-13 audit
(``docs/pipeline_bug_catalogue_v2_2026-05-14.html``):

* ``mahabharat_arjun_battle`` — Hindi-mythology character lock + period
  costume; HindutavaAnimated reference panel.
* ``aita_couch_conflict`` — modern MyStoriesAnimated AITA scene that
  produces the most gibberish in-panel text under prod.
* ``sports_ronaldinho_pitch`` — the "5 different Ronaldinhos" bug — same
  character should look the same across seeds.
* ``history_baghdad_mongol_1258`` — the "WW1 trench in 1258" bug — era
  anchor must dominate the modern-era attractor.

These beats are deliberately stable + diverse: one Indian (Vedic), one
modern Western, one sports identity, one 13th-century military. They're
the smallest representative set that exercises all four channels'
visual styles + era-anchor + character-lock at once.

Used by:
* ``scripts/image_bake_off.py`` — the CLI harness
* ``tests/test_prompt_per_model.py`` — the prompt-builder unit tests
"""

from __future__ import annotations


BEATS: dict[str, dict[str, str]] = {
    "mahabharat_arjun_battle": dict(
        subject="ancient Indian warrior prince Arjun",
        action="drawing back his recurve bow Gandiva at the climax of battle",
        scene="standing on his royal chariot, smoke and distant warriors",
        style=(
            "Amar Chitra Katha comic-book illustration, flat saturated "
            "colors, bold black ink lines"
        ),
        era="Vedic-India late Bronze Age 1200 BCE",
        character=(
            "young man mid-20s, fair skin, long black hair tied back in a "
            "topknot, blue silk dhoti with gold trim, golden chest armor "
            "breastplate, ornate quiver of arrows on back"
        ),
        mood="heroic, climactic",
        shot_type="wide cinematic shot, subject centered, vertical 9:16",
    ),
    "aita_couch_conflict": dict(
        subject="frustrated young woman",
        action="sitting on a couch holding her phone, looking annoyed",
        scene="modern apartment living room, mid-day",
        style=(
            "sketch cartoon illustration, pastel fills, soft black ink "
            "outlines"
        ),
        era="contemporary 2026",
        character=(
            "brown-haired woman in her late 20s, yellow t-shirt, blue jeans, "
            "expressive eyes"
        ),
        mood="annoyed, conflict",
        shot_type="medium shot, eye level",
    ),
    "sports_ronaldinho_pitch": dict(
        subject="Brazilian footballer Ronaldinho",
        action="taking a free kick, leaning back, leg cocked",
        scene="football pitch under stadium lights, blurred crowd behind",
        style="Tifo Football line-art illustration, ink + flat color",
        era="2002 World Cup era",
        character=(
            "mid-20s Brazilian man, brown skin, curly pulled-back hair, "
            "lean athletic build, Brazil national team yellow #10 jersey"
        ),
        mood="confident, athletic",
        shot_type="medium wide shot, action pose",
    ),
    "history_baghdad_mongol_1258": dict(
        subject="Mongol cavalry warrior",
        action="riding a horse, drawing a curved saber",
        scene="outside the walls of Baghdad, smoke rising in the background",
        style=(
            "historical illustrated plate, Osprey Publishing style, "
            "watercolor + ink"
        ),
        era=(
            "13th-century Mongol-Yuan warband, 1258 CE, fur-lined lamellar "
            "armor + lacquered leather + curved sabers + composite recurve "
            "bows"
        ),
        character=(
            "Mongol warrior, weathered face, long mustache, conical helmet "
            "with neck guard"
        ),
        mood="ominous, siege",
        shot_type="medium wide, low-angle hero",
    ),
}


__all__ = ["BEATS"]
