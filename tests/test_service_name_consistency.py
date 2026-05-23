"""Regression-pin: stop reintroducing the wrong Cloud Run service names.

Hit twice in one session: `cloud/render-worker-v2/deploy.sh` AND
`cloud/web-server/deploy.sh` BOTH referenced `ytfactory-tts-chatterbox`
when the actual live service is named `tts-chatterbox` (audit D3.22 —
deploys default to the dir name without the `ytfactory-` prefix). Each
mismatch wedged a deploy with `_resolve_url` returning empty + the
post-resolve guard exit-1'ing. Plus `pipeline/cloud/services.py:76`
held the wrong catalog name for weeks.

The test below makes the catalog the single source of truth and
asserts no production deploy.sh / production Python file references
a Cloud Run service name that doesn't exist in the catalog.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from pipeline.cloud.services import _SERVICES

REPO = Path(__file__).resolve().parent.parent

# Files we care about: every deploy.sh + every Python file under
# pipeline/ + control/ + cloud/. Skip tests/ (they fake URLs) and
# docs/ + ai/ (free-form prose).
_PROD_PATHS = ["cloud", "pipeline", "control"]

# Names that USED to be wrong and must never come back. Add to this
# list whenever we kill another naming drift.
_FORBIDDEN_NAMES = {
    # The lone GPU service without the `ytfactory-` prefix; "audit
    # D3.22" if you want the why. The canonical name is `tts-chatterbox`.
    "ytfactory-tts-chatterbox",
}


def _gather_files() -> list[Path]:
    out: list[Path] = []
    for root in _PROD_PATHS:
        d = REPO / root
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in {".py", ".sh", ".yaml", ".yml"}:
                continue
            if "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


class ServiceNameConsistencyTest(unittest.TestCase):
    def test_catalog_names_are_unique(self) -> None:
        names = [s.name for s in _SERVICES]
        self.assertEqual(len(names), len(set(names)),
                         f"duplicate service names in catalog: {names}")

    def test_chatterbox_canonical_is_tts_chatterbox(self) -> None:
        # Pin the rename direction: the actual deployed Cloud Run
        # service is `tts-chatterbox`. Catalog must agree.
        chat = next((s for s in _SERVICES if s.short == "chatterbox"), None)
        self.assertIsNotNone(chat)
        assert chat is not None
        self.assertEqual(
            chat.name, "tts-chatterbox",
            f"chatterbox catalog entry has name={chat.name!r}; the live "
            f"Cloud Run service is `tts-chatterbox` (see "
            f"cloud/tts-chatterbox/deploy.sh:16, audit D3.22). Don't "
            f"normalize to `ytfactory-tts-chatterbox` without also "
            f"renaming the live service — `gcloud run services "
            f"describe <name>` is keyed on this string."
        )

    def test_no_forbidden_names_in_production_code(self) -> None:
        # Sweep all production .py / .sh / .yaml files for any name
        # that the catalog doesn't know about. Comments are allowed
        # (a doc may reference a defunct name historically), but
        # raw-string occurrences in code lines are not.
        offenders: list[tuple[str, int, str, str]] = []
        for path in _gather_files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for lineno, line in enumerate(lines, start=1):
                # Skip lines that look like a comment in any language
                # we touch (#, //, --). Comments may carry historical
                # names; behaviour-bearing lines must not.
                stripped = line.lstrip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                for bad in _FORBIDDEN_NAMES:
                    if bad in line:
                        offenders.append(
                            (str(path.relative_to(REPO)), lineno, bad, line.strip()[:120]),
                        )
        if offenders:
            msg = "Forbidden service names found in production code:\n"
            for p, ln, name, snippet in offenders:
                msg += f"  {p}:{ln}: {name!r} -> {snippet}\n"
            msg += (
                "\nThese names refer to Cloud Run services that don't "
                "exist. Use the catalog name from pipeline.cloud.services "
                "instead (e.g. `tts-chatterbox`)."
            )
            self.fail(msg)


if __name__ == "__main__":
    unittest.main()
