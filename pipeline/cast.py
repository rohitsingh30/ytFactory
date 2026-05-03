"""Stage 3.5 — author per-story narrator description.

Critique pattern across aita02_v3, aita02_v4, and aita03 was the
same: the channel-locked character (a round-headed cartoon girl in
a yellow shirt) doesn't fit the narrator (a grandma in aita03, an
adult woman with friends in aita02). Smiling-through-conflict on
every Short.

Resolution (memory: project_per_story_character.md): channel YAML
keeps the *aesthetic* (line style, palette). The narrator is
authored per-story by the LLM to match the source narrator's age,
gender, profession, mood — using the channel aesthetic as a style
constraint.

Output schema:

    {
      "narrator": {
        "description": "<one-line cartoon-character description matching channel aesthetic>",
        "default_emotion": "<the narrator's dominant emotional tone>",
        "age_band": "child|teen|young-adult|adult|middle-aged|elder",
        "gender": "female|male|non-binary|unspecified"
      },
      "supporting": [
        {
          "name": "<the name or relation as the narration refers to the character — e.g. 'Amelia' or 'sister'>",
          "aliases": ["<other ways the narration refers to this character>"],
          "description": "<one-line cartoon-character description, distinct from narrator>"
        }
      ]
    }

Critic finding 2026-05: when ``supporting`` was empty (the v1 default),
beats that talked about "Amelia" rendered the NARRATOR character because
no other character was visually defined. Author one supporting entry per
recurring named/relational character in the story so prompts.py can
render the right person on screen.

`make_shorts.py` reads this file's `narrator.description` as the
character_description prepended to every per-beat prompt. Falls
back to the channel YAML's `character_description` if the cast
file is missing — keeps existing channels working.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import llm


_PROMPT = """\
You are designing a single recurring cartoon character for a YouTube
Shorts narrator. The character will be drawn in this aesthetic on
every beat of the Short:

CHANNEL AESTHETIC (the visual style, locked):
\"\"\"
{style_prefix}
\"\"\"

The narrator is the person telling the story below. The character
on screen should LOOK like that narrator: their age, their gender,
their probable wardrobe given the story context.

RELATIONSHIP-MARKER → DEMOGRAPHIC inference (apply BEFORE writing
the description — the rendered character is wrong if you skip this):

  • "my daughter-in-law / DIL", "my son and DIL", "my grandkid",
    "my grandchild", "my MIL", "my FIL" said FROM the OP's mouth →
    OP is **middle-aged or elder**. If the OP is the MIL/FIL or
    grandparent, render age 50-70. Wardrobe: cardigan, blouse,
    reading glasses on a chain, sensible shoes — NOT a yellow t-shirt.

  • "my husband / wife", "we're trying for a baby", "my fiancé" →
    OP is adult, late-20s to 40s. Adult wardrobe.

  • "my mom / dad", "my parents grounded me", "I'm in college / high
    school" → OP is teen or young-adult.

  • "my toddler", "my baby", "my kid started kindergarten" → OP is
    a young-adult parent (late-20s to mid-30s).

  • "my boss / coworker / coworker's wife" → OP is a working adult.

When two markers conflict (e.g. "my husband" and "my MIL") the OLDER
demographic wins for the OP — they're the one telling the story, so
their generation is the one that has both relationships. Default to
the older end of the band when uncertain. Age-ambiguous "round-headed
cartoon kid" defaults are a known failure mode (critique
aita-birth-pool, principle #24); do NOT emit them.

Wardrobe must be SPECIFIC to the inferred demographic. A 60yo
grandmother in a yellow t-shirt and blue jeans is a render bug.

Hard rules for the NARRATOR description:
- One character, single subject.
- Match the channel aesthetic — same line style, same palette.
- Specific wardrobe + hair (one outfit, one hairstyle) that the
  diffusion model can render consistently across beats.
- Round-headed simple cartoon shapes (the channel pattern).
- DO NOT include any text/labels/logos in the description.
- Keep the description short — about 30-50 words. Diffusion attention
  drops past ~50 tokens.

SELF-CONSISTENCY (NARRATOR DESCRIPTION) — do NOT mix incompatible style
tokens: pick ONE hair length AND ONE hair styling. "Long wavy hair tied
in a low ponytail" is two different hairstyles fighting each other.
Pick one ("long wavy hair, loose" OR "low ponytail"). Same rule for
outfits: pick one top, one bottom — not "hoodie and a long coat over a
sweater". The diffusion model resolves contradictions by averaging
between them, which produces drift across beats.

