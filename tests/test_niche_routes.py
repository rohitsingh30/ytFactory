"""Tests for control/niche_routes.py — read-only endpoints used by the operator UI."""
from __future__ import annotations

import unittest

import httpx

from control.niche_routes import NICHES, router


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


class NichesEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_all_niches(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/niches")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["niches"]), len(NICHES))

    async def test_each_niche_has_required_card_fields(self) -> None:
        # Reel UI expects these on every entry. If a key drops out, the
        # card renders with a missing gradient/emoji/etc and looks broken.
        for n in NICHES:
            for key in ("key", "label", "tagline", "emoji", "color_from",
                        "color_to", "sample_hooks", "channel_key_for_chat"):
                self.assertIn(key, n, f"niche {n.get('key')} missing {key}")
            self.assertIsInstance(n["sample_hooks"], list)
            self.assertGreaterEqual(len(n["sample_hooks"]), 1,
                                    f"niche {n['key']} should have at least one sample_hook")

    async def test_open_prompt_card_present(self) -> None:
        keys = {n["key"] for n in NICHES}
        self.assertIn("open", keys, "the 'open prompt' fallback card must exist")

    async def test_production_channels_present(self) -> None:
        keys = {n["key"] for n in NICHES}
        self.assertIn("mystoriesanimated", keys)
        self.assertIn("sportstoriesanimated", keys)


class VoicesEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_voices_returns_empty(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/voices")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["voices"], [])
        self.assertEqual(body["languages"], [])
        self.assertIsNone(body["default"])

    async def test_voice_clones_returns_empty(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/voice_clones")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["clones"], [])


if __name__ == "__main__":
    unittest.main()
