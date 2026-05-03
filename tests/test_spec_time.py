"""Tests for time-token resolution and visibility windows."""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT, fake_beat_list  # noqa: F401

from render_from_spec import resolve_time, resolve_visibility


def _ctx(beats=None, total=20.0):
    beats = beats or fake_beat_list([(0.0, 2.0), (2.0, 5.0), (5.0, 17.5)])
    return {
        "start": 0.0,
        "end": total,
        "closer_start": beats[-1].start,
        "closer_end": beats[-1].end,
        "beat": beats,
    }


class ResolveTimeTest(unittest.TestCase):
    def test_absolute_number(self):
        self.assertEqual(resolve_time(3.14, _ctx()), 3.14)
        self.assertEqual(resolve_time(0, _ctx()), 0.0)

    def test_named_start(self):
        self.assertEqual(resolve_time("start", _ctx()), 0.0)

    def test_named_end(self):
        ctx = _ctx(total=22.5)
        self.assertEqual(resolve_time("end", ctx), 22.5)

    def test_closer_start(self):
        beats = fake_beat_list([(0, 2), (2, 5), (5, 17.34)])
        ctx = _ctx(beats=beats)
        self.assertAlmostEqual(resolve_time("closer_start", ctx), 5.0)

    def test_closer_end(self):
        beats = fake_beat_list([(0, 2), (5, 19.6)])
        ctx = _ctx(beats=beats)
        self.assertAlmostEqual(resolve_time("closer_end", ctx), 19.6)

    def test_arithmetic_subtraction(self):
        beats = fake_beat_list([(0, 2), (5, 17.34)])
        ctx = _ctx(beats=beats)
        self.assertAlmostEqual(resolve_time("closer_start - 0.3", ctx), 4.7)

    def test_arithmetic_addition(self):
        ctx = _ctx(total=20.0)
        self.assertAlmostEqual(resolve_time("end - 1.5", ctx), 18.5)
        self.assertAlmostEqual(resolve_time("start + 2", ctx), 2.0)

    def test_beat_index_positive(self):
        beats = fake_beat_list([(0, 2), (2, 5), (5, 17.5)])
        ctx = _ctx(beats=beats)
        self.assertAlmostEqual(resolve_time("beat[1].start", ctx), 2.0)
        self.assertAlmostEqual(resolve_time("beat[2].end", ctx), 17.5)

    def test_beat_index_negative(self):
        beats = fake_beat_list([(0, 2), (2, 5), (5, 17.5)])
        ctx = _ctx(beats=beats)
        self.assertAlmostEqual(resolve_time("beat[-1].start", ctx), 5.0)
        self.assertAlmostEqual(resolve_time("beat[-1].end", ctx), 17.5)

    def test_beat_arithmetic(self):
        beats = fake_beat_list([(0, 2), (2, 5), (5, 17.5)])
        ctx = _ctx(beats=beats)
        self.assertAlmostEqual(resolve_time("beat[1].start + 0.5", ctx), 2.5)

    def test_invalid_time_raises(self):
        with self.assertRaises(ValueError):
            resolve_time("not_a_token", _ctx())


class ResolveVisibilityTest(unittest.TestCase):
    def test_default_when_none(self):
        f, t = resolve_visibility(None, _ctx(total=20.0))
        self.assertEqual(f, 0.0)
        self.assertEqual(t, 20.0)

    def test_from_to(self):
        f, t = resolve_visibility({"from": 0, "to": "closer_start"},
                                  _ctx(beats=fake_beat_list([(0, 2), (5, 17.0)])))
        self.assertEqual(f, 0.0)
        self.assertAlmostEqual(t, 5.0)

    def test_at_for_shorthand(self):
        f, t = resolve_visibility({"at": 5.0, "for": 0.3}, _ctx())
        self.assertAlmostEqual(f, 5.0)
        self.assertAlmostEqual(t, 5.3)

    def test_from_to_with_arithmetic(self):
        ctx = _ctx(total=20.0)
        f, t = resolve_visibility({"from": "closer_start - 0.3", "to": "end"},
                                  _ctx(beats=fake_beat_list([(0, 2), (5, 17.0)]), total=20.0))
        self.assertAlmostEqual(f, 4.7)
        self.assertAlmostEqual(t, 20.0)


if __name__ == "__main__":
    unittest.main()
