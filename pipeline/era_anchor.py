"""Era anchor loader + validator for historyrecapped image-gen prompts.

Reads ``pipeline/era_taxonomy.yaml`` (curated era → costume tokens)
and exposes:

* :func:`load_taxonomy` — lazy-loaded dict of {era_key: EraEntry}.
* :func:`is_known_era` — True if a string matches a taxonomy key.
* :func:`era_prefix_for` — formatted ``"[ERA — <tokens>]"`` prepend
  string for ``pipeline/images/images.py::build_full_prompt``.
* :func:`closest_match` — Levenshtein-style suggestion for typo'd
  era keys, used by the script_check soft validator.

Added 2026-05-14 per Phase 4b of plan.md to fix the audit's
"Mongols 1258 rendered as WW1 trench soldiers" bug. The era anchor
prepends concrete costume + period tokens to the image-gen prompt
SO the diffusion model's attention sees "lamellar armor + composite
bow" before "soldiers", suppressing the modern-uniform default.

The taxonomy is intentionally small (≈14 eras) — covers the audit's
historyrecapped topic surface (Mongols, WW1, WW2, Roman, Tudor,
Mughal, Edo, etc.) and grows as new history topics arrive. NOT
meant to be exhaustive of all human history — only eras the channel
actually narrates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


_logger = logging.getLogger(__name__)

_TAXONOMY_PATH = Path(__file__).parent / "era_taxonomy.yaml"

# Lazy-loaded cache. Reset by tests via :func:`_reset_cache_for_tests`.
_CACHE: dict[str, "EraEntry"] | None = None


@dataclass(frozen=True)
class EraEntry:
    """One era's costume / negative tokens, parsed from era_taxonomy.yaml."""
    key: str
    description: str
    tokens: str
    negatives: str

    def prefix(self) -> str:
        """The string prepended to image_gen prompts.

        Format: ``[ERA — <costume tokens, single line>]``. The brackets
        and ``[ERA — ]`` framing tell the diffusion model this is a
        directorial constraint, not a scene description; FLUX.2 klein
        respects bracketed prompt structure as priority guidance.
        """
        # Collapse multi-line YAML block scalars to one line so the
        # prompt stays readable in logs + diff tools.
        flat = " ".join(self.tokens.strip().split())
        return f"[ERA — {flat}]"


def _reset_cache_for_tests() -> None:
    """Clear the lazy taxonomy cache. Tests use this between cases
    that mutate the YAML file."""
    global _CACHE
    _CACHE = None


def load_taxonomy(path: Path | None = None) -> dict[str, EraEntry]:
    """Load the era taxonomy YAML, returning a dict keyed by era_key.

    Caches the parse on first call. Pass ``path`` to override the
    default location (used by tests).

    Raises ``RuntimeError`` if the YAML is malformed (caller should
    crash hard — a broken taxonomy can't safely fall back to "no
    era anchor", that's how the bug we're fixing happens).
    """
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE
    p = path or _TAXONOMY_PATH
    try:
        raw = yaml.safe_load(p.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"era_taxonomy.yaml unreadable / malformed: {exc}"
        ) from exc
    if not isinstance(raw, dict) or "eras" not in raw:
        raise RuntimeError(
            f"era_taxonomy.yaml missing top-level 'eras' map (got "
            f"{type(raw).__name__})"
        )
    eras_raw = raw["eras"] or {}
    out: dict[str, EraEntry] = {}
    for key, body in eras_raw.items():
        if not isinstance(body, dict):
            _logger.warning(
                "era_taxonomy.yaml: era %r has non-dict body, skipping",
                key,
            )
            continue
        tokens = (body.get("tokens") or "").strip()
        if not tokens:
            _logger.warning(
                "era_taxonomy.yaml: era %r has empty tokens, skipping",
                key,
            )
            continue
        out[str(key)] = EraEntry(
            key=str(key),
            description=(body.get("description") or "").strip(),
            tokens=tokens,
            negatives=(body.get("negatives") or "").strip(),
        )
    if path is None:
        _CACHE = out
    return out


def is_known_era(era_key: str | None) -> bool:
    """True if ``era_key`` is in the taxonomy.

    Case-sensitive (taxonomy keys are kebab-case lowercase).
    Falsy / non-string inputs return False.
    """
    if not era_key or not isinstance(era_key, str):
        return False
    return era_key in load_taxonomy()


def era_prefix_for(era_key: str | None) -> str | None:
    """Return the ``"[ERA — ...]"`` prefix string for ``era_key``.

    Returns None when:
      * era_key is falsy / not a string
      * era_key is not in the taxonomy (unknown era — caller should
        log a warning via :func:`closest_match`)

    Caller (``pipeline/images/images.py::build_full_prompt``)
    prepends this to the prompt string ahead of character_description
    so the era anchor dominates the diffusion attention.
    """
    if not is_known_era(era_key):
        return None
    return load_taxonomy()[era_key].prefix()


def closest_match(era_key: str, *, n: int = 3) -> list[str]:
    """Return up to ``n`` closest taxonomy keys to ``era_key`` by
    edit distance (poor-man's Levenshtein via difflib).

    Used by the soft validator to surface "did you mean X?" hints
    when a script emits a typo'd era_anchor.

    Returns an empty list when the taxonomy is empty.
    """
    if not era_key:
        return []
    import difflib  # noqa: PLC0415  # cheap stdlib, no need at module scope
    keys = list(load_taxonomy().keys())
    return difflib.get_close_matches(era_key, keys, n=n, cutoff=0.5)


def negatives_for(era_key: str | None) -> str | None:
    """Return the era's anti-prompt fragments as a comma-separated string.

    Used by ``pipeline/images/images_cloudrun.py`` to extend the
    base ``ANTI_TEXT_SUFFIX`` (added 2026-05-14) with era-specific
    suppression tokens. None when era_key is unknown.
    """
    if not is_known_era(era_key):
        return None
    return load_taxonomy()[era_key].negatives or None


def all_known_eras() -> list[str]:
    """Sorted list of every era key in the taxonomy. Used by the
    validator's "available eras" diagnostic message."""
    return sorted(load_taxonomy().keys())
