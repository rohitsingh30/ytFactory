"""Tests for the spec language: _lookup_path, resolve_value, _eval_ast.

Covers the substitution + expression-eval engine that drives both
template instantiation (render-time) and make_spec (pre-render).
"""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401  (sys.path side effect)

from render_from_spec import _eval_ast, _lookup_path, resolve_value
import ast


class LookupPathTest(unittest.TestCase):
    def test_dict_lookup(self):
        ns = {"a": {"b": {"c": 42}}}
        self.assertEqual(_lookup_path("a.b.c", ns), 42)

    def test_list_index(self):
        ns = {"items": ["zero", "one", "two"]}
        self.assertEqual(_lookup_path("items.0", ns), "zero")
        self.assertEqual(_lookup_path("items.2", ns), "two")

    def test_negative_list_index(self):
        ns = {"items": ["a", "b", "c"]}
        self.assertEqual(_lookup_path("items.-1", ns), "c")

    def test_mixed_dict_and_list(self):
        ns = {"buttons": [{"bg": "red"}, {"bg": "green"}]}
        self.assertEqual(_lookup_path("buttons.0.bg", ns), "red")
        self.assertEqual(_lookup_path("buttons.1.bg", ns), "green")

    def test_missing_key_raises(self):
        with self.assertRaises(KeyError):
            _lookup_path("a.missing", {"a": {"b": 1}})


class EvalAstTest(unittest.TestCase):
    def _eval(self, expr: str, ns: dict):
        return _eval_ast(ast.parse(expr, mode="eval").body, ns)

    def test_arithmetic(self):
        self.assertEqual(self._eval("2 + 3", {}), 5)
        self.assertEqual(self._eval("10 - 4", {}), 6)
        self.assertEqual(self._eval("3 * 4", {}), 12)
        self.assertEqual(self._eval("9 / 2", {}), 4.5)

    def test_unary_minus(self):
        self.assertEqual(self._eval("-7", {}), -7)
        self.assertEqual(self._eval("-(2 + 3)", {}), -5)

    def test_name_lookup(self):
        self.assertEqual(self._eval("pad", {"pad": 36}), 36)
        self.assertEqual(self._eval("pad + 10", {"pad": 36}), 46)

    def test_function_call(self):
        ns = {"sq": lambda x: x * x}
        self.assertEqual(self._eval("sq(5)", ns), 25)

    def test_attribute_access_on_dict(self):
        # ast.Attribute path — falls back to dict-style lookup
        self.assertEqual(self._eval("p.x", {"p": {"x": 7}}), 7)


class ResolveValuePassthroughTest(unittest.TestCase):
    def test_int_passes_through(self):
        self.assertEqual(resolve_value(42, {}), 42)

    def test_float_passes_through(self):
        self.assertEqual(resolve_value(3.14, {}), 3.14)

    def test_none_passes_through(self):
        self.assertIsNone(resolve_value(None, {}))

    def test_dict_recurses(self):
        self.assertEqual(resolve_value({"a": 1, "b": "two"}, {}), {"a": 1, "b": "two"})

    def test_list_recurses(self):
        self.assertEqual(resolve_value([1, 2, 3], {}), [1, 2, 3])

    def test_plain_string_unchanged(self):
        self.assertEqual(resolve_value("hello world", {}), "hello world")

    def test_hex_color_unchanged(self):
        self.assertEqual(resolve_value("#ff4500", {}), "#ff4500")
        self.assertEqual(resolve_value("#141414eb", {}), "#141414eb")


class ResolveValueSubstitutionTest(unittest.TestCase):
    def test_pure_substitution_returns_raw_value(self):
        ns = {"width": 940}
        self.assertEqual(resolve_value("${width}", ns), 940)

    def test_pure_substitution_preserves_lists(self):
        # Critical: each-loops require ${buttons} to return the LIST,
        # not the str repr. Bug we hit on first run.
        ns = {"buttons": [{"line1": "Like"}, {"line1": "Comment"}]}
        out = resolve_value("${buttons}", ns)
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["line1"], "Like")
        self.assertEqual(out[1]["line1"], "Comment")

    def test_pure_substitution_preserves_dicts(self):
        ns = {"btn": {"bg": "#c83238", "fg": "#ffffff"}}
        out = resolve_value("${btn}", ns)
        self.assertEqual(out, {"bg": "#c83238", "fg": "#ffffff"})

    def test_pure_substitution_dotted_path(self):
        ns = {"btn": {"bg": "#c83238"}}
        self.assertEqual(resolve_value("${btn.bg}", ns), "#c83238")

    def test_pure_substitution_list_index(self):
        ns = {"opts": ["AITA?", "WIBTA?", "AIO?"]}
        self.assertEqual(resolve_value("${opts.0}", ns), "AITA?")
        self.assertEqual(resolve_value("${opts.-1}", ns), "AIO?")

    def test_embedded_substitution_stringifies(self):
        ns = {"author": "dil-issue-1046"}
        self.assertEqual(resolve_value("u/${author}", ns), "u/dil-issue-1046")

    def test_embedded_with_arithmetic_after_sub(self):
        # ${width} - 80  should evaluate to a number
        self.assertEqual(resolve_value("${width} - 80", {"width": 980}), 900)

    def test_bare_name_evaluates(self):
        self.assertEqual(resolve_value("pad", {"pad": 36}), 36)

    def test_function_call_in_value(self):
        ns = {"pill_x": lambda i: 36 + i * 100, "i": 2}
        self.assertEqual(resolve_value("pill_x(i)", ns), 236)

    def test_arithmetic_with_helpers(self):
        ns = {"pad": 36, "pill_w": 200}
        self.assertEqual(resolve_value("pad + pill_w + pad", ns), 272)

    def test_unresolvable_string_falls_back_to_literal(self):
        # plain English doesn't parse as an expression — keep as-is
        self.assertEqual(resolve_value("for asshole", {}), "for asshole")
        self.assertEqual(resolve_value("Comment", {}), "Comment")

    def test_recursive_dict_substitution(self):
        ns = {"width": 940, "btn": {"bg": "#c83238", "fg": "#ffffff"}}
        out = resolve_value(
            {"x": "${width}", "fill": "${btn.bg}", "label": "Like"},
            ns,
        )
        self.assertEqual(out, {"x": 940, "fill": "#c83238", "label": "Like"})

    def test_recursive_list_substitution(self):
        ns = {"a": 1, "b": 2}
        self.assertEqual(resolve_value(["${a}", "${b}", 3], ns), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
