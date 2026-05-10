#!/usr/bin/env python3
"""Thin CLI shim — real implementation lives in :mod:`pipeline.render.sports_doc`.

Long-form sports documentary renderer (12-25 min, 16:9, broadcast-style
hybrid of footage clips + commentary takes + scoreline cards). Authored
by the /make-sports-doc skill.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.render.sports_doc import cli_main


if __name__ == "__main__":
    cli_main()
