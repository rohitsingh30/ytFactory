#!/usr/bin/env python3
"""Scan an assistant response for claim-without-evidence.

Stdin: full assistant response text (typically a single Stop-hook turn).
Stdout: JSON {"claims": [..], "evidence": [..], "unsupported": [..]}.
Exit 0 always (caller decides what to do with `unsupported`).

A "claim" is a sentence asserting completion / success / a property of
the world. An "evidence anchor" is something concrete in the same
response that grounds it: a commit SHA, a file path, an exit code,
a quoted artifact field, a tool-result block.

Heuristic, not semantic. Tuned to catch the 2026-05-24 "shipped/verified
off rev-healthy alone" failure mode where claim words appeared without
artifact citation.
"""
from __future__ import annotations

import json
import re
import sys

# Claim words: assertions of completion or success.
_CLAIM_RE = re.compile(
    r"\b(shipped|deployed|verified|fixed|working|works|done|complete[d]?|"
    r"succeeded?|landed|passing|green|all tests pass|"
    r"confirmed|rev[- ]healthy|in production|live)\b",
    re.IGNORECASE,
)

# Evidence anchors: concrete artifacts.
_EVIDENCE_RES = [
    re.compile(r"\b[0-9a-f]{7,40}\b"),               # commit SHA
    re.compile(r"exit (?:code )?[ =]?\s*0\b"),       # process exit 0
    re.compile(r"\bpassed\b|✓|✅|PASS\b"),            # test pass
    re.compile(r"\bgs://[\w\-./]+"),                  # GCS uri
    re.compile(r"/[\w\-./]+\.(?:py|sh|yaml|yml|md|json|jsonl|mp4|png)"),  # file path
    re.compile(r"\bjob[_ -]?id[=:]\s*[a-f0-9]{16,}"), # job id
    re.compile(r"\bfallback_count[:=]\s*0\b"),       # specific success field
    re.compile(r"^\s*\$\s+.*", re.MULTILINE),        # shell command echo
    re.compile(r"<tool_use_result\b", re.IGNORECASE),
    re.compile(r"```(?:bash|python|json|yaml|diff)", re.IGNORECASE),
    re.compile(r"\b[0-9]+/[0-9]+ (?:tests? )?(?:pass|green|ok)\b", re.IGNORECASE),
    re.compile(r"\bcommit [0-9a-f]{7,40}\b"),
    re.compile(r"\brevision [a-z0-9\-]+", re.IGNORECASE),
]

# Sentence splitter — coarse but good enough for the heuristic.
_SENT_RE = re.compile(r"[^.!?\n]{4,500}[.!?]")


def scan(text: str) -> dict:
    sentences: list[str] = []
    for m in _SENT_RE.finditer(text):
        sentences.append(m.group(0).strip())

    claims: list[str] = []
    for s in sentences:
        if _CLAIM_RE.search(s):
            claims.append(s)

    # Collect all evidence anchors found anywhere in the response.
    evidence: list[str] = []
    for rx in _EVIDENCE_RES:
        for m in rx.finditer(text):
            ev = m.group(0).strip()[:200]
            if ev and ev not in evidence:
                evidence.append(ev)

    # A claim is "supported" if any evidence anchor appears within 600
    # characters of it (loosely "same paragraph"). This is heuristic —
    # tighter context would be better but expensive to compute.
    unsupported: list[str] = []
    for c in claims:
        c_pos = text.find(c)
        if c_pos < 0:
            continue
        window = text[max(0, c_pos - 600): c_pos + 600]
        supported = any(rx.search(window) for rx in _EVIDENCE_RES)
        if not supported:
            unsupported.append(c)

    return {"claims": claims, "evidence": evidence, "unsupported": unsupported}


def main() -> int:
    raw = sys.stdin.read()
    out = scan(raw)
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
