#!/usr/bin/env python3
"""Verify that an assistant response addresses every extracted user ask.

argv[1]: JSON array of user asks (output of extract_user_asks.py)
stdin: the assistant response text

stdout: JSON array of asks deemed "dropped" — coverage too low.
exit 0 always.

Heuristic: for each ask, tokenize the ask text (keep alnum strings
>= 4 chars, drop stopwords). Count how many of those tokens appear
anywhere in the response (case-insensitive substring). If the
fraction matched is < 25% AND fewer than 3 absolute matches, flag
the ask as dropped.

Previous implementation was inlined into stop_audit.sh as a heredoc
with `python -` and a `<<<` here-string. The here-string never
actually wired stdin to the script — `python -` was reading the
heredoc as its script source, then `sys.stdin.read()` saw EOF, so
text was always empty, so every ask was always flagged. That false-
positive blocked legitimate turns repeatedly. This helper has its
own stdin and own argv — no bash interaction quirks.
"""
from __future__ import annotations

import json
import re
import sys

# Stopwords that aren't substantive tokens. Short words are filtered
# automatically by the length>=4 rule; this list catches longer
# fillers.
_STOPWORDS = {
    "this", "that", "with", "from", "into", "does", "need", "also",
    "just", "like", "what", "when", "want", "have", "make", "they",
    "them", "your", "you're", "still", "even", "tell", "give", "show",
}


def tokens(s: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9_]{4,}", s.lower())
        if t not in _STOPWORDS
    ]


def main() -> int:
    asks_raw = sys.argv[1] if len(sys.argv) > 1 else "[]"
    try:
        asks = json.loads(asks_raw)
    except json.JSONDecodeError:
        json.dump([], sys.stdout)
        return 0

    text = sys.stdin.read().lower()
    dropped: list[dict] = []
    for a in asks:
        ask_text = a.get("ask", "")
        toks = set(tokens(ask_text))
        if not toks:
            # Ask has no substantive tokens — can't measure coverage,
            # assume it's covered by virtue of being responded to at all.
            continue
        matches = sum(1 for t in toks if t in text)
        coverage = matches / max(len(toks), 1)
        if coverage < 0.25 and matches < 3:
            dropped.append(a)

    json.dump(dropped, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
