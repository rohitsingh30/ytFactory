"""Tests for pipeline.channels — production + burner channel registry."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from pipeline import channels as ch_mod
from pipeline.channels import (
    BurnerAccount,
    Channel,
    _load_burners,
    _load_channels,
    all_burners,
    all_channels,
    all_niches,
    burner_slugs,
    channel_for_niche,
    channel_rotation,
    get_channel,
    is_known_channel,
    niche_channel_map,
    resolve_burner,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data, allow_unicode=True))


def _channels_yaml(entries: list[dict]) -> dict:
    return {"channels": entries}


def _burners_yaml(entries: list[dict]) -> dict:
    return {"burners": entries}


def _simple_channel(slug="testchan", channel_id="UC_test", rotation=True) -> dict:
    return {
        "slug": slug,
        "youtube_title": f"Test {slug}",
        "youtube_channel_id": channel_id,
        "in_rotation": rotation,
        "in_production": True,
        "niches": {},
    }


def _niched_channel(slug="nichedchan", channel_id="UC_niched") -> dict:
    return {
        "slug": slug,
        "youtube_title": "Niched",
        "youtube_channel_id": channel_id,
        "in_rotation": True,
        "in_production": True,
        "niches": {
            "my_niche": ["subdir", "variant.yaml"],
            "flat_niche": ["", "flat.yaml"],
        },
    }


# ── Channel model tests ───────────────────────────────────────────────────


class TestChannelModel(unittest.TestCase):
    def _make(self, slug="mychan", **kwargs) -> Channel:
        return Channel(slug=slug, **kwargs)

    def test_channel_yaml_path(self):
        c = self._make()
        self.assertIn("mychan", c.channel_yaml_path())

    def test_learnings_dir(self):
        c = self._make()
        self.assertIn("mychan", c.learnings_dir())

    def test_channel_variants_dir(self):
        c = self._make()
        self.assertIn("mychan", c.channel_variants_dir())

    def test_channel_scripts_dir(self):
        c = self._make()
        self.assertIn("mychan", c.channel_scripts_dir())

    def test_variant_yaml_path_missing_niche(self):
        c = self._make(niches={})
        self.assertIsNone(c.variant_yaml_path("no_niche"))

    def test_variant_yaml_path_with_niche(self):
        c = self._make(niches={"my_niche": ["subdir", "variant.yaml"]})
        result = c.variant_yaml_path("my_niche")
        self.assertIn("variant.yaml", result)  # type: ignore[operator]

    def test_state_dir_missing_niche(self):
        c = self._make(slug="chan", niches={})
        self.assertEqual(c.state_dir("no_niche"), "chan")

    def test_state_dir_with_subdir(self):
        c = self._make(slug="chan", niches={"my_niche": ["subdir", "x.yaml"]})
        self.assertEqual(c.state_dir("my_niche"), "chan/subdir")

    def test_state_dir_flat_niche_empty_subdir(self):
        c = self._make(slug="chan", niches={"flat_niche": ["", "flat.yaml"]})
        self.assertEqual(c.state_dir("flat_niche"), "chan")


# ── _load_channels tests ──────────────────────────────────────────────────


class TestLoadChannels(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._path = Path(self._td.name) / "channels.yaml"

    def tearDown(self):
        self._td.cleanup()

    def _load(self, data: dict) -> tuple:
        _write_yaml(self._path, data)
        return _load_channels(self._path)

    def test_loads_single_channel(self):
        result = self._load(_channels_yaml([_simple_channel()]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].slug, "testchan")

    def test_raises_on_missing_channels_key(self):
        _write_yaml(self._path, {"other": []})
        with self.assertRaises(ValueError):
            _load_channels(self._path)

    def test_raises_on_non_dict(self):
        self._path.write_text("[1, 2, 3]")
        with self.assertRaises(ValueError):
            _load_channels(self._path)

    def test_raises_on_duplicate_slug(self):
        data = _channels_yaml([_simple_channel("dup"), _simple_channel("dup", "UC_other")])
        _write_yaml(self._path, data)
        with self.assertRaises(ValueError):
            _load_channels(self._path)

    def test_raises_on_duplicate_channel_id(self):
        data = _channels_yaml([
            _simple_channel("chan1", "UC_SAME"),
            _simple_channel("chan2", "UC_SAME"),
        ])
        _write_yaml(self._path, data)
        with self.assertRaises(ValueError):
            _load_channels(self._path)

    def test_channel_without_id_ok(self):
        entry = _simple_channel()
        entry.pop("youtube_channel_id")
        result = self._load(_channels_yaml([entry]))
        self.assertIsNone(result[0].youtube_channel_id)


# ── Public API tests ──────────────────────────────────────────────────────


class TestPublicAPI(unittest.TestCase):
    def test_all_channels_returns_tuple(self):
        result = all_channels()
        self.assertIsInstance(result, tuple)
        self.assertGreater(len(result), 0)

    def test_channel_rotation_returns_slugs(self):
        rot = channel_rotation()
        self.assertIsInstance(rot, list)
        for slug in rot:
            self.assertIsInstance(slug, str)

    def test_get_channel_known(self):
        c = all_channels()[0]
        result = get_channel(c.slug)
        self.assertIsNotNone(result)
        self.assertEqual(result.slug, c.slug)  # type: ignore[union-attr]

    def test_get_channel_unknown_returns_none(self):
        self.assertIsNone(get_channel("no_such_channel_xyz"))

    def test_is_known_channel_true(self):
        c = all_channels()[0]
        self.assertTrue(is_known_channel(c.slug))

    def test_is_known_channel_false(self):
        self.assertFalse(is_known_channel("no_such_channel_xyz"))

    def test_all_niches_sorted(self):
        niches = all_niches()
        self.assertEqual(niches, sorted(niches))

    def test_niche_channel_map_returns_dict(self):
        ncm = niche_channel_map()
        self.assertIsInstance(ncm, dict)

    def test_channel_for_niche_returns_channel(self):
        # At least one channel must have niches in real channels.yaml.
        ncm = niche_channel_map()
        if ncm:
            niche_key = next(iter(ncm))
            c = channel_for_niche(niche_key)
            self.assertIsNotNone(c)

    def test_channel_for_niche_unknown_returns_none(self):
        self.assertIsNone(channel_for_niche("no_such_niche_xyz_abc"))


# ── BurnerAccount + _load_burners ─────────────────────────────────────────


class TestLoadBurners(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._path = Path(self._td.name) / "burners.yaml"

    def tearDown(self):
        self._td.cleanup()

    def test_missing_file_returns_empty(self):
        result = _load_burners(Path(self._td.name) / "nonexistent.yaml")
        self.assertEqual(result, ())

    def test_empty_file_returns_empty(self):
        self._path.write_text("")
        result = _load_burners(self._path)
        self.assertEqual(result, ())

    def test_no_burners_key_returns_empty(self):
        _write_yaml(self._path, {"other": []})
        result = _load_burners(self._path)
        self.assertEqual(result, ())

    def test_loads_burners(self):
        data = _burners_yaml([{
            "slug": "burner1",
            "youtube_title": "Burner 1",
            "youtube_channel_id": "UCb1",
            "google_email": "b1@example.com",
        }])
        _write_yaml(self._path, data)
        result = _load_burners(self._path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].slug, "burner1")

    def test_duplicate_burner_slug_raises(self):
        entry = {
            "slug": "dup",
            "youtube_title": "Dup",
            "youtube_channel_id": "UCdup",
            "google_email": "dup@example.com",
        }
        _write_yaml(self._path, _burners_yaml([entry, dict(entry, youtube_channel_id="UC2")]))
        with self.assertRaises(ValueError):
            _load_burners(self._path)


class TestBurnerPublicAPI(unittest.TestCase):
    def test_all_burners_returns_tuple(self):
        self.assertIsInstance(all_burners(), tuple)

    def test_burner_slugs_returns_list(self):
        self.assertIsInstance(burner_slugs(), list)

    def test_resolve_burner_unknown(self):
        self.assertIsNone(resolve_burner("no_such_burner_xyz"))

    def test_resolve_burner_known(self):
        burners = all_burners()
        if burners:
            b = burners[0]
            result = resolve_burner(b.slug)
            self.assertIsNotNone(result)
            self.assertEqual(result.slug, b.slug)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
