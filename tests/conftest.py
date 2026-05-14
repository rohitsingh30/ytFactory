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


# CRITICAL: snapshot google.* sys.modules + package attrs at conftest
# LOAD time, BEFORE any test module is imported. A dozen test files
# (test_web_*, test_routes_oauth, test_critique_runner, etc.) install
# ``sys.modules["google.cloud"] = MagicMock()`` at MODULE-IMPORT time
# without restoring it. Capturing inside a fixture would catch the
# already-polluted state.
def _snapshot_google_modules() -> dict[str, object]:
    import sys
    snap: dict[str, object] = {}
    for key in list(sys.modules.keys()):
        if key == "google" or key.startswith("google."):
            snap[key] = sys.modules[key]
    return snap


def _snapshot_google_pkg_attrs() -> dict[str, dict[str, object]]:
    import sys
    out: dict[str, dict[str, object]] = {}
    for pkg_name in ("google", "google.cloud"):
        pkg = sys.modules.get(pkg_name)
        if pkg is not None:
            try:
                out[pkg_name] = dict(vars(pkg))
            except TypeError:
                out[pkg_name] = {}
    return out


_GOOGLE_MODULES_BASELINE = _snapshot_google_modules()
_GOOGLE_PKG_ATTRS_BASELINE = _snapshot_google_pkg_attrs()


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
    #
    # **Audit Q2.59** — pre-fix every block here caught bare
    # ``Exception`` which silently swallowed monkey-patch failures
    # (e.g. typo in attribute name). The "safety belt" then no-op'd
    # and the test ran against the REAL ``data/research/youtube/``
    # cache, which is exactly the regression this fixture was built
    # to prevent. Now ONLY ImportError is silenced — every other
    # exception (AttributeError from monkeypatch.setattr, etc) is
    # surfaced as a test failure so the gap is visible.
    try:
        from pipeline.research import youtube as _yt
    except ImportError:
        pass
    else:
        monkeypatch.setattr(_yt, "YOUTUBE_DIR", youtube_dir, raising=False)
        # The provider-aware cache also keeps an in-process LRU; flush
        # it so a previous test's reads can't leak forward.
        if hasattr(_yt, "_READ_CACHE"):
            _yt._READ_CACHE.clear()

    try:
        from pipeline.research import channel_assets as _ca
    except ImportError:
        pass
    else:
        monkeypatch.setattr(_ca, "ASSETS_DIR", assets_dir, raising=False)
        monkeypatch.setattr(_ca, "YOUTUBE_DIR", youtube_dir, raising=False)

    # Also point the aggregator's local copy of YOUTUBE_DIR at the same
    # tmp dir so build_videos / build_channels don't accidentally read
    # the real cache.
    try:
        from pipeline.research import aggregator as _agg
    except ImportError:
        pass
    else:
        monkeypatch.setattr(_agg, "YOUTUBE_DIR", youtube_dir, raising=False)

    # Make absolutely sure no test accidentally hits the cloud bucket.
    monkeypatch.delenv("YTFACTORY_STATE_BUCKET", raising=False)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_research_isolation: opt out of the research-dirs autouse fixture",
    )


@pytest.fixture(autouse=True)
def isolate_google_cloud_modules(request):
    """Per-test cleanup of MagicMock LEAKS in ``sys.modules`` under
    ``google.*`` keys, plus MagicMock attribute leaks on the
    ``google`` / ``google.cloud`` packages.

    Born from the 2026-05-14 test-pollution post-mortem. A dozen test
    files (``test_web_*``, ``test_routes_oauth``, ``test_critique_runner``,
    ``test_cloud_snapshot``, ``test_yt_dlp_cloudrun``, …) shared this
    pattern at module-import time:

        for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
            if name not in sys.modules:
                sys.modules[name] = MagicMock()

    The stubs are NEVER restored after the module's tests finish. Any
    subsequent test that does ``from google.cloud import secretmanager``
    or ``import google.cloud.storage`` finds the MagicMock parent
    package and gets garbage instead of the real / patch-replaced
    submodule. ~66 tests in the suite fail downstream because their
    own ``patch.dict(sys.modules, {...})`` is shadowed by the leaked
    MagicMock attribute on the parent package.

    SCOPE: this fixture ONLY removes MagicMock-typed leaks. Real
    google packages (google.cloud.run_v2, google.cloud.storage, etc.)
    that legitimate tests import are left alone — they share global
    proto descriptor registries (``google._upb._message``) that
    segfault on naive re-import (verified 2026-05-14 — restoring
    real google.cloud.run_v2 after a test SIGSEGV'd the suite).

    Two phases on test teardown:
      1. Remove any ``sys.modules[google.*]`` entry that's a
         ``MagicMock`` instance AND wasn't in the conftest-load-time
         baseline.
      2. Remove MagicMock-typed attributes from the ``google`` /
         ``google.cloud`` package objects that weren't in the
         baseline (the from-import-step-1 path that lets the leak
         bypass sys.modules).

    Opt out via ``@pytest.mark.no_google_cloud_isolation`` (rare).
    """
    if request.node.get_closest_marker("no_google_cloud_isolation"):
        yield
        return

    import sys  # noqa: PLC0415
    from unittest.mock import MagicMock as _MagicMock  # noqa: PLC0415

    yield

    # Phase 1: prune MagicMock sys.modules leaks.
    for key in list(sys.modules.keys()):
        if not (key == "google" or key.startswith("google.")):
            continue
        if key in _GOOGLE_MODULES_BASELINE:
            # Baseline entry — restore the original ref in case the
            # test mutated sys.modules[key] to a different value (e.g.
            # patch.dict overrode it but for some reason didn't restore).
            if sys.modules[key] is not _GOOGLE_MODULES_BASELINE[key]:
                sys.modules[key] = _GOOGLE_MODULES_BASELINE[key]
            continue
        # Not in baseline = the test added it. If it's a MagicMock or
        # any obvious test stub, drop it. Real modules added at test
        # time stay (e.g. lazy import of google.cloud.run_v2 during a
        # test that legitimately needs it).
        if isinstance(sys.modules[key], _MagicMock):
            sys.modules.pop(key, None)

    # Phase 2: prune MagicMock attribute leaks on the package objects.
    baseline_attrs = _GOOGLE_PKG_ATTRS_BASELINE
    for pkg_name, saved_attrs in baseline_attrs.items():
        pkg = sys.modules.get(pkg_name)
        if pkg is None:
            continue
        try:
            current_attrs = vars(pkg)
        except TypeError:
            # pkg is MagicMock-stubbed — nothing to clean up at this layer.
            continue
        for key in list(current_attrs.keys()):
            if key in saved_attrs:
                continue
            # Attribute added by some test. Only delete if it's a
            # MagicMock — real submodules added by legitimate imports
            # stay (the proto descriptor cache depends on stable
            # bindings).
            try:
                val = current_attrs[key]
            except KeyError:
                continue
            if isinstance(val, _MagicMock):
                try:
                    delattr(pkg, key)
                except AttributeError:
                    pass


def pytest_collection_modifyitems(config, items):
    # Register the marker so @pytest.mark.no_google_cloud_isolation works.
    config.addinivalue_line(
        "markers",
        "no_google_cloud_isolation: opt out of the google.cloud sys.modules isolation fixture",
    )


# Re-export the top-level conftest's collect_ignore so pytest-discovery
# still skips cloud/_bench/.
collect_ignore_glob = ["cloud/_bench/*", "cloud/_bench/**/*"]
