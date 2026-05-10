"""100% coverage for control/routes/niche_specs_routes.py."""
from __future__ import annotations

import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI

from tests._helpers import PROJECT_ROOT  # noqa: F401
import control.routes.niche_specs_routes as nsr
from control.routes.niche_specs_routes import router
from pipeline.niche_specs import NicheDoc


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _doc(key: str = "test_niche", label: str = "Test Niche", **overrides) -> NicheDoc:
    data = {
        "key": key,
        "label": label,
        "description": "A compact description for tests.",
        "prompt_style_guide": "Punchy and clear.",
        "length_kind": "short",
        "voice": "sarah",
        "format": "animated",
        "source_kind": "manual",
        "source_ref": None,
        "hook_template": "Hook {topic}",
        "closer_template": "Comment below.",
        "image_style": "Warm sketch art.",
        "music_bed": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "created_by": "user",
    }
    data.update(overrides)
    return NicheDoc(**data)


class NicheSpecsRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_unknown_channel_returns_404(self) -> None:
        with patch.object(nsr.customization, "get_channel", return_value=None):
            async with await self._client() as c:
                r = await c.get("/api/channels/missing/niches")
        self.assertEqual(r.status_code, 404)

    async def test_list_niches_route(self) -> None:
        with patch.object(nsr.customization, "get_channel", return_value=object()), \
             patch.object(nsr, "list_niches", return_value=[_doc("b", "Bee"), _doc("a", "Aye")]):
            async with await self._client() as c:
                r = await c.get("/api/channels/chan/niches")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([n["key"] for n in r.json()["niches"]], ["b", "a"])

    async def test_get_niche_found_and_missing(self) -> None:
        with patch.object(nsr.customization, "get_channel", return_value=object()), \
             patch.object(nsr, "get_niche", side_effect=[_doc("one"), None]):
            async with await self._client() as c:
                ok = await c.get("/api/channels/chan/niches/one")
                miss = await c.get("/api/channels/chan/niches/two")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["key"], "one")
        self.assertEqual(miss.status_code, 404)

    async def test_create_niche_conflict_and_success(self) -> None:
        body = _doc("new_one").model_dump(mode="json")
        with patch.object(nsr.customization, "get_channel", return_value=object()), \
             patch.object(nsr, "get_niche", side_effect=[_doc("new_one"), None]), \
             patch.object(nsr, "save_niche", side_effect=lambda _ch, doc: doc):
            async with await self._client() as c:
                conflict = await c.post("/api/channels/chan/niches", json=body)
                ok = await c.post("/api/channels/chan/niches", json=body)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["key"], "new_one")

    async def test_update_key_mismatch_missing_and_success_preserves_provenance(self) -> None:
        old = _doc("same_key", created_at="old-time", created_by="backfill")
        body = _doc("same_key", created_at="new-time", created_by="user").model_dump(mode="json")
        mismatch = _doc("body_key").model_dump(mode="json")
        saved_docs: list[NicheDoc] = []

        def save(_channel: str, doc: NicheDoc) -> NicheDoc:
            saved_docs.append(doc)
            return doc

        with patch.object(nsr.customization, "get_channel", return_value=object()), \
             patch.object(nsr, "get_niche", side_effect=[None, old]), \
             patch.object(nsr, "save_niche", side_effect=save):
            async with await self._client() as c:
                bad = await c.put("/api/channels/chan/niches/path_key", json=mismatch)
                missing = await c.put("/api/channels/chan/niches/same_key", json=body)
                ok = await c.put("/api/channels/chan/niches/same_key", json=body)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(saved_docs[0].created_at, "old-time")
        self.assertEqual(saved_docs[0].created_by, "backfill")

    async def test_delete_niche_missing_and_success(self) -> None:
        with patch.object(nsr.customization, "get_channel", return_value=object()), \
             patch.object(nsr, "delete_niche", side_effect=[False, True]):
            async with await self._client() as c:
                missing = await c.delete("/api/channels/chan/niches/old")
                ok = await c.delete("/api/channels/chan/niches/old")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"deleted": "old"})

    async def test_draft_route_validates_and_falls_back(self) -> None:
        channel = SimpleNamespace(language="en", default_format="animated", label="Chan", tagline="Tag")
        with patch.object(nsr.customization, "get_channel", return_value=channel), \
             patch.object(nsr, "_draft_via_azure", return_value=None), \
             patch.dict(os.environ, {}, clear=True):
            async with await self._client() as c:
                empty = await c.post("/api/channels/chan/niches/draft", json={"description": "  "})
                long = await c.post("/api/channels/chan/niches/draft", json={"description": "x" * 801})
                ok = await c.post("/api/channels/chan/niches/draft", json={"description": "new cooking niche"})
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(long.status_code, 400)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertFalse(ok.json()["ai_configured"])
        self.assertEqual(ok.json()["draft"]["key"], "new_cooking_niche")


