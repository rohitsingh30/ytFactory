"""Tests for pipeline.niches — NICHE_CHANNEL canonical routing.

Pinning the boot-time validator added 2026-05-14 (Tier 0 batch D).
Pre-fix: every NICHE_CHANNEL entry pointed at a stale variant YAML
location (`<channel>/variants/<v>.yaml` — the legacy layout removed
in the 2026-05-10 nuclear cleanup), so the endswith() check in
pipeline/paths.py:RenderPaths.from_yaml_path() silently missed every
niche route and degraded to flat layout. Catalogue: YAML-04 through
YAML-14 + NCH-09/10.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestNichePathsExist(unittest.TestCase):
    """Every variant YAML referenced in NICHE_CHANNEL must exist."""

    def test_every_niche_yaml_resolves_to_existing_file(self):
        from pipeline import niches
        missing: list[str] = []
        for niche_key, (chan_dir, chan_yaml) in niches.NICHE_CHANNEL.items():
            full = REPO_ROOT / chan_yaml
            if not full.exists():
                missing.append(f"{niche_key}: {chan_yaml} -> {full}")
        self.assertEqual(missing, [],
                         f"NICHE_CHANNEL has stale paths: {missing}")

    def test_validator_raises_when_path_missing(self):
        """Boot-time validator must fail-fast on a stale entry."""
        # Re-import niches with a fake stale entry to exercise the
        # validator. Use importlib.reload to get a fresh module.
        import importlib
        from pipeline import niches as niches_mod
        # Save original
        orig = dict(niches_mod.NICHE_CHANNEL)
        try:
            niches_mod.NICHE_CHANNEL["bad-niche"] = (
                "x/y", "pipeline/variants/no_such/path.yaml",
            )
            with self.assertRaises(FileNotFoundError) as ctx:
                niches_mod._validate_niche_paths_exist()
            self.assertIn("bad-niche", str(ctx.exception))
            self.assertIn("no_such/path.yaml", str(ctx.exception))
        finally:
            niches_mod.NICHE_CHANNEL.clear()
            niches_mod.NICHE_CHANNEL.update(orig)

    def test_validator_skipped_when_env_set(self):
        """Tests / build tooling can opt out via env var."""
        import importlib
        from pipeline import niches as niches_mod
        os.environ["YTFACTORY_NICHE_PATHS_NO_VALIDATE"] = "1"
        try:
            orig = dict(niches_mod.NICHE_CHANNEL)
            try:
                niches_mod.NICHE_CHANNEL["bad-niche"] = (
                    "x/y", "pipeline/variants/no_such/path.yaml",
                )
                # Should NOT raise.
                niches_mod._validate_niche_paths_exist()
            finally:
                niches_mod.NICHE_CHANNEL.clear()
                niches_mod.NICHE_CHANNEL.update(orig)
        finally:
            os.environ.pop("YTFACTORY_NICHE_PATHS_NO_VALIDATE", None)

    def test_paths_use_pipeline_variants_layout(self):
        """All variant YAML paths must use the canonical layout
        ``pipeline/variants/<channel>/<variant>.yaml``. Pre-fix paths
        had ``<channel>/variants/...`` order which the path-resolver's
        endswith() check would silently miss."""
        from pipeline import niches
        for niche_key, (_, chan_yaml) in niches.NICHE_CHANNEL.items():
            self.assertTrue(
                chan_yaml.startswith("pipeline/variants/"),
                f"{niche_key} variant path {chan_yaml!r} doesn't start with "
                f"pipeline/variants/ — endswith() match in paths.py will "
                f"silently fail and route to flat layout"
            )


class TestNicheChannelDirsMatchRealChannels(unittest.TestCase):
    """Channel slug in NICHE_CHANNEL[niche][0] must match a real channel."""

    def test_sports_ranked_uses_sportsrecapped_not_sportstoriesanimated(self):
        """YAML-14: pre-fix sports_ranked pointed at
        sportstoriesanimated/ which never existed. Must be sportsrecapped."""
        from pipeline import niches
        chan_dir, _ = niches.NICHE_CHANNEL["sports_ranked"]
        self.assertTrue(
            chan_dir.startswith("sportsrecapped/"),
            f"sports_ranked channel_dir is {chan_dir!r}; expected sportsrecapped/...",
        )


if __name__ == "__main__":
    unittest.main()