SUPPORTING CHARACTERS (the people the narrator talks ABOUT):
- Identify every recurring named character or relational role in the
  story (e.g. "Amelia", "my sister", "her boyfriend Jake", "my MIL").
- Group aliases: if the story uses both "Amelia" and "my sister" for
  the same person, emit ONE supporting entry with name="Amelia" and
  aliases=["sister", "my sister"].
- Each supporting character needs a distinct visual: different hair
  colour OR different age OR different wardrobe from the narrator.
  Without this, the diffusion model paints them as the narrator.
- Same channel aesthetic applies — same line style and palette.
- Same self-consistency rules apply (one hair, one outfit).
- Skip throwaway one-line characters (a stranger, a cashier).
- Keep each supporting description short (~30 words).

SOURCE STORY (raw):
\"\"\"
{story}
\"\"\"

Return ONLY a JSON object with this exact shape (no prose, no fences):
{{
  "narrator": {{
    "description": "<one-line cartoon character description, ~30-50 words>",
    "default_emotion": "<dominant emotional tone like 'tense', 'frustrated', 'amused', 'desperate'>",
    "age_band": "<child|teen|young-adult|adult|middle-aged|elder>",
    "gender": "<female|male|non-binary|unspecified>"
  }},
  "supporting": [
    {{
      "name": "<name or relation as the narration says it>",
      "aliases": ["<other forms the narration uses>"],
      "description": "<one-line cartoon description, ~30 words, visually distinct from narrator>"
    }}
  ]
}}
"""


def author_cast(
    *,
    raw_story: dict,
    channel_cfg: dict,
    out_path: Path,
) -> dict:
    """Author the cast for one story. Caches to ``out_path``.

    ``raw_story`` is the pull_stories.py raw schema:
    ``{slug, title, body, source, url, metadata}``.
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    story_text = f"{title}\n\n{body}" if title else body
    if not story_text:
        raise ValueError("raw_story has no title or body")

    style_prefix = (channel_cfg or {}).get("image_style_prefix", "").strip()
    if not style_prefix:
        raise ValueError("channel config missing image_style_prefix")

    prompt = _PROMPT.format(
        style_prefix=style_prefix[:1500],
        story=story_text[:5000],
    )

    print(f"[cast] authoring narrator via claude CLI for {raw_story.get('slug')!r}…")
    raw = llm.call_claude_cli(prompt, output_json=True, model=llm.model_for("cast"))

    if not isinstance(raw, dict) or "narrator" not in raw:
        raise ValueError(f"cast author returned unexpected shape: {raw!r}")
    narr = raw["narrator"]
    if not isinstance(narr, dict) or not narr.get("description"):
        raise ValueError(f"cast.narrator.description missing: {raw!r}")

    raw.setdefault("supporting", [])

    # Self-consistency lint: catch contradictory hair/outfit tokens that
    # would drift across beats. Doesn't reject (we don't want to fail a
    # render on a soft style nit), just warns so the operator can edit
    # cast.json before re-running. The bug we're fixing: "long wavy
    # hair tied in a loose low ponytail" is two different hairstyles.
    for warning in _lint_self_consistency(narr.get("description", "")):
        print(f"[cast] narrator self-consistency: {warning}")
    for sup in raw.get("supporting") or []:
        if not isinstance(sup, dict):
            continue
        for warning in _lint_self_consistency(sup.get("description", "")):
            print(f"[cast] supporting {sup.get('name','?')!r} "
                  f"self-consistency: {warning}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(raw, indent=2))
    print(
        f"[cast] wrote {out_path} — "
        f"{narr.get('age_band', '?')} {narr.get('gender', '?')} "
        f"({narr.get('default_emotion', '?')}) + "
        f"{len(raw.get('supporting') or [])} supporting"
    )
    return raw


# Tokens that, when both present in one description, fight each other
# at diffusion time and produce drift across beats.
_HAIR_LENGTH_TOKENS = ("long", "short", "shoulder-length", "buzzed")
_HAIR_STYLE_TOKENS = (
    "ponytail", "bun", "braid", "braided", "loose", "down",
    "tied", "pinned", "messy bun",
)
# "Loose" and "down" are styling words that conflict with "tied" /
# "ponytail" / "bun" / "braid". The lint catches a description that
# asserts BOTH a "tied" form AND a "loose/down" form.
_HAIR_CONFLICT_PAIRS = [
    ({"loose", "down"}, {"ponytail", "bun", "braid", "braided", "tied", "pinned"}),
]


def _lint_self_consistency(description: str) -> list[str]:
    """Return a list of self-conflict warnings for one character description.

    Currently checks: conflicting hair-styling tokens. Empty list = clean.
    """
    if not description:
        return []
    desc = description.lower()
    warnings: list[str] = []
    # Multiple length tokens? "long short hair" is rare but symmetrical
    # to the rule, so include it.
    seen_lengths = [t for t in _HAIR_LENGTH_TOKENS if f" {t} " in f" {desc} "]
    if len(seen_lengths) > 1:
        warnings.append(
            f"description asserts multiple hair lengths {seen_lengths!r} — "
            f"pick one"
        )
    # Conflicting style tokens (loose vs tied, down vs ponytail, etc.).
    for set_a, set_b in _HAIR_CONFLICT_PAIRS:
        hits_a = {t for t in set_a if t in desc}
        hits_b = {t for t in set_b if t in desc}
        if hits_a and hits_b:
            warnings.append(
                f"description mixes {sorted(hits_a)!r} with "
                f"{sorted(hits_b)!r} — these are conflicting hairstyles, "
                f"pick one"
            )
    return warnings


def load_cast(path: Path) -> dict | None:
    """Read a cast.json. Returns None if missing or malformed."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        if "narrator" in raw and raw["narrator"].get("description"):
            return raw
    except (json.JSONDecodeError, KeyError, AttributeError):
        return None
    return None


# ---------------------------------------------------------------------
# Dossier-driven cast (sports / event channels)
# ---------------------------------------------------------------------

# Default analyst-narrator cartoon for SportsStoriesAnimated. Channel
# YAMLs override this via top-level ``narrator_persona`` once we want
# per-channel narrator art.
_SPORTS_NARRATOR_DEFAULT = (
    "Football analyst, late-thirties man in a navy half-zip jumper over a "
    "white tee. Short brown hair, trimmed beard. Hand-drawn editorial "
    "line-art on cream parchment. Calm measured expression."
)


def _person_to_supporting(person: dict) -> dict:
    """Flatten a wiki_research dossier person into a cast supporting entry.

    The dossier ``visual`` block is the source of truth (era-specific kit,
    body, hair). We collapse it into one ~30-50 word string the diffusion
    model can consume. Keep it concise — CLIP attention drops past ~50
    tokens.
    """
    v = person.get("visual") or {}
    parts: list[str] = []
    if v.get("body"):
        parts.append(v["body"].strip())
    if v.get("hair"):
        parts.append(v["hair"].strip())
    if v.get("facial_hair") and v["facial_hair"] not in (parts[-1] if parts else ""):
        parts.append(v["facial_hair"].strip())
    if v.get("kit"):
        parts.append(v["kit"].strip())
    if v.get("shirt_number"):
        parts.append(f"shirt #{v['shirt_number']}")
    if v.get("era_notes"):
        parts.append(v["era_notes"].strip())
    description = ". ".join(p for p in parts if p)
    description = description[:280]  # hard cap; CLIP attention budget

    aliases = list(person.get("aliases") or [])
    name = person.get("name", "")
    # Make sure the canonical name is searchable as an alias too — the
    # narration may use any form. De-dupe.
    seen = set()
    aliases = [a for a in [name, *aliases] if a and not (a in seen or seen.add(a))]

    out = {
        "name": name,
        "aliases": aliases,
        "description": description,
    }
    if person.get("seed") is not None:
        out["seed"] = person["seed"]
    return out


def cast_from_dossier(
    *,
    dossier: dict,
    channel_cfg: dict,
    out_path: Path,
) -> dict:
    """Produce a cast.json from a wiki_research dossier — no LLM call.

    This is the sports/event path: the dossier already names every
    person with their era-specific visual descriptors and a per-character
    seed, so we just transform shape. The narrator is a channel-level
    persona (the analyst), not story-specific.
    """
    if not isinstance(dossier, dict) or "people" not in dossier:
        raise ValueError("dossier missing 'people'")

    narrator_desc = (
        (channel_cfg or {}).get("narrator_persona")
        or _SPORTS_NARRATOR_DEFAULT
    ).strip()

    cast = {
        "narrator": {
            "description": narrator_desc,
            "default_emotion": "analytical",
            "age_band": "adult",
            "gender": "unspecified",
        },
        "supporting": [_person_to_supporting(p) for p in dossier["people"]
                       if isinstance(p, dict) and p.get("name")],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cast, indent=2))
    print(
        f"[cast] wrote {out_path} from dossier — "
        f"{len(cast['supporting'])} supporting characters, "
        f"narrator persona from {'channel YAML' if channel_cfg.get('narrator_persona') else 'default'}"
    )
    return cast
