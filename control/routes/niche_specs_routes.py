"""Per-channel niche-JSON REST endpoints.

A "niche" here is a JSON document with a fixed schema (see
``pipeline.niche_specs.NicheDoc``). It describes a Short format
authoritatively — used by the channel page to list addable formats and
by the create wizard as the source of "what kinds of Short can this
channel produce".

Endpoints:

    GET    /api/channels/{ch}/niches          — list NicheDocs
    GET    /api/channels/{ch}/niches/{key}    — single NicheDoc
    POST   /api/channels/{ch}/niches          — create (body: NicheDoc)
    PUT    /api/channels/{ch}/niches/{key}    — update (body: NicheDoc)
    DELETE /api/channels/{ch}/niches/{key}    — delete
    POST   /api/channels/{ch}/niches/draft    — AI pre-fill from one-line
                                                description, returns a
                                                NicheDoc the user can
                                                review + save (no write
                                                happens here).

The draft endpoint uses Azure OpenAI when configured. Without Azure it
falls back to a deterministic stub so the dev workflow still works.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pipeline.schemas import customization
from pipeline.niche_specs import (
    NicheDoc,
    delete_niche,
    get_niche,
    list_niches,
    save_niche,
    slugify_label,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels")


def _ensure_channel(channel_key: str) -> None:
    if not customization.get_channel(channel_key):
        raise HTTPException(404, f"unknown channel: {channel_key!r}")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/{channel}/niches")
async def list_niches_route(channel: str) -> dict:
    _ensure_channel(channel)
    docs = list_niches(channel)
    return {"niches": [d.model_dump(mode="json") for d in docs]}


@router.get("/{channel}/niches/{key}")
async def get_niche_route(channel: str, key: str) -> dict:
    _ensure_channel(channel)
    doc = get_niche(channel, key)
    if not doc:
        raise HTTPException(404, f"niche not found: {channel}/{key}")
    return doc.model_dump(mode="json")


@router.post("/{channel}/niches")
async def create_niche_route(channel: str, doc: NicheDoc) -> dict:
    _ensure_channel(channel)
    if get_niche(channel, doc.key):
        raise HTTPException(409, f"niche already exists: {channel}/{doc.key}")
    saved = save_niche(channel, doc)
    return saved.model_dump(mode="json")


@router.put("/{channel}/niches/{key}")
async def update_niche_route(channel: str, key: str, doc: NicheDoc) -> dict:
    _ensure_channel(channel)
    if doc.key != key:
        raise HTTPException(400, f"path key {key!r} does not match body key {doc.key!r}")
    existing = get_niche(channel, key)
    if not existing:
        raise HTTPException(404, f"niche not found: {channel}/{key}")
    # Preserve provenance — the user can rename the label, not the
    # creation timestamp / creator.
    doc.created_at = existing.created_at
    doc.created_by = existing.created_by
    saved = save_niche(channel, doc)
    return saved.model_dump(mode="json")


@router.delete("/{channel}/niches/{key}")
async def delete_niche_route(channel: str, key: str) -> dict:
    _ensure_channel(channel)
    if not delete_niche(channel, key):
        raise HTTPException(404, f"niche not found: {channel}/{key}")
    return {"deleted": key}


# ---------------------------------------------------------------------------
# AI-assisted draft (no write — caller still POSTs the result)
# ---------------------------------------------------------------------------


class DraftRequest(BaseModel):
    description: str


_DRAFT_SYSTEM_PROMPT = """You design Short-video "niches" — concrete JSON
specs the renderer can consume. Given a one-line description from the
user, return a single JSON object (no prose, no code fences) with EXACTLY
these fields:

  key                  short snake_case slug, 3-40 chars, [a-z0-9_] only
  label                human title, 2-60 chars, e.g. "AITA Cooking"
  description          one-sentence blurb, 60-200 chars
  prompt_style_guide   2-4 sentence voice/tone guide for the LLM script writer
  length_kind          "short" for ≤90s vertical Shorts, "long" for multi-minute
                       horizontal long-form. Default "short".
  voice                voice id — "sarah" (en female default), "michael"
                       (en male), "hf_alpha" (hi female). Default "sarah".
  format               one of: animated, text, cooking, footage, split_screen,
                       rhyme, footage_only, long_form, sports_doc
  source_kind          one of: reddit, wikipedia, manual, x_twitter, youtube, rss
  source_ref           specific source pointer if applicable (e.g.
                       "r/AmItheAsshole" or "Wikipedia:List_of_common_misconceptions")
                       or null
  hook_template        one-line hook pattern with {placeholders} the writer fills
  closer_template      one-line closer / CTA pattern
  image_style          short visual aesthetic guide (e.g. "2D crayon, warm palette")
  music_bed            optional named bed (e.g. "ambient_low") or null

