"""Tests for control/rate_limit.py — IP quotas + global spend cap.

Uses the in-memory backend; no Firestore round-trips.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

from control.core import rate_limit  # noqa: E402
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


# ---------------------------------------------------------------------------
# _MemoryBackend.reset()
# ---------------------------------------------------------------------------

class MemoryBackendResetTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def test_reset_clears_counters_and_spend(self):
        backend = rate_limit.get_backend()
        backend.incr("1.2.3.4", "chat")
        backend.add_spend(1.0)
        backend.reset()
        self.assertEqual(backend.get("1.2.3.4", "chat"), 0)
        self.assertEqual(backend.get_spend(), 0.0)


# ---------------------------------------------------------------------------
# Owner-IP bypass (line 198)
# ---------------------------------------------------------------------------

class OwnerIpBypassTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def test_owner_ip_bypasses_quota(self):
        """Owner IPs skip the per-IP counter entirely."""
        req = _fake_request(ip="10.0.0.1")
        with patch.dict("os.environ", {"YTFACTORY_OWNER_IPS": "10.0.0.1"}, clear=False):
            # Re-import the frozenset since module-level constant was already set.
            with patch.object(rate_limit, "_OWNER_IPS", frozenset(["10.0.0.1"])):
                # Should NOT raise even beyond the quota.
                for _ in range(200):
                    rate_limit.check_and_increment(req, "chat")

    def test_non_owner_ip_not_bypassed(self):
        """Non-owner IPs are still subject to quota."""
        req = _fake_request(ip="5.5.5.5")
        with patch.object(rate_limit, "_OWNER_IPS", frozenset(["10.0.0.1"])):
            # Burn the chat quota for 5.5.5.5.
            for _ in range(rate_limit.quota_for("chat")):
                rate_limit.check_and_increment(req, "chat")
            with self.assertRaises(HTTPException):
                rate_limit.check_and_increment(req, "chat")


# ---------------------------------------------------------------------------
# Firestore helpers: _client, _ip_counter_ref, _spend_doc_ref
# ---------------------------------------------------------------------------

class FirestoreHelperTest(unittest.TestCase):
    def test_client_returns_firestore_client(self):
        mock_client = MagicMock()
        with patch("google.cloud.firestore.Client", return_value=mock_client):
            result = rate_limit._client()
        self.assertIs(result, mock_client)

    def test_ip_counter_ref_returns_ref(self):
        mock_db = MagicMock()
        mock_ref = MagicMock()
        # Chain: .collection().document().collection().document().collection().document()
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.collection.return_value.document.return_value = mock_ref
        with patch.object(rate_limit, "_client", return_value=mock_db):
            ref = rate_limit._ip_counter_ref("1.2.3.4", "chat")
        self.assertIsNotNone(ref)

    def test_spend_doc_ref_returns_ref(self):
        mock_db = MagicMock()
        with patch.object(rate_limit, "_client", return_value=mock_db):
            ref = rate_limit._spend_doc_ref()
        self.assertIsNotNone(ref)


# ---------------------------------------------------------------------------
# FirestoreBackend methods (mocked)
# ---------------------------------------------------------------------------

class FirestoreBackendTest(unittest.TestCase):
    def _make_fb(self):
        from control.core.rate_limit import _FirestoreBackend
        return _FirestoreBackend()

    def test_incr_returns_incremented_count(self):
        fb = self._make_fb()
        snap = MagicMock()
        snap.to_dict.return_value = {"count": 5}
        mock_ref = MagicMock()
        mock_ref.get.return_value = snap
        with patch.object(rate_limit, "_ip_counter_ref", return_value=mock_ref):
            with patch("google.cloud.firestore.Increment", return_value=1):
                count = fb.incr("1.2.3.4", "chat")
        self.assertEqual(count, 5)

    def test_get_existing_returns_count(self):
        fb = self._make_fb()
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"count": 7}
        mock_ref = MagicMock()
        mock_ref.get.return_value = snap
        with patch.object(rate_limit, "_ip_counter_ref", return_value=mock_ref):
            count = fb.get("1.2.3.4", "chat")
        self.assertEqual(count, 7)

    def test_get_missing_returns_zero(self):
        fb = self._make_fb()
        snap = MagicMock()
        snap.exists = False
        mock_ref = MagicMock()
        mock_ref.get.return_value = snap
        with patch.object(rate_limit, "_ip_counter_ref", return_value=mock_ref):
            count = fb.get("9.9.9.9", "chat")
        self.assertEqual(count, 0)

    def test_add_spend_returns_total(self):
        fb = self._make_fb()
        snap = MagicMock()
        snap.to_dict.return_value = {"usd": 3.14}
        mock_ref = MagicMock()
        mock_ref.get.return_value = snap
        with patch.object(rate_limit, "_spend_doc_ref", return_value=mock_ref):
            with patch("google.cloud.firestore.Increment", return_value=0.5):
                total = fb.add_spend(0.5)
        self.assertAlmostEqual(total, 3.14)

    def test_get_spend_existing_returns_value(self):
        fb = self._make_fb()
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"usd": 2.5}
        mock_ref = MagicMock()
        mock_ref.get.return_value = snap
        with patch.object(rate_limit, "_spend_doc_ref", return_value=mock_ref):
            spend = fb.get_spend()
        self.assertAlmostEqual(spend, 2.5)

    def test_get_spend_missing_returns_zero(self):
        fb = self._make_fb()
        snap = MagicMock()
        snap.exists = False
        mock_ref = MagicMock()
        mock_ref.get.return_value = snap
        with patch.object(rate_limit, "_spend_doc_ref", return_value=mock_ref):
            spend = fb.get_spend()
        self.assertEqual(spend, 0.0)


# ---------------------------------------------------------------------------
# get_backend() — firestore branch (line 157)
# ---------------------------------------------------------------------------

class GetBackendFirestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def tearDown(self) -> None:
        rate_limit.reset_backend()
        os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

    def test_firestore_backend_when_env_says_firestore(self):
        from control.core.rate_limit import _FirestoreBackend
        with patch.dict(os.environ, {"YTFACTORY_QUEUE_BACKEND": "firestore"}):
            rate_limit.reset_backend()
            backend = rate_limit.get_backend()
            self.assertIsInstance(backend, _FirestoreBackend)
