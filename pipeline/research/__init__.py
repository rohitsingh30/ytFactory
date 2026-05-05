"""Research + aggregation stack (Wikipedia, YouTube, Reddit-derived
sources for the dashboard) and YouTube cross-engagement automation.

Sub-packaged 2026-05-05 (Phase 3 / Tier-S3 layout cleanup): the
4 flat modules under ``pipeline/`` that fed the research dashboard
or interacted with the YouTube Data API were grouped here for
discoverability.

Public surface from the legacy ``pipeline/research.py`` is
re-exported below so existing callers keep working unchanged.
"""
from __future__ import annotations

# Re-export the legacy pipeline.research surface (now in aggregator.py)
# so `from pipeline.research import X` keeps working.
from pipeline.research.aggregator import *  # noqa: F401,F403