Pick sensible defaults from the channel context. Output the JSON object
ONLY, no markdown."""


def _draft_via_azure(channel_key: str, description: str) -> Optional[NicheDoc]:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
    if not endpoint or not api_key:
        return None
    model = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o-mini")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

    try:
        from openai import AzureOpenAI  # noqa: PLC0415

        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        ch = customization.get_channel(channel_key)
        ch_ctx = (
            f"Channel: {ch.label} ({channel_key}) — {ch.tagline}. "
            f"Language: {ch.language}. Default format: {ch.default_format}. "
            if ch
            else f"Channel: {channel_key}. "
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": f"{ch_ctx}\nNiche description: {description}"},
            ],
            max_completion_tokens=600,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("Azure OpenAI draft error")
        return None

    raw = _extract_json_obj(text)
    if not raw:
        return None
    try:
        return NicheDoc(**{**raw, "created_by": "ai_chat"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI draft failed schema validation: %s\nraw=%s", exc, text[:300])
        return None


def _extract_json_obj(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        # Last-ditch: pluck the first balanced {...} block.
        depth = 0
        start = -1
        for i, c in enumerate(text):
            if c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        obj = json.loads(text[start : i + 1])
                        return obj if isinstance(obj, dict) else None
                    except Exception:  # noqa: BLE001
                        return None
        return None


def _draft_stub(channel_key: str, description: str) -> NicheDoc:
    """Deterministic offline draft so dev works without Azure.

    Pulls every default from the channel registry — the user is expected
    to tweak in the form before saving.
    """
    ch = customization.get_channel(channel_key)
    label_seed = description.strip().splitlines()[0][:60] or "New Niche"
    label = label_seed[:1].upper() + label_seed[1:]
    key = slugify_label(label)
    fmt = (ch.default_format if ch else "animated") or "animated"
    voice = "hf_alpha" if (ch and ch.language and ch.language.startswith("hi")) else "sarah"
    fmt_validated = fmt if fmt in NicheDoc.model_fields["format"].annotation.__args__ else "animated"  # type: ignore[attr-defined]
    length_kind = "long" if fmt_validated in ("footage_only", "long_form", "sports_doc") else "short"
    return NicheDoc(
        key=key,
        label=label,
        description=description.strip()[:380],
        prompt_style_guide=(
            "Concise, hook-first narration. Match the channel's established voice "
            "(see config.yaml) and keep sentences punchy enough for Shorts pacing."
        ),
        length_kind=length_kind,  # type: ignore[arg-type]
        voice=voice,
        format=fmt_validated,  # type: ignore[arg-type]
        source_kind="manual",
        source_ref=None,
        hook_template="{hook}",
        closer_template="What would you do? Comment + Subscribe.",
        image_style="",
        music_bed=None,
        created_by="ai_chat",
    )


@router.post("/{channel}/niches/draft")
async def draft_niche_route(channel: str, req: DraftRequest) -> dict:
    """Pre-fill a NicheDoc from a one-line description. Does NOT save."""
    _ensure_channel(channel)
    desc = (req.description or "").strip()
    if not desc:
        raise HTTPException(400, "description is required")
    if len(desc) > 800:
        raise HTTPException(400, "description too long (max 800 chars)")

    doc = _draft_via_azure(channel, desc) or _draft_stub(channel, desc)
    return {
        "draft": doc.model_dump(mode="json"),
        "ai_configured": bool(os.environ.get("AZURE_OPENAI_API_KEY")),
    }
