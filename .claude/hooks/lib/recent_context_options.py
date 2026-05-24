#!/usr/bin/env python3
"""Extract choice-options from recent assistant messages in the
transcript. Used by:
  - P12 ambiguity check (userprompt_init.sh) — to detect that the
    user's short prompt is a RESPONSE to a recent option list, not
    a fresh ambiguous request.
  - P12 block message — when blocking, surface the options to the
    user so they can pick.

argv[1]: transcript_path
argv[2] (optional): "json" | "text" — output format
        default "json": {"has_options": bool, "options": [...]}
        "text" prints options as numbered lines for stderr injection
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# Patterns that indicate the agent offered multiple-choice options
# in its recent message.
# Match "1. text" / "2) text" / "A. text" / "A) text" — numbered or
# letter-labeled list items.
_NUMBERED_OPTION_RE = re.compile(
    r"^\s*([0-9]+|[A-D])[.)]\s+(.{8,200})$",
    re.MULTILINE,
)
# Match markdown table rows like "| A | text… |" or "| 1 | text… |"
_MARKDOWN_TABLE_NUMBERED_RE = re.compile(
    r"^\|\s*([0-9]+|[A-D])\s*\|\s*(.{8,200}?)\s*\|",
    re.MULTILINE,
)
# Match bold-labeled options like "**A.** text" or "**1.** text"
_BOLD_OPTION_RE = re.compile(
    r"\*\*([0-9]+|[A-D])[.)]\*\*\s+(.{8,200})",
)
# Section headers like "**Pick one:**" that strongly imply options follow
_PICK_PROMPT_RE = re.compile(
    r"\b(pick|choose|which (one|of|do|should)|select)\b",
    re.IGNORECASE,
)


def last_assistant_text(transcript_path: str) -> str:
    last = ""
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                if m.get("role") != "assistant":
                    continue
                c = m.get("content", "")
                if isinstance(c, list):
                    text = " ".join(
                        x.get("text", "") for x in c
                        if isinstance(x, dict) and x.get("type") == "text"
                    )
                else:
                    text = str(c)
                if text.strip():
                    last = text
    except Exception:
        pass
    return last


def extract_options(text: str) -> list[str]:
    """Return up to 6 distinct option-labels from a multi-choice block
    in the assistant's recent message."""
    options: list[str] = []
    seen: set[str] = set()

    for m in _NUMBERED_OPTION_RE.finditer(text):
        label = f"{m.group(1)}: {m.group(2).strip()}"
        sig = label[:60].lower()
        if sig not in seen:
            seen.add(sig)
            options.append(label[:200])

    for m in _MARKDOWN_TABLE_NUMBERED_RE.finditer(text):
        label = f"{m.group(1)}: {m.group(2).strip()}"
        sig = label[:60].lower()
        if sig not in seen:
            seen.add(sig)
            options.append(label[:200])

    return options[:6]


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"has_options": False, "options": []}))
        return 0
    transcript = sys.argv[1]
    fmt = sys.argv[2] if len(sys.argv) > 2 else "json"

    text = last_assistant_text(transcript)
    options = extract_options(text) if text else []
    has_pick_signal = bool(_PICK_PROMPT_RE.search(text)) if text else False

    result = {
        "has_options": len(options) >= 2 or has_pick_signal,
        "options": options,
        "has_pick_signal": has_pick_signal,
    }

    if fmt == "text":
        if result["has_options"]:
            for opt in options:
                print(f"  - {opt}")
        else:
            print("(no recent option-list detected)")
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
