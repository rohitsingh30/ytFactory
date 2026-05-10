"""Tests for control/routes/channels_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from control.routes.channels_routes import router, _account_for_channel
from pipeline.schemas.customization import ChannelSummary, CustomizationSchema, CHANNEL_REGISTRY


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _fake_summary(key: str = "testchan") -> ChannelSummary:
    return ChannelSummary(
        key=key,
        label="Test Channel",
        tagline="Testing",
        language="en",
        default_format="animated",
        default_voice="sarah",
        default_length_s=60,
        variants_count=2,
    )


def _fake_schema(channel: str = "testchan") -> CustomizationSchema:
    return CustomizationSchema(
        channel=channel,
        label="Test Channel",
        tagline="Testing",
        language="en",
        variants=[],
        fields=[],
    )


class TestAccountForChannel(unittest.TestCase):
    def test_unknown_channel_returns_none(self) -> None:
        result = _account_for_channel("does_not_exist_xyz")
        self.assertIsNone(result)

    def test_known_channel_no_yaml(self) -> None:
        entry = CHANNEL_REGISTRY[0]
        with patch("control.routes.channels_routes.PROJECT_ROOT") as mock_root:
            mock_path = MagicMock(spec=Path)
            mock_path.exists.return_value = False
            mock_root.__truediv__ = MagicMock(return_value=mock_path)
            result = _account_for_channel(entry["key"])
        self.assertEqual(result, entry["key"])

    def test_known_channel_yaml_with_account(self) -> None:
        entry = CHANNEL_REGISTRY[0]
        yaml_content = "upload:\n  account: myaccount\n"
        with patch("control.routes.channels_routes.PROJECT_ROOT") as mock_root:
            mock_path = MagicMock(spec=Path)
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = yaml_content
            mock_root.__truediv__ = MagicMock(return_value=mock_path)
            result = _account_for_channel(entry["key"])
        self.assertEqual(result, "myaccount")

    def test_known_channel_yaml_no_account_falls_back(self) -> None:
        entry = CHANNEL_REGISTRY[0]
        yaml_content = "upload: {}\n"
        with patch("control.routes.channels_routes.PROJECT_ROOT") as mock_root:
            mock_path = MagicMock(spec=Path)
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = yaml_content
            mock_root.__truediv__ = MagicMock(return_value=mock_path)
            result = _account_for_channel(entry["key"])
        self.assertEqual(result, entry["key"])

    def test_known_channel_yaml_parse_error(self) -> None:
        import yaml
        entry = CHANNEL_REGISTRY[0]
        with patch("control.routes.channels_routes.PROJECT_ROOT") as mock_root:
            mock_path = MagicMock(spec=Path)
            mock_path.exists.return_value = True
            mock_path.read_text.side_effect = OSError("can't read")
            mock_root.__truediv__ = MagicMock(return_value=mock_path)
            result = _account_for_channel(entry["key"])
        self.assertEqual(result, entry["key"])


class TestListChannels(unittest.IsolatedAsyncioTestCase):
    async def test_list_returns_channels(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        chans = [_fake_summary("chan1"), _fake_summary("chan2")]
        with patch("control.routes.channels_routes.customization.list_channels",
                   return_value=chans):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["channels"]), 2)
        self.assertEqual(data["channels"][0]["key"], "chan1")


class TestGetChannel(unittest.IsolatedAsyncioTestCase):
    async def test_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=_fake_summary("testchan")):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels/testchan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["key"], "testchan")

    async def test_not_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels/unknown")
        self.assertEqual(r.status_code, 404)


class TestGetSchema(unittest.IsolatedAsyncioTestCase):
    async def test_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_customization_schema",
                   return_value=_fake_schema("testchan")):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels/testchan/customization_schema")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["channel"], "testchan")

    async def test_not_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_customization_schema",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels/unknown/customization_schema")
        self.assertEqual(r.status_code, 404)


class TestPatchDefaults(unittest.IsolatedAsyncioTestCase):
    async def test_patch_ok(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=_fake_summary("testchan")):
            with patch("control.routes.channels_routes.customization.save_user_defaults"):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.patch("/api/channels/testchan/defaults",
                                           json={"defaults": {"voice": "emma"}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    async def test_patch_not_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.patch("/api/channels/unknown/defaults",
                                       json={"defaults": {}})
        self.assertEqual(r.status_code, 404)


class TestAvatarBanner(unittest.IsolatedAsyncioTestCase):
    """Uses 'mystoriesanimated' which is in CHANNEL_REGISTRY."""

    CHANNEL = "mystoriesanimated"

    async def test_avatar_channel_not_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels/unknown/avatar.jpg")
        self.assertEqual(r.status_code, 404)

    async def test_avatar_asset_not_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=_fake_summary(self.CHANNEL)):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("control.routes.channels_routes.channel_assets.asset_path",
                           return_value=None):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.get(f"/api/channels/{self.CHANNEL}/avatar.jpg")
        self.assertEqual(r.status_code, 404)

    async def test_avatar_asset_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_path = MagicMock(spec=Path)
        mock_path.__str__ = MagicMock(return_value="/fake/avatar.jpg")
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=_fake_summary(self.CHANNEL)):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("control.routes.channels_routes.channel_assets.asset_path",
                           return_value=mock_path):
                    with patch("control.routes.channels_routes.FileResponse",
                               return_value=JSONResponse({"ok": True})):
                        async with httpx.AsyncClient(transport=transport,
                                                     base_url="http://test") as client:
                            r = await client.get(f"/api/channels/{self.CHANNEL}/avatar.jpg")
        self.assertEqual(r.status_code, 200)

    async def test_banner_asset_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_path = MagicMock(spec=Path)
        mock_path.__str__ = MagicMock(return_value="/fake/banner.jpg")
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=_fake_summary(self.CHANNEL)):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("control.routes.channels_routes.channel_assets.asset_path",
                           return_value=mock_path):
                    with patch("control.routes.channels_routes.FileResponse",
                               return_value=JSONResponse({"ok": True})):
                        async with httpx.AsyncClient(transport=transport,
                                                     base_url="http://test") as client:
                            r = await client.get(f"/api/channels/{self.CHANNEL}/banner.jpg")
        self.assertEqual(r.status_code, 200)

    async def test_avatar_no_account(self) -> None:
        """When _account_for_channel returns empty string, raise 404."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=_fake_summary(self.CHANNEL)):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=None):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.get(f"/api/channels/{self.CHANNEL}/avatar.jpg")
        self.assertEqual(r.status_code, 404)


