#!/usr/bin/env python3
"""Thin CLI shim — real implementation lives in :mod:`pipeline.render.long_form`.

Kept here so existing invocations (cron, /make-katha skill,
/make-sleep-history skill, etc.) keep working without any retraining.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.render.long_form import cli_main


if __name__ == "__main__":
    cli_main()
