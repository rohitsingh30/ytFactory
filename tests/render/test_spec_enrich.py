"""Tests for :func:`pipeline.render.spec_enrich.populate_render_extras`.

Pins the broken-pipe fix: ``era_anchor_prefix`` + ``character_description``
flow from script.metadata + cast.json into ``spec.extra`` so the
visualize plugins actually receive them. Pre-fix 17+ renders shipped
with WW1 trenches instead of the script's declared era because nothing
bridged the two phases. See ``pipeline/render/spec_enrich.py`` for the
why.

These tests use the REAL :mod:`pipeline.era_anchor` lookup against the
real ``pipeline/era_taxonomy.yaml`` — no mocking. The taxonomy is
small + stable, and mocking would let the lookup-shape change
silently. Picks era keys known to exist (``ww1-1914-1918-trench`` etc).
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from pipeline import era_anchor
from pipeline.render.spec import build_spec
from pipeline.render.spec_enrich import populate_render_extras


# A known era key from pipeline/era_taxonomy.yaml. Picked because it's
# unlikely to be renamed (WW1 is the canonical trench-warfare era).
KNOWN_ERA_KEY = "ww1-1914-1918-trench"


def _spec(channel: str = "historyrecapped"):
    """Build a minimal RenderSpec via the real builder, no YAMLs needed."""
    return build_spec(
        {"channel": channel, "channel_overrides": {}},
        channel_yaml_path=None,
        variant_yaml_path=None,
    )


class EraAnchorPrefixTest(unittest.TestCase):
    def test_known_era_anchor_populates_spec_extra(self):
        spec = _spec()
        script = {"metadata": {"era_anchor": KNOWN_ERA_KEY}}

        populate_render_extras(spec, script)

        prefix = spec.extra.get("era_anchor_prefix")
        self.assertIsNotNone(prefix, "era_anchor_prefix should be set")
        # Real lookup produces the "[ERA — <tokens>]" bracketed form.
        self.assertTrue(prefix.startswith("[ERA"))
        # Sanity: matches what era_anchor.era_prefix_for would return
        # directly — guards against the helper diverging from the
        # canonical lookup.
        self.assertEqual(prefix, era_anchor.era_prefix_for(KNOWN_ERA_KEY))

    def test_legacy_era_lock_key_works(self):
        """Earlier audit drafts used ``era_lock``; populate_render_extras
        accepts it as a fallback so older scripts on disk don't regress."""
        spec = _spec()
        script = {"metadata": {"era_lock": KNOWN_ERA_KEY}}

        populate_render_extras(spec, script)

        self.assertEqual(
            spec.extra.get("era_anchor_prefix"),
            era_anchor.era_prefix_for(KNOWN_ERA_KEY),
        )

    def test_unknown_era_key_falls_through_with_warning(self):
        """An unknown era key MUST NOT crash, MUST NOT populate the
        prefix, and MUST log a warning naming the offending key.
        That last bit is how the operator gets the "did you mean X?"
        signal — silent fall-through is the bug we're guarding against.
        """
        spec = _spec()
        script = {"metadata": {"era_anchor": "totally-fake-era-9999"}}

        with self.assertLogs("pipeline.render.spec_enrich", level="WARNING") as cm:
            populate_render_extras(spec, script)

        self.assertNotIn("era_anchor_prefix", spec.extra)
        # The warning text must include the unknown key — without that
        # the operator can't grep the worker log for the bad input.
        joined = "\n".join(cm.output)
        self.assertIn("totally-fake-era-9999", joined)

    def test_no_metadata_no_change(self):
        spec = _spec()
        populate_render_extras(spec, {})
        self.assertNotIn("era_anchor_prefix", spec.extra)

    def test_pre_populated_era_prefix_not_overwritten(self):
        """Idempotency: when a caller has already set the key
        (test fixtures, future pre-stage code), don't clobber it."""
        spec = _spec()
        spec.extra["era_anchor_prefix"] = "[ERA — pre-populated, do not touch]"
        script = {"metadata": {"era_anchor": KNOWN_ERA_KEY}}

        populate_render_extras(spec, script)

        self.assertEqual(
            spec.extra["era_anchor_prefix"],
            "[ERA — pre-populated, do not touch]",
        )


