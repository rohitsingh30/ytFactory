"""Per-frame anatomy QC for character-driven channels.

Catches the class-of-bug surfaced by the user 2026-05-07 on the
TIFU love-you and trainee Shorts: weird hands, extra hands, fused
fingers — the hallmark diffusion artefacts that the existing
`quality_gate.check_image` doesn't detect (it only checks
brightness, edge density, file size, optional OCR).

Approach: haiku vision via the `claude` CLI's `Read` tool. The
model is given a focused yes/no rubric — it just has to detect
hand/finger glitches, not produce a full critique. Returns
``(ok, reason)`` with the same shape as ``quality_gate.check_image``
so the existing QC retry loop in ``pipeline/render/shorts.py``
absorbs it without restructuring.

Cost: ~$0.002 per image at haiku 4.5 + 1024px input. For a 26-beat
Short that's ~$0.05 added per render. Latency: ~2-4s per check.
Both acceptable when the alternative is a hand-glitched final mp4.

Opt in via channel YAML: ``anatomy_check: true``. Default off so
non-character channels (history archival, cosmos footage-only) skip.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import cli as llm


_RUBRIC_PROMPT = """\
You are a quality-control reviewer for AI-generated cartoon images.
Read the image at {path}. Reply ONLY with the JSON object below.

Inspect the image SPECIFICALLY for these diffusion-glitch failure
modes that pull viewers out of a Short:

1. Hands with the wrong number of fingers (not exactly 5 per hand,
   when fingers are visible — clenched fists are fine).
2. Extra hands or arms attached to a body where they don't belong
   (e.g. a third hand, a hand growing out of a torso).
3. Fingers fused together, melted, bent at impossible angles, or
   one finger drawn as two.
4. Hands at anatomically impossible positions (wrist twisted past
   180°, palm-out when arm is rotated palm-in, etc.).

Style cues like "thick black outlines, crayon shading, simple
round-headed characters" are CHANNEL STYLE — not glitches.
Distorted faces / eyes / clothing are NOT in scope. Focus only on
hands and fingers.

If the image has NO hands or fingers visible (off-frame, behind
back, clenched, in pockets, etc.) — that is FINE, return ok=true.

Return JSON exactly:
{{
  "ok": true | false,
  "reason": "short reason string, ≤60 chars; empty if ok"
}}
"""


def check_anatomy(
    png_path: Path,
    *,
    model: str = "haiku",
    timeout_s: int = 60,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for hand/finger anatomy.

    On any LLM-side error (timeout, parse failure, unreachable
    keychain), returns ``(True, "")`` — fail open. The quality gate
    is a *belt*, not a *substitute*: a flaky vision call shouldn't
    block an otherwise-fine image. The retry loop catches genuine
    glitches via the user-flagged `qc=fail reason=...` path.
    """
    try:
        prompt = _RUBRIC_PROMPT.format(path=str(png_path))
        result = llm.call_claude_cli(
            prompt,
            output_json=True,
            allowed_tools=["Read"],
            add_dirs=[png_path.parent],
            model=model,
            timeout_s=timeout_s,
            budget_usd=0.05,
            stage="anatomy_check",
        )
    except Exception as e:
        # Fail open on infrastructure issues. The dominant gate is
        # luminance/edges; anatomy is the cherry on top.
        return True, ""

    if not isinstance(result, dict):
        return True, ""
    ok = bool(result.get("ok", True))
    reason = str(result.get("reason", "") or "")[:120]
    if ok:
        return True, ""
    return False, f"anatomy: {reason}" if reason else "anatomy: glitch detected"
