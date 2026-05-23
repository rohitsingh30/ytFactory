"""Regression-pin: `image_provider` has a single source of truth.

Hit today: `pipeline/variants/mystoriesanimated/tifu.yaml:17` had
`image_provider: mflux` (a retired laptop provider). Every beat failed
image-gen with `unknown image_provider: 'mflux'`; 24/24 → 0 images →
render killed at compose.

Root cause: 20 different YAMLs each carried their own `image_provider:`
line and ONE of them drifted. The structural fix removed all those
lines — there is now ONE canonical value:
`pipeline.images.images.CANONICAL_IMAGE_PROVIDER`. Every consumer
defaults to it via Python. YAMLs no longer set it at all.

This test pins that invariant:
1. The constant exists and equals "cloudrun_z_image_turbo".
2. No production YAML sets `image_provider:` (drift surface is gone).
3. Code call sites that read `image_provider` from spec.extra must
   default to the constant, not hardcode the literal.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _yaml_roots() -> list[Path]:
    return [REPO / "pipeline" / "channels", REPO / "pipeline" / "variants"]


_PROVIDER_LINE = re.compile(r"^\s*image_provider\s*:\s*([^\s#]+)")


class ImageProviderCanonicalTest(unittest.TestCase):
    def test_canonical_constant_exists_and_is_expected(self) -> None:
        from pipeline.images.images import CANONICAL_IMAGE_PROVIDER  # noqa: PLC0415
        self.assertEqual(
            CANONICAL_IMAGE_PROVIDER, "cloudrun_z_image_turbo",
            "CANONICAL_IMAGE_PROVIDER is the single source of truth; "
            "if you're swapping to a new image model, update the dispatcher "
            "in pipeline/images/images.py FIRST, then bump this test.",
        )

    def test_no_yaml_sets_image_provider(self) -> None:
        offenders: list[tuple[str, int, str]] = []
        for root in _yaml_roots():
            if not root.exists():
                continue
            for path in root.rglob("*.yaml"):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except Exception:
                    continue
                for lineno, line in enumerate(lines, start=1):
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    m = _PROVIDER_LINE.match(line)
                    if m:
                        offenders.append(
                            (str(path.relative_to(REPO)), lineno, m.group(1)),
                        )
        if offenders:
            msg = (
                "YAMLs are not allowed to set `image_provider`. The "
                "canonical value lives in pipeline.images.images."
                "CANONICAL_IMAGE_PROVIDER; consumers default to it.\n"
                "Offending lines:\n"
            )
            for p, ln, v in offenders:
                msg += f"  {p}:{ln}: image_provider: {v}\n"
            msg += (
                "\nDelete the line; the rendering pipeline will use the "
                "canonical constant. If you NEED to override "
                "per-channel (e.g. introducing a second provider), "
                "first add the new value to "
                "_PROVIDER_CAPABILITIES in pipeline/images/images.py "
                "and update this test."
            )
            self.fail(msg)

    def test_code_callsites_do_not_hardcode_provider_literal(self) -> None:
        # Any .py file under pipeline/render/ or pipeline/images/ that
        # reads `image_provider` from spec/extra/dict MUST default to
        # the constant, not the literal "cloudrun_z_image_turbo". The
        # niche_schema.py preset dicts are reference data (not runtime
        # defaults); exempt them explicitly.
        scan_roots = [REPO / "pipeline" / "render", REPO / "pipeline" / "images"]
        offenders: list[tuple[str, int, str]] = []
        pattern = re.compile(
            r'image_provider["\']\s*,\s*["\']cloudrun_z_image_turbo["\']'
        )
        for root in scan_roots:
            if not root.exists():
                continue
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except Exception:
                    continue
                for lineno, line in enumerate(lines, start=1):
                    if pattern.search(line):
                        offenders.append(
                            (str(path.relative_to(REPO)), lineno, line.strip()[:120]),
                        )
        if offenders:
            msg = (
                "Code call sites hardcode the literal "
                "'cloudrun_z_image_turbo' instead of importing "
                "CANONICAL_IMAGE_PROVIDER:\n"
            )
            for p, ln, snippet in offenders:
                msg += f"  {p}:{ln}: {snippet}\n"
            msg += (
                "\nReplace with:\n"
                "  from pipeline.images.images import CANONICAL_IMAGE_PROVIDER\n"
                "  provider = spec.extra.get('image_provider') or CANONICAL_IMAGE_PROVIDER\n"
            )
            self.fail(msg)


if __name__ == "__main__":
    unittest.main()
