"""Coverage tests for ``requirements-control.txt`` — the cloud image's
runtime dependency contract.

These tests exist because the 2026-05-11 deploy of ``ytfactory-web``
shipped without ``google-cloud-run``. The control plane's
``trigger_render_job`` then ImportErrored on the SDK path inside the
container, fell back to ``gcloud run jobs execute``, and that failed
with::

    ERROR: (gcloud.run.jobs.execute) You do not currently have an
    active account selected.

…because the bundled gcloud CLI isn't authenticated as the runtime
service account. Net effect: every chat-confirmed render failed at
the ``dispatch`` stage. The user surfaced it; the test suite did not.
This module makes sure that class of bug fails ``pytest`` instead of
prod next time.

Two complementary checks:

1. ``ProductionRequiredGcpPackagesTest`` — explicit allow-list of
   GCP pip packages the cloud image MUST install. Walking the import
   graph alone isn't enough: a package may be imported via lazy
   ``noqa: PLC0415`` blocks scattered across modules, and the
   intent (hot-path vs gracefully-degraded) is what matters.

2. ``GoogleCloudImportInventoryTest`` — walks every ``.py`` file in
   the cloud-bound source roots, finds every ``google.cloud.X``
   import, and asserts each subpackage is either pinned in
   ``requirements-control.txt`` (via the allow-list) or explicitly
   marked optional (graceful ``try/except ImportError`` degradation
   only). Catches a NEW ``google.cloud.X`` import added in code but
   forgotten in the deps.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements-control.txt"

# Source roots the ``ytfactory-web`` Cloud Run image bakes in (mirrors
# the COPY directives in ``cloud/web-server/Dockerfile``).
CLOUD_SOURCE_ROOTS = [
    REPO / "control",
    REPO / "web",
    REPO / "pipeline",
]

# Map of ``google.cloud.<sub>`` → pip distribution name. Standard
# Google convention, but listed explicitly so a typo in a new import
# can't silently slip through (we'd rather get a "subpackage X not
# in map" error than guess).
GCP_SUBPACKAGE_TO_PIP = {
    "firestore": "google-cloud-firestore",
    "storage": "google-cloud-storage",
    "run_v2": "google-cloud-run",
    "secretmanager": "google-cloud-secret-manager",
    "bigquery": "google-cloud-bigquery",
    "logging": "google-cloud-logging",
    "pubsub_v1": "google-cloud-pubsub",
    "tasks_v2": "google-cloud-tasks",
}

# pip names the cloud image MUST install. Hot path or "raises clear
# error if missing" — i.e. removing one breaks production. Adding to
# this set is a deliberate decision; see the comment per package.
PRODUCTION_REQUIRED_GCP_PACKAGES = {
    # control.core.jobs._FirestoreJobs, control.core.queue,
    # control.core.rate_limit, control.core.scheduler, etc.
    # The single most-used GCP client across the orchestrator.
    "google-cloud-firestore",
    # control.core.storage signed_url, pipeline.research.youtube
    # GCS-canonical cache, pipeline.cloud.snapshot, niche_specs, …
    "google-cloud-storage",
    # control.core.cloud_run.trigger_render_job — without this the
    # whole render path is broken (the 2026-05-11 regression).
    "google-cloud-run",
    # pipeline.upload.upload._persist_token — rotates refreshed
    # YouTube OAuth tokens into Secret Manager when the blob lives
    # under SECRETS_ROOT (cloud-only path). Hot on every cloud-side
    # token refresh.
    "google-cloud-secret-manager",
}

# pip names that may be missing from the cloud image without breaking
# anything — every call site is wrapped in ``try/except ImportError``
# with a graceful fallback that doesn't crash the request. Adding
# here is also a deliberate decision; if a path moves OFF graceful
# degradation it must move into ``PRODUCTION_REQUIRED_GCP_PACKAGES``.
OPTIONAL_GCP_PACKAGES = {
    # pipeline.cloud.cost — billing dashboard. Falls back to
    # "BigQuery client init failed" message in the cost panel
    # rather than crashing the request.
    "google-cloud-bigquery",
    # pipeline.observability.gcp_log_bridge — only exercised when
    # ``K_SERVICE`` env is unset (laptop/dev), see
    # pipeline/observability/exporters.py:235. Cloud Run uses the
    # native stdout-collector path.
    "google-cloud-logging",
}

# Subpackages that come transitively with any GCP client (api_core,
# auth, protobuf) — never need an explicit pin.
GCP_TRANSITIVE_PACKAGES = {
    "api_core",
    "auth",
    "protobuf",
}

# Match either ``from google.cloud import X`` (single name; comma-
# separated names handled below) or ``import google.cloud.X``.
_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+google\.cloud\s+import\s+([a-zA-Z0-9_,\s]+?)(?:\s+as\s+\w+)?\s*(?:#.*)?$"
)
_DOT_IMPORT_RE = re.compile(
    r"^\s*import\s+google\.cloud\.([a-zA-Z0-9_]+)(?:\s+as\s+\w+)?\s*(?:#.*)?$"
)


def _iter_py_files():
    for root in CLOUD_SOURCE_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            # Skip __pycache__, tests, and any vendored snapshots.
            if "__pycache__" in p.parts:
                continue
            yield p


def _scan_google_cloud_imports() -> dict[str, list[str]]:
    """Return ``{subpackage: [file:line, …]}`` for every
    ``google.cloud.X`` import in the cloud source roots."""
    found: dict[str, list[str]] = {}
    for py in _iter_py_files():
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = py.read_text(encoding="utf-8", errors="replace")
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            m = _DOT_IMPORT_RE.match(line)
            if m:
                sub = m.group(1)
                found.setdefault(sub, []).append(
                    f"{py.relative_to(REPO)}:{lineno}"
                )
                continue
            m = _FROM_IMPORT_RE.match(line)
            if not m:
                continue
            for raw_name in m.group(1).split(","):
                # ``from google.cloud import storage as gcs`` → after
                # the regex strips the alias we just have ``storage``.
                # ``from google.cloud import firestore as _fs`` likewise.
                name = raw_name.strip().split()[0] if raw_name.strip() else ""
                if not name:
                    continue
                found.setdefault(name, []).append(
                    f"{py.relative_to(REPO)}:{lineno}"
                )
    return found


def _requirements_text() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


def _is_pinned(pkg: str, text: str) -> bool:
    """``pkg`` appears as a top-level requirement (not a substring of
    a comment / longer name). We accept ``pkg``, ``pkg==x``,
    ``pkg>=x``, ``pkg~=x``, ``pkg<…`` etc."""
    pat = re.compile(
        r"^\s*" + re.escape(pkg) + r"(?:\s*[=<>!~].*)?\s*$",
        flags=re.MULTILINE,
    )
    return bool(pat.search(text))


class ProductionRequiredGcpPackagesTest(unittest.TestCase):
    """Static contract — ``requirements-control.txt`` MUST pin every
    package the cloud image needs at runtime. Catches the 2026-05-11
    google-cloud-run regression at ``pytest`` time."""

    def test_every_required_package_is_pinned(self):
        text = _requirements_text()
        missing = sorted(
            pkg for pkg in PRODUCTION_REQUIRED_GCP_PACKAGES
            if not _is_pinned(pkg, text)
        )
        self.assertEqual(
            missing, [],
            "Production-required GCP package(s) missing from "
            "requirements-control.txt:\n  "
            + "\n  ".join(missing)
            + "\n\nWithout these the ytfactory-web cloud image "
              "silently breaks production code paths. See the per-"
              "package comment in requirements-control.txt for the "
              "exact failure mode."
        )

    def test_no_overlap_between_required_and_optional(self):
        """A package can't be both 'must install' and 'safe to skip'."""
        overlap = PRODUCTION_REQUIRED_GCP_PACKAGES & OPTIONAL_GCP_PACKAGES
        self.assertEqual(overlap, set(), f"overlap: {overlap}")

    def test_subpackage_map_covers_required_pip_names(self):
        """Every value in PRODUCTION_REQUIRED_GCP_PACKAGES must appear
        in GCP_SUBPACKAGE_TO_PIP (otherwise the inventory test below
        can't reverse-look-up the dep when it sees the import)."""
        mapped = set(GCP_SUBPACKAGE_TO_PIP.values())
        unmapped = sorted(
            PRODUCTION_REQUIRED_GCP_PACKAGES - mapped - OPTIONAL_GCP_PACKAGES
            - {p for p in PRODUCTION_REQUIRED_GCP_PACKAGES if p in mapped}
        )
        # Every required pkg should be in the map; OPTIONAL is also
        # expected to be in the map for the inventory test.
        for pkg in PRODUCTION_REQUIRED_GCP_PACKAGES:
            self.assertIn(
                pkg, mapped,
                f"{pkg} is required but not in GCP_SUBPACKAGE_TO_PIP — "
                f"add a mapping so the inventory test can recognise it"
            )


