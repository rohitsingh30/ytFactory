"""Tests for control/com.ytfactory.*.plist launchd files.

Catches the class-of-bug surfaced by audit T1.2: a script path
inside a plist's ``ProgramArguments`` going stale when its file
moves on disk. launchd silently no-ops a missing-script invocation
on the next scheduled tick, so the regression is invisible until
someone notices a downstream artifact stopped landing.

Strategy: parse every plist into a plistlib dict, walk
``ProgramArguments`` for the bash ``-c`` payload, extract every
relative ``scripts/...`` reference, and assert each exists.
"""
from __future__ import annotations

import plistlib
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLIST_DIR = REPO_ROOT / "control"

# A relative path token that looks like a script invocation.
# Matches scripts/foo.py, scripts/ops/foo.py, scripts/foo.sh, etc.
_SCRIPT_REF = re.compile(r"\b(scripts/[\w./-]+\.(?:py|sh))\b")


def _all_plists() -> list[Path]:
    return sorted(PLIST_DIR.glob("com.ytfactory.*.plist"))


def _bash_payloads(pl: dict) -> list[str]:
    """Extract all bash ``-c`` payloads from a plist's ProgramArguments."""
    pa = pl.get("ProgramArguments") or []
    payloads: list[str] = []
    if isinstance(pa, list) and len(pa) >= 3 and pa[0].endswith("/bash") and pa[1] == "-c":
        payloads.append(pa[2])
    return payloads


class TestPlistScriptReferences(unittest.TestCase):
    """Every script referenced from a plist must exist on disk."""

    def test_every_plist_loads(self):
        plists = _all_plists()
        self.assertGreaterEqual(
            len(plists), 1, "expected at least one ytFactory plist in control/"
        )
        for path in plists:
            with self.subTest(plist=path.name):
                with path.open("rb") as f:
                    data = plistlib.load(f)
                self.assertIn("Label", data, f"{path.name} missing Label key")

    def test_every_referenced_script_exists(self):
        # T1.2 regression — the pre-fix state had upload-next.plist
        # invoking ``scripts/upload_next.py`` which had moved to
        # ``scripts/ops/upload_next.py``. launchd silently no-op'd
        # for as long as the drift existed.
        for path in _all_plists():
            with path.open("rb") as f:
                data = plistlib.load(f)
            for payload in _bash_payloads(data):
                refs = _SCRIPT_REF.findall(payload)
                # Also catch absolute paths like /Users/rohit/ytFactory/scripts/x.py.
                for token in payload.split():
                    if "/scripts/" in token:
                        # Strip trailing chars like quotes, parens, semicolons.
                        cleaned = token.strip("'\";")
                        # Reduce to the scripts/... tail if absolute.
                        idx = cleaned.find("scripts/")
                        if idx > 0:
                            cleaned = cleaned[idx:]
                        m = _SCRIPT_REF.search(cleaned)
                        if m:
                            refs.append(m.group(1))
                for rel in set(refs):
                    with self.subTest(plist=path.name, script=rel):
                        self.assertTrue(
                            (REPO_ROOT / rel).exists(),
                            f"{path.name} references {rel} which does not exist on disk",
                        )


if __name__ == "__main__":
    unittest.main()
