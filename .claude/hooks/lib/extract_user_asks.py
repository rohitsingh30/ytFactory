#!/usr/bin/env python3
"""Extract distinct asks from a user message.

Stdin: raw user message text.
Stdout: JSON array of {"ask": str, "kind": "imperative"|"question"|"correction"|"directive"}.

Used by UserPromptSubmit hook to seed the turn checklist. The Stop hook
reads this checklist back and verifies the agent's response addressed
every item.

This is grep + heuristics — not a perfect parser. The goal is to surface
distinct asks an agent might silently drop, not a full NL semantic split.
"""
from __future__ import annotations

import json
import re
import sys

# Imperatives: short sentences starting with a verb directing action.
_IMPERATIVE_VERBS = (
    "do|run|check|read|write|fix|build|deploy|test|verify|investigate|"
    "render|generate|create|delete|cancel|stop|start|continue|spawn|"
    "kill|grep|find|look|show|tell|give|use|skip|don't|do not|never|"
    "always|prefer|fold|move|push|pull|commit|trigger|queue|implement|"
    "research|critique|publish|ship|merge|open|close|update|add|remove"
)
_IMPERATIVE_RE = re.compile(
    rf"(?:^|[\n.!?]\s*)((?:{_IMPERATIVE_VERBS})\b[^.!?\n]{{2,200}}[.!?]?)",
    re.IGNORECASE,
)

# Questions: sentences ending with ?
_QUESTION_RE = re.compile(r"([^.!?\n]{4,300}\?)", re.IGNORECASE)

# Corrections: explicit "this is wrong", "no", "not X", etc.
_CORRECTION_RE = re.compile(
    r"((?:^|[\n.!?]\s*)(?:no |that's not|this is not|wrong |stop |"
    r"don't |you didn't|you missed|why didn't)[^.!?\n]{2,200}[.!?]?)",
    re.IGNORECASE,
)

# Numbered items in the message: "1. foo", "2. bar"
_NUMBERED_RE = re.compile(
    r"(?:^|\n)\s*(\d+[.)]\s+[^\n]{4,400})",
    re.MULTILINE,
)

# Bulleted items: "- foo" or "* foo"
_BULLETED_RE = re.compile(
    r"(?:^|\n)\s*([-*]\s+[^\n]{4,400})",
    re.MULTILINE,
)


def extract(text: str) -> list[dict]:
    """Return a list of {ask, kind} items, deduplicated by first 80 chars."""
    seen: set[str] = set()
    items: list[dict] = []

    def _add(ask: str, kind: str) -> None:
        ask = re.sub(r"\s+", " ", ask).strip(" .,!?")
        if len(ask) < 5:
            return
        sig = ask[:80].lower()
        if sig in seen:
            return
        seen.add(sig)
        items.append({"ask": ask[:400], "kind": kind})

    # Numbered + bulleted lists are usually the cleanest signal — handle
    # them first so they show up before imperative-style picks of the same
    # text.
    for m in _NUMBERED_RE.finditer(text):
        _add(m.group(1), "directive")
    for m in _BULLETED_RE.finditer(text):
        _add(m.group(1), "directive")
    for m in _CORRECTION_RE.finditer(text):
        _add(m.group(1), "correction")
    for m in _QUESTION_RE.finditer(text):
        _add(m.group(1), "question")
    for m in _IMPERATIVE_RE.finditer(text):
        _add(m.group(1), "imperative")

    # If we found nothing structured, treat the entire message as a
    # single ask (short messages like "do it" / "go" / "continue").
    if not items and text.strip():
        _add(text.strip(), "imperative")

    return items


def main() -> int:
    raw = sys.stdin.read()
    out = extract(raw)
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