class GoogleCloudImportInventoryTest(unittest.TestCase):
    """Dynamic check — walk the cloud source roots, find every
    ``google.cloud.X`` import, classify it. Catches a NEW
    ``from google.cloud import …`` added in code but missed in the
    deps (the original failure mode, just in advance)."""

    def setUp(self):
        self.imports = _scan_google_cloud_imports()
        self.requirements = _requirements_text()

    def test_every_import_is_either_required_or_optional(self):
        """No ``google.cloud.X`` may slip in unclassified."""
        unclassified: dict[str, list[str]] = {}
        for sub, sites in self.imports.items():
            if sub in GCP_TRANSITIVE_PACKAGES:
                continue
            pip = GCP_SUBPACKAGE_TO_PIP.get(sub)
            if pip is None:
                unclassified[sub] = sites
                continue
            if pip in PRODUCTION_REQUIRED_GCP_PACKAGES:
                continue
            if pip in OPTIONAL_GCP_PACKAGES:
                continue
            unclassified[sub] = sites
        self.assertEqual(
            unclassified, {},
            "google.cloud.X imports found that aren't classified as "
            "required-or-optional in tests/test_cloud_runtime_deps.py:\n"
            + "\n".join(
                f"  google.cloud.{sub}\n    " + "\n    ".join(sites)
                for sub, sites in unclassified.items()
            )
            + "\n\nDecide: hot-path (add to PRODUCTION_REQUIRED_GCP_"
              "PACKAGES + requirements-control.txt) or graceful-"
              "degradation (add to OPTIONAL_GCP_PACKAGES)."
        )

    def test_required_imports_are_actually_pinned(self):
        """Every import classified as production-required must be
        pinned in ``requirements-control.txt`` — defends against the
        case where someone bumps the allow-list but forgets the file."""
        missing: dict[str, list[str]] = {}
        for sub, sites in self.imports.items():
            pip = GCP_SUBPACKAGE_TO_PIP.get(sub)
            if pip is None or pip not in PRODUCTION_REQUIRED_GCP_PACKAGES:
                continue
            if not _is_pinned(pip, self.requirements):
                missing[pip] = sites
        self.assertEqual(
            missing, {},
            "Imports require these packages but they're missing from "
            "requirements-control.txt:\n"
            + "\n".join(
                f"  {pip} (needed by: {', '.join(sites[:3])})"
                for pip, sites in missing.items()
            )
        )

    def test_secretmanager_is_imported_somewhere(self):
        """Sanity-pin: this is the package whose absence would silently
        break YouTube token rotation in the cloud — make sure the
        production-required set still reflects a real import. If the
        secretmanager call site disappears, this assertion fails and
        the operator is forced to revisit the allow-list."""
        self.assertIn(
            "secretmanager", self.imports,
            "No google.cloud.secretmanager import found — if the cloud-"
            "side token rotation path was removed, drop "
            "google-cloud-secret-manager from PRODUCTION_REQUIRED_GCP_"
            "PACKAGES too (and from requirements-control.txt)."
        )

    def test_run_v2_is_imported_somewhere(self):
        """Same sanity pin for the dispatcher SDK."""
        self.assertIn(
            "run_v2", self.imports,
            "No google.cloud.run_v2 import found — if cloud_run.py "
            "was removed or rewritten to use a different dispatch "
            "mechanism, drop google-cloud-run from the required set."
        )


