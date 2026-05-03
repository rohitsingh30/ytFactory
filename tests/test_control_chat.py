"""Tests for the chat service + chat routes.

Azure OpenAI is mocked; we verify:
- proposal extraction from a synthetic assistant reply
- /api/chat/confirm enqueues a PULL_STORY task with the proposal payload
- /api/chat returns a graceful "not configured" message when env is missing
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

# In-memory queue + a stub Azure client BEFORE importing control.*
os.environ["YTFACTORY_AGENT_TOKEN"] = "test-token"
os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

import httpx  # noqa: E402

from control import chat_routes, chat_service as cs_module  # noqa: E402
from control.chat_service import _extract_proposal, ChatResult  # noqa: E402
from control.queue import get_queue, reset_queue  # noqa: E402
from shared.schema import ShortProposal, TaskKind, TaskStatus  # noqa: E402


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(chat_routes.router)
    return app


SAMPLE_REPLY = """Got it — sounds like the Aguero 2012 title-winner.

Here's a proposal:

```json
{"type":"short_proposal","channel":"sportstoriesanimated","format":"animated",
 "topic":"Aguero's stoppage-time goal vs QPR, May 2012","source_kind":"wikipedia_topic",
 "source_ref":"Sergio Agüero","length_s":55,"notes":"Use real broadcast cut-in at the goal moment"}
```

Confirm to render."""


class ExtractProposalTest(unittest.TestCase):
    def test_extracts_from_fenced_json(self):
        proposal = _extract_proposal(SAMPLE_REPLY)
        self.assertIsNotNone(proposal)
        assert proposal is not None  # mypy
        self.assertEqual(proposal.channel, "sportstoriesanimated")
        self.assertEqual(proposal.format, "animated")
        self.assertEqual(proposal.length_s, 55)
        self.assertEqual(proposal.source_kind, "wikipedia_topic")
        self.assertEqual(proposal.source_ref, "Sergio Agüero")

    def test_no_proposal_returns_none(self):
        self.assertIsNone(_extract_proposal("Just chatting, no JSON yet."))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(_extract_proposal('{"type":"short_proposal","channel":'))

    def test_tolerates_whitespace_after_colon(self):
        text = '{"type": "short_proposal","channel":"mahabharathindi","topic":"Karna","format":"animated"}'
        proposal = _extract_proposal(text)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.channel, "mahabharathindi")


class ChatRoutesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()

    async def test_chat_returns_not_configured_when_azure_missing(self) -> None:
        # Force chat_service to look unconfigured.
        original = chat_routes.chat_service
        try:
            chat_routes.chat_service = cs_module.ChatService()
            chat_routes.chat_service._client = None  # noqa: SLF001
            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/chat", json={"message": "hi"})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertFalse(body["configured"])
            self.assertIn("not configured", body["response"].lower())
            self.assertTrue(body["session_id"])
        finally:
            chat_routes.chat_service = original

    async def test_chat_round_trip_with_mocked_azure(self) -> None:
        # Mock the Azure client to return our canned SAMPLE_REPLY.
        original = chat_routes.chat_service
        try:
            svc = cs_module.ChatService()
            fake_client = MagicMock()
            fake_resp = MagicMock()
            fake_resp.choices = [MagicMock()]
            fake_resp.choices[0].message.content = SAMPLE_REPLY
            fake_client.chat.completions.create = MagicMock(return_value=fake_resp)
            svc._client = fake_client  # noqa: SLF001
            chat_routes.chat_service = svc

            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/chat", json={"message": "Aguero short pls"})

            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["configured"])
            self.assertIsNotNone(body["proposal"])
            self.assertEqual(body["proposal"]["channel"], "sportstoriesanimated")
            session_id = body["session_id"]

            # Confirm → enqueues a PULL_STORY task.
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r2 = await client.post("/api/chat/confirm", json={"session_id": session_id})
            self.assertEqual(r2.status_code, 200, r2.text)
            confirm = r2.json()
            self.assertTrue(confirm["job_id"])
            self.assertTrue(confirm["task_id"])

            # Task is in the queue with the right shape.
            q = get_queue()
            task = q.get(confirm["task_id"])
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.kind, TaskKind.PULL_STORY)
            self.assertEqual(task.status, TaskStatus.QUEUED)
            self.assertEqual(task.job_id, confirm["job_id"])
            self.assertEqual(task.payload["channel"], "sportstoriesanimated")
            self.assertEqual(task.payload["topic"], "Aguero's stoppage-time goal vs QPR, May 2012")
        finally:
            chat_routes.chat_service = original

    async def test_confirm_404_when_no_proposal(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/chat/confirm", json={"session_id": "no-such-session"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
