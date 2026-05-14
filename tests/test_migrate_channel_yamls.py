"""Tests for scripts/migrate_channel_yamls.py.

Pin:
- Idempotency (running twice = no-op the second time)
- Top-level short defaults lift correctly into defaults.short
- Each per-kind block lifts correctly into defaults.long
- Block-name → visual_mode / overlay_timeline implications
- Channel-wide keys (name, branding, etc) stay at root
- Dry-run never writes; --apply does
"""
from __future__ import annotations

import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

# Import the migrator's pure migrate_yaml_doc helper for unit tests +
# main() for the CLI integration test.
import sys
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import migrate_channel_yamls as M  # noqa: E402


class MigrateYamlDocPureTest(unittest.TestCase):
    def test_lifts_top_level_short_keys(self):
        doc = {
            "name": "ChannelA",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "x.wav",
            "output_resolution": [1080, 1920],
            "narrator_visual_mode": "voice_only",
        }
        new_doc, notes = M.migrate_yaml_doc(doc)
        self.assertIn("defaults", new_doc)
        self.assertIn("short", new_doc["defaults"])
        self.assertEqual(new_doc["defaults"]["short"]["tts_provider"], "cloudrun_chatterbox")
        self.assertEqual(new_doc["defaults"]["short"]["output_resolution"], [1080, 1920])
        # narrator_visual_mode is channel-wide — stays at root.
        self.assertEqual(new_doc["narrator_visual_mode"], "voice_only")
        self.assertEqual(new_doc["name"], "ChannelA")
        # Notes mention each lifted key.
        joined_notes = "\n".join(notes)
        self.assertIn("tts_provider", joined_notes)
        self.assertIn("output_resolution", joined_notes)

    def test_lifts_long_form_block(self):
        doc = {
            "name": "ChannelB",
            "long_form": {
                "tts_voice": "long_voice.wav",
                "tts_speed": 0.85,
                "render_mode": "image_panels",
            },
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        self.assertEqual(new_doc["defaults"]["long"]["tts_voice"], "long_voice.wav")
        self.assertEqual(new_doc["defaults"]["long"]["tts_speed"], 0.85)
        self.assertEqual(new_doc["defaults"]["long"]["render_mode"], "image_panels")
        # long_form: block deleted from root.
        self.assertNotIn("long_form", new_doc)

    def test_lifts_long_form_doc_implies_overlay_timeline(self):
        doc = {
            "name": "Sports",
            "long_form_doc": {
                "tts_voice": "doc.wav",
                "footage_dir": "footage/sports",
            },
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        self.assertEqual(new_doc["defaults"]["long"]["tts_voice"], "doc.wav")
        # long_form_doc → overlay_timeline default = True.
        self.assertTrue(new_doc["defaults"]["long"]["overlay_timeline"])
        self.assertNotIn("long_form_doc", new_doc)

    def test_lifts_kathaa_implies_footage_windows(self):
        doc = {
            "name": "HindutavaAnimated",
            "kathaa": {"aspect": "16:9", "tts_voice": "kathaa.wav"},
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        self.assertEqual(new_doc["defaults"]["long"]["tts_voice"], "kathaa.wav")
        self.assertEqual(new_doc["defaults"]["long"]["visual_mode"], "footage_windows")
        self.assertNotIn("kathaa", new_doc)

    def test_lifts_footage_only_implies_footage_windows(self):
        doc = {
            "name": "HistoryRecapped",
            "footage_only": {"caption_mode": "shorts"},
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        self.assertEqual(new_doc["defaults"]["long"]["caption_mode"], "shorts")
        self.assertEqual(new_doc["defaults"]["long"]["visual_mode"], "footage_windows")
        self.assertNotIn("footage_only", new_doc)

    def test_lifts_sports_doc_implies_overlay_timeline(self):
        doc = {
            "name": "Sports",
            "sports_doc": {"tts_voice": "doc.wav"},
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        self.assertTrue(new_doc["defaults"]["long"]["overlay_timeline"])
        self.assertNotIn("sports_doc", new_doc)

    def test_channel_wide_keys_stay_at_root(self):
        doc = {
            "name": "X",
            "character_description": "smart and curious",
            "image_style_prefix": "isometric, soft palette",
            "branding": {"logo": "x.png"},
            "fiction_disclosure": True,
            "upload": {"privacy": "public"},
            "tts_provider": "cloudrun_chatterbox",
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        self.assertEqual(new_doc["character_description"], "smart and curious")
        self.assertEqual(new_doc["image_style_prefix"], "isometric, soft palette")
        self.assertEqual(new_doc["branding"], {"logo": "x.png"})
        self.assertTrue(new_doc["fiction_disclosure"])
        self.assertEqual(new_doc["upload"], {"privacy": "public"})
        # Lifted into defaults.short.
        self.assertEqual(new_doc["defaults"]["short"]["tts_provider"], "cloudrun_chatterbox")

    def test_idempotent_already_migrated_doc(self):
        doc = {
            "name": "X",
            "defaults": {
                "short": {"tts_provider": "y"},
                "long": {"tts_voice": "z.wav"},
            },
        }
        new_doc, notes = M.migrate_yaml_doc(doc)
        self.assertEqual(new_doc, doc)
        self.assertEqual(notes, [])

    def test_idempotent_run_twice(self):
        doc = {
            "name": "X",
            "tts_provider": "y",
            "long_form": {"tts_voice": "z.wav"},
        }
        once, _ = M.migrate_yaml_doc(doc)
        twice, notes_2 = M.migrate_yaml_doc(once)
        self.assertEqual(once, twice)
        self.assertEqual(notes_2, [])

    def test_combined_short_and_long_blocks(self):
        # Simulates the realistic shape: top-level short defaults +
        # one or more nested long blocks.
        doc = {
            "name": "Sports",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
            "output_resolution": [1080, 1920],
            "long_form": {"tts_voice": "long.wav", "render_mode": "archival_footage"},
            "long_form_doc": {"tts_voice": "doc.wav"},
        }
        new_doc, _ = M.migrate_yaml_doc(doc)
        # Short defaults lifted from top.
        self.assertEqual(new_doc["defaults"]["short"]["tts_provider"], "cloudrun_chatterbox")
        # Long defaults merged from BOTH blocks (later wins for the
        # tts_voice clash — sports_doc beats sleep history).
        self.assertEqual(new_doc["defaults"]["long"]["tts_voice"], "doc.wav")
        self.assertEqual(new_doc["defaults"]["long"]["render_mode"], "archival_footage")
        self.assertTrue(new_doc["defaults"]["long"]["overlay_timeline"])

    def test_empty_doc_returns_empty(self):
        new_doc, notes = M.migrate_yaml_doc({})
        self.assertEqual(new_doc, {})
        self.assertEqual(notes, [])


class MigrateOneFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-yaml-migrate-"))
        self.path = self.tmp / "channel.yaml"
        self.path.write_text(textwrap.dedent("""\
            name: TestChannel
            tts_provider: cloudrun_chatterbox
            tts_voice: x.wav
            long_form:
              tts_voice: long.wav
              render_mode: image_panels
        """))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_write(self):
        before = self.path.read_text()
        changed, notes = M.migrate_one_file(self.path, apply=False)
        after = self.path.read_text()
        self.assertTrue(changed)
        self.assertEqual(before, after)
        self.assertTrue(any("dry-run" in n for n in notes))

    def test_apply_rewrites_file(self):
        changed, notes = M.migrate_one_file(self.path, apply=True)
        self.assertTrue(changed)
        new_doc = yaml.safe_load(self.path.read_text())
        self.assertIn("defaults", new_doc)
        self.assertEqual(new_doc["defaults"]["short"]["tts_provider"], "cloudrun_chatterbox")
        self.assertEqual(new_doc["defaults"]["long"]["tts_voice"], "long.wav")
        self.assertNotIn("long_form", new_doc)
        self.assertNotIn("tts_provider", new_doc)  # lifted

    def test_apply_then_dry_run_no_change(self):
        # First apply migrates; second dry-run finds nothing to do.
        M.migrate_one_file(self.path, apply=True)
        changed, notes = M.migrate_one_file(self.path, apply=False)
        self.assertFalse(changed)
        self.assertTrue(any("already in new shape" in n for n in notes))

    def test_apply_twice_idempotent_byte_equivalent(self):
        M.migrate_one_file(self.path, apply=True)
        first_run = self.path.read_text()
        M.migrate_one_file(self.path, apply=True)
        second_run = self.path.read_text()
        self.assertEqual(first_run, second_run)

    def test_handles_invalid_yaml(self):
        self.path.write_text("name: x\n  bad-indent: !\n")
        changed, notes = M.migrate_one_file(self.path, apply=False)
        self.assertFalse(changed)
        self.assertTrue(any("parse error" in n for n in notes))

    def test_handles_non_mapping_root(self):
        self.path.write_text("- list\n- root\n")
        changed, notes = M.migrate_one_file(self.path, apply=False)
        self.assertFalse(changed)
        self.assertTrue(any("not a YAML mapping" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
