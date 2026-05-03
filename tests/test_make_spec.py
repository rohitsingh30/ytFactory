"""Tests for make_spec.py — channel template + script JSON → resolved spec."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from tests._helpers import PROJECT_ROOT  # noqa: F401

from make_spec import _lookup_path, make_spec, substitute


class SubstituteTest(unittest.TestCase):
    def setUp(self):
        self.script = {
            "slug": "abc-def",
            "narration": "Story here.",
            "title_options": ["Hook A", "Hook B"],
        }
        self.raw = {"metadata": {"author": "throwaway42", "score": 9001}}
        self.channel = {"name": "AITA Cooking"}
        self.ns = {"script": self.script, "raw": self.raw, "channel": self.channel}

    def test_pure_script_substitution(self):
        self.assertEqual(substitute("${script.slug}", self.ns), "abc-def")
        self.assertEqual(substitute("${script.narration}", self.ns), "Story here.")

    def test_pure_raw_substitution(self):
        self.assertEqual(substitute("${raw.metadata.author}", self.ns), "throwaway42")
        self.assertEqual(substitute("${raw.metadata.score}", self.ns), 9001)

    def test_pure_channel_substitution(self):
        self.assertEqual(substitute("${channel.name}", self.ns), "AITA Cooking")

    def test_list_index_in_path(self):
        self.assertEqual(substitute("${script.title_options.0}", self.ns), "Hook A")
        self.assertEqual(substitute("${script.title_options.1}", self.ns), "Hook B")

    def test_pure_substitution_returns_raw_type(self):
        # Not stringified — important for spec correctness
        out = substitute("${script.title_options}", self.ns)
        self.assertIsInstance(out, list)

    def test_embedded_substitution(self):
        self.assertEqual(substitute("u/${raw.metadata.author}", self.ns),
                         "u/throwaway42")

    def test_template_internal_refs_left_alone(self):
        # ${width}, ${btn.bg}, etc. (no script./raw./channel. prefix)
        # must pass through unchanged for the render-time engine.
        self.assertEqual(substitute("${width}", self.ns), "${width}")
        self.assertEqual(substitute("${btn.bg}", self.ns), "${btn.bg}")
        self.assertEqual(substitute("${title}", self.ns), "${title}")

    def test_mixed_refs_only_substitutes_known_namespaces(self):
        out = substitute("u/${raw.metadata.author} on ${channel.name}", self.ns)
        self.assertEqual(out, "u/throwaway42 on AITA Cooking")
        # ${unknown.x} stays
        self.assertEqual(substitute("hi ${unknown.x}", self.ns), "hi ${unknown.x}")

    def test_recursive_dict(self):
        out = substitute(
            {"author": "${raw.metadata.author}", "title": "${script.title_options.0}"},
            self.ns,
        )
        self.assertEqual(out, {"author": "throwaway42", "title": "Hook A"})

    def test_recursive_list(self):
        out = substitute(["${script.slug}", "literal", "${channel.name}"], self.ns)
        self.assertEqual(out, ["abc-def", "literal", "AITA Cooking"])

    def test_non_string_passthrough(self):
        self.assertEqual(substitute(42, self.ns), 42)
        self.assertEqual(substitute(3.14, self.ns), 3.14)
        self.assertIsNone(substitute(None, self.ns))
        self.assertEqual(substitute(True, self.ns), True)

    def test_plain_string_unchanged(self):
        self.assertEqual(substitute("hello", self.ns), "hello")


class MakeSpecTest(unittest.TestCase):
    """End-to-end test of make_spec without touching the renderer."""

    def test_full_make_spec(self):
        channel = {
            "name": "Test Channel",
            "spec_template": {
                "version": "2",
                "slug": "${script.slug}",
                "audio": {
                    "narration": {
                        "text": "${script.narration}",
                        "tts": {"voice": "af_heart"},
                    },
                },
                "templates": {
                    "card": {
                        "params": ["title", "width"],
                        "size": {"w": "${width}", "h": "auto"},
                        "layout": [
                            {"kind": "text", "text": "${title}", "size": 56},
                        ],
                    },
                },
                "overlays": [
                    {
                        "id": "card",
                        "template": "card",
                        "args": {
                            "title": "${script.title_options.0}",
                            "width": 980,
                        },
                    },
                ],
            },
        }
        script = {
            "slug": "test-slug-001",
            "narration": "Once upon a time...",
            "title_options": ["A great title", "A backup title"],
        }
        raw = {"metadata": {"author": "user42"}}

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            channel_path = tmp / "channel.yaml"
            script_path = tmp / "script.json"
            raw_path = tmp / "raw.json"
            channel_path.write_text(yaml.safe_dump(channel))
            script_path.write_text(json.dumps(script))
            raw_path.write_text(json.dumps(raw))

            out = make_spec(channel_path, script_path, raw_path, tmp / "specs")
            spec = yaml.safe_load(out.read_text())

        # script.* substituted
        self.assertEqual(spec["slug"], "test-slug-001")
        self.assertEqual(spec["audio"]["narration"]["text"], "Once upon a time...")
        # list index resolves
        self.assertEqual(spec["overlays"][0]["args"]["title"], "A great title")
        # template-internal ${width} / ${title} survives untouched
        tpl = spec["templates"]["card"]
        self.assertEqual(tpl["size"]["w"], "${width}")
        self.assertEqual(tpl["layout"][0]["text"], "${title}")

    def test_missing_spec_template_raises(self):
        channel = {"name": "broken"}  # no spec_template
        script = {"slug": "x"}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cp = tmp / "c.yaml"
            sp = tmp / "s.json"
            cp.write_text(yaml.safe_dump(channel))
            sp.write_text(json.dumps(script))
            with self.assertRaises(ValueError):
                make_spec(cp, sp, None, tmp / "out")

    def test_auto_locates_raw(self):
        # Convention: <channel-dir>/raw/<slug>.json
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            channel_dir = tmp / "data" / "intermediate" / "ch"
            (channel_dir / "raw").mkdir(parents=True)
            (channel_dir / "scripts").mkdir(parents=True)

            script = {"slug": "foo-bar", "title_options": ["t"]}
            (channel_dir / "scripts" / "foo-bar.json").write_text(json.dumps(script))
            raw = {"metadata": {"author": "u_auto"}}
            (channel_dir / "raw" / "foo-bar.json").write_text(json.dumps(raw))

            channel = {
                "spec_template": {
                    "slug": "${script.slug}",
                    "author": "${raw.metadata.author}",
                },
            }
            cp = tmp / "channel.yaml"
            cp.write_text(yaml.safe_dump(channel))

            out = make_spec(
                cp, channel_dir / "scripts" / "foo-bar.json",
                None, channel_dir / "specs",
            )
            spec = yaml.safe_load(out.read_text())
            self.assertEqual(spec["author"], "u_auto")  # auto-found raw


class LookupPathTest(unittest.TestCase):
    """make_spec has its own _lookup_path; mirror behaviour of render_from_spec's."""

    def test_dict_chain(self):
        self.assertEqual(_lookup_path("a.b.c", {"a": {"b": {"c": 7}}}), 7)

    def test_list_index(self):
        self.assertEqual(_lookup_path("xs.0", {"xs": ["zero", "one"]}), "zero")
        self.assertEqual(_lookup_path("xs.-1", {"xs": ["zero", "one"]}), "one")


if __name__ == "__main__":
    unittest.main()
