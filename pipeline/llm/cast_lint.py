"""Cast-shape validators.

Mirrors :mod:`pipeline.llm.script_check` for the cast stage. Pulls
the rules that have historically been embedded inside ``cast.py``'s
prompt out into a standalone module so:

1. The :class:`pipeline.llm.contracts.cast_contract.CastContract` can
   import the rules + their paired examples (drift-impossible).
2. Other code paths (post-render critic, manual cast.json edits, the
   web UI's edit page) can run the same lints without re-implementing.

Two classes of issue today:

- **Shape errors** (gate the orchestrator's retry loop):
  - ``narrator_missing``        — top-level ``narrator`` field missing
  - ``narrator_description_missing`` — ``narrator.description`` empty
  - ``supporting_not_list``     — ``supporting`` present but wrong type

- **Soft errors** (warning-level — don't reject but log for the operator):
  - ``narrator_self_conflict``  — contradictory hair tokens in the
    narrator description (was ``cast._lint_self_consistency``)
  - ``supporting_self_conflict`` — same for any supporting entry
  - ``description_too_long``    — diffusion attention drops past ~50
    tokens; SDXL ignores the back half of long descriptions
  - ``description_has_text_token`` — wardrobe with "logo", "label",
    "writing" produces gibberish in-image text on SDXL/FLUX.

Future hard rules (when we wire image-judge feedback as a learning
loop):
  - cast voice_ref must match a registered voice in ``voice_refs/``
  - supporting characters need at least ONE distinguishing feature
    versus the narrator
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from pipeline import observability as _obs


@dataclass(frozen=True)
class CastIssue:
    """One cast-shape complaint. Mirrors :class:`script_check.ScriptIssue`."""
    severity: str    # "error" | "warning"
    code: str
    message: str
    target_path: str   # which slot in cast.json this is about


# Hair-conflict registry — same data the original
# ``cast._lint_self_consistency`` used, lifted into a paired-rules
# table so the prompt can echo BAD examples sourced from the
# validator (the same drift-prevention pattern as
# script_check._CTA_RULES).
_HAIR_LENGTH_TOKENS: tuple[str, ...] = (
    "long", "short", "shoulder-length", "buzzed",
)
_HAIR_STYLE_TOKENS: tuple[str, ...] = (
    "ponytail", "bun", "braid", "braided", "loose", "down",
    "tied", "pinned", "messy bun",
)
_HAIR_CONFLICT_PAIRS: list[tuple[set[str], set[str]]] = [
    ({"loose", "down"},
     {"ponytail", "bun", "braid", "braided", "tied", "pinned"}),
]


# Wardrobe tokens that produce in-image text gibberish on SDXL / FLUX.
_TEXT_BAIT_TOKENS: tuple[str, ...] = (
    "logo", "label", "writing", "letters", "text", "wording",
    "embroidered text", "screen printed",
)


# Approximate max words for a character description before SDXL's
# attention starts dropping the back half. Empirically ~50.
_DESC_MAX_WORDS = 60


# Bad examples that should NOT appear in a description — used both
# for the lint AND echoed into the cast contract's prompt block.
EXAMPLES_BAD_NARRATOR_DESC: tuple[str, ...] = (
    "Long wavy hair tied in a low ponytail.",      # two hairstyles
    "Short bob with long flowing curls.",          # length conflict
    "Yellow t-shirt with the logo of her bakery.", # text-bait
    "Wearing a hoodie under a long coat over a sweater.", # outfit pile
)
EXAMPLES_GOOD_NARRATOR_DESC: tuple[str, ...] = (
    ("Round-headed adult woman in flat 2D crayon style with thick black "
     "outlines and pastel colors. Light blue blouse, flour-dusted apron, "
     "dark skirt. Shoulder-length brown hair, side part. Slightly tired "
     "eyes, raised brows."),
    ("Round-headed elder woman, 60s, cardigan over a blouse, long skirt, "
     "sensible shoes. Short gray hair in a neat bun, reading glasses on a "
     "chain. Same flat 2D crayon style and palette as the rest."),
)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


@_obs.traced("llm.cast_lint.check_cast", category="llm")
def check_cast(cast: dict) -> list[CastIssue]:
    """Run every cast-shape gate. Returns ALL issues — caller separates
    errors from warnings.
    """
    issues: list[CastIssue] = []

    # Top-level shape.
    if not isinstance(cast, dict):
        issues.append(CastIssue(
            severity="error", code="bad_shape",
            message=f"cast must be an object, got {type(cast).__name__}",
            target_path="cast",
        ))
        return issues

    narr = cast.get("narrator")
    if not isinstance(narr, dict):
        issues.append(CastIssue(
            severity="error", code="narrator_missing",
            message="cast.narrator must be an object",
            target_path="cast.narrator",
        ))
        return issues
    desc = (narr.get("description") or "").strip()
    if not desc:
        issues.append(CastIssue(
            severity="error", code="narrator_description_missing",
            message="cast.narrator.description must be non-empty",
            target_path="cast.narrator.description",
        ))

    if "supporting" in cast and not isinstance(cast["supporting"], list):
        issues.append(CastIssue(
            severity="error", code="supporting_not_list",
            message=(f"cast.supporting must be an array, "
                     f"got {type(cast['supporting']).__name__}"),
            target_path="cast.supporting",
        ))

    # Soft lints on the narrator description.
    issues.extend(_lint_description(
        desc, target_path="cast.narrator.description",
        who="narrator",
    ))

    # Soft lints on every supporting entry.
    sup = cast.get("supporting") or []
    if isinstance(sup, list):
        for i, s in enumerate(sup):
            if not isinstance(s, dict):
                issues.append(CastIssue(
                    severity="warning", code="supporting_bad_shape",
                    message=f"cast.supporting[{i}] is not an object",
                    target_path=f"cast.supporting[{i}]",
                ))
                continue
            s_desc = (s.get("description") or "").strip()
            if not s_desc:
                issues.append(CastIssue(
                    severity="warning", code="supporting_description_missing",
                    message=f"cast.supporting[{i}].description is empty",
                    target_path=f"cast.supporting[{i}].description",
                ))
                continue
            issues.extend(_lint_description(
                s_desc,
                target_path=f"cast.supporting[{i}].description",
                who=f"supporting[{s.get('name', i)!r}]",
            ))

    return issues


def _lint_description(description: str, *, target_path: str, who: str) -> list[CastIssue]:
    """Soft lint a single character description.

    Returns warnings only — these don't block the render but they
    surface to operators / the post-render critic + are echoed back
    into regen prompts when the orchestrator retries.
    """
    issues: list[CastIssue] = []
    desc = description.lower()

    # Multiple length tokens? "long short hair" is rare but symmetrical.
    seen_lengths = [t for t in _HAIR_LENGTH_TOKENS
                    if f" {t} " in f" {desc} "]
    if len(seen_lengths) > 1:
        issues.append(CastIssue(
            severity="warning",
            code=f"{_who_to_code(who)}_self_conflict",
            message=(f"{who} description asserts multiple hair lengths "
                     f"{seen_lengths!r} — pick one"),
            target_path=target_path,
        ))

    # Conflicting style tokens (loose vs tied, down vs ponytail, …).
    for set_a, set_b in _HAIR_CONFLICT_PAIRS:
        hits_a = sorted(t for t in set_a if t in desc)
        hits_b = sorted(t for t in set_b if t in desc)
        if hits_a and hits_b:
            issues.append(CastIssue(
                severity="warning",
                code=f"{_who_to_code(who)}_self_conflict",
                message=(f"{who} description mixes {hits_a!r} with "
                         f"{hits_b!r} — conflicting hairstyles, pick one"),
                target_path=target_path,
            ))

    # Word-count cap.
    n_words = len([w for w in re.split(r"\s+", description) if w])
    if n_words > _DESC_MAX_WORDS:
        issues.append(CastIssue(
            severity="warning",
            code="description_too_long",
            message=(f"{who} description is {n_words} words — diffusion "
                     f"attention drops past ~{_DESC_MAX_WORDS}; trim it"),
            target_path=target_path,
        ))

    # Text-bait wardrobe.
    found = [t for t in _TEXT_BAIT_TOKENS if t in desc]
    if found:
        issues.append(CastIssue(
            severity="warning",
            code="description_has_text_token",
            message=(f"{who} description includes text-bait tokens "
                     f"{found!r} — SDXL renders these as in-image gibberish"),
            target_path=target_path,
        ))

    return issues


def _who_to_code(who: str) -> str:
    """Map a who-tag to a constraint-code prefix.

    'narrator' → 'narrator', 'supporting[\\'Lily\\']' → 'supporting'.
    Used so a single failing supporting entry doesn't get a unique
    constraint name per-character (would explode the regen prompt).
    """
    return "narrator" if who == "narrator" else "supporting"


# ---------------------------------------------------------------------------
# Convenience for contracts: which rule names are errors vs warnings?
# ---------------------------------------------------------------------------

ERROR_CODES: frozenset[str] = frozenset({
    "bad_shape",
    "narrator_missing",
    "narrator_description_missing",
    "supporting_not_list",
})


def errors(issues: Iterable[CastIssue]) -> list[CastIssue]:
    return [i for i in issues if i.severity == "error"]


def warnings(issues: Iterable[CastIssue]) -> list[CastIssue]:
    return [i for i in issues if i.severity == "warning"]
