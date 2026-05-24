"""F2 counter-test (catalogued in /ai/known-fragility.md) — every
``spec.extra`` key referenced by production code MUST be declared in
``pipeline.render.spec.RenderSpecExtras``.

The bug shape: ``RenderSpec.extra`` was an untyped ``dict[str, Any]``.
A typo in ``spec_enrich.py`` (e.g. ``caracter_description`` instead of
``character_description``) silently produced empty character lock —
the render kept going, the mp4 shipped with protagonist drift across
beats, and nothing in the logs said why.

This test scans every Python file under ``pipeline/``, ``cloud/``, and
``control/`` for ``spec.extra.get("KEY")`` and ``spec.extra["KEY"]``
patterns, collects every distinct ``KEY``, and asserts each is in the
``RenderSpecExtras`` TypedDict's optional-key set. A typo on the
production side becomes a test failure on your laptop — not a silently
broken render six hours later.

Tests/fixtures are intentionally excluded: ``_smoke_test_passthrough``
and similar test-only sentinels do not belong in the production
TypedDict.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.render.spec import RenderSpecExtras


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("pipeline", "cloud", "control")

# Skip files that are the registry itself or pure-docstring modules — they
# contain example strings that look like real accesses to the regex but
# aren't actual consumers. Paths are relative to the repo root.
_SCAN_SKIP_FILES = {
    "pipeline/render/spec.py",  # the registry's own file; docstring examples
}

# Match both spec.extra.get("KEY", default) and spec.extra["KEY"].
_GET_PATTERN = re.compile(r"""spec\.extra\.get\(\s*['"]([A-Za-z_][\w]*)['"]""")
_INDEX_PATTERN = re.compile(r"""spec\.extra\[\s*['"]([A-Za-z_][\w]*)['"]\s*\]""")


def _enumerate_production_keys() -> dict[str, list[str]]:
    """Return ``{key: [site, ...]}`` for every key referenced in
    production code. Sites are formatted ``path:line_no`` (relative to
    repo root) so a failure message can point at the offending line.

    Skips lines whose first non-whitespace character is ``#`` so that
    inline-comment examples (``# spec.extra["foo"] is ...``) don't trip
    the gate. Triple-string docstring examples are still a small risk —
    the registry file is excluded entirely via ``_SCAN_SKIP_FILES``."""
    keys: dict[str, list[str]] = {}
    for d in _SCAN_DIRS:
        base = _REPO_ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            try:
                text = p.read_text()
            except OSError:
                continue
            rel = p.relative_to(_REPO_ROOT)
            if str(rel) in _SCAN_SKIP_FILES:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for pat in (_GET_PATTERN, _INDEX_PATTERN):
                    for m in pat.finditer(line):
                        keys.setdefault(m.group(1), []).append(f"{rel}:{line_no}")
    return keys


class SpecExtraTypedKeysTest(unittest.TestCase):
    """Every key referenced via ``spec.extra.get/[]`` in production
    code must be declared in ``RenderSpecExtras``. Catches typos at
    test time (F2 in /ai/known-fragility.md)."""

    def test_every_production_key_is_declared_in_typed_dict(self):
        declared = set(RenderSpecExtras.__optional_keys__) | set(
            RenderSpecExtras.__required_keys__
        )
        used = _enumerate_production_keys()
        missing = {k: sites for k, sites in used.items() if k not in declared}

        if missing:
            lines = [
                "spec.extra key(s) referenced in production code but NOT "
                "declared in RenderSpecExtras (pipeline/render/spec.py).",
                "",
                "Add each key + a one-line docstring to RenderSpecExtras OR "
                "fix the typo at the listed site. F2 in "
                "/ai/known-fragility.md is the bug class this guards.",
                "",
            ]
            for k in sorted(missing):
                lines.append(f"  {k!r}")
                for site in missing[k]:
                    lines.append(f"      {site}")
            self.fail("\n".join(lines))

    def test_typed_dict_is_non_empty(self):
        """Guard against accidentally emptying the registry — if every
        key is removed, the first test trivially passes. Pin a floor
        that approximates the 2026-05-24 audit size."""
        declared = set(RenderSpecExtras.__optional_keys__) | set(
            RenderSpecExtras.__required_keys__
        )
        # At F2-fix time there are 24 distinct production keys. Floor of
        # 20 absorbs a few legitimate removals before requiring a
        # deliberate update to this test.
        self.assertGreaterEqual(
            len(declared), 20,
            f"RenderSpecExtras shrunk below the 20-key floor "
            f"(found {len(declared)}). If keys were intentionally "
            f"removed, lower the floor; otherwise the registry was "
            f"truncated by accident.",
        )


class SpecExtraTypedDictShapeTest(unittest.TestCase):
    """Defensive: pin that RenderSpecExtras is total=False (every key
    optional). A future refactor to total=True would force every render
    to populate every key, which is wrong."""

    def test_render_spec_extras_is_total_false(self):
        # TypedDict with total=False marks every key as NotRequired;
        # __required_keys__ is therefore empty.
        self.assertEqual(
            set(RenderSpecExtras.__required_keys__),
            set(),
            "RenderSpecExtras must be total=False; required keys would "
            "force every render to populate every extra key.",
        )


if __name__ == "__main__":
    unittest.main()
