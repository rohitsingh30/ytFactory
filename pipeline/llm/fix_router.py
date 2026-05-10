"""Fix routing — maps a Fix's target_path to the owning stage.

Used by the post-render critic loop (and any in-pipeline judge) to
route a list of Fixes back to the stages that produced the broken
artifacts. The router doesn't EXECUTE anything — it just resolves
``Fix → (stage_name, sub_index_or_None)`` so the pipeline runner can
group fixes by stage, invalidate the right cache keys, and re-fire
those stages with the fixes embedded in the regen prompt.

The ownership map is intentionally explicit — the alternative
(infer-from-target_stage) leaves us with unrouteable fixes when a
judge produces a Fix without setting target_stage. Explicit
``STAGE_OWNERS`` is debuggable from a log line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .fix import Fix


# ``target_path`` patterns → owning stage. Patterns are evaluated in
# order; first match wins. The ``[*]`` token captures the sub-index
# (beat / chunk number) so the runner can invalidate ONLY that key.
#
# Adding a new stage: register its artifact paths here AND set its
# contract's ``artifact_paths`` class var to the same patterns.
_STAGE_OWNERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^narration$"),                     "rewrite"),
    (re.compile(r"^hook$"),                          "rewrite"),
    (re.compile(r"^title_options(\[\d+\])?$"),        "rewrite"),
    (re.compile(r"^cast(\..+)?$"),                   "cast"),
    (re.compile(r"^beats\[(\d+)\]\.prompt$"),        "prompts"),
    (re.compile(r"^beats\[(\d+)\]\.image$"),         "images"),
    (re.compile(r"^audio(\.chunk\[(\d+)\])?$"),      "tts"),
    (re.compile(r"^captions$"),                      "asr"),
    (re.compile(r"^compose(\..+)?$"),                "compose"),
]


@dataclass(frozen=True)
class RoutedFix:
    """A Fix that's been resolved to its owning stage + sub_index.

    ``sub_index`` is parsed from ``[N]`` in the target_path when
    present (e.g. ``beats[14].image`` → 14). Stages with fanout=1
    leave it ``None``.
    """
    fix: Fix
    stage: str
    sub_index: int | None


class UnroutableFix(ValueError):
    """Raised when no STAGE_OWNERS pattern matches the Fix's target_path."""


def route(fix: Fix) -> RoutedFix:
    """Resolve a single Fix to its owning stage + sub_index.

    Honours an explicit ``fix.target_stage`` if set — judges that
    already know the answer skip the regex walk. Otherwise scans
    ``_STAGE_OWNERS`` in order.
    """
    if fix.target_stage and fix.target_path:
        sub = _extract_sub_index(fix.target_path)
        return RoutedFix(fix=fix, stage=fix.target_stage, sub_index=sub)

    if fix.target_stage and not fix.target_path:
        return RoutedFix(fix=fix, stage=fix.target_stage, sub_index=None)

    for pat, stage in _STAGE_OWNERS:
        m = pat.match(fix.target_path)
        if m is None:
            continue
        sub = _extract_sub_index(fix.target_path)
        return RoutedFix(fix=fix, stage=stage, sub_index=sub)

    raise UnroutableFix(
        f"no stage owns target_path={fix.target_path!r} "
        f"(constraint={fix.constraint!r} from {fix.source_judge or 'unknown'})"
    )


def _extract_sub_index(target_path: str) -> int | None:
    """Pull the FIRST ``[N]`` index out of a target_path. None if absent."""
    m = re.search(r"\[(\d+)\]", target_path)
    return int(m.group(1)) if m else None


def cascade(fixes: list[Fix]) -> dict[str, dict[int | None, list[Fix]]]:
    """Group a flat list of Fixes by (stage, sub_index).

    Returns ``{stage: {sub_index: [Fix, …]}}`` so the pipeline runner
    can re-fire each stage exactly once per affected sub_index — even
    if the judge produced multiple fixes against the same artifact.
    Unrouteable fixes go under ``stage="_unrouted"`` so callers can
    surface them rather than silently drop.
    """
    out: dict[str, dict[int | None, list[Fix]]] = {}
    for f in fixes:
        try:
            r = route(f)
            stage = r.stage
            sub = r.sub_index
        except UnroutableFix:
            stage = "_unrouted"
            sub = None
        out.setdefault(stage, {}).setdefault(sub, []).append(f)
    return out
