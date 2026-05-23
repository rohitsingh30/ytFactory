"""Tests for pipeline.cosmos_footage_prep — 100% line coverage."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class _FakeResponse(io.BytesIO):
    """A response object that works with shutil.copyfileobj (reads until EOF)."""
    def __init__(self, data: bytes, content_type: str):
        super().__init__(data)
        self._content_type = content_type
        # Provide a dict-like headers object
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mock_urlopen(data: bytes, *, content_type: str = "video/mp4"):
    """Return a context-manager that yields a FakeResponse for urlopen mocking."""
    resp = _FakeResponse(data, content_type)
    return resp


def _minimal_shotlist(kind="still_ken_burns", aspect="16:9", source_url=None):
    entry = {
        "source": "clip01.mp4",
        "source_url": source_url or "https://upload.wikimedia.org/test.jpg",
        "source_type": kind,
        "in_s": 0.0,
        "out_s": 5.0,
    }
    return {
        "clips": [entry],
        "aspect": aspect,
    }


# ---------------------------------------------------------------------------
# _http_get
# ---------------------------------------------------------------------------

class TestHttpGet(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dest = Path(self.td.name) / "out.jpg"

    def tearDown(self):
        self.td.cleanup()

    @patch("urllib.request.urlopen")
    def test_basic_download(self, mock_open):
        resp = _mock_urlopen(b"image-data", content_type="image/jpeg")
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _http_get
        _http_get("https://example.com/img.jpg", self.dest)
        self.assertTrue(self.dest.exists())

    @patch("urllib.request.urlopen")
    def test_image_content_type_guard_fail(self, mock_open):
        resp = _mock_urlopen(b"html", content_type="text/html")
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _http_get
        with self.assertRaises(RuntimeError) as ctx:
            _http_get("https://example.com/img.jpg", self.dest, expect_kind="image")
        self.assertIn("image", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_video_content_type_guard_fail(self, mock_open):
        resp = _mock_urlopen(b"html", content_type="text/html")
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _http_get
        with self.assertRaises(RuntimeError) as ctx:
            _http_get("https://example.com/clip.mp4", self.dest, expect_kind="video")
        self.assertIn("video", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_video_octet_stream_ok(self, mock_open):
        resp = _mock_urlopen(b"video-data", content_type="application/octet-stream")
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _http_get
        _http_get("https://example.com/clip.mp4", self.dest, expect_kind="video")
        # No exception means it passed the guard

    def test_file_url_absolute(self):
        src = Path(self.td.name) / "src.jpg"
        src.write_bytes(b"source-data")
        from pipeline.cosmos_footage_prep import _http_get
        _http_get(f"file://{src}", self.dest)
        self.assertEqual(self.dest.read_bytes(), b"source-data")

    def test_file_url_not_found_raises(self):
        from pipeline.cosmos_footage_prep import _http_get
        with self.assertRaises(OSError):
            _http_get("file:///nonexistent/path/img.jpg", self.dest)

    def test_file_url_relative(self):
        # file://host/path → urllib resolves as network path or missing file
        from pipeline.cosmos_footage_prep import _http_get
        with self.assertRaises(OSError):
            _http_get("file://no-such/path/img.jpg", self.dest)

    def test_file_url_dotslash(self):
        # file://./path → urllib treats as network/relative path → fails
        from pipeline.cosmos_footage_prep import _http_get
        with self.assertRaises(OSError):
            _http_get("file://./no-such-asset.jpg", self.dest)


# ---------------------------------------------------------------------------
# _resolve_wikimedia
# ---------------------------------------------------------------------------

class TestResolveWikimedia(unittest.TestCase):
    def test_already_direct_url(self):
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        url = "https://upload.wikimedia.org/wikipedia/commons/thumb/img.jpg"
        result = _resolve_wikimedia(url)
        self.assertEqual(result, url)

    def test_non_wikimedia_returns_none(self):
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        result = _resolve_wikimedia("https://example.com/img.jpg")
        self.assertIsNone(result)

    def test_category_page_returns_none(self):
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        result = _resolve_wikimedia(
            "https://commons.wikimedia.org/wiki/Category:Astronomy"
        )
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_file_page_resolved(self, mock_open):
        api_resp = {
            "query": {
                "pages": {
                    "-1": {
                        "imageinfo": [
                            {"thumburl": "https://upload.wikimedia.org/thumb.jpg", "url": "https://upload.wikimedia.org/full.jpg"}
                        ]
                    }
                }
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        url = "https://commons.wikimedia.org/wiki/File:Eddington.jpg"
        result = _resolve_wikimedia(url)
        self.assertEqual(result, "https://upload.wikimedia.org/thumb.jpg")

    @patch("urllib.request.urlopen")
    def test_no_imageinfo_returns_none(self, mock_open):
        api_resp = {"query": {"pages": {"-1": {}}}}
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        result = _resolve_wikimedia("https://commons.wikimedia.org/wiki/File:Test.jpg")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen", side_effect=Exception("network error"))
    def test_network_error_returns_none(self, _mock):
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        result = _resolve_wikimedia("https://commons.wikimedia.org/wiki/File:Test.jpg")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_uses_url_fallback_when_no_thumburl(self, mock_open):
        api_resp = {
            "query": {
                "pages": {
                    "-1": {
                        "imageinfo": [
                            {"url": "https://upload.wikimedia.org/full.jpg"}
                        ]
                    }
                }
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_wikimedia
        result = _resolve_wikimedia(
            "https://en.wikipedia.org/wiki/File:Test.jpg"
        )
        self.assertEqual(result, "https://upload.wikimedia.org/full.jpg")


# ---------------------------------------------------------------------------
# _resolve_nasa_image
# ---------------------------------------------------------------------------

class TestResolveNasaImage(unittest.TestCase):
    def test_non_nasa_returns_none(self):
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        self.assertIsNone(_resolve_nasa_image("https://example.com/img.jpg"))

    def test_nasa_without_details_returns_none(self):
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        self.assertIsNone(
            _resolve_nasa_image("https://images.nasa.gov/gallery")
        )

    @patch("urllib.request.urlopen")
    def test_item_without_href_skipped(self, mock_open):
        """Items with empty href are skipped (covers line 143: continue)."""
        api_resp = {
            "collection": {
                "items": [
                    {},                                               # no href key
                    {"href": ""},                                     # empty href
                    {"href": "https://images-assets.nasa.gov/image/iss/iss~orig.jpg"},
                ]
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        result = _resolve_nasa_image("https://images.nasa.gov/details/iss-001")
        self.assertIn("orig", result)

    @patch("urllib.request.urlopen")
    def test_nasa_details_resolved(self, mock_open):
        api_resp = {
            "collection": {
                "items": [
                    {"href": "https://images-assets.nasa.gov/image/iss/iss~orig.jpg"},
                    {"href": "https://images-assets.nasa.gov/image/iss/iss~large.jpg"},
                ]
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        result = _resolve_nasa_image(
            "https://images.nasa.gov/details/iss-001"
        )
        self.assertIn("orig", result)

    @patch("urllib.request.urlopen")
    def test_no_ranked_picks_first(self, mock_open):
        api_resp = {
            "collection": {
                "items": [
                    {"href": "https://images-assets.nasa.gov/image/iss/iss.jpg"},
                ]
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        result = _resolve_nasa_image("https://images.nasa.gov/details/iss-001")
        self.assertIsNotNone(result)

    @patch("urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_network_error_returns_none(self, _mock):
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        result = _resolve_nasa_image("https://images.nasa.gov/details/iss-001")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_empty_items_returns_none(self, mock_open):
        api_resp = {"collection": {"items": []}}
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_nasa_image
        result = _resolve_nasa_image("https://images.nasa.gov/details/iss-001")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _resolve_archive_org
# ---------------------------------------------------------------------------

class TestResolveArchiveOrg(unittest.TestCase):
    def test_non_archive_returns_none(self):
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        self.assertIsNone(_resolve_archive_org("https://example.com/item"))

    def test_direct_download_url(self):
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        url = "https://archive.org/download/item/file.mp4"
        result = _resolve_archive_org(url)
        self.assertEqual(result, url)

    def test_non_download_non_details_returns_none(self):
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        result = _resolve_archive_org("https://archive.org/search?q=test")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_details_resolved_h264(self, mock_open):
        api_resp = {
            "files": [
                {"name": "video.mp4", "format": "h.264", "size": "1000"},
                {"name": "video.ogv", "format": "Ogg Video", "size": "1000"},
            ]
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        result = _resolve_archive_org("https://archive.org/details/my-item")
        self.assertIn("video.mp4", result)

    @patch("urllib.request.urlopen")
    def test_details_falls_back_to_mp4(self, mock_open):
        api_resp = {
            "files": [
                {"name": "video.mp4", "format": "Unknown", "size": "1000"},
            ]
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        result = _resolve_archive_org("https://archive.org/details/my-item")
        self.assertIn("video.mp4", result)

    @patch("urllib.request.urlopen")
    def test_no_mp4_returns_none(self, mock_open):
        api_resp = {"files": [{"name": "video.ogv", "format": "Ogg Video"}]}
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        result = _resolve_archive_org("https://archive.org/details/my-item")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_network_error_returns_none(self, _mock):
        from pipeline.cosmos_footage_prep import _resolve_archive_org
        result = _resolve_archive_org("https://archive.org/details/my-item")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _resolve_url
# ---------------------------------------------------------------------------

class TestResolveUrl(unittest.TestCase):
    def test_file_url(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "file://some/path.jpg"
        resolved, reason = _resolve_url(url)
        self.assertIsNotNone(resolved)
        self.assertIsNone(reason)

    def test_direct_upload_wikimedia(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "https://upload.wikimedia.org/wiki/commons/img.jpg"
        resolved, reason = _resolve_url(url)
        self.assertEqual(resolved, url)

    def test_direct_asset_jpg(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "https://example.com/photo.jpg"
        resolved, reason = _resolve_url(url)
        self.assertEqual(resolved, url)
        self.assertIsNone(reason)

    def test_direct_asset_mp4(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "https://example.com/video.mp4"
        resolved, reason = _resolve_url(url)
        self.assertEqual(resolved, url)

    def test_pexels_manual(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "https://www.pexels.com/video/123"
        resolved, reason = _resolve_url(url)
        self.assertIsNone(resolved)
        self.assertIsNotNone(reason)

    def test_royalsociety_manual(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "https://royalsocietypublishing.org/doi/abs/10.1098/rsta.1920.0009"
        resolved, reason = _resolve_url(url)
        self.assertIsNone(resolved)

    def test_unrecognised_host(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        url = "https://unknown-site.com/page"
        resolved, reason = _resolve_url(url)
        self.assertIsNone(resolved)
        self.assertIn("unrecognised", reason)

    @patch("urllib.request.urlopen")
    def test_wikimedia_resolved(self, mock_open):
        api_resp = {
            "query": {
                "pages": {
                    "-1": {
                        "imageinfo": [
                            {"thumburl": "https://upload.wikimedia.org/thumb.jpg", "url": "..."}
                        ]
                    }
                }
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_url
        resolved, reason = _resolve_url(
            "https://commons.wikimedia.org/wiki/File:Test.jpg"
        )
        self.assertIsNotNone(resolved)

    def test_wikimedia_unresolvable(self):
        from pipeline.cosmos_footage_prep import _resolve_url
        with patch("urllib.request.urlopen", side_effect=Exception):
            resolved, reason = _resolve_url(
                "https://en.wikipedia.org/wiki/File:Test.jpg"
            )
        self.assertIsNone(resolved)
        self.assertIsNotNone(reason)

    @patch("urllib.request.urlopen", side_effect=Exception)
    def test_nasa_unresolvable(self, _mock):
        from pipeline.cosmos_footage_prep import _resolve_url
        resolved, reason = _resolve_url(
            "https://images.nasa.gov/details/hubble-001"
        )
        self.assertIsNone(resolved)

    @patch("urllib.request.urlopen")
    def test_nasa_resolved_success(self, mock_open):
        """_resolve_url: NASA resolves → return (r, None) — covers line 203."""
        api_resp = {
            "collection": {
                "items": [
                    {"href": "https://images-assets.nasa.gov/image/iss/iss~orig.jpg"},
                ]
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(api_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_url
        resolved, reason = _resolve_url("https://images.nasa.gov/details/iss-001")
        self.assertIsNotNone(resolved)
        self.assertIsNone(reason)

    @patch("urllib.request.urlopen", side_effect=Exception)
    def test_archive_unresolvable(self, _mock):
        from pipeline.cosmos_footage_prep import _resolve_url
        resolved, reason = _resolve_url(
            "https://archive.org/details/my-item"
        )
        self.assertIsNone(resolved)

    @patch("urllib.request.urlopen")
    def test_archive_resolved_success(self, mock_open):
        """_resolve_url: archive.org resolves → return (r, None) — covers line 209."""
        meta_resp = {"files": [{"name": "video.mp4", "format": "h.264"}]}
        resp = MagicMock()
        resp.read.return_value = json.dumps(meta_resp).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        from pipeline.cosmos_footage_prep import _resolve_url
        resolved, reason = _resolve_url("https://archive.org/details/my-video-item")
        self.assertIsNotNone(resolved)
        self.assertIsNone(reason)


# ---------------------------------------------------------------------------
# _kenburns
# ---------------------------------------------------------------------------

class TestKenburns(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    @patch("subprocess.run")
    def test_9_16_aspect(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        still = Path(self.td.name) / "still.jpg"
        still.touch()
        out = Path(self.td.name) / "out.mp4"
        from pipeline.cosmos_footage_prep import _kenburns
        _kenburns(still, out, duration_s=5.0, aspect="9:16")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("1080", " ".join(str(c) for c in cmd))

    @patch("subprocess.run")
    def test_16_9_aspect(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        still = Path(self.td.name) / "still.jpg"
        still.touch()
        out = Path(self.td.name) / "out.mp4"
        from pipeline.cosmos_footage_prep import _kenburns
        _kenburns(still, out, duration_s=8.0, aspect="16:9")
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# _entries_from_shotlist
# ---------------------------------------------------------------------------

class TestEntriesFromShotlist(unittest.TestCase):
    def test_clips(self):
        from pipeline.cosmos_footage_prep import _entries_from_shotlist
        sl = {"clips": [{"source": "a.mp4"}], "aspect": "16:9"}
        entries, container = _entries_from_shotlist(sl)
        self.assertEqual(container, "clips")
        self.assertEqual(len(entries), 1)

    def test_windows(self):
        from pipeline.cosmos_footage_prep import _entries_from_shotlist
        sl = {"windows": [{"source": "a.mp4"}], "aspect": "9:16"}
        entries, container = _entries_from_shotlist(sl)
        self.assertEqual(container, "windows")

    def test_neither_raises(self):
        from pipeline.cosmos_footage_prep import _entries_from_shotlist
        with self.assertRaises(ValueError):
            _entries_from_shotlist({"aspect": "16:9"})


# ---------------------------------------------------------------------------
# prep_shotlist
# ---------------------------------------------------------------------------

class TestPrepShotlist(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _write_shotlist(self, channel, slug, data):
        sl_dir = self.root / "data" / channel / "shotlist"
        sl_dir.mkdir(parents=True)
        (sl_dir / f"{slug}.json").write_text(json.dumps(data))

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_still_kenburns_path(self, mock_open, mock_run):
        # Set up still download response using proper BytesIO-backed response
        resp = _mock_urlopen(b"img-data", content_type="image/jpeg")
        mock_open.return_value = resp
        mock_run.return_value = MagicMock(returncode=0)

        shotlist = {
            "clips": [
                {
                    "source": "clip01.mp4",
                    "source_url": "https://upload.wikimedia.org/test.jpg",
                    "source_type": "still_ken_burns",
                    "in_s": 0.0,
                    "out_s": 5.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "test-slug", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "test-slug")
        self.assertEqual(len(result.fetched), 1)

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_still_kenburns_already_exists_skipped(self, mock_open, mock_run):
        shotlist = {
            "clips": [
                {
                    "source": "clip01.mp4",
                    "source_url": "https://upload.wikimedia.org/test.jpg",
                    "source_type": "still_ken_burns",
                    "in_s": 0.0,
                    "out_s": 5.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "test-slug", shotlist)
        # Create the destination file so it's "already there"
        dest = self.root / "data" / "cosmosdecoded" / "footage" / "long_sources" / "clip01.mp4"
        dest.parent.mkdir(parents=True)
        dest.touch()

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "test-slug")
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(len(result.fetched), 0)

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_video_download_path(self, mock_open, mock_run):
        resp = _mock_urlopen(b"x" * 2048, content_type="video/mp4")
        mock_open.return_value = resp

        shotlist = {
            "clips": [
                {
                    "source": "clip02.mp4",
                    "source_url": "https://example.com/video.mp4",
                    "source_type": "video",
                    "in_s": 0.0,
                    "out_s": 8.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "test-slug2", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "test-slug2")
        self.assertEqual(len(result.fetched), 1)

    @patch("urllib.request.urlopen")
    def test_manual_fallback_unresolvable(self, mock_open):
        shotlist = {
            "clips": [
                {
                    "source": "clip03.mp4",
                    "source_url": "https://www.pexels.com/video/123",
                    "source_type": "video",
                    "in_s": 0.0,
                    "out_s": 8.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "test-slug3", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "test-slug3")
        self.assertEqual(len(result.manual), 1)

    def test_missing_source_name_is_error(self):
        shotlist = {
            "clips": [
                {
                    "source_url": "https://example.com/video.mp4",
                    "source_type": "video",
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "test-slug4", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "test-slug4")
        self.assertEqual(len(result.errors), 1)

    def test_no_source_url_is_manual(self):
        shotlist = {
            "clips": [
                {
                    "source": "clip05.mp4",
                    "source_type": "video",
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "test-slug5", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "test-slug5")
        self.assertEqual(len(result.manual), 1)

    def test_missing_shotlist_raises_system_exit(self):
        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            with self.assertRaises(SystemExit):
                prep_mod.prep_shotlist("cosmosdecoded", "no-such-slug")

    def test_unsupported_aspect_raises_system_exit(self):
        shotlist = {
            "clips": [{"source": "x.mp4", "source_url": "file://x.mp4", "source_type": "video",
                       "in_s": 0.0, "out_s": 5.0}],
            "aspect": "4:3",  # unsupported
        }
        self._write_shotlist("cosmosdecoded", "bad-aspect", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            with self.assertRaises(SystemExit):
                prep_mod.prep_shotlist("cosmosdecoded", "bad-aspect")

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_download_error_recorded(self, mock_open, mock_run):
        mock_open.side_effect = Exception("network down")

        shotlist = {
            "clips": [
                {
                    "source": "clip_err.mp4",
                    "source_url": "https://upload.wikimedia.org/test.jpg",
                    "source_type": "still_ken_burns",
                    "in_s": 0.0,
                    "out_s": 5.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "err-slug", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "err-slug")
        self.assertEqual(len(result.errors), 1)

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_force_refetches(self, mock_open, mock_run):
        resp = _mock_urlopen(b"img-data", content_type="image/jpeg")
        mock_open.return_value = resp
        mock_run.return_value = MagicMock(returncode=0)

        shotlist = {
            "clips": [
                {
                    "source": "clip01.mp4",
                    "source_url": "https://upload.wikimedia.org/test.jpg",
                    "source_type": "still_ken_burns",
                    "in_s": 0.0,
                    "out_s": 5.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "force-slug", shotlist)
        dest = self.root / "data" / "cosmosdecoded" / "footage" / "long_sources" / "clip01.mp4"
        dest.parent.mkdir(parents=True)
        dest.touch()

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "force-slug", force=True)
        self.assertEqual(len(result.fetched), 1)

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_windows_container_uses_sources_subdir(self, mock_open, mock_run):
        resp = _mock_urlopen(b"x" * 2048, content_type="video/mp4")
        mock_open.return_value = resp

        shotlist = {
            "windows": [
                {
                    "source": "shot01.mp4",
                    "source_url": "https://example.com/video.mp4",
                    "source_type": "video",
                    "in_s": 0.0,
                    "out_s": 8.0,
                }
            ],
            "aspect": "9:16",
        }
        self._write_shotlist("cosmosdecoded", "windows-slug", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "windows-slug")
        sources_dir = self.root / "data" / "cosmosdecoded" / "footage" / "sources"
        self.assertTrue(sources_dir.exists())

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_video_small_file_raises_and_cleaned_up(self, mock_open, mock_run):
        """Video downloads <1024 bytes → RuntimeError (line 327) + cleanup (line 334)."""
        resp = _mock_urlopen(b"x" * 100, content_type="video/mp4")
        mock_open.return_value = resp

        shotlist = {
            "clips": [
                {
                    "source": "tiny.mp4",
                    "source_url": "https://example.com/clip.mp4",
                    "source_type": "video",
                    "in_s": 0.0,
                    "out_s": 8.0,
                }
            ],
            "aspect": "16:9",
        }
        self._write_shotlist("cosmosdecoded", "tiny-slug", shotlist)

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            result = prep_mod.prep_shotlist("cosmosdecoded", "tiny-slug")
        self.assertEqual(len(result.errors), 1)
        self.assertIn("empty file", result.errors[0][2])


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_main_success(self, mock_open, mock_run):
        resp = _mock_urlopen(b"img-data", content_type="image/jpeg")
        mock_open.return_value = resp
        mock_run.return_value = MagicMock(returncode=0)

        shotlist = {
            "clips": [
                {
                    "source": "clip.mp4",
                    "source_url": "https://upload.wikimedia.org/test.jpg",
                    "source_type": "still_ken_burns",
                    "in_s": 0.0,
                    "out_s": 5.0,
                }
            ],
            "aspect": "16:9",
        }
        sl_dir = self.root / "data" / "cosmosdecoded" / "shotlist"
        sl_dir.mkdir(parents=True)
        (sl_dir / "main-slug.json").write_text(json.dumps(shotlist))

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            ret = prep_mod.main(["--channel", "cosmosdecoded", "--slug", "main-slug"])
        self.assertEqual(ret, 0)

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_main_with_errors_returns_1(self, mock_open, mock_run):
        mock_open.side_effect = Exception("network error")

        shotlist = {
            "clips": [
                {
                    "source": "clip.mp4",
                    "source_url": "https://upload.wikimedia.org/test.jpg",
                    "source_type": "still_ken_burns",
                    "in_s": 0.0,
                    "out_s": 5.0,
                }
            ],
            "aspect": "16:9",
        }
        sl_dir = self.root / "data" / "cosmosdecoded" / "shotlist"
        sl_dir.mkdir(parents=True)
        (sl_dir / "err-slug.json").write_text(json.dumps(shotlist))

        import pipeline.cosmos_footage_prep as prep_mod
        with patch.object(prep_mod, "REPO_ROOT", self.root):
            ret = prep_mod.main(["--channel", "cosmosdecoded", "--slug", "err-slug"])
        self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