class CharacterDescriptionTest(unittest.TestCase):
    def setUp(self):
        # Stand up a synthetic channel folder under a temp project_root
        # so RenderPaths.from_channel_dir resolves into our sandbox
        # instead of the real ytFactory tree. We mutate
        # pipeline.paths.PROJECT_ROOT (the default RenderPaths picks up
        # from when project_root=None) for the duration of each test.
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_root = Path(self._tmp.name)
        import pipeline.paths as _paths
        self._orig_root = _paths.PROJECT_ROOT
        _paths.PROJECT_ROOT = self._tmp_root

    def tearDown(self):
        import pipeline.paths as _paths
        _paths.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def _write_cast(self, channel: str, slug: str, body: dict | str) -> Path:
        cast_dir = self._tmp_root / channel / "cast"
        cast_dir.mkdir(parents=True, exist_ok=True)
        cast_path = cast_dir / f"{slug}.json"
        if isinstance(body, str):
            cast_path.write_text(body)
        else:
            cast_path.write_text(json.dumps(body))
        return cast_path

    def test_narrator_description_extracted(self):
        spec = _spec(channel="historyrecapped")
        self._write_cast("historyrecapped", "test01", {
            "narrator": {
                "description": "weathered British infantry corporal, "
                               "1916, muddy uniform, thousand-yard stare",
            },
        })
        script = {"slug": "test01"}

        populate_render_extras(spec, script)

        self.assertEqual(
            spec.extra.get("character_description"),
            "weathered British infantry corporal, 1916, muddy uniform, "
            "thousand-yard stare",
        )

    def test_top_level_character_description_fallback(self):
        """Older channels (sportsrecapped legacy shape) store the
        description at the top level, not under narrator.description.
        populate_render_extras must accept both shapes."""
        spec = _spec(channel="sportsrecapped")
        self._write_cast("sportsrecapped", "tifo01", {
            "character_description": "Brazilian #10 forward, 2002 World Cup kit",
        })
        script = {"slug": "tifo01"}

        populate_render_extras(spec, script)

        self.assertEqual(
            spec.extra.get("character_description"),
            "Brazilian #10 forward, 2002 World Cup kit",
        )

    def test_narrator_takes_priority_over_top_level(self):
        """When BOTH shapes exist, narrator.description wins (it's the
        modern canonical shape; top-level is legacy fallback)."""
        spec = _spec(channel="historyrecapped")
        self._write_cast("historyrecapped", "test02", {
            "narrator": {"description": "modern shape"},
            "character_description": "legacy shape",
        })
        script = {"slug": "test02"}

        populate_render_extras(spec, script)

        self.assertEqual(spec.extra.get("character_description"), "modern shape")

    def test_missing_cast_json_silent_no_key(self):
        """Channels without a cast stage (footage-only, archival) don't
        author cast.json. populate_render_extras MUST treat that as the
        documented default — no key, no warning, no crash."""
        spec = _spec(channel="cosmosdecoded")  # no cast.json on disk
        script = {"slug": "missing-cast-slug"}

        # Capture log records to assert NO warning fires.
        with self.assertLogs("pipeline.render.spec_enrich", level="DEBUG") as cm:
            # assertLogs raises if NO records are emitted, so log a
            # marker first to satisfy the assertion contract.
            logging.getLogger("pipeline.render.spec_enrich").debug("marker")
            populate_render_extras(spec, script)

        self.assertNotIn("character_description", spec.extra)
        # No WARNING-level records for the missing-file case.
        warning_records = [r for r in cm.records if r.levelno >= logging.WARNING]
        self.assertEqual(
            warning_records, [],
            f"missing cast.json should not warn; got: {[r.getMessage() for r in warning_records]}",
        )

    def test_malformed_cast_json_graceful(self):
        """Cast.json that isn't valid JSON: log a WARNING and skip — the
        render proceeds without character_description rather than
        crashing the worker."""
        spec = _spec(channel="historyrecapped")
        self._write_cast("historyrecapped", "broken01", "{ this is not json")
        script = {"slug": "broken01"}

        with self.assertLogs("pipeline.render.spec_enrich", level="WARNING") as cm:
            populate_render_extras(spec, script)

        self.assertNotIn("character_description", spec.extra)
        self.assertTrue(
            any("unreadable" in r.getMessage() or "malformed" in r.getMessage()
                for r in cm.records),
            f"expected a malformed-cast warning, got: {[r.getMessage() for r in cm.records]}",
        )

    def test_cast_json_missing_fields_no_crash(self):
        """Cast.json that's valid JSON but has neither narrator.description
        nor top-level character_description: don't crash, don't set the
        key. (The cast LLM stage sometimes produces partial output.)"""
        spec = _spec(channel="historyrecapped")
        self._write_cast("historyrecapped", "partial01", {
            "narrator": {"name": "Sarah", "voice_id": "abc"},  # no description
            "other_field": "irrelevant",
        })
        script = {"slug": "partial01"}

        populate_render_extras(spec, script)  # must not raise

        self.assertNotIn("character_description", spec.extra)

    def test_pre_populated_character_description_not_overwritten(self):
        """Idempotency: pre-populated character_description survives."""
        spec = _spec(channel="historyrecapped")
        spec.extra["character_description"] = "pre-set, do not touch"
        self._write_cast("historyrecapped", "test03", {
            "narrator": {"description": "should-be-ignored"},
        })
        script = {"slug": "test03"}

        populate_render_extras(spec, script)

        self.assertEqual(
            spec.extra["character_description"],
            "pre-set, do not touch",
        )

    def test_no_slug_no_attempt(self):
        """When the script has no slug, we can't resolve cast.json —
        skip silently."""
        spec = _spec(channel="historyrecapped")
        populate_render_extras(spec, {"metadata": {}})
        self.assertNotIn("character_description", spec.extra)

    def test_cast_json_top_level_is_a_list_no_crash(self):
        """Defensive: cast.json that's valid JSON but the wrong shape
        (list at top level instead of dict) — warn + skip, don't crash."""
        spec = _spec(channel="historyrecapped")
        self._write_cast("historyrecapped", "wrongshape", json.dumps(["a", "b"]))
        script = {"slug": "wrongshape"}

        with self.assertLogs("pipeline.render.spec_enrich", level="WARNING"):
            populate_render_extras(spec, script)

        self.assertNotIn("character_description", spec.extra)


