"""Tests for control/rate_limit.py — IP quotas + global spend cap.

Uses the in-memory backend; no Firestore round-trips.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

from control import rate_limit  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _fake_request(ip: str = "1.2.3.4", forwarded: str | None = None):
    req = MagicMock()
    headers = {}
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    req.headers = headers
    req.client = MagicMock()
    req.client.host = ip
    return req


class IpExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def test_uses_x_forwarded_for_first_hop(self):
        req = _fake_request(ip="10.0.0.1", forwarded="203.0.113.7, 198.51.100.5")
        self.assertEqual(rate_limit.client_ip(req), "203.0.113.7")

    def test_falls_back_to_request_client(self):
        req = _fake_request(ip="10.0.0.1")
        self.assertEqual(rate_limit.client_ip(req), "10.0.0.1")


class DailyQuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def test_chat_quota_enforced_after_default_limit(self):
        req = _fake_request(ip="9.9.9.9")
        chat_quota = rate_limit.quota_for("chat")
        # Within quota — no raise.
        for _ in range(chat_quota):
            rate_limit.check_and_increment(req, "chat")
        # One more = 429.
        with self.assertRaises(HTTPException) as cm:
            rate_limit.check_and_increment(req, "chat")
        self.assertEqual(cm.exception.status_code, 429)

    def test_each_ip_has_its_own_bucket(self):
        a = _fake_request(ip="1.1.1.1")
        b = _fake_request(ip="2.2.2.2")
        # Burn A's chat quota.
        for _ in range(rate_limit.quota_for("chat")):
            rate_limit.check_and_increment(a, "chat")
        with self.assertRaises(HTTPException):
            rate_limit.check_and_increment(a, "chat")
        # B is unaffected.
        rate_limit.check_and_increment(b, "chat")

    def test_confirm_quota_independent_of_chat(self):
        req = _fake_request(ip="3.3.3.3")
        # confirm has a different quota and must not consume chat budget.
        for _ in range(rate_limit.quota_for("confirm")):
            rate_limit.check_and_increment(req, "confirm")
        with self.assertRaises(HTTPException):
            rate_limit.check_and_increment(req, "confirm")
        # Chat budget still intact.
        rate_limit.check_and_increment(req, "chat")


class SpendCapTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def test_below_cap_passes(self):
        rate_limit.record_token_usage(1000, 500)  # tiny cost
        rate_limit.check_spend_cap()  # no raise

    def test_above_cap_raises_503(self):
        # Force the running spend over the cap.
        cap = rate_limit.daily_cap_usd()
        # Push spend over the cap by computing the equivalent token count.
        cost_per_1k = float(os.environ.get("YTFACTORY_AZURE_COST_PER_1K_TOK_USD", "0.005"))
        tokens_needed = int(cap / cost_per_1k * 1000) + 100_000
        rate_limit.record_token_usage(tokens_needed, 0)
        self.assertGreaterEqual(rate_limit.daily_spend_usd(), cap)
        with self.assertRaises(HTTPException) as cm:
            rate_limit.check_spend_cap()
        self.assertEqual(cm.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
