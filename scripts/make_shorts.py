#!/usr/bin/env python3
"""Thin CLI shim — real implementation lives in :mod:`pipeline.render.shorts`.

Why this shim exists: the cloud worker
(``workers/heavy/render_short.py``) shells out to this exact path
(``python scripts/make_shorts.py --channel … --script …``). Promoting
the renderer to ``pipeline.render.shorts`` would break that subprocess
contract; keeping a thin re-entry here keeps the worker, all docs, and
muscle-memory CLI invocations working.

The shim does three things only:
  1. Add the repo root to ``sys.path`` so ``import pipeline`` works
     when invoked via ``python scripts/make_shorts.py``.
  2. Hand control to :func:`pipeline.render.shorts.cli_main`.
  3. Print an OUTPUT_MANIFEST line on stdout so the cloud worker can
     locate the produced mp4 + thumb without guessing per-channel paths
     (formerly hardcoded as ``data/shorts/<slug>.mp4`` which broke when
     the per-channel layout landed in 2026-05-05).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo root = parent of scripts/. Defensive — covers `python scripts/foo.py`
# from any cwd, and falls back gracefully if pipeline is already on path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # pragma: no cover

from pipeline.render.shorts import cli_main


if __name__ == "__main__":
    cli_main()
