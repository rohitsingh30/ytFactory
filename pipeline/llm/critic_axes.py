"""Shared per-axis scoring + verdict derivation for the post-render critic.

Created 2026-05-14 to fix the rubber-stamp bug in
``pipeline/llm/critic.py`` and ``pipeline/llm/contracts/critic_contract.py``.

Before this module existed, the critic asked the LLM for a free-form
``verdict`` ∈ {SHIP, FIX, BLOCK} with no scoring rubric. The LLM
defaulted to SHIP for every render — 27 of 27 rendered jobs in
Firestore as of 2026-05-13 had ``critique.verdict == "SHIP"`` despite
broken cast continuity, gibberish AI captions, anachronistic visuals,
and other bugs that any human viewer would flag in 2 seconds.

Root cause: the contract had no rule for "if any quality axis < 7 →
not SHIP". Without that rule, the LLM's natural prior is to be
permissive ("nothing is catastrophically wrong → ship it").

Fix: split the single verdict into 6 quality axes scored 1-10 each.
The verdict is **derived** from the axes deterministically — the LLM
no longer has discretion over the shipping decision; it only has
discretion over the per-axis scoring. Any axis < 4 → BLOCK; any
axis 4-6 → FIX; all axes ≥ 7 → SHIP.

The 6 axes mirror the 6 review lenses already listed in
``pipeline/llm/contracts/critic_contract.py::build_prompt`` (hook
strength, sync, cast lock, pacing, CTA, mute-mode legibility) plus
``source_fidelity`` to catch the long-form "r/nosleep horror →
generic awareness essay" failure mode.

Used by:
- ``pipeline/llm/critic.py::critique_short`` — vision-bearing critic
  (Claude CLI backend, laptop-only today).
- ``pipeline/llm/contracts/critic_contract.py::CriticContract`` —
  contract-driven critic stage (used by ``pipeline_runner.py``).

Both paths import :func:`derive_verdict` so the SHIP/FIX/BLOCK gate is
identical regardless of which orchestration path is running. Adding
a new path? Import this module and use the shared helper instead of
re-implementing the threshold logic.
"""

from __future__ import annotations

from typing import Mapping


# ---------------------------------------------------------------------
# Axis definitions
# ---------------------------------------------------------------------

# Axis name → human-readable description used in the LLM prompt. Order
# is stable so the prompt is deterministic and tests can pin against
# this tuple.
AXES: tuple[tuple[str, str], ...] = (
    (
        "hook_strength",
        (
            "Does the first 1.5 s stop a thumb-scroll? Static T-pose "
            "characters, generic backgrounds, and missing on-screen "
            "punchline text are all hook failures."
        ),
    ),
    (
        "caption_legibility",
        (
            "Are captions readable on a phone? Real ASR-aligned "
            "lower-third overlay = high. Tiny pill stuck on a character's "
            "belt buckle, AI-hallucinated gibberish text baked into the "
            "panel, or NO captions at all (silent on mute) = low. "
            "Channels with captions_enabled=false should still score "
            "≥7 here as long as visuals carry the story without text."
        ),
    ),
    (
        "cast_continuity",
        (
            "Is the protagonist the same person in every panel? Same "
            "face, hair, age, body, clothing? A 'Ronaldinho' video that "
            "shows 5 different anonymous footballers across 5 beats = "
            "low. Long-form '[NAME APPEARANCE LOCK]' style consistency "
            "= high."
        ),
    ),
    (
        "mute_mode_score",
        (
            "80% of Shorts viewers watch on mute. Can a sound-off viewer "
            "follow the story from captions + visuals alone? If captions "
            "are missing/illegible AND visuals are decontextualised "
            "(blank backgrounds, character T-poses, no scene props) → "
            "very low."
        ),
    ),
    (
        "source_fidelity",
        (
            "Does the script honour the source material? r/nosleep "
            "URLs that get rendered as generic 'attention is currency' "
            "essays = catastrophic. Long-form scripts that fabricate "
            "events or sand off the actual punchline of the source "
            "post = low. For LLM-only sources (no URL), score on "
            "internal consistency: are claimed facts grounded?"
        ),
    ),
    (
        "closer_strength",
        (
            "Does the closer panel drive engagement? Generic Like+Bell "
            "card with no spoken closer text on screen and no YTA-vs-NTA "
            "split = low. A baked closer panel showing the channel's "
            "configured closer_format ('LIKE if YTA, COMMENT if NTA. "
            "AITA?', 'SUBSCRIBE for more deep dives', etc.) with the "
            "spoken closing question as overlay = high."
        ),
    ),
)


AXIS_NAMES: tuple[str, ...] = tuple(name for name, _ in AXES)


