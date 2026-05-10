#!/usr/bin/env python3
"""Thin CLI shim — real implementation lives in :mod:`pipeline.render.footage_only`.

Channel-agnostic footage-only renderer. Driven by a per-channel shotlist
(JSON) + narration; no AI image gen. Used by historyrecapped (cron),
cosmosdecoded (Eddington-style physics), and any future footage-only
channel via ``--channel <slug>``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.render.footage_only import cli_main


if __name__ == "__main__":
    cli_main()
