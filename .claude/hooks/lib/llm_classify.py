#!/usr/bin/env python3
"""LLM-backed classifier for hook decisions.

Used by analysis_vs_facts.sh (P14) and critique_quality.sh (P27)
to do semantic checks that pure regex can't.

Stdin: JSON {"question": "<the yes/no question>", "context": "<text to classify>"}
Stdout: JSON {"answer": "yes"|"no"|"unsure", "reason": "<one line>"}
Exit 0 always. Caller decides what to do with the answer.

Uses Anthropic SDK with Haiku model (cheap + fast). Falls back to
"unsure" if the API isn't available, so hooks fail open rather than
blocking everything.
"""
from __future__ import annotations

import json
import os
import sys


def classify(question: str, context: str) -> dict:
    """Single-shot yes/no/unsure classification via Haiku."""
    try:
        import anthropic
    except ImportError:
        return {"answer": "unsure", "reason": "anthropic SDK not installed"}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"answer": "unsure", "reason": "ANTHROPIC_API_KEY not set"}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        # Use Haiku — cheap and fast (~500ms).
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            system=(
                "You answer yes/no/unsure questions about whether a "
                "given text matches a property. Respond ONLY with JSON: "
                '{"answer": "yes"|"no"|"unsure", "reason": "<one short line>"}. '
                "No markdown, no extra prose. If you cannot determine the "
                "answer from the text alone, return 'unsure'."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Text to evaluate:\n```\n{context[:6000]}\n```\n\n"
                    "Return JSON only."
                ),
            }],
            timeout=10,
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", None) == "text"
        ).strip()
        # The model sometimes wraps in markdown despite instruction; strip.
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as exc:
        return {"answer": "unsure", "reason": f"llm error: {exc}"[:200]}


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        json.dump({"answer": "unsure", "reason": "bad input json"}, sys.stdout)
        return 0
    question = payload.get("question", "")
    context = payload.get("context", "")
    out = classify(question, context)
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
