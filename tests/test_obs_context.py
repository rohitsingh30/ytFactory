"""Tests for :mod:`pipeline.observability.context`.

Standalone — no SDK init needed.
"""
from __future__ import annotations

import unittest

from pipeline.observability.context import RenderContext, ctx, current_context


class TestRenderContext(unittest.TestCase):
    def test_default_is_empty(self) -> None:
        c = RenderContext()
        self.assertIsNone(c.channel)
        self.assertIsNone(c.slug)
        self.assertEqual(c.as_attributes(), {})

    def test_as_attributes_prefixed(self) -> None:
        c = RenderContext(channel="A", slug="s1", render_kind="short")
        attrs = c.as_attributes()
        self.assertEqual(attrs["ytfactory.channel"], "A")
        self.assertEqual(attrs["ytfactory.slug"], "s1")
        self.assertEqual(attrs["ytfactory.render_kind"], "short")
        self.assertNotIn("ytfactory.niche", attrs)  # niche unset

    def test_merged_keeps_existing_when_override_is_none(self) -> None:
        c = RenderContext(channel="A", slug="s1")
        m = c.merged(slug=None, niche="aita")
        self.assertEqual(m.channel, "A")
        self.assertEqual(m.slug, "s1")
        self.assertEqual(m.niche, "aita")

    def test_as_dict_drops_none(self) -> None:
        c = RenderContext(channel="A")
        d = c.as_dict()
        self.assertEqual(d, {"channel": "A"})


class TestCtxStack(unittest.TestCase):
    def test_root_is_empty(self) -> None:
        self.assertEqual(current_context(), RenderContext())

    def test_push_then_pop(self) -> None:
        with ctx(channel="A"):
            self.assertEqual(current_context().channel, "A")
        self.assertIsNone(current_context().channel)

    def test_nested_pushes_inherit(self) -> None:
        with ctx(channel="A", render_kind="short"):
            with ctx(slug="s1"):
                cur = current_context()
                self.assertEqual(cur.channel, "A")
                self.assertEqual(cur.render_kind, "short")
                self.assertEqual(cur.slug, "s1")
            # back to outer
            self.assertEqual(current_context().channel, "A")
            self.assertIsNone(current_context().slug)

    def test_inner_can_override_outer(self) -> None:
        with ctx(channel="A"):
            with ctx(channel="B"):
                self.assertEqual(current_context().channel, "B")
            self.assertEqual(current_context().channel, "A")


if __name__ == "__main__":
    unittest.main()
