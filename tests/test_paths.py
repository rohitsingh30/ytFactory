"""Tests for pipeline.paths — the canonical layout module.

What's pinned here:
* Niched channels nest per-slug subdirs under ``<niche>/`` (mystoriesanimated,
  sportstoriesanimated/ranked) — but channel-wide subdirs stay flat.
* Flat channels have no niche segment anywhere.
* :meth:`from_channel_yaml` resolves variants via NICHE_CHANNEL with
  graceful fallback for unregistered variants.
* Slug-shaped helpers compose path correctly.
* :meth:`ensure_dirs` actually creates dirs.
* Every channel root in the repo is reachable via :meth:`for_channel`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.paths import (
    DATA_ROOT,
    MODEL_CACHE_DIR,
    PROJECT_ROOT as PATHS_PROJECT_ROOT,
    RESEARCH_DIR,
    RenderPaths,
    Subdir,
    TELEMETRY_DIR,
)


# All real channel roots in the repo. Canonical layout MUST handle each.
_PRODUCTION_CHANNELS = [
    "airecap",
    "cosmosdecoded",
    "hindutavaanimated",
    "historyrecapped",
    "mystoriesanimated",
    "rhymetimejunction",
    "sportstoriesanimated",
]


class FlatChannelLayoutTest(unittest.TestCase):
    """Flat (no-niche) channels: <channel>/<subdir>/<slug>.<ext>."""

    def test_flat_root_equals_channel_root(self):
        p = RenderPaths.for_channel("historyrecapped")
        self.assertEqual(p.root, p.channel_root)
        self.assertIsNone(p.niche)

    def test_per_slug_subdirs_at_channel_root(self):
        p = RenderPaths.for_channel("historyrecapped")
        self.assertEqual(p.narrations, p.project_root / "historyrecapped" / "narrations")
        self.assertEqual(p.shorts,     p.project_root / "historyrecapped" / "shorts")
        self.assertEqual(p.uploads,    p.project_root / "historyrecapped" / "uploads")
        self.assertEqual(p.cache,      p.project_root / "historyrecapped" / "cache")

    def test_channel_wide_subdirs_at_channel_root(self):
        p = RenderPaths.for_channel("airecap")
        self.assertEqual(p.config_yaml, p.project_root / "airecap" / "config.yaml")
        self.assertEqual(p.learnings,   p.project_root / "airecap" / "learnings")
        self.assertEqual(p.scripts,     p.project_root / "airecap" / "scripts")
        self.assertEqual(p.branding,    p.project_root / "airecap" / "branding")

    def test_channel_dir_string_is_just_channel(self):
        self.assertEqual(RenderPaths.for_channel("airecap").channel_dir, "airecap")


class NichedChannelLayoutTest(unittest.TestCase):
    """Niched channels: per-slug subdirs nest under <niche>/, channel-wide stay flat."""

    def test_niched_root_includes_niche_segment(self):
        p = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
        self.assertEqual(
            p.root,
            p.project_root / "mystoriesanimated" / "reddit_amitheasshole",
        )
        self.assertEqual(p.niche, "reddit_amitheasshole")

    def test_per_slug_subdirs_nest_under_niche(self):
        p = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
        base = p.project_root / "mystoriesanimated" / "reddit_amitheasshole"
        self.assertEqual(p.narrations, base / "narrations")
        self.assertEqual(p.uploads,    base / "uploads")
        self.assertEqual(p.shorts,     base / "shorts")
        self.assertEqual(p.cache,      base / "cache")
        self.assertEqual(p.cast,       base / "cast")

    def test_channel_wide_subdirs_DO_NOT_nest_under_niche(self):
        p = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
        # config.yaml, learnings/, scripts/, branding/ all stay at channel_root,
        # NOT under the niche dir. Multiple niches share these.
        self.assertEqual(p.config_yaml, p.project_root / "mystoriesanimated" / "config.yaml")
        self.assertEqual(p.learnings,   p.project_root / "mystoriesanimated" / "learnings")
        self.assertEqual(p.scripts,     p.project_root / "mystoriesanimated" / "scripts")
        self.assertEqual(p.branding,    p.project_root / "mystoriesanimated" / "branding")

    def test_footage_is_channel_wide(self):
        # Multiple niches share the same yt-dlp source mp4s; footage must
        # never nest under niche or every niche re-downloads the same clips.
        p = RenderPaths.for_channel("sportstoriesanimated", "ranked")
        self.assertEqual(
            p.footage_sources,
            p.project_root / "sportstoriesanimated" / "footage" / "sources",
        )
        self.assertEqual(
            p.footage_long_sources,
            p.project_root / "sportstoriesanimated" / "footage" / "long_sources",
        )

    def test_channel_dir_string_compounds_when_niched(self):
        p = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
        self.assertEqual(p.channel_dir, "mystoriesanimated/reddit_amitheasshole")


class SlugHelpersTest(unittest.TestCase):
    """Per-slug path builders compose subdirs + slug correctly."""

    def test_flat_channel_slug_paths(self):
        p = RenderPaths.for_channel("historyrecapped")
        slug = "battle-of-britain-few"
        root = p.project_root / "historyrecapped"
        self.assertEqual(p.narration_for(slug), root / "narrations" / f"{slug}.json")
        self.assertEqual(p.raw_for(slug),       root / "raw" / f"{slug}.json")
        self.assertEqual(p.upload_record_for(slug), root / "uploads" / f"{slug}.json")
        self.assertEqual(p.x_upload_record_for(slug), root / "uploads" / f"{slug}.x.json")
        self.assertEqual(p.short_for(slug),     root / "shorts" / f"{slug}.mp4")
        self.assertEqual(p.short_thumb_for(slug), root / "shorts" / f"{slug}.thumb.png")
        self.assertEqual(p.long_form_for(slug), root / "long_form" / f"{slug}.mp4")
        self.assertEqual(p.cache_for(slug),     root / "cache" / slug)
        self.assertEqual(p.scratch_for(slug),   root / "scratch" / slug)
        self.assertEqual(p.critiques_for(slug), root / "critiques" / slug)
        self.assertEqual(p.shotlist_for(slug),  root / "shotlist" / f"{slug}.json")
        self.assertEqual(p.cast_for(slug),      root / "cast" / f"{slug}.json")

    def test_niched_channel_slug_paths(self):
        p = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
        slug = "amitheasshole-aita-for-something"
        base = p.project_root / "mystoriesanimated" / "reddit_amitheasshole"
        self.assertEqual(p.narration_for(slug), base / "narrations" / f"{slug}.json")
        self.assertEqual(p.upload_record_for(slug), base / "uploads" / f"{slug}.json")
        self.assertEqual(p.short_for(slug),     base / "shorts" / f"{slug}.mp4")


class FromChannelDirTest(unittest.TestCase):
    """Parses a compound ``<channel>[/<niche>]`` string (legacy callers)."""

    def test_flat_channel_dir(self):
        p = RenderPaths.from_channel_dir("historyrecapped")
        self.assertEqual(p.channel, "historyrecapped")
        self.assertIsNone(p.niche)
        self.assertEqual(p.channel_dir, "historyrecapped")

    def test_compound_channel_dir(self):
        p = RenderPaths.from_channel_dir("mystoriesanimated/reddit_amitheasshole")
        self.assertEqual(p.channel, "mystoriesanimated")
        self.assertEqual(p.niche, "reddit_amitheasshole")
        self.assertEqual(p.channel_dir, "mystoriesanimated/reddit_amitheasshole")


class FromChannelYamlTest(unittest.TestCase):
    """Resolves (channel, niche) from a channel YAML path."""

    def test_flat_channel_yaml_resolves_to_no_niche(self):
        p = RenderPaths.from_channel_yaml(Path("historyrecapped/config.yaml"))
        self.assertEqual(p.channel, "historyrecapped")
        self.assertIsNone(p.niche)

    def test_variant_yaml_in_niche_channel_resolves_via_NICHE_CHANNEL(self):
        # aita_animated.yaml maps to niche "aita" → reddit_amitheasshole dir
        p = RenderPaths.from_channel_yaml(
            Path("mystoriesanimated/variants/aita_animated.yaml")
        )
        self.assertEqual(p.channel, "mystoriesanimated")
        self.assertEqual(p.niche, "reddit_amitheasshole")

    def test_sports_ranked_variant_resolves_to_ranked_niche(self):
        p = RenderPaths.from_channel_yaml(
            Path("sportstoriesanimated/variants/ranked.yaml")
        )
        self.assertEqual(p.channel, "sportstoriesanimated")
        self.assertEqual(p.niche, "ranked")

    def test_unregistered_variant_warns_and_falls_back_flat(self):
        # When a variant isn't in NICHE_CHANNEL, fall back to flat
        # (warn loudly so the operator adds the entry).
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p = RenderPaths.from_channel_yaml(
                Path("mystoriesanimated/variants/aita_text.yaml")
            )
        # aita_text isn't registered. Fallback to flat under mystoriesanimated.
        self.assertEqual(p.channel, "mystoriesanimated")
        self.assertIsNone(p.niche)
        self.assertIn("WARN", buf.getvalue())
        self.assertIn("aita_text.yaml", buf.getvalue())

    def test_unparseable_yaml_path_raises(self):
        with self.assertRaises(ValueError):
            RenderPaths.from_channel_yaml(Path("not/a/valid/layout.yaml"))


class ProductionChannelCoverageTest(unittest.TestCase):
    """Every actual channel in the repo must be reachable via for_channel
    AND its config.yaml must exist."""

    def test_every_production_channel_has_config_yaml_on_disk(self):
        for ch in _PRODUCTION_CHANNELS:
            with self.subTest(channel=ch):
                p = RenderPaths.for_channel(ch)
                self.assertTrue(
                    p.config_yaml.exists(),
                    f"channel {ch!r}: expected config.yaml at {p.config_yaml}",
                )

    def test_every_NICHE_CHANNEL_entry_resolves(self):
        """Every NICHE_CHANNEL entry must resolve, but YAML→niche lookup is
        ambiguous when multiple niches share a YAML (style overlays).

        Example: ``aita``, ``malicious``, ``prorevenge`` all map to
        ``aita_animated.yaml`` (same visual style, different source
        subreddits). ``from_channel_yaml(aita_animated.yaml)`` can only
        return ONE niche — by design, the first NICHE_CHANNEL match
        wins (``reddit_amitheasshole``). Operators that need a non-default
        niche dir for the same YAML must call ``RenderPaths.for_channel(
        channel, niche)`` directly. This test asserts the lookup
        terminates and the channel matches; we only check the niche when
        the YAML is unique to one entry.
        """
        from collections import Counter
        from pipeline import niches

        yaml_counts = Counter(y for _, y in niches.NICHE_CHANNEL.values())

        for niche_key, (chan_dir, chan_yaml) in niches.NICHE_CHANNEL.items():
            with self.subTest(niche=niche_key):
                p = RenderPaths.from_channel_yaml(chan_yaml)
                expected_channel = chan_dir.split("/", 1)[0]
                self.assertEqual(
                    p.channel, expected_channel,
                    f"{niche_key!r}: channel mismatch from yaml {chan_yaml}",
                )
                if yaml_counts[chan_yaml] == 1:
                    # Unique YAML: niche must match exactly.
                    self.assertEqual(p.channel_dir, chan_dir)
                # Else (shared YAML): ambiguous lookup; channel is enough.


class EnsureDirsTest(unittest.TestCase):
    """ensure_dirs actually mkdirs; properties are pure (no side effects)."""

    def test_property_access_does_NOT_create_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            p = RenderPaths.for_channel("mychannel", project_root=Path(td))
            _ = p.narrations  # touch the property
            _ = p.cache_for("some-slug")
            self.assertFalse(p.narrations.exists())
            self.assertFalse(p.cache.exists())

    def test_ensure_dirs_creates_named_subdirs(self):
        with tempfile.TemporaryDirectory() as td:
            p = RenderPaths.for_channel("mychannel", project_root=Path(td))
            p.ensure_dirs(Subdir.NARRATIONS, Subdir.UPLOADS, Subdir.CACHE)
            self.assertTrue(p.narrations.is_dir())
            self.assertTrue(p.uploads.is_dir())
            self.assertTrue(p.cache.is_dir())
            # NOT created (we didn't ask).
            self.assertFalse(p.shorts.exists())

    def test_ensure_dirs_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            p = RenderPaths.for_channel("mychannel", project_root=Path(td))
            p.ensure_dirs(Subdir.NARRATIONS)
            p.ensure_dirs(Subdir.NARRATIONS)  # second call must not raise
            self.assertTrue(p.narrations.is_dir())


class CrossChannelDataDirsTest(unittest.TestCase):
    """The few cross-channel state buckets that legitimately stay under data/."""

    def test_data_roots_are_under_project_root(self):
        self.assertEqual(DATA_ROOT,        PATHS_PROJECT_ROOT / "data")
        self.assertEqual(RESEARCH_DIR,     PATHS_PROJECT_ROOT / "data" / "research")
        self.assertEqual(TELEMETRY_DIR,    PATHS_PROJECT_ROOT / "data" / "telemetry")
        self.assertEqual(MODEL_CACHE_DIR,  PATHS_PROJECT_ROOT / "data" / "cache")


if __name__ == "__main__":
    unittest.main()