class TestInspiration(unittest.IsolatedAsyncioTestCase):
    CHANNEL = "mystoriesanimated"

    async def test_channel_not_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/channels/unknown/inspiration")
        self.assertEqual(r.status_code, 404)

    async def test_inspiration_no_videos(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        summary = _fake_summary(self.CHANNEL)
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=summary):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("pipeline.research.youtube.load_account", return_value={}):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.get(f"/api/channels/{self.CHANNEL}/inspiration")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["videos"], [])

    async def test_inspiration_with_videos_and_limit(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        summary = _fake_summary(self.CHANNEL)
        videos = [
            {"video_id": "abc", "title": "Test 1", "view_count": 100},
            {"video_id": "def", "title": "Test 2", "view_count": 200},
            {"video_id": "ghi", "title": "Test 3"},
        ]
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=summary):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("pipeline.research.youtube.load_account",
                           return_value={"videos": videos}):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.get(
                            f"/api/channels/{self.CHANNEL}/inspiration?limit=2")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertLessEqual(len(data["videos"]), 2)

    async def test_inspiration_video_without_video_id_skipped(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        summary = _fake_summary(self.CHANNEL)
        # A video entry without video_id should be skipped
        videos = [{"title": "No ID here"}, {"video_id": "xyz"}]
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=summary):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("pipeline.research.youtube.load_account",
                           return_value={"videos": videos}):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.get(f"/api/channels/{self.CHANNEL}/inspiration")
        self.assertEqual(r.status_code, 200)
        # Only the one with video_id should appear
        self.assertEqual(len(r.json()["videos"]), 1)
        self.assertEqual(r.json()["videos"][0]["video_id"], "xyz")

    async def test_inspiration_thumbnail_fallback(self) -> None:
        """Video with no thumbnail_url gets ytimg fallback."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        summary = _fake_summary(self.CHANNEL)
        videos = [{"video_id": "abc123", "title": "No Thumbnail"}]
        with patch("control.routes.channels_routes.customization.get_channel",
                   return_value=summary):
            with patch("control.routes.channels_routes._account_for_channel",
                       return_value=self.CHANNEL):
                with patch("pipeline.research.youtube.load_account",
                           return_value={"videos": videos}):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.get(f"/api/channels/{self.CHANNEL}/inspiration")
        self.assertEqual(r.status_code, 200)
        vid = r.json()["videos"][0]
        self.assertIn("ytimg.com", vid["thumbnail"])


if __name__ == "__main__":
    unittest.main()
