"""Top-level conftest.

Excludes the ``cloud/_bench/`` directory from collection — those are
archived cloud-service scaffolds (see ``cloud/_bench/README.md``) that
duplicate basenames from the live ``cloud/<service>/`` directories
and would otherwise cause pytest "import file mismatch" errors.

Also auto-restores ``sys.modules["googleapiclient*"]`` between tests
so test files that monkey-patch the package (e.g.
``tests/test_research_cross_engage.py`` and
``tests/test_pipeline_youtube_stats.py``) don't pollute later tests
that import the real client.
"""
from __future__ import annotations

import sys

import pytest


collect_ignore_glob = [
    "cloud/_bench/*",
    "cloud/_bench/**/*",
]


_GOOGLE_KEYS = (
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "googleapiclient.http",
)


@pytest.fixture(autouse=True)
def _restore_googleapiclient_modules():
    """Snapshot + restore ``sys.modules`` entries for ``googleapiclient``
    and the ``google.cloud.storage`` submodule binding around every
    test. Tests that install fake stubs (``_install_fake_google_for_ce``,
    ``_install_fake_googleapiclient``, ``_install_fake_google``) leak
    the fake into all later tests.

    We do NOT snapshot ``google.cloud.firestore`` — too many tests use
    the real Firestore client and patching its ``sys.modules`` entry
    would force every test to re-import. We DO snapshot
    ``google.cloud.storage`` (via the package-attribute trick) because
    the ``from google.cloud import storage`` idiom in
    ``control/routes/script_jobs_routes.py`` binds the submodule onto
    the ``google.cloud`` package after the first import; without the
    restore, any later test that ``patch.dict("sys.modules",
    {"google.cloud.storage": fake})`` finds the test passes but the
    target call site bypasses the fake via the package attribute.
    """
    saved = {k: sys.modules.get(k) for k in _GOOGLE_KEYS}
    google_cloud_pkg = sys.modules.get("google.cloud")
    saved_storage_attr = (
        getattr(google_cloud_pkg, "storage", None)
        if google_cloud_pkg is not None else None
    )
    saved_firestore_attr = (
        getattr(google_cloud_pkg, "firestore", None)
        if google_cloud_pkg is not None else None
    )
    saved_storage_modules = {
        k: sys.modules.get(k)
        for k in ("google.cloud.storage", "google.cloud.firestore")
    }
    yield
    for k, prev in saved.items():
        if prev is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = prev
    for k, prev in saved_storage_modules.items():
        if prev is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = prev
    google_cloud_pkg = sys.modules.get("google.cloud")
    if google_cloud_pkg is not None:
        for attr_name, prev in (
            ("storage", saved_storage_attr),
            ("firestore", saved_firestore_attr),
        ):
            if prev is None:
                if hasattr(google_cloud_pkg, attr_name):
                    try:
                        delattr(google_cloud_pkg, attr_name)
                    except AttributeError:
                        pass
            else:
                setattr(google_cloud_pkg, attr_name, prev)