class CloudRunDispatchEnvironmentTest(unittest.TestCase):
    """Behavioural canary — when the cloud image is running (``K_SERVICE``
    env set, indicating Cloud Run), ``cloud_run._sdk_available()`` MUST
    return True. Otherwise a silent CLI fallback inside the container
    fails with "no active account selected" — exactly the 2026-05-11
    regression.

    This test is informational locally (we don't usually have
    ``K_SERVICE`` set on the laptop) but PROVES the contract holds
    once we add a Cloud-Run-side smoke test that runs the suite at
    container build time. See ``cloud/web-server/Dockerfile`` for the
    in-image pytest invocation we want to wire.
    """

    def test_sdk_available_when_pinned(self):
        """If ``google-cloud-run`` is pinned, the SDK must be importable
        in the test environment that mirrors ``requirements-control.txt``.
        Surfaces a venv drift before the cloud build does.

        Uses ``importlib.util.find_spec`` rather than a real import so
        we don't pollute ``sys.modules`` for downstream tests that
        ``patch.dict(sys.modules, ...)`` to inject fakes (e.g.
        ``tests/test_upload_youtube.py::test_secret_mount_path_…``).
        """
        text = _requirements_text()
        if not _is_pinned("google-cloud-run", text):
            self.skipTest(
                "google-cloud-run not pinned — covered by "
                "ProductionRequiredGcpPackagesTest"
            )
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.find_spec("google.cloud.run_v2")
        self.assertIsNotNone(
            spec,
            "google-cloud-run is pinned in requirements-control.txt "
            "but not installed in this venv. Run: "
            "pip install -r requirements-control.txt"
        )

    def test_secretmanager_available_when_pinned(self):
        """Same drift check for the YouTube-token rotation client.
        Uses ``find_spec`` for the same reason — see test above."""
        text = _requirements_text()
        if not _is_pinned("google-cloud-secret-manager", text):
            self.skipTest("google-cloud-secret-manager not pinned")
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.find_spec("google.cloud.secretmanager")
        self.assertIsNotNone(
            spec,
            "google-cloud-secret-manager is pinned but not installed "
            "in this venv. Run: pip install -r requirements-control.txt"
        )


if __name__ == "__main__":
    unittest.main()
