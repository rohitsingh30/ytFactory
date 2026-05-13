"""Audit Q2.37 — /api/voices/{voice_id}/sample.wav returns 202 on
cache miss instead of synthesizing in the request handler.

Tests exercise: web/server.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import httpx

from web import server as _server


def _make_client():
    transport = httpx.ASGITransport(app=_server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestVoiceSampleQ237(unittest.IsolatedAsyncioTestCase):
    """Audit Q2.37 — pre-fix this synthesised inside the request
    handler on cache miss. Kokoro takes 5-15 s on Apple Silicon
    and much longer on Cloud Run CPU; the browser audio element
    hung that long before any byte arrived. Now: on cache miss
    return 202 with a 2-second Retry-After and kick off background
    synth.
    """

    async def asyncSetUp(self):
        # Find a real voice id from the global VOICES list. Use the
        # first one — its existence is the only contract we need.
        if not _server.VOICES:
            self.skipTest("VOICES list empty — no voice to test against")
        self.voice_id = _server.VOICES[0]["id"]
        self.sample_path = _server.VOICE_SAMPLES_DIR / f"{self.voice_id}.wav"
        # Ensure cache miss for this test.
        self.sample_path.unlink(missing_ok=True)
        # Clear any pending markers.
        _server._VOICE_SAMPLE_PENDING.discard(self.voice_id)

    async def asyncTearDown(self):
        # Clean up so the test doesn't leave state for other tests.
        _server._VOICE_SAMPLE_PENDING.discard(self.voice_id)
        # Best-effort wipe of any sample we may have triggered.
        self.sample_path.unlink(missing_ok=True)

    async def test_unknown_voice_returns_404(self):
        with patch.object(_server, "AGENT_TOKEN", "test-token"):
            async with _make_client() as c:
                r = await c.get(
                    "/api/voices/no-such-voice/sample.wav",
                    headers={"Authorization": "Bearer test-token"},
                )
        self.assertEqual(r.status_code, 404)

    async def test_cache_miss_returns_202_with_retry_after(self):
        # Patch the background synth so it doesn't actually invoke Kokoro.
        async def _fake_warm(*_args, **_kw):
            pass

        with patch.object(_server, "AGENT_TOKEN", "test-token"), \
             patch.object(_server, "_warm_voice_sample", new=_fake_warm):
            async with _make_client() as c:
                r = await c.get(
                    f"/api/voices/{self.voice_id}/sample.wav",
                    headers={"Authorization": "Bearer test-token"},
                )
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.headers.get("Retry-After"), "3")
        body = r.json()
        self.assertIn("synthesizing", body["detail"])
        self.assertIn(self.voice_id, body["detail"])

    async def test_second_request_does_not_re_enqueue(self):
        # First request kicks off; second request should NOT trigger a
        # second background task while the first is in flight. We
        # observe this by counting how many times _warm_voice_sample
        # was scheduled.
        call_count = 0

        async def _fake_warm(*_args, **_kw):
            nonlocal call_count
            call_count += 1
            # Don't actually finish — leave the pending marker set so
            # the second request sees in-flight state.
            import asyncio
            await asyncio.sleep(0.5)

        with patch.object(_server, "AGENT_TOKEN", "test-token"), \
             patch.object(_server, "_warm_voice_sample", new=_fake_warm):
            async with _make_client() as c:
                r1 = await c.get(
                    f"/api/voices/{self.voice_id}/sample.wav",
                    headers={"Authorization": "Bearer test-token"},
                )
                r2 = await c.get(
                    f"/api/voices/{self.voice_id}/sample.wav",
                    headers={"Authorization": "Bearer test-token"},
                )
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 202)
        # Only one background task should have been kicked off.
        self.assertEqual(call_count, 1)


    async def test_cache_hit_returns_file(self):
        # Pre-populate the sample to exercise the cache-hit branch.
        self.sample_path.parent.mkdir(parents=True, exist_ok=True)
        # 16-byte stub so FileResponse has something to serve.
        self.sample_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        with patch.object(_server, "AGENT_TOKEN", "test-token"):
            async with _make_client() as c:
                r = await c.get(
                    f"/api/voices/{self.voice_id}/sample.wav",
                    headers={"Authorization": "Bearer test-token"},
                )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/wav")


if __name__ == "__main__":
    unittest.main()
