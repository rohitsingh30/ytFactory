"""P2d — verifies upload + OAuth instrumentation.

* ``upload.upload_short`` opens a parent ``upload_short`` span.
* ``upload.youtube_upload`` opens a child ``youtube_upload`` span.
* ``upload.authenticate`` opens an ``oauth_authenticate`` span; a
  :class:`RefreshTokenLost` raised inside marks the span ERROR.
* ``x_upload.post_short`` opens an ``x_post_short`` span; the inner
  ``x_post`` span nests under it.

We don't hit any real YouTube / X API — the impl functions are
patched via ``unittest.mock``.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import observability as obs
from pipeline.upload import upload as up_mod
from pipeline.upload import x_upload as xup_mod


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())


class TestYoutubeUploadSpan(_Base):
    def test_youtube_upload_emits_span_with_metadata(self) -> None:
        with patch.object(
            up_mod, "_youtube_upload_impl",
            return_value={"video_id": "v123", "url": "https://yt/v123"},
        ):
            res = up_mod.youtube_upload(
                Path("/tmp/x.mp4"),
                title="my title",
                description="desc",
                tags=["a", "b"],
                category_id="24",
                privacy="public",
                publish_at=None,
                account="rohit",
            )
        self.assertEqual(res["video_id"], "v123")
        s = next(s for s in self._spans() if s.name == "youtube_upload")
        self.assertEqual(s.attributes["ytfactory.meta.account"], "rohit")
        self.assertEqual(s.attributes["ytfactory.meta.title"], "my title")
        self.assertEqual(s.attributes["ytfactory.meta.privacy"], "public")
        self.assertEqual(s.attributes["ytfactory.meta.tag_count"], 2)
        self.assertEqual(s.attributes["ytfactory.meta.video_id"], "v123")


class TestUploadShortSpan(_Base):
    def test_upload_short_wraps_youtube_upload_as_child(self) -> None:
        # Stub _upload_short_impl so we don't run dedupe / metadata.
        with patch.object(
            up_mod, "_upload_short_impl",
            return_value={"video_id": "abc", "skipped": False},
        ):
            res = up_mod.upload_short(
                project_root=Path("/tmp"),
                channel_yaml={"upload": {"account": "rohit"}},
                channel_dir="historyrecapped",
                slug="aita-001",
                mp4_path=Path("/tmp/x.mp4"),
                script={"title": "x"},
            )
        self.assertEqual(res["video_id"], "abc")
        names = [s.name for s in self._spans()]
        self.assertIn("upload_short", names)

    def test_upload_short_records_refresh_token_lost(self) -> None:
        rtl = up_mod.RefreshTokenLost("rohit")
        rtl.account = "rohit"  # type: ignore[attr-defined]
        with patch.object(up_mod, "_upload_short_impl", side_effect=rtl):
            with self.assertRaises(up_mod.RefreshTokenLost):
                up_mod.upload_short(
                    project_root=Path("/tmp"),
                    channel_yaml={"upload": {"account": "rohit"}},
                    channel_dir="historyrecapped",
                    slug="aita-001",
                    mp4_path=Path("/tmp/x.mp4"),
                    script={"title": "x"},
                )
        s = next(s for s in self._spans() if s.name == "upload_short")
        self.assertFalse(s.status.is_ok)
        self.assertEqual(
            s.attributes["ytfactory.meta.error_class"], "RefreshTokenLost",
        )


class TestAuthenticateSpan(_Base):
    def test_authenticate_emits_span(self) -> None:
        with patch.object(up_mod, "_authenticate_impl",
                          return_value="creds-stub"):
            out = up_mod.authenticate("rohit", interactive=False)
        self.assertEqual(out, "creds-stub")
        s = next(s for s in self._spans()
                 if s.name == "oauth_authenticate")
        self.assertEqual(s.attributes["ytfactory.meta.account"], "rohit")
        self.assertFalse(s.attributes["ytfactory.meta.interactive"])


class TestXUploadSpans(_Base):
    def test_x_post_emits_span(self) -> None:
        Path("/tmp/x.mp4").write_bytes(b"x" * 32)
        with patch.object(
            xup_mod, "_x_post_impl",
            return_value={"tweet_id": "t1", "media_id": "m1"},
        ):
            out = xup_mod.x_post(
                Path("/tmp/x.mp4"), text="caption", account="b1",
            )
        self.assertEqual(out["tweet_id"], "t1")
        s = next(s for s in self._spans() if s.name == "x_post")
        self.assertEqual(s.attributes["ytfactory.meta.account"], "b1")
        self.assertEqual(s.attributes["ytfactory.meta.tweet_id"], "t1")

    def test_post_short_wraps_x_post(self) -> None:
        with patch.object(
            xup_mod, "_post_short_impl",
            return_value={"tweet_id": "tt", "skipped": False},
        ):
            out = xup_mod.post_short(
                project_root=Path("/tmp"),
                channel_yaml={"x": {"account": "b1", "enabled": True}},
                channel_dir="cosmosdecoded",
                slug="eddington",
                mp4_path=Path("/tmp/y.mp4"),
                script={"title": "x"},
            )
        self.assertEqual(out["tweet_id"], "tt")
        s = next(s for s in self._spans() if s.name == "x_post_short")
        self.assertEqual(s.attributes["ytfactory.meta.tweet_id"], "tt")


if __name__ == "__main__":
    unittest.main()