class CombinedFlowTest(unittest.TestCase):
    """End-to-end: both keys flow from a single populate_render_extras call."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_root = Path(self._tmp.name)
        import pipeline.paths as _paths
        self._orig_root = _paths.PROJECT_ROOT
        _paths.PROJECT_ROOT = self._tmp_root

    def tearDown(self):
        import pipeline.paths as _paths
        _paths.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def test_both_keys_populated_in_one_call(self):
        spec = build_spec(
            {"channel": "historyrecapped", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        cast_dir = self._tmp_root / "historyrecapped" / "cast"
        cast_dir.mkdir(parents=True, exist_ok=True)
        (cast_dir / "combined01.json").write_text(json.dumps({
            "narrator": {"description": "Tommy in trench coat"},
        }))
        script = {
            "slug": "combined01",
            "metadata": {"era_anchor": KNOWN_ERA_KEY},
        }

        populate_render_extras(spec, script)

        self.assertIn("era_anchor_prefix", spec.extra)
        self.assertIn("character_description", spec.extra)
        self.assertEqual(
            spec.extra["character_description"],
            "Tommy in trench coat",
        )

    def test_idempotent_double_call(self):
        """Calling twice produces the same spec.extra — no
        accumulation, no overwrite of the first-pass values."""
        spec = build_spec(
            {"channel": "historyrecapped", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        cast_dir = self._tmp_root / "historyrecapped" / "cast"
        cast_dir.mkdir(parents=True, exist_ok=True)
        (cast_dir / "idem01.json").write_text(json.dumps({
            "narrator": {"description": "first-pass value"},
        }))
        script = {
            "slug": "idem01",
            "metadata": {"era_anchor": KNOWN_ERA_KEY},
        }

        populate_render_extras(spec, script)
        snapshot = dict(spec.extra)

        # Mutate the cast file between calls — second call must NOT
        # pick up the change because the key is already populated.
        (cast_dir / "idem01.json").write_text(json.dumps({
            "narrator": {"description": "MUTATED — should not be picked up"},
        }))
        populate_render_extras(spec, script)

        self.assertEqual(spec.extra, snapshot)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
