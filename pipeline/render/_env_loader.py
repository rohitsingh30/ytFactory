"""Shared `.env` loader for the render pipeline entry points.

**Audit D3.48** — pre-fix three render modules had near-identical
``_load_env`` implementations:

  * ``pipeline/render/long_form.py``  — strips outer quotes
  * ``pipeline/render/sports_doc.py`` — strips outer quotes
  * ``pipeline/render/footage_only.py`` — does NOT strip quotes (bug)

The footage_only version drifted, so a value like
``HF_TOKEN="hf_..."`` was written to os.environ as the literal
``"hf_..."`` string (with quotes) — every HF API call then 401'd
because the bearer-token header included the literal quote chars.

This module is the single source of truth. All three callers now
import :func:`load_dotenv_into_environ` from here.

Behaviour:

* Reads ``<repo_root>/.env``; no-op if missing (returns early).
* Strips comments (``# ...``) and blank lines.
* Splits on the FIRST ``=``; key + value both stripped of
  surrounding whitespace.
* Strips matching outer quotes (single or double) from the value.
* Uses ``os.environ.setdefault`` so existing env vars are NOT
  overridden (the shell wins over .env).
"""
from __future__ import annotations

import os
from pathlib import Path


def _strip_outer_quotes(s: str) -> str:
    """Strip ONE pair of outer matching quotes (single or double)."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def load_dotenv_into_environ(repo_root: Path) -> None:
    """Load ``<repo_root>/.env`` into os.environ via setdefault."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), _strip_outer_quotes(v.strip()))


__all__ = ["load_dotenv_into_environ"]
