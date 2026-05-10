"""Tests for control/state_routes.py — channel state CRUD against GCS.

GCS is mocked: a single ``_FakeBucket`` holds the in-memory blobs for
the duration of the test. Auth is bypassed by setting K_SERVICE per
test (the same trust-IAM path Cloud Run uses in prod). K_SERVICE is
*scoped* to setUp/tearDown so it doesn't leak into other tests in the
suite that need the off-Cloud-Run auth path (test_agent_lease_protocol,
test_scheduler_routes, etc.).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

from control.routes.state_routes import router as state_router


# ---------------------------------------------------------------------------
# Fake GCS — minimal subset of google.cloud.storage we use.
# ---------------------------------------------------------------------------


class _FakeBlob:
    def __init__(self, store: dict, name: str) -> None:
        self._store = store
        self.name = name

    def exists(self) -> bool:
        return self.name in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self.name]

    def upload_from_string(self, payload, content_type=None):  # noqa: ARG002
        self._store[self.name] = payload if isinstance(payload, bytes) else payload.encode("utf-8")

    def delete(self) -> None:
        self._store.pop(self.name, None)

    @property
    def updated(self):
        return None


class _FakeBucket:
    def __init__(self, store: dict) -> None:
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)


class _FakeClient:
    def __init__(self, store: dict) -> None:
        self._store = store

    def bucket(self, _name: str) -> _FakeBucket:
        return _FakeBucket(self._store)

    def list_blobs(self, _bucket: str, prefix: str):
        for name, _data in sorted(self._store.items()):
            if name.startswith(prefix):
                yield _FakeBlob(self._store, name)


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(state_router)
    return app


class StateRoutesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # K_SERVICE makes the auth check trust IAM (Cloud Run prod path).
        # Scoped to this test so it doesn't poison the rest of the suite.
        self._prev_k_service = os.environ.get("K_SERVICE")
        os.environ["K_SERVICE"] = "test-suite"
        self._store: dict[str, bytes] = {}
        self._patch = patch(
            "control.routes.state_routes._gcs_client",
            return_value=_FakeClient(self._store),
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        if self._prev_k_service is None:
            os.environ.pop("K_SERVICE", None)
        else:
            os.environ["K_SERVICE"] = self._prev_k_service

    async def _client(self):
        app = _make_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def test_put_then_get_round_trips(self) -> None:
        # Use a non-narration kind so the test exercises CRUD plumbing
        # without going through schema validation (covered by
        # NarrationSchemaIntegrationTest below).
        body = {"slug": "aguero-9320", "_comment": "shotlist", "windows": []}
        async with await self._client() as c:
            r = await c.put(
                "/api/state/sportsrecapped/shotlist/aguero-9320",
                json=body,
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("uri", r.json())
            r = await c.get("/api/state/sportsrecapped/shotlist/aguero-9320")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), body)

    async def test_get_404_when_absent(self) -> None:
        async with await self._client() as c:
            r = await c.get("/api/state/sportsrecapped/shotlist/missing")
            self.assertEqual(r.status_code, 404)

    async def test_delete_removes(self) -> None:
        async with await self._client() as c:
            await c.put(
                "/api/state/historyrecapped/shotlist/pompeii",
                json={"slug": "pompeii", "windows": []},
            )
            r = await c.delete("/api/state/historyrecapped/shotlist/pompeii")
            self.assertEqual(r.status_code, 200, r.text)
            r = await c.get("/api/state/historyrecapped/shotlist/pompeii")
            self.assertEqual(r.status_code, 404)

    async def test_delete_404_when_absent(self) -> None:
        async with await self._client() as c:
            r = await c.delete("/api/state/historyrecapped/shotlist/never-existed")
            self.assertEqual(r.status_code, 404)

    async def test_list_returns_slugs_sorted(self) -> None:
        async with await self._client() as c:
            for slug in ("zulu", "alpha", "mike"):
                await c.put(
                    f"/api/state/cosmosdecoded/shotlist/{slug}",
                    json={"slug": slug, "windows": []},
                )
            r = await c.get("/api/state/cosmosdecoded/shotlist")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["channel"], "cosmosdecoded")
            self.assertEqual(body["kind"], "shotlist")
            # FakeClient yields in sorted order; the route preserves that.
            self.assertEqual(body["slugs"], ["alpha", "mike", "zulu"])

    async def test_list_excludes_nested_subdirs(self) -> None:
        # Nested niche layout: <channel>/<niche>/shotlist/<slug>.json.
        # The list endpoint v1 returns flat-layout only.
        self._store["mystoriesanimated/reddit_amitheasshole/shotlist/sub.json"] = b"{}"
        async with await self._client() as c:
            await c.put(
                "/api/state/mystoriesanimated/shotlist/flat",
                json={"slug": "flat", "windows": []},
            )
            r = await c.get("/api/state/mystoriesanimated/shotlist")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["slugs"], ["flat"])

    async def test_invalid_kind_rejected(self) -> None:
        async with await self._client() as c:
            r = await c.get("/api/state/sportsrecapped/random_dir/whatever")
            self.assertEqual(r.status_code, 400)
            self.assertIn("invalid kind", r.json()["detail"])

    async def test_invalid_slug_chars_rejected(self) -> None:
        # Slugs with characters outside [A-Za-z0-9_.\-] would let a
        # caller poison cross-system tooling that re-shells the slug
        # (think `slug=foo;rm -rf` propagating into ffmpeg/ffprobe).
        # _SAFE_RE rejects them as 400 before any GCS work.
        async with await self._client() as c:
            r = await c.put(
                "/api/state/sportsrecapped/shotlist/evil@slug",
                json={"x": 1},
            )
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("invalid slug", r.json()["detail"])

    async def test_health_no_auth(self) -> None:
        async with await self._client() as c:
            r = await c.get("/api/state/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertIn("narrations", body["allowed_kinds"])

    async def test_narration_put_validates_against_niche_schema(self) -> None:
        # PUTs to kind=narrations must conform to the universal NicheVideo
        # schema. A free-form payload (which would have round-tripped fine
        # for kind=shotlist) gets rejected at 422.
        async with await self._client() as c:
            r = await c.put(
                "/api/state/historyrecapped/narrations/freeform",
                json={"slug": "freeform", "narration": "Hello"},
            )
            self.assertEqual(r.status_code, 422, r.text)
            detail = r.json()["detail"]
            self.assertIn("NicheVideo", detail["error"])
            # Specific missing fields surface so the producer can fix.
            error_locs = {tuple(e["loc"]) for e in detail["errors"]}
            self.assertIn(("channel",), error_locs)
            self.assertIn(("title",), error_locs)
            self.assertIn(("beats",), error_locs)

    async def test_narration_put_round_trips_when_schema_conformant(self) -> None:
        # End-to-end: a fully schema-conformant payload passes
        # validation, lands in GCS, comes back via GET unchanged.
        from tests.test_niche_schema import _history_short_payload  # noqa: PLC0415
        body = _history_short_payload()
        body["slug"] = "valid-conformant-slug"
        async with await self._client() as c:
            r = await c.put(
                "/api/state/historyrecapped/narrations/valid-conformant-slug",
                json=body,
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("uri", r.json())
            r = await c.get("/api/state/historyrecapped/narrations/valid-conformant-slug")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["slug"], "valid-conformant-slug")
            self.assertEqual(r.json()["niche"], "history-short")

    async def test_narration_put_rejects_url_payload_channel_mismatch(self) -> None:
        # URL says channel=historyrecapped but payload says channel=mystoriesanimated.
        # Reject 422 — would otherwise let a caller spoof writes across channels.
        from tests.test_niche_schema import _history_short_payload  # noqa: PLC0415
        body = _history_short_payload()
        body["channel"] = "mystoriesanimated"
        body["slug"] = "channel-mismatch"
        async with await self._client() as c:
            r = await c.put(
                "/api/state/historyrecapped/narrations/channel-mismatch",
                json=body,
            )
            self.assertEqual(r.status_code, 422, r.text)
            self.assertIn("channel", r.json()["detail"])

    async def test_narration_put_rejects_url_payload_slug_mismatch(self) -> None:
        from tests.test_niche_schema import _history_short_payload  # noqa: PLC0415
        body = _history_short_payload()
        body["slug"] = "payload-slug"
        async with await self._client() as c:
            r = await c.put(
                "/api/state/historyrecapped/narrations/url-slug",
                json=body,
            )
            self.assertEqual(r.status_code, 422, r.text)
            self.assertIn("slug", r.json()["detail"])

    async def test_corrupt_json_in_storage_returns_502(self) -> None:
        # Direct backdoor write of non-JSON bytes — simulates external
        # corruption (e.g. a manual gsutil cp of a binary file).
        self._store["sportsrecapped/narrations/garbage.json"] = b"\x00\x01not json"
        async with await self._client() as c:
            r = await c.get("/api/state/sportsrecapped/narrations/garbage")
            self.assertEqual(r.status_code, 502)


class StateRoutesAuthTest(unittest.IsolatedAsyncioTestCase):
    """Off-Cloud-Run auth path: K_SERVICE unset, bearer token required."""

    def setUp(self) -> None:
        # Pretend we're on a laptop dev machine, not Cloud Run.
        self._k_service = os.environ.pop("K_SERVICE", None)
        # Snapshot YTFACTORY_AGENT_TOKEN so we restore it in tearDown
        # (other test modules — test_agent_lease_protocol, test_scheduler —
        # set it at module load and expect it to persist).
        self._prev_token = os.environ.get("YTFACTORY_AGENT_TOKEN")
        os.environ["YTFACTORY_AGENT_TOKEN"] = "expected-token"
        self._patch = patch(
            "control.routes.state_routes._gcs_client",
            return_value=_FakeClient({}),
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        if self._k_service is not None:
            os.environ["K_SERVICE"] = self._k_service
        if self._prev_token is None:
            os.environ.pop("YTFACTORY_AGENT_TOKEN", None)
        else:
            os.environ["YTFACTORY_AGENT_TOKEN"] = self._prev_token

    async def _client(self):
        app = _make_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def test_401_without_bearer(self) -> None:
        async with await self._client() as c:
            r = await c.get("/api/state/sportsrecapped/narrations/x")
            self.assertEqual(r.status_code, 401)

    async def test_403_with_wrong_token(self) -> None:
        async with await self._client() as c:
            r = await c.get(
                "/api/state/sportsrecapped/narrations/x",
                headers={"Authorization": "Bearer wrong"},
            )
            self.assertEqual(r.status_code, 403)

    async def test_200_with_correct_token(self) -> None:
        # Use kind=shotlist so the auth-only test isn't ALSO exercising
        # the niche-schema validator. The schema integration is covered
        # in StateRoutesTest.test_narration_put_*.
        async with await self._client() as c:
            await c.put(
                "/api/state/sportsrecapped/shotlist/x",
                json={"slug": "x", "windows": []},
                headers={"Authorization": "Bearer expected-token"},
            )
            r = await c.get(
                "/api/state/sportsrecapped/shotlist/x",
                headers={"Authorization": "Bearer expected-token"},
            )
            self.assertEqual(r.status_code, 200, r.text)

    async def test_503_when_token_not_configured(self) -> None:
        """K_SERVICE unset + YTFACTORY_AGENT_TOKEN not set → 503."""
        prev = os.environ.pop("YTFACTORY_AGENT_TOKEN", None)
        try:
            async with await self._client() as c:
                r = await c.get("/api/state/sportsrecapped/shotlist/x")
            self.assertEqual(r.status_code, 503)
            self.assertIn("YTFACTORY_AGENT_TOKEN", r.json()["detail"])
        finally:
            if prev is not None:
                os.environ["YTFACTORY_AGENT_TOKEN"] = prev

    async def test_invalid_channel_chars_400(self) -> None:
        """Channel name with chars outside [A-Za-z0-9_.-] → 400."""
        os.environ["YTFACTORY_AGENT_TOKEN"] = "expected-token"
        async with await self._client() as c:
            r = await c.get(
                "/api/state/chan@bad/shotlist/slug",
                headers={"Authorization": "Bearer expected-token"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertIn("invalid channel", r.json()["detail"])


class GcsClientDirectTest(unittest.TestCase):
    """Cover the _gcs_client() function body (lines 100-101)."""

    def test_returns_storage_client(self):
        import sys
        from control.routes.state_routes import _gcs_client
        mock_client_instance = MagicMock()
        mock_storage = MagicMock()
        mock_storage.Client.return_value = mock_client_instance
        with patch.dict("sys.modules", {"google.cloud.storage": mock_storage}):
            result = _gcs_client()
        mock_storage.Client.assert_called_once()
        self.assertEqual(result, mock_client_instance)


class StateListNonJsonBlobTest(unittest.IsolatedAsyncioTestCase):
    """Cover the `continue` on line 234 for non-.json blobs in list."""

    def setUp(self) -> None:
        self._prev_k_service = os.environ.get("K_SERVICE")
        os.environ["K_SERVICE"] = "test-suite"
        self._store: dict[str, bytes] = {}
        self._patch = patch(
            "control.routes.state_routes._gcs_client",
            return_value=_FakeClient(self._store),
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        if self._prev_k_service is None:
            os.environ.pop("K_SERVICE", None)
        else:
            os.environ["K_SERVICE"] = self._prev_k_service

    async def test_list_skips_non_json_blobs(self) -> None:
        """A blob without .json extension in the prefix is skipped (line 234 `continue`)."""
        # Inject a non-.json blob directly into the store at the right prefix
        self._store["cosmosdecoded/shotlist/readme.txt"] = b"not json"
        self._store["cosmosdecoded/shotlist/real-slug.json"] = b'{"slug": "real-slug"}'
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/api/state/cosmosdecoded/shotlist")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("real-slug", body["slugs"])
        self.assertNotIn("readme", body["slugs"])


if __name__ == "__main__":
    unittest.main()
