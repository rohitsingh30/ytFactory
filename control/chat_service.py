"""Chat service — Azure OpenAI conversation that produces a ShortProposal.

Ported from ~/trading/app/services/chat_service.py. Same architectural shape:
- Stateful sessions, in-memory by default (Firestore-backed in production).
- System prompt asks the model to emit a JSON block once it has enough info.
- Proposal extraction is regex + balanced-brace, identical to trading's pattern.

Differences from trading:
- Schema is `short_proposal` (channel/format/topic/source/length) not
  `strategy_proposal` (entry/exit/timeframe/risk).
- No image/chart input — Shorts are described in text.
- Session backend is pluggable (memory / Firestore) like the queue.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from control.core.schema import ShortProposal

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the ytFactory chat assistant. Your job is to take a user's idea
for a YouTube Short and turn it into a concrete render proposal.

The system can produce these channel formats:

- **mystoriesanimated** — Reddit AITA / TIFU / TIL stories, animated 2D crayon style.
  Formats: `animated` (default), `text` (Reddit text overlay), `cooking` (text
  overlay over silent cooking footage), `cliffhanger` (Part-1 ends mid-story
  and Part-2 auto-publishes once the channel hits a sub threshold).
- **sportstoriesanimated** — Football moments, Tifo line-art with REAL broadcast
  cut-ins at the climax. Formats: `animated`, `ranked` (Top-5 countdown).
- **mahabharathindi** — Mahabharat episodes, narrated in Hindi, Amar Chitra
  Katha comic-book aesthetic. Format: `animated`.

Your conversation goal:
1. Ask 1-3 short clarifying questions if the idea is fuzzy. Keep it concise.
2. Once you know channel + topic + (optionally) source, propose by including
   EXACTLY this JSON block in your response:

```json
{"type":"short_proposal","channel":"...","format":"...","topic":"...",
 "source_kind":"...","source_ref":"...","length_s":55,"notes":"..."}
```

Rules:
- Allowed `channel` values: mystoriesanimated | sportstoriesanimated | mahabharathindi | auto
- Allowed `format`: animated | text | cooking | cliffhanger | ranked | auto
- `source_kind`: reddit_url | wikipedia_topic | user_text | youtube_video | auto
- `source_ref`: the URL / wikipedia topic / pasted text. May be null if `source_kind=auto`.
- `length_s`: 50–60 (default 55). Shorts under 50s perform worse in 2026.
- `notes`: any extra direction worth preserving (visual cues, character names,
  language preference, etc.).
- Be conversational and brief. Don't lecture about the platform.
- DO NOT propose until channel + topic are clear. Default `format` to `animated`
  if the user didn't say otherwise.
- After proposing, if the user wants edits, discuss and propose again.
"""


_MAX_TURNS_PER_SESSION = 50
_SESSION_TTL_S = 3600  # 1 hour


@dataclass
class _Session:
    messages: list[dict]
    proposal: Optional[ShortProposal] = None
    created_at: float = 0.0
    last_active: float = 0.0


@dataclass
class ChatResult:
    response: str
    proposal: Optional[ShortProposal] = None


class ChatService:
    """In-memory chat session manager. Replace with Firestore when deploying."""

    def __init__(self) -> None:
        self._client = self._build_client()
        self._model: str = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o-mini")
        self._sessions: dict[str, _Session] = {}

    @staticmethod
    def _build_client():
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
        key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
        if not endpoint or not key:
            return None
        from openai import AzureOpenAI  # noqa: PLC0415 — lazy
        return AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=key,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        )

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.last_active > _SESSION_TTL_S]
        for k in expired:
            del self._sessions[k]

    def _get_session(self, session_id: str) -> _Session:
        self._cleanup_expired()
        if session_id not in self._sessions:
            self._sessions[session_id] = _Session(messages=[], created_at=time.time(), last_active=time.time())
        sess = self._sessions[session_id]
        sess.last_active = time.time()
        return sess

    def get_proposal(self, session_id: str) -> Optional[ShortProposal]:
        self._cleanup_expired()
        sess = self._sessions.get(session_id)
        return sess.proposal if sess else None

    def clear_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def chat(self, session_id: str, message: str) -> ChatResult:
        if not self._client:
            return ChatResult(response=_unconfigured_message())

        sess = self._get_session(session_id)

        if len(sess.messages) >= _MAX_TURNS_PER_SESSION * 2:
            return ChatResult(response="Session limit reached. Start a new chat to continue.")

        sess.messages.append({"role": "user", "content": message})

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + sess.messages,
                max_completion_tokens=800,
            )
            assistant_text = resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            logger.exception("Azure OpenAI chat error")
            sess.messages.pop()  # roll back the user turn so retries don't double-charge
            detail = str(e)
            if "content_filter" in detail.lower():
                return ChatResult(response="The message was filtered by content policy. Please rephrase.")
            return ChatResult(response=f"Chat error: {detail[:200]}")

        # Best-effort spend tracking (real billing comes from Azure; this just
        # feeds the daily cap circuit breaker in control/rate_limit.py).
        try:
            from control.core import rate_limit  # noqa: PLC0415 — avoid import cycle
            usage = getattr(resp, "usage", None)
            if usage is not None:
                rate_limit.record_token_usage(
                    int(getattr(usage, "prompt_tokens", 0) or 0),
                    int(getattr(usage, "completion_tokens", 0) or 0),
                )
        except Exception:  # noqa: BLE001 — telemetry must never fail the chat
            logger.warning("token-usage tracking failed", exc_info=True)

        sess.messages.append({"role": "assistant", "content": assistant_text})

        proposal = _extract_proposal(assistant_text)
        if proposal:
            sess.proposal = proposal

        return ChatResult(response=assistant_text, proposal=proposal)


def _unconfigured_message() -> str:
    return (
        "Chat AI not configured. Set AZURE_OPENAI_ENDPOINT and "
        "AZURE_OPENAI_API_KEY environment variables to enable chat-driven "
        "Short generation."
    )


def _extract_proposal(text: str) -> Optional[ShortProposal]:
    """Pull a {"type":"short_proposal", ...} JSON block out of the assistant turn."""
    start = text.find('{"type":"short_proposal"')
    if start == -1:
        # Tolerate whitespace around the colon: '"type": "short_proposal"'.
        marker = text.find('"type": "short_proposal"')
        if marker == -1:
            return None
        start = text.rfind("{", 0, marker)
        if start == -1:
            return None

    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        logger.warning("short_proposal JSON parse failed: %r", text[start:end][:200])
        return None
    if data.get("type") != "short_proposal":
        return None

    try:
        return ShortProposal(
            channel=data.get("channel", "auto"),
            format=data.get("format", "auto"),
            topic=data.get("topic", ""),
            source_kind=data.get("source_kind", "auto"),
            source_ref=data.get("source_ref") or None,
            length_s=int(data.get("length_s", 55)),
            notes=data.get("notes", ""),
        )
    except Exception:  # noqa: BLE001
        logger.warning("short_proposal validation failed: %r", data)
        return None


def new_session_id() -> str:
    return str(uuid.uuid4())
