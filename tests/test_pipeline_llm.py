"""Tests for pipeline.llm — focuses on the pure JSON-extraction logic.

The actual `claude -p` subprocess call is not exercised here (that's
integration); we test that the parser tolerates the messes that real
LLM outputs come back with: ```json fences, surrounding chatter,
JSON-looking but invalid content, etc.
"""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import ClaudeCLIError, _parse_inner_json


class ParseInnerJsonHappyPathTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(_parse_inner_json('{"a": 1}'), {"a": 1})

    def test_plain_array(self):
        self.assertEqual(_parse_inner_json("[1, 2, 3]"), [1, 2, 3])

    def test_nested_object(self):
        out = _parse_inner_json('{"narrator": {"description": "x", "age_band": "adult"}}')
        self.assertEqual(out["narrator"]["description"], "x")
        self.assertEqual(out["narrator"]["age_band"], "adult")

    def test_unicode_passthrough(self):
        out = _parse_inner_json('{"emoji": "👍"}')
        self.assertEqual(out["emoji"], "👍")


class ParseInnerJsonFenceStrippingTest(unittest.TestCase):
    def test_json_code_fence(self):
        out = _parse_inner_json('```json\n{"a": 1}\n```')
        self.assertEqual(out, {"a": 1})

    def test_bare_code_fence(self):
        out = _parse_inner_json("```\n{\"a\": 2}\n```")
        self.assertEqual(out, {"a": 2})

    def test_fenced_array(self):
        out = _parse_inner_json('```json\n[{"k": "v"}]\n```')
        self.assertEqual(out, [{"k": "v"}])

    def test_extra_whitespace_around_fences(self):
        out = _parse_inner_json('  ```json\n  {"a": 1}\n  ```  \n')
        self.assertEqual(out, {"a": 1})


class ParseInnerJsonFallbackExtractionTest(unittest.TestCase):
    """When the model wraps JSON in chatter, the parser falls back to
    finding the first balanced {...} or [...] block."""

    def test_object_buried_in_text(self):
        out = _parse_inner_json(
            "Here's the result you asked for:\n"
            '{"score": 7, "reason": "fine"}\n'
            "Hope that helps."
        )
        self.assertEqual(out, {"score": 7, "reason": "fine"})

    def test_array_buried_in_text(self):
        out = _parse_inner_json(
            "Sure thing! [\"a\", \"b\", \"c\"] there you go"
        )
        self.assertEqual(out, ["a", "b", "c"])

    def test_brace_balance_handles_nested(self):
        # Inner braces shouldn't terminate the outer scan early.
        out = _parse_inner_json(
            'preface {"outer": {"inner": {"deep": 42}}} suffix'
        )
        self.assertEqual(out, {"outer": {"inner": {"deep": 42}}})

    def test_prefers_object_over_later_array(self):
        # First balanced block wins when both are present.
        out = _parse_inner_json('{"a": 1} then [1, 2]')
        self.assertEqual(out, {"a": 1})


class ParseInnerJsonFailureTest(unittest.TestCase):
    def test_no_json_at_all_raises(self):
        with self.assertRaises(ClaudeCLIError):
            _parse_inner_json("just words, no JSON here")

    def test_truncated_object_raises(self):
        # Unbalanced braces — extraction can't find a complete block
        with self.assertRaises(ClaudeCLIError):
            _parse_inner_json('{"a": 1, "b": ')

    def test_garbage_inside_braces_raises(self):
        with self.assertRaises(ClaudeCLIError):
            _parse_inner_json("{not actually json}")


if __name__ == "__main__":
    unittest.main()
