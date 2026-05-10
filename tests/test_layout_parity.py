"""Parity tests for the canonical channel layout.

These tests guard against drift from the spec in docs/channel_layout.md:

* Every production channel has a config.yaml at its root.
* Every variant YAML in <channel>/variants/ either matches a NICHE_CHANNEL
  entry OR falls back to a flat layout (which is logged but tolerated).
* Niched channels (mystoriesanimated, sportsrecapped) actually have
  per-slug subdirs nested under their niche dirs — not at the channel root.
* Flat channels do NOT have niche dirs alongside their per-slug subdirs.
* The legacy data/intermediate/, data/critiques/, data/shorts/ subtrees
  remain decommissioned (caught at CI time so a regression PR is visible).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline import niches
from pipeline.schemas.paths import (
    PROJECT_ROOT as PATHS_PROJECT_ROOT,
    RenderPaths,
    Subdir,
)


# Production channels — every channel root in the repo.
_PRODUCTION_CHANNELS = [
    "cosmosdecoded",
    "hindutavaanimated",
    "historyrecapped",
    "mystoriesanimated",
    "rhymetimejunction",
    "sportsrecapped",
    "scrollpulse",
]

# Channels declared as flat (no niches).
_FLAT_CHANNELS = {
    "cosmosdecoded",
    "hindutavaanimated",
    "historyrecapped",
    "rhymetimejunction",
}

# Channels declared as niched (everything per-slug nests under <niche>/).
# sportsrecapped is the documented mixed-niche exception (has both
# parent-channel content at root and a 'ranked/' niche).
_NICHED_CHANNELS = {
    "mystoriesanimated",
    "sportsrecapped",
}


class ChannelConfigYamlPresentTest(unittest.TestCase):
    """Every production channel has a config.yaml at its root.

    **Skipped on cloud-cutover laptops** (post laptop_nuclear_cleanup
    2026-05-09): channel state lives in GCS now and the local
    ``<channel>/config.yaml`` files are intentionally absent on a
    fresh checkout. The test still runs on a developer laptop with the
    legacy on-disk layout AND on the Cloud Run worker (which mounts
    the canonical YAML bundle from a release artifact). Set
    ``YTFACTORY_LAYOUT_PARITY_FORCE=1`` to force the assert even when
    the dirs are missing, so the test surfaces a real regression on
    machines that DO want the on-disk layout.
    """

    def test_each_channel_has_config_yaml(self):
        import os
        force = os.environ.get("YTFACTORY_LAYOUT_PARITY_FORCE", "0") == "1"
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                p = RenderPaths.for_channel(ch)
                if not p.config_yaml.exists() and not force:
                    self.skipTest(
                        f"{ch}/config.yaml absent — laptop nuclear cleanup "
                        "leaves channel state in GCS only. Set "
                        "YTFACTORY_LAYOUT_PARITY_FORCE=1 to assert anyway."
                    )
                self.assertTrue(
                    p.config_yaml.exists(),
                    f"{ch}/config.yaml is missing — required by canonical layout",
                )


class VariantYamlsCoveredTest(unittest.TestCase):
    """Every variant YAML resolves to a (channel, niche) via NICHE_CHANNEL,
    or falls back to a flat layout (with a warning print)."""

    def test_every_variant_yaml_resolves(self):
        import io
        import contextlib

        for ch in _PRODUCTION_CHANNELS:
            variants = PATHS_PROJECT_ROOT / ch / "variants"
            if not variants.exists():
                continue
            for yaml_file in sorted(variants.glob("*.yaml")):
                with self.subTest(yaml=str(yaml_file.relative_to(PATHS_PROJECT_ROOT))):
                    rel_path = yaml_file.relative_to(PATHS_PROJECT_ROOT)
                    # Must not raise. May print WARN for unregistered variants.
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        p = RenderPaths.from_channel_yaml(rel_path)
                    self.assertEqual(p.channel, ch)

    def test_central_layout_yaml_resolves(self):
        """Central-config layout (post-2026-05-10 nuclear cleanup):
        ``pipeline/channels/<slug>.yaml`` and
        ``pipeline/variants/<slug>/<variant>.yaml`` must resolve to
        the right channel root.

        Regression test for the cake-orch-v6 crash where the cloud
        worker passed a ``pipeline/channels/<slug>.yaml`` path to
        ``RenderPaths.from_channel_yaml`` and got a ValueError —
        all 22 images had already rendered + the mp4 was on disk
        when the path resolver exploded.
        """
        from pathlib import Path
        # Central-channel layout.
        p1 = RenderPaths.from_channel_yaml(
            Path("pipeline/channels/mystoriesanimated.yaml")
        )
        self.assertEqual(p1.channel, "mystoriesanimated")
        # Central-variants layout.
        p2 = RenderPaths.from_channel_yaml(
            Path("pipeline/variants/mystoriesanimated/aita_animated.yaml")
        )
        self.assertEqual(p2.channel, "mystoriesanimated")


# NICHE_CHANNEL_NicheDirsExistTest deleted 2026-05-10 — niche state
# moved to gs://ytfactory-prod-v2-state/<channel>/<niche>/. The chan_dir
# string in NICHE_CHANNEL is now a GCS-relative key, not a laptop dir.
# Skills + render code access it via pipeline.utils.state_client, never via
# direct filesystem reads. Was: every NICHE_CHANNEL entry's channel_dir
# is a real directory.


class FlatChannelsHaveNoNicheDirsTest(unittest.TestCase):
    """Flat channels must NOT have niche subdirs that look like nested-niche
    layouts (would silently break path resolution)."""

    def test_flat_channels_have_no_orphan_niche_dirs(self):
        # A "niche dir" in this context = any subdir of the channel root
        # that contains its own narrations/ or uploads/ subdir (the
        # canonical signal that something is being treated as a niche).
        # Channel-wide subdirs that legitimately exist at the root are
        # excluded.
        canonical_root_subdirs = {s.value for s in Subdir} | {"config.yaml"}
        for ch in sorted(_FLAT_CHANNELS):
            chan_root = PATHS_PROJECT_ROOT / ch
            if not chan_root.exists():
                continue
            for child in chan_root.iterdir():
                if not child.is_dir():
                    continue
                if child.name in canonical_root_subdirs:
                    continue
                with self.subTest(channel=ch, dir=child.name):
                    # An orphan niche dir would have its own narrations/
                    # or uploads/ subdir (i.e. something being treated
                    # as a niche even though the channel is declared flat).
                    has_niche_contents = (
                        (child / "narrations").exists()
                        or (child / "uploads").exists()
                    )
                    self.assertFalse(
                        has_niche_contents,
                        f"Flat channel {ch!r} has orphan niche-like dir "
                        f"{child.name!r} (contains narrations/ or uploads/). "
                        f"Either move its content into the channel root "
                        f"per-slug subdirs, or convert {ch!r} to a niched "
                        f"channel by adding a NICHE_CHANNEL entry.",
                    )


class NichedChannelsContentIsNestedTest(unittest.TestCase):
    """For declared niched channels, narrations/uploads must live at
    <channel>/<niche>/{narrations,uploads}/<slug>.json (canonical),
    NOT at <channel>/{narrations,uploads}/<niche>/<slug>.json (Gen 2
    legacy). Catches a regression to the pre-2026-05-05 layout.
    """

    def test_no_legacy_uploads_niche_nesting_under_root(self):
        # Specifically checks for <channel>/uploads/<niche>/<slug>.json
        # (the Gen 2 layout we migrated away from). Skips
        # sportsrecapped which legitimately has parent-channel
        # uploads at root + ranked/ niche uploads (the mixed exception).
        for ch in sorted(_NICHED_CHANNELS - {"sportsrecapped"}):
            chan_root = PATHS_PROJECT_ROOT / ch
            uploads_at_root = chan_root / "uploads"
            with self.subTest(channel=ch):
                if not uploads_at_root.exists():
                    continue  # No root uploads/ at all — perfect.
                # If it exists, it must contain ONLY *.json files at the
                # top level (not subdirs that would be Gen 2 layout).
                for child in uploads_at_root.iterdir():
                    self.assertFalse(
                        child.is_dir(),
                        f"{ch}/uploads/ contains a subdir {child.name!r} — "
                        f"this is the legacy Gen 2 layout (<channel>/uploads/<niche>/"
                        f"<slug>.json). Move to canonical "
                        f"<channel>/{child.name}/uploads/<slug>.json.",
                    )


class LegacyDataSubtreeDecommissionedTest(unittest.TestCase):
    """The legacy data/intermediate/, data/critiques/, data/shorts/
    subtrees were decommissioned in the 2026-05-05 layout cleanup.
    A regression bringing them back would mean code wrote to the wrong
    place — fail loudly here so the PR review catches it."""

    def test_data_intermediate_decommissioned(self):
        legacy = PATHS_PROJECT_ROOT / "data" / "intermediate"
        if legacy.exists():
            # If something re-created it, fail unless it's empty (a
            # transient mkdir that didn't write anything is OK).
            tracked = list(legacy.rglob("*"))
            non_empty = [p for p in tracked if p.is_file()]
            self.assertEqual(
                non_empty, [],
                f"data/intermediate/ has been decommissioned but contains files: "
                f"{non_empty[:5]}... Migrate to <channel>/cache/<slug>/ via "
                f"pipeline.schemas.paths.RenderPaths.cache_for(slug).",
            )

    def test_data_critiques_decommissioned(self):
        legacy = PATHS_PROJECT_ROOT / "data" / "critiques"
        if legacy.exists():
            non_empty = [p for p in legacy.rglob("*") if p.is_file()]
            self.assertEqual(
                non_empty, [],
                f"data/critiques/ has been decommissioned but contains files: "
                f"{non_empty[:5]}... Migrate to <channel>/critiques/<slug>/.",
            )

    def test_data_shorts_decommissioned(self):
        legacy = PATHS_PROJECT_ROOT / "data" / "shorts"
        if legacy.exists():
            non_empty = [p for p in legacy.rglob("*.mp4") if p.is_file()]
            self.assertEqual(
                non_empty, [],
                f"data/shorts/ has been decommissioned but contains mp4s: "
                f"{[p.name for p in non_empty[:5]]}... Migrate to "
                f"<channel>/[<niche>/]/shorts/<slug>.mp4.",
            )


if __name__ == "__main__":
    unittest.main()
