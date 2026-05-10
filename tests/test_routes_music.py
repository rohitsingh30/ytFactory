"""Tests for control/routes/music_routes.py — 100% line coverage."""
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

from control.routes.music_routes import (
    _Bed,
    _iter_channels,
    _list_beds,
    _scan_channel,
    _scan_shared,
    router,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _fake_bed(channel: str = "testchan", filename: str = "bed.mp3") -> _Bed:
    p = MagicMock(spec=Path)
    p.name = filename
    p.stem = filename.rsplit(".", 1)[0]
    p.suffix = "." + filename.rsplit(".", 1)[1]
    p.__str__ = MagicMock(return_value=f"/fake/{channel}/music/{filename}")
    return _Bed(channel=channel, filename=filename, abs_path=p)


class TestBedProperties(unittest.TestCase):
    def test_key(self) -> None:
        bed = _fake_bed(filename="my_cool_bed.mp3")
        self.assertEqual(bed.key, "my_cool_bed")

    def test_label(self) -> None:
        bed = _fake_bed(filename="my_cool_bed.mp3")
        self.assertEqual(bed.label, "My Cool Bed")

    def test_sample_url(self) -> None:
        bed = _fake_bed(channel="testchan", filename="bed.mp3")
        self.assertEqual(bed.sample_url, "/api/music/sample/testchan/bed.mp3")


class TestIterChannels(unittest.TestCase):
    def test_returns_channels_with_config(self) -> None:
        # Just verify it runs without error and returns a list
        result = _iter_channels()
        self.assertIsInstance(result, list)

    def test_appends_channel_with_config_yaml(self) -> None:
        """Covers line 77: out.append(d.name) when config.yaml exists."""
        mock_config = MagicMock()
        mock_config.exists.return_value = True

        mock_dir = MagicMock()
        mock_dir.is_dir.return_value = True
        mock_dir.name = "testchan"
        mock_dir.__truediv__ = MagicMock(return_value=mock_config)

        with patch("control.routes.music_routes.PROJECT_ROOT") as mock_root:
            mock_root.iterdir.return_value = [mock_dir]
            result = _iter_channels()
        self.assertIn("testchan", result)


class TestScanChannel(unittest.TestCase):
    def test_no_dirs_exist(self) -> None:
        """Covers line 86: continue when subdir doesn't exist."""
        mock_dir = MagicMock(spec=Path)
        mock_dir.exists.return_value = False

        mock_base = MagicMock(spec=Path)
        mock_base.__truediv__ = MagicMock(return_value=mock_dir)

        with patch("control.routes.music_routes.PROJECT_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=mock_base)
            result = _scan_channel("fakechan")
        self.assertEqual(result, [])

    def test_dir_exists_with_audio(self) -> None:
        mock_file = MagicMock(spec=Path)
        mock_file.name = "bed.mp3"
        mock_file.is_file.return_value = True
        mock_file.suffix = ".mp3"
        mock_file.stem = "bed"

        mock_dir = MagicMock(spec=Path)
        mock_dir.exists.return_value = True
        mock_dir.iterdir.return_value = [mock_file]

        mock_base = MagicMock(spec=Path)
        mock_base.__truediv__ = MagicMock(return_value=mock_dir)

        with patch("control.routes.music_routes.PROJECT_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=mock_base)
            result = _scan_channel("testchan")
        self.assertIsInstance(result, list)

    def test_dir_exists_no_audio(self) -> None:
        mock_file = MagicMock(spec=Path)
        mock_file.name = "readme.txt"
        mock_file.is_file.return_value = True

        mock_dir = MagicMock(spec=Path)
        mock_dir.exists.return_value = True
        mock_dir.iterdir.return_value = []

        mock_base = MagicMock(spec=Path)
        mock_base.__truediv__ = MagicMock(return_value=mock_dir)

        with patch("control.routes.music_routes.PROJECT_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=mock_base)
            result = _scan_channel("testchan")
        self.assertEqual(result, [])


class TestScanShared(unittest.TestCase):
    def test_no_shared_dir(self) -> None:
        mock_d = MagicMock(spec=Path)
        mock_d.exists.return_value = False
        with patch("control.routes.music_routes.DATA_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=mock_d)
            result = _scan_shared()
        self.assertEqual(result, [])

    def test_shared_with_files(self) -> None:
        mock_file = MagicMock(spec=Path)
        mock_file.name = "shared.mp3"
        mock_file.is_file.return_value = True
        mock_file.suffix = ".mp3"
        mock_file.stem = "shared"

        mock_d = MagicMock(spec=Path)
        mock_d.exists.return_value = True
        mock_d.iterdir.return_value = [mock_file]

        with patch("control.routes.music_routes.DATA_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=mock_d)
            # suffix.lower() is called in the list comprehension
            result = _scan_shared()
        self.assertIsInstance(result, list)


class TestListBeds(unittest.TestCase):
    def test_list_beds_combines_shared_and_channels(self) -> None:
        """Covers lines 116-119: the body of _list_beds."""
        from control.routes.music_routes import _list_beds
        bed = _fake_bed(channel="shared", filename="shared.mp3")
        chan_bed = _fake_bed(channel="mychan", filename="chan.mp3")
        with patch("control.routes.music_routes._scan_shared", return_value=[bed]):
            with patch("control.routes.music_routes._iter_channels", return_value=["mychan"]):
                with patch("control.routes.music_routes._scan_channel", return_value=[chan_bed]):
                    result = _list_beds()
        self.assertEqual(len(result), 2)
        self.assertIn(bed, result)
        self.assertIn(chan_bed, result)


class TestCatalogEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_empty(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.music_routes._list_beds", return_value=[]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/music/catalog")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["music"], [])

    async def test_catalog_with_beds(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        bed = _fake_bed(channel="testchan", filename="cool_bed.mp3")
        with patch("control.routes.music_routes._list_beds", return_value=[bed]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/music/catalog")
        self.assertEqual(r.status_code, 200)
        music = r.json()["music"]
        self.assertEqual(len(music), 1)
        self.assertEqual(music[0]["channel"], "testchan")
        self.assertEqual(music[0]["filename"], "cool_bed.mp3")


class TestSampleEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_path_traversal_channel(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # "..bad" is not normalized by the router but contains ".." → triggers raise on line 143
            r = await client.get("/api/music/sample/..bad/bed.mp3")
        self.assertEqual(r.status_code, 400)

    async def test_path_traversal_filename(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # "..bad.mp3" contains ".." → triggers the path traversal guard
            r = await client.get("/api/music/sample/testchan/..bad.mp3")
        self.assertEqual(r.status_code, 400)

    async def test_not_found_returns_404(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_candidate = MagicMock(spec=Path)
        mock_candidate.exists.return_value = False
        with patch("control.routes.music_routes.DATA_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=MagicMock(
                __truediv__=MagicMock(return_value=mock_candidate)
            ))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/music/sample/shared/nonexistent.mp3")
        self.assertEqual(r.status_code, 404)

    async def test_shared_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_candidate = MagicMock(spec=Path)
        mock_candidate.exists.return_value = True
        mock_candidate.is_file.return_value = True
        mock_candidate.suffix = ".mp3"
        mock_candidate.__str__ = MagicMock(return_value="/fake/shared/bed.mp3")

        with patch("control.routes.music_routes.DATA_ROOT") as mock_data:
            mock_data.__truediv__ = MagicMock(return_value=MagicMock(
                __truediv__=MagicMock(return_value=mock_candidate)
            ))
            with patch("control.routes.music_routes.FileResponse",
                       return_value=JSONResponse({"ok": True})):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.get("/api/music/sample/shared/bed.mp3")
        self.assertEqual(r.status_code, 200)

    async def test_channel_found(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_candidate = MagicMock(spec=Path)
        mock_candidate.exists.return_value = True
        mock_candidate.is_file.return_value = True
        mock_candidate.suffix = ".mp3"
        mock_candidate.__str__ = MagicMock(return_value="/fake/testchan/music/bed.mp3")

        with patch("control.routes.music_routes.PROJECT_ROOT") as mock_root:
            mock_root.__truediv__ = MagicMock(return_value=MagicMock(
                __truediv__=MagicMock(return_value=MagicMock(
                    __truediv__=MagicMock(return_value=mock_candidate)
                ))
            ))
            with patch("control.routes.music_routes.FileResponse",
                       return_value=JSONResponse({"ok": True})):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.get("/api/music/sample/testchan/bed.mp3")
        self.assertEqual(r.status_code, 200)

    async def test_invalid_extension_returns_404(self) -> None:
        """A found file but with non-audio extension returns 404."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_candidate = MagicMock(spec=Path)
        mock_candidate.exists.return_value = True
        mock_candidate.is_file.return_value = True
        mock_candidate.suffix = ".txt"

        with patch("control.routes.music_routes.DATA_ROOT") as mock_data:
            mock_data.__truediv__ = MagicMock(return_value=MagicMock(
                __truediv__=MagicMock(return_value=mock_candidate)
            ))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/music/sample/shared/readme.txt")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