# Thresholds. Pinned conservative — see module docstring for why.
SHIP_MIN: int = 7   # all axes ≥ 7 → SHIP
BLOCK_MAX: int = 3  # any axis ≤ 3 → BLOCK (irrecoverable)
# else (any axis in 4..6) → FIX (try a regen pass)

VERDICTS: tuple[str, ...] = ("SHIP", "FIX", "BLOCK")


# ---------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------


def derive_verdict(axes: Mapping[str, int] | None) -> str:
    """Map an axes dict to a verdict.

    Rules (apply in order; first match wins):
      1. ``axes`` missing or not a Mapping → ``"FIX"`` (the LLM didn't
         emit axes — treat as a soft failure that the runner can
         re-prompt for; better than silently shipping).
      2. Any required axis missing → ``"FIX"`` for the same reason.
      3. Any axis ≤ ``BLOCK_MAX`` (3) → ``"BLOCK"``.
      4. Any axis < ``SHIP_MIN`` (7) → ``"FIX"``.
      5. All axes ≥ ``SHIP_MIN`` → ``"SHIP"``.

    Non-integer axis values (the LLM hallucinated a string or float)
    are treated as missing → ``"FIX"``. We do NOT silently coerce —
    a critic that emits ``"high"`` for an axis is broken and needs
    to be re-prompted, not approximated.

    Returns one of :data:`VERDICTS`.
    """
    if not isinstance(axes, Mapping):
        return "FIX"

    # Pass 1: every named axis must be present and an int.
    parsed: dict[str, int] = {}
    for name in AXIS_NAMES:
        if name not in axes:
            return "FIX"
        v = axes[name]
        # bool is a subclass of int in Python; reject explicitly so
        # ``axes={"hook_strength": True}`` doesn't get treated as 1.
        if isinstance(v, bool) or not isinstance(v, int):
            return "FIX"
        parsed[name] = v

    # Pass 2: clamp + apply thresholds.
    if any(v <= BLOCK_MAX for v in parsed.values()):
        return "BLOCK"
    if any(v < SHIP_MIN for v in parsed.values()):
        return "FIX"
    return "SHIP"


def weakest_axis(axes: Mapping[str, int] | None) -> tuple[str, int] | None:
    """Return ``(axis_name, score)`` of the lowest-scoring axis.

    Returns ``None`` if axes are missing/malformed (matches the
    ``"FIX"`` early-return in :func:`derive_verdict` — caller should
    treat as "no specific weak axis to surface").

    Used by callers that want to log/persist a single human-readable
    "what's the worst thing about this render" string for the
    dashboard (replacing the legacy ``weakest_param`` free-form
    field).
    """
    if not isinstance(axes, Mapping):
        return None
    parsed: list[tuple[str, int]] = []
    for name in AXIS_NAMES:
        v = axes.get(name)
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        parsed.append((name, v))
    if not parsed:
        return None
    return min(parsed, key=lambda kv: kv[1])


def axes_summary(axes: Mapping[str, int] | None) -> str:
    """Single-line "axis=score axis=score …" string for log lines.

    Returns ``"<no axes>"`` if axes are missing/malformed. Order
    matches :data:`AXIS_NAMES` for stable log diffing.
    """
    if not isinstance(axes, Mapping):
        return "<no axes>"
    parts: list[str] = []
    for name in AXIS_NAMES:
        v = axes.get(name)
        if isinstance(v, bool) or not isinstance(v, int):
            parts.append(f"{name}=?")
        else:
            parts.append(f"{name}={v}")
    return " ".join(parts)


def render_axes_block(indent: str = "  ") -> str:
    """Format the AXES list as a markdown-ish prompt block for the LLM.

    Used by both ``critic.py::_CRITIC_PROMPT`` and
    ``critic_contract.py::build_prompt`` so the prompt phrasing stays
    in sync. Indent is configurable so the caller can nest under
    "Score these 1-10:" headings of varying depth.
    """
    lines: list[str] = []
    for name, desc in AXES:
        lines.append(f"{indent}- **{name}** (1-10) — {desc}")
    return "\n".join(lines)


# JSON-Schema fragment for the ``axes`` object — embedded by both
# critic.py's ``_CRITIC_SCHEMA`` and the contract's regen prompt.
# Pinned here so the SHIP/FIX/BLOCK gate is enforced at the schema
# level, not just at the post-LLM derivation level.
def axes_json_schema() -> dict:
    """Return a JSON-Schema fragment describing the axes object.

    Use as the ``"axes"`` property in a parent object schema; mark
    ``"axes"`` as required at the parent level so the LLM cannot
    omit it.
    """
    return {
        "type": "object",
        "required": list(AXIS_NAMES),
        "additionalProperties": False,
        "properties": {
            name: {"type": "integer", "minimum": 1, "maximum": 10}
            for name in AXIS_NAMES
        },
    }