class NicheSpecsHelpersTest(unittest.TestCase):
    def test_extract_json_object_variants(self) -> None:
        self.assertEqual(nsr._extract_json_obj('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(nsr._extract_json_obj('[1, 2]'))
        self.assertEqual(nsr._extract_json_obj('prefix {"b": 2} suffix'), {"b": 2})
        self.assertIsNone(nsr._extract_json_obj('prefix {bad json} suffix'))
        self.assertIsNone(nsr._extract_json_obj('no json here'))

    def test_draft_stub_uses_channel_defaults_and_fallbacks(self) -> None:
        hindi = SimpleNamespace(language="hi", default_format="footage_only")
        invalid = SimpleNamespace(language="en", default_format="made_up")
        with patch.object(nsr.customization, "get_channel", side_effect=[hindi, invalid, None]):
            hi_doc = nsr._draft_stub("hindutavaanimated", "gita lesson")
            invalid_doc = nsr._draft_stub("bad", "odd format")
            none_doc = nsr._draft_stub("none", "!!!")
        self.assertEqual(hi_doc.voice, "hf_alpha")
        self.assertEqual(hi_doc.length_kind, "long")
        self.assertEqual(invalid_doc.format, "animated")
        self.assertEqual(none_doc.key, "niche")

    def test_draft_via_azure_missing_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(nsr._draft_via_azure("chan", "desc"))

    def test_draft_via_azure_success_without_channel_context(self) -> None:
        raw = _doc("ai_one", created_by="ai_chat").model_dump(mode="json")
        resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)))])
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = resp
        fake_cls = MagicMock(return_value=fake_client)
        fake_openai = types.SimpleNamespace(AzureOpenAI=fake_cls)
        with patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": "https://azure", "AZURE_OPENAI_API_KEY": "key"}, clear=True), \
             patch.dict(sys.modules, {"openai": fake_openai}), \
             patch.object(nsr.customization, "get_channel", return_value=None):
            doc = nsr._draft_via_azure("chan", "desc")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc.key, "ai_one")
        fake_cls.assert_called_once()

    def test_draft_via_azure_exception_non_json_and_invalid_schema(self) -> None:
        with patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": "x", "AZURE_OPENAI_API_KEY": "y"}, clear=True), \
             patch.dict(sys.modules, {"openai": types.SimpleNamespace(AzureOpenAI=MagicMock(side_effect=RuntimeError("boom")))}):
            self.assertIsNone(nsr._draft_via_azure("chan", "desc"))

        client = MagicMock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"key": "bad key"}'))]),
        ]
        fake_openai = types.SimpleNamespace(AzureOpenAI=MagicMock(return_value=client))
        with patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": "x", "AZURE_OPENAI_API_KEY": "y"}, clear=True), \
             patch.dict(sys.modules, {"openai": fake_openai}), \
             patch.object(nsr.customization, "get_channel", return_value=SimpleNamespace(label="L", tagline="T", language="en", default_format="animated")):
            self.assertIsNone(nsr._draft_via_azure("chan", "desc"))
            self.assertIsNone(nsr._draft_via_azure("chan", "desc"))


if __name__ == "__main__":
    unittest.main()
