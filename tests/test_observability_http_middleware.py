"""Audit Q2.11 — ``_CARRY_KEYS`` must include every dashboard-filterable
identity attr so the FastAPI middleware can promote them onto HTTP
server spans.

Pre-fix the set was ``("channel", "slug", "job_id", "niche", "account")``
only; the dashboard slices by ``render_kind``, ``render_mode``,
``run_id``, and ``user`` returned empty for HTTP spans because the
attribute was never set on them, even when a non-HTTP child span in
the same trace carried the value.
"""
from __future__ import annotations

import unittest

from pipeline.observability.http_middleware import _CARRY_KEYS


class TestCarryKeysCoverage(unittest.TestCase):
    def test_carry_keys_includes_all_dashboard_filterable_attrs(self):
        for k in (
            "channel", "slug", "job_id", "niche", "account",
            # Audit Q2.11 — the four newly-promoted keys.
            "render_kind", "render_mode", "run_id", "user",
        ):
            with self.subTest(key=k):
                self.assertIn(k, _CARRY_KEYS)


if __name__ == "__main__":
    unittest.main()
