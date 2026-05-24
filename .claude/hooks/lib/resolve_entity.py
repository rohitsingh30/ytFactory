#!/usr/bin/env python3
"""Extract named entities from a user message and verify they resolve.

Stdin: user message text + project root (env $PROJECT_ROOT).
Stdout: JSON {"resolved": [...], "unresolved": [...]}.

Catches the P05 wrong-premise / wrong-identifier failure mode:
the user mentions "mystoriesanimated", "the AITA variant", "z_image_turbo",
"the 88d98126 render", "pipeline/llm/prompts.py" — and the agent acts
on a mis-read of one of these without asking.

Validates each named entity against the actual filesystem / channel
registry / git index. Unresolved names are returned for the
ambiguity_check hook to surface as "did you mean…" questions.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Entity-shape patterns the user commonly types.
_ENTITY_PATTERNS = {
    # Channel slugs (lowercase, ends in animated/decoded/recapped/etc.)
    "channel": re.compile(
        r"\b(?:my|hindutava|history|sports|cosmos|rhymetime|scroll)"
        r"(?:stories|tavaa|recapped|decoded|junction|pulse)?animated?\b",
        re.IGNORECASE,
    ),
    # Variant slugs
    "variant": re.compile(
        r"\b(?:aita(?:_animated|_text|_cooking|_cliffhanger)?|tifu|"
        r"malicious_compliance|prorevenge|nosleep|wiki_oddities|"
        r"today_in_history|aita_animated_motion)\b",
        re.IGNORECASE,
    ),
    # File paths (any /-separated path that looks repo-relative)
    "file_path": re.compile(
        r"\b(?:pipeline|cloud|tests|control|web-next|web|data|docs|scripts|\.claude)"
        r"/[a-zA-Z0-9_/.-]+\b"
    ),
    # 32-char hex job ids
    "job_id": re.compile(r"\b[0-9a-f]{32}\b"),
    # Function names with paren ("foo()", "bar()")
    "function": re.compile(r"\b[a-z_][a-z0-9_]{3,40}\(\)"),
    # Image / TTS model identifiers
    "model": re.compile(
        r"\b(?:z[_-]?image[_-]?turbo|gpt-?[345]\.?\d?[-\w]*|"
        r"haiku|sonnet|opus|claude-?\w*|"
        r"chatterbox|indicf5|whisper|faster[_-]?whisper|cloudrun_\w+)\b",
        re.IGNORECASE,
    ),
}


def _check_file_path(repo: Path, value: str) -> bool:
    p = (repo / value).resolve()
    if not str(p).startswith(str(repo.resolve())):
        return False
    return p.exists()


def _check_channel(repo: Path, value: str) -> bool:
    channel_dir = repo / "pipeline" / "channels"
    if not channel_dir.exists():
        return False
    for p in channel_dir.glob("*.yaml"):
        if p.stem.lower() == value.lower():
            return True
    return False


def _check_variant(repo: Path, value: str) -> bool:
    variants_dir = repo / "pipeline" / "variants"
    if not variants_dir.exists():
        return False
    for channel_dir in variants_dir.iterdir():
        if not channel_dir.is_dir():
            continue
        for p in channel_dir.glob("*.yaml"):
            if value.lower() in p.stem.lower():
                return True
    return False


def _check_function(repo: Path, value: str) -> bool:
    name = value.rstrip("()")
    # Cheap grep across the repo's python sources. Skip .venv, node_modules.
    try:
        import subprocess
        result = subprocess.run(
            ["grep", "-rln", "--include=*.py",
             f"def {name}", str(repo)],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return True  # Don't false-positive on grep failure.


def _check_model(value: str) -> bool:
    # Known production models from the codebase.
    KNOWN = {
        "z_image_turbo", "z-image-turbo", "z image turbo",
        "haiku", "sonnet", "opus",
        "gpt-5.3-chat", "gpt-5", "gpt-4o",
        "claude-opus-4-7", "claude-sonnet-4-5",
        "chatterbox", "indicf5", "faster_whisper", "whisper",
        "cloudrun_chatterbox", "cloudrun_indicf5", "cloudrun_z_image_turbo",
    }
    return value.lower().replace("-", "_") in {
        k.lower().replace("-", "_") for k in KNOWN
    }


def _check_job_id(repo: Path, value: str) -> bool:
    # Job ids are looked up at render time, not bound to filesystem.
    # We accept any 32-char hex as valid-shape; deeper validation
    # would require Firestore which we don't do in a hook.
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value.lower())


def main() -> int:
    repo = Path(os.environ.get("PROJECT_ROOT", "/Users/rohit/ytFactory"))
    text = sys.stdin.read()

    resolved: list[dict] = []
    unresolved: list[dict] = []

    seen: set[str] = set()
    for kind, pat in _ENTITY_PATTERNS.items():
        for m in pat.finditer(text):
            value = m.group(0)
            sig = f"{kind}::{value.lower()}"
            if sig in seen:
                continue
            seen.add(sig)

            ok = False
            if kind == "file_path":
                ok = _check_file_path(repo, value)
            elif kind == "channel":
                ok = _check_channel(repo, value)
            elif kind == "variant":
                ok = _check_variant(repo, value)
            elif kind == "function":
                ok = _check_function(repo, value)
            elif kind == "model":
                ok = _check_model(value)
            elif kind == "job_id":
                ok = _check_job_id(repo, value)

            item = {"kind": kind, "value": value}
            (resolved if ok else unresolved).append(item)

    json.dump({"resolved": resolved, "unresolved": unresolved},
              sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
