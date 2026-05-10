"""Test-suite-wide fixtures.

Born from the 2026-05-10 token-issue post-mortem: ``VideosBatchingTest``
in ``test_pipeline_youtube_stats.py`` had a fixture that monkey-patched
``pipeline.research.youtube.YOUTUBE_DIR`` to a tempdir, but other code
paths inside the same test (notably ``iter_channel_configs()`` which
walks PROJECT_ROOT) still saw the real laptop layout. Combined with
the fake ``googleapiclient`` returning ``"Biggie / UC_b / V_000"``
placeholder data and ``fetch_account()`` writing through the *real*
``YOUTUBE_DIR``, this corrupted the production cache files that get
baked into every prod deploy.

This module's autouse fixtures + the in-module guards (see
``pipeline/research/youtube.py::fetch_account``) make sure the same
mistake can never silently corrupt the real cache again. Two layers:

1. ``isolate_research_dirs`` (autouse) repoints the per-test versions
   of every research write surface to a per-test ``tmp_path``. Tests
   that genuinely need the laptop's real cache opt out via
   ``@pytest.mark.no_research_isolation``.

2. ``pipeline/research/youtube.py::_assert_safe_to_write`` raises if
   anything tries to write inside the real ``data/research/youtube/``
   while ``PYTEST_CURRENT_TEST`` is set. Belt-and-braces — even if a
   future fixture forgets to apply the patch.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolate_research_dirs(request, tmp_path, monkeypatch):
    """Repoint every research write-surface to a per-test tmp dir.

    Opt out with ``@pytest.mark.no_research_isolation`` when a test
    genuinely needs the laptop's real cache (rare — usually only smoke
    tests that pre-load fixtures from the real disk).
    """
    if request.node.get_closest_marker("no_research_isolation"):
        return

    research_root = tmp_path / "research"
    youtube_dir = research_root / "youtube"
    assets_dir = research_root / "channel_assets"
    analytics_dir = research_root / "analytics"
    youtube_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    analytics_dir.mkdir(parents=True, exist_ok=True)

    # Imports kept inside the fixture so a test failing to import
    # pipeline.research.* doesn't take the whole suite down.
    try:
        from pipeline.research import youtube as _yt
        monkeypatch.setattr(_yt, "YOUTUBE_DIR", youtube_dir, raising=False)
        # The provider-aware cache also keeps an in-process LRU; flush
        # it so a previous test's reads can't leak forward.
        if hasattr(_yt, "_READ_CACHE"):
            _yt._READ_CACHE.clear()
    except Exception:
        pass

    try:
        from pipeline.research import channel_assets as _ca
        monkeypatch.setattr(_ca, "ASSETS_DIR", assets_dir, raising=False)
        monkeypatch.setattr(_ca, "YOUTUBE_DIR", youtube_dir, raising=False)
    except Exception:
        pass

    # Also point the aggregator's local copy of YOUTUBE_DIR at the same
    # tmp dir so build_videos / build_channels don't accidentally read
    # the real cache.
    try:
        from pipeline.research import aggregator as _agg
        monkeypatch.setattr(_agg, "YOUTUBE_DIR", youtube_dir, raising=False)
    except Exception:
        pass

    # Make absolutely sure no test accidentally hits the cloud bucket.
    monkeypatch.delenv("YTFACTORY_STATE_BUCKET", raising=False)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_research_isolation: opt out of the research-dirs autouse fixture",
    )


# Re-export the top-level conftest's collect_ignore so pytest-discovery
# still skips cloud/_bench/.
collect_ignore_glob = ["cloud/_bench/*", "cloud/_bench/**/*"]
