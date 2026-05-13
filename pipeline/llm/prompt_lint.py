"""Per-beat prompt-shape validators.

Mirrors :mod:`pipeline.llm.script_check` and :mod:`pipeline.llm.cast_lint`
for the prompts stage.

The legacy ``pipeline.llm.prompts._validate_and_clean`` already gates
the most fragile bug — "expected JSON array, got dict" — but it does
so by raising, which the caller swallows and falls back to heuristic
prompts (visible in cake-orch-v5 logs as
``[prompts] WARNING: failed to author prompts: expected JSON array,
got dict; continuing with heuristic prompts``). Heuristic prompts
work but they don't carry per-beat cast lock or kit-lock metadata,
so they're a quality cliff. The orchestrator fix: surface that
failure as a Fix → regenerate with a focused prompt → keep the
LLM-authored quality.

Constraints emitted here:

- ``prompts_array`` (error)             — top-level must be JSON array
- ``prompts_count_mismatch`` (error)    — array length must equal
  the number of beats in the upstream Script (the renderer aligns
  prompts → beats by INDEX; mismatched counts crash the alignment)
- ``prompt_item_object`` (error)        — each item must be an object
- ``prompt_scene_required`` (error)     — each item.scene must be non-empty
- ``prompt_text_bait`` (warning)        — items containing
  text-bait words ("text", "writing", quotes around hooks) — the
  legacy code strips these post-hoc but warning the model is cheaper
  than letting it author them and stripping
- ``prompt_cast_drift`` (warning)       — items mention a character
  not present in cast.supporting (signals the model invented a
  character or misspelled a name)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from pipeline import observability as _obs

# Tokens that produce in-image gibberish text on SDXL/FLUX (mirrors the
# central registry from pipeline.images.images.strip_text_bait — kept
# here as a separate copy because importing would create a circular dep
# (orchestrator → contracts → prompt_lint → images → orchestrator)).
TEXT_BAIT_TOKENS: tuple[str, ...] = (
    "text", "writing", "letters", "label", "logo", "wording",
    "caption", "subtitle", "title card",
)

# Imperative leakage — the model occasionally echoes its meta-instruction
# back into the scene field ("Replace hook visual with…", "Generate an
# image of…"). The renderer strips these but the lint catches earlier.
META_IMPERATIVE_PREFIXES: tuple[str, ...] = (
    "generate an image",
    "replace ",
    "render a ",
    "create an illustration",
    "draw a ",
)


@dataclass(frozen=True)
class PromptIssue:
    """One per-beat prompt-shape complaint."""
    severity: str    # "error" | "warning"
    code: str
    message: str
    target_path: str   # 'beats[N].prompt' or 'beats[N].scene' or 'beats'


@_obs.traced("llm.prompt_lint.check_prompts", category="llm",
             capture=["expected_beats"])
def check_prompts(
    prompts: list,
    *,
    expected_beats: int,
    cast: dict | None = None,
) -> list[PromptIssue]:
    """Validate the LLM's prompt-array output.

    Args:
        prompts: The raw output (must be a list).
        expected_beats: How many beats the upstream Script produced.
            The renderer aligns prompts → beats by INDEX, so a count
            mismatch is a hard error.
        cast: Optional cast.json — if present, each item.scene is
            checked for character names that don't appear in
            cast.narrator + cast.supporting (cast_drift warning).

    Returns:
        Every issue. Caller separates errors from warnings.
    """
    issues: list[PromptIssue] = []

    if not isinstance(prompts, list):
        issues.append(PromptIssue(
            severity="error", code="prompts_array",
            message=(f"prompts must be a JSON array, got "
                     f"{type(prompts).__name__}"),
            target_path="beats",
        ))
        return issues

    if len(prompts) != expected_beats:
        issues.append(PromptIssue(
            severity="error", code="prompts_count_mismatch",
            message=(f"prompts has {len(prompts)} items but the script "
                     f"has {expected_beats} beats — counts must match "
                     f"so the renderer can align by index"),
            target_path="beats",
        ))

    cast_names = _gather_cast_names(cast) if cast else set()

    for i, item in enumerate(prompts):
        if not isinstance(item, dict):
            issues.append(PromptIssue(
                severity="error", code="prompt_item_object",
                message=(f"beats[{i}] must be an object, got "
                         f"{type(item).__name__}"),
                target_path=f"beats[{i}].prompt",
            ))
            continue
        scene = (item.get("scene") or "").strip()
        if not scene:
            issues.append(PromptIssue(
                severity="error", code="prompt_scene_required",
                message=f"beats[{i}].scene is empty",
                target_path=f"beats[{i}].scene",
            ))
            continue

        scene_lc = scene.lower()
        # Text-bait soft check.
        bait_hits = [t for t in TEXT_BAIT_TOKENS if t in scene_lc]
        if bait_hits:
            issues.append(PromptIssue(
                severity="warning", code="prompt_text_bait",
                message=(f"beats[{i}].scene contains text-bait tokens "
                         f"{bait_hits!r} — describe shape/colour/icon "
                         f"instead, the diffusion model will render "
                         f"these as gibberish text otherwise"),
                target_path=f"beats[{i}].scene",
            ))

        # Imperative leakage.
        if any(scene_lc.startswith(p) for p in META_IMPERATIVE_PREFIXES):
            issues.append(PromptIssue(
                severity="warning", code="prompt_meta_imperative",
                message=(f"beats[{i}].scene starts with a meta-instruction "
                         f"phrase — describe the SCENE itself, not what to "
                         f"do with it"),
                target_path=f"beats[{i}].scene",
            ))

        # Cast-drift soft check.
        # Audit D3.42 — pre-fix this also computed
        # `mentioned = _mentioned_cast_names(scene, cast_names)` but
        # never read it. The `unknown` line below is the only real
        # check (proper-noun-not-in-cast). Removed the dead variable.
        if cast_names:
            unknown = [n for n in _proper_nouns(scene) if n not in cast_names
                       and n.lower() not in {m.lower() for m in cast_names}]
            if unknown:
                issues.append(PromptIssue(
                    severity="warning", code="prompt_cast_drift",
                    message=(f"beats[{i}].scene mentions character(s) "
                             f"{unknown!r} not in cast — likely the "
                             f"model invented a character or misspelled "
                             f"a name. Cast has: {sorted(cast_names)!r}."),
                    target_path=f"beats[{i}].scene",
                ))

    return issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_COMMON_PROPER_NOUNS_TO_IGNORE: frozenset[str] = frozenset({
    # Story-shape words that are capitalised at sentence start.
    "Then", "Now", "When", "After", "Before", "But", "She", "He", "They",
    "Her", "His", "Their", "Two", "Three", "Four", "Five", "Six", "Seven",
    "Eight", "Nine", "Ten", "Hundred", "Thousand",
    # AITA-class boilerplate.
    "AITA", "WIBTA", "YTA", "NTA", "NAH", "ESH", "MIL", "FIL", "SIL", "BIL",
    "DIL", "OOP", "OP",
    # Common scene words.
    "Wedding", "Birthday", "Cake", "House", "Room", "Kitchen", "Living",
    "Family", "Mother", "Father", "Sister", "Brother", "Daughter", "Son",
    "Husband", "Wife", "Friend", "Boss",
})


def _proper_nouns(text: str) -> set[str]:
    """Cheap heuristic: extract candidate proper nouns from prose.

    Misses two-word names ("Mary Jane") and over-fires on
    sentence-initial Capitalised words; that's why cast_drift is a
    WARNING not an error. Good enough to surface "model invented Bob".
    """
    return {n for n in _PROPER_NOUN_RE.findall(text)
            if n not in _COMMON_PROPER_NOUNS_TO_IGNORE}


def _gather_cast_names(cast: dict) -> set[str]:
    """All names + aliases the cast.json declares."""
    names: set[str] = set()
    sup = cast.get("supporting") or []
    for s in sup:
        if not isinstance(s, dict):
            continue
        if (n := s.get("name")):
            names.add(str(n).strip())
        for a in s.get("aliases") or []:
            names.add(str(a).strip())
    return names


# ---------------------------------------------------------------------------
# Convenience for contracts
# ---------------------------------------------------------------------------

ERROR_CODES: frozenset[str] = frozenset({
    "prompts_array", "prompts_count_mismatch",
    "prompt_item_object", "prompt_scene_required",
})


def errors(issues: Iterable[PromptIssue]) -> list[PromptIssue]:
    return [i for i in issues if i.severity == "error"]


def warnings(issues: Iterable[PromptIssue]) -> list[PromptIssue]:
    return [i for i in issues if i.severity == "warning"]
