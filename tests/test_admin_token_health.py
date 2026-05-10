"""Tests for /api/admin/token-health (plan-D3)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web import server


def _admin_request():
    """Minimal Request stub with the attributes _require_admin reads."""
    return SimpleNamespace(state=SimpleNamespace(user_is_admin=True, user_email="admin@x"))


def _non_admin_request():
    return SimpleNamespace(state=SimpleNamespace(user_is_admin=False, user_email="u@x"))


class TokenHealthEndpointTest(unittest.IsolatedAsyncioTestCase):

    async def test_non_admin_403s(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as cm:
            await server.admin_token_health(_non_admin_request())
        self.assertEqual(cm.exception.status_code, 403)

    async def test_aggregates_all_channels(self):
        accounts = [("acct1", "chan1"), ("acct2", "chan2"), ("acct3", "chan3")]
        statuses = {
            "acct1": {"account": "acct1", "state": "ok",
                       "expiry": "2099-01-01", "path": "/secrets/youtube-token-acct1/value"},
            "acct2": {"account": "acct2", "state": "no_refresh_token",
                       "expiry": None, "path": "/secrets/youtube-token-acct2/value"},
            "acct3": {"account": "acct3", "state": "missing",
                       "path": "/Users/x/.config/ytfactory/youtube_token_acct3.json"},
        }
        with patch("pipeline.research.youtube.iter_channel_configs",
                   return_value=accounts):
            with patch("pipeline.upload.upload.inspect_token_status",
                       side_effect=lambda a: statuses[a]):
                resp = await server.admin_token_health(_admin_request())
        self.assertEqual(resp["summary"], {"total": 3, "ok": 1, "broken": 2})
        by_acct = {r["account"]: r for r in resp["accounts"]}
        self.assertEqual(by_acct["acct1"]["state"], "ok")
        self.assertEqual(by_acct["acct1"]["source"], "secret_mount")
        self.assertEqual(by_acct["acct2"]["state"], "no_refresh_token")
        self.assertEqual(by_acct["acct2"]["source"], "secret_mount")
        self.assertEqual(by_acct["acct3"]["state"], "missing")
        # acct3 path is a config_dir style — should be tagged as missing
        # (no secret mount), distinct from secret_mount-but-empty.
        self.assertEqual(by_acct["acct3"]["source"], "missing")

    async def test_empty_channel_list(self):
        with patch("pipeline.research.youtube.iter_channel_configs", return_value=[]):
            resp = await server.admin_token_health(_admin_request())
        self.assertEqual(resp["summary"], {"total": 0, "ok": 0, "broken": 0})
        self.assertEqual(resp["accounts"], [])

    async def test_response_includes_fetched_at(self):
        with patch("pipeline.research.youtube.iter_channel_configs", return_value=[]):
            resp = await server.admin_token_health(_admin_request())
        self.assertIn("fetched_at", resp)
        # ISO-8601 with timezone.
        self.assertIn("T", resp["fetched_at"])

    async def test_missing_scopes_surfaced(self):
        with patch("pipeline.research.youtube.iter_channel_configs",
                   return_value=[("a1", "c1")]):
            with patch("pipeline.upload.upload.inspect_token_status",
                       return_value={"account": "a1", "state": "missing_scopes",
                                     "missing": ["youtube.upload"], "path": "/p"}):
                resp = await server.admin_token_health(_admin_request())
        self.assertEqual(resp["accounts"][0]["missing_scopes"], ["youtube.upload"])
        self.assertEqual(resp["summary"]["broken"], 1)


if __name__ == "__main__":
    unittest.main()
