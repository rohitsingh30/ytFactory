"""Typed read/write helpers for the critique-chat messages subcollection.

Schema lives in [`docs/critique_chat.md`](../../docs/critique_chat.md).
This module is the single touchpoint between Python code and the
``critiques/<id>/messages`` Firestore subcollection so a schema
change only requires editing one file.

The runner uses ``add_user_message`` / ``add_agent_message`` /
``add_action_message``; the browser uses the equivalent JS SDK
calls. Field names match exactly so a future Pydantic-on-the-server
contract validator can scan both call sites.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Values for the ``role`` field. Anything outside this set is rejected
# at write time so a typo never lands in Firestore.
ROLE_USER = "user"
ROLE_AGENT = "agent"
ROLE_SYSTEM = "system"
_ALLOWED_ROLES = (ROLE_USER, ROLE_AGENT, ROLE_SYSTEM)

# Values for the optional ``action`` field. Front-end renders these as
# coloured chips; treat as a closed enum so the UI doesn't need to
# guess at unknown action types.
ACTION_FILE_READ = "file_read"
ACTION_FILE_EDITED = "file_edited"
ACTION_TEST_ADDED = "test_added"
ACTION_GATE_RUNNING = "gate_running"
ACTION_GATE_PASSED = "gate_passed"
ACTION_GATE_FAILED = "gate_failed"
ACTION_COMMIT_CREATED = "commit_created"
ACTION_PUSH_PENDING = "push_pending"
ACTION_PUSHED = "pushed"
ACTION_AGENT_THINKING = "agent_thinking"
ACTION_AGENT_FINISHED = "agent_finished"

_ALLOWED_ACTIONS = (
    ACTION_FILE_READ,
    ACTION_FILE_EDITED,
    ACTION_TEST_ADDED,
    ACTION_GATE_RUNNING,
    ACTION_GATE_PASSED,
    ACTION_GATE_FAILED,
    ACTION_COMMIT_CREATED,
    ACTION_PUSH_PENDING,
    ACTION_PUSHED,
    ACTION_AGENT_THINKING,
    ACTION_AGENT_FINISHED,
)


@dataclasses.dataclass(frozen=True)
class ChatMessage:
    """One row from ``critiques/<id>/messages``.

    Only the fields the runner reads back are typed here; the
    Firestore doc may carry additional fields the client UI uses
    (e.g. ``client_seq`` for optimistic ordering) that we ignore.
    """
    message_id: str
    role: str
    text: str
    ts: Any  # Firestore timestamp; opaque on the runner side
    action: str | None = None
    action_data: dict | None = None

    @classmethod
    def from_snapshot(cls, snap) -> "ChatMessage":
        d = snap.to_dict() or {}
        return cls(
            message_id=snap.id,
            role=str(d.get("role", "")),
            text=str(d.get("text", "")),
            ts=d.get("ts"),
            action=d.get("action"),
            action_data=d.get("action_data"),
        )


def _critique_messages_collection(client, critique_id: str):
    return client.collection("critiques").document(critique_id).collection("messages")


def add_message(
    client,
    critique_id: str,
    *,
    role: str,
    text: str,
    action: str | None = None,
    action_data: dict | None = None,
) -> str:
    """Append a single message to the critique's chat thread.

    Returns the new doc id so callers can correlate messages with
    their internal state. Raises :class:`ValueError` on invalid role
    or action — refusing the write is the right move because a typo
    here would silently produce a "ghost" message the UI doesn't
    know how to render.
    """
    if role not in _ALLOWED_ROLES:
        raise ValueError(f"role must be one of {_ALLOWED_ROLES}, got {role!r}")
    if action is not None and action not in _ALLOWED_ACTIONS:
        raise ValueError(f"action must be one of {_ALLOWED_ACTIONS}, got {action!r}")
    if not text and action is None:
        raise ValueError("at least one of `text` or `action` must be set")

    from google.cloud import firestore  # noqa: PLC0415
    coll = _critique_messages_collection(client, critique_id)
    doc = {
        "role": role,
        "text": text,
        "ts": firestore.SERVER_TIMESTAMP,
    }
    if action is not None:
        doc["action"] = action
    if action_data is not None:
        doc["action_data"] = action_data

    ref = coll.document()  # auto-id
    ref.set(doc)
    return ref.id


def add_user_message(client, critique_id: str, text: str) -> str:
    """Convenience for the case the laptop runner most often
    creates: a synthetic "user" message (e.g. when re-injecting a
    gate failure as a follow-up turn for the agent to act on).
    """
    return add_message(client, critique_id, role=ROLE_USER, text=text)


def add_agent_message(
    client,
    critique_id: str,
    text: str,
    *,
    action: str | None = None,
    action_data: dict | None = None,
) -> str:
    return add_message(
        client, critique_id, role=ROLE_AGENT,
        text=text, action=action, action_data=action_data,
    )


def add_action_message(
    client,
    critique_id: str,
    action: str,
    *,
    text: str = "",
    action_data: dict | None = None,
) -> str:
    """Pure-action chip with no body text — e.g.
    ``add_action_message(client, cid, ACTION_GATE_RUNNING)``.
    The UI renders this as a chip standalone."""
    return add_message(
        client, critique_id, role=ROLE_AGENT,
        text=text, action=action, action_data=action_data,
    )


def fetch_messages(client, critique_id: str) -> list[ChatMessage]:
    """One-shot read of the entire conversation, oldest-first.

    The runner calls this when assembling the prompt for the next
    agent turn — the agent needs full context, not just the latest
    user message.
    """
    coll = _critique_messages_collection(client, critique_id)
    snaps = coll.order_by("ts").stream()
    return [ChatMessage.from_snapshot(s) for s in snaps]


def messages_to_prompt_history(messages: Iterable[ChatMessage]) -> str:
    """Render the message history into the format the agent prompt
    expects. Plain text, role-prefixed, action-chips inlined so the
    agent sees what it's already done.
    """
    lines: list[str] = []
    for m in messages:
        if m.role == ROLE_USER:
            lines.append(f"USER: {m.text}")
        elif m.role == ROLE_AGENT:
            if m.action and not m.text:
                lines.append(f"[{m.action}]")
            elif m.action:
                lines.append(f"AGENT [{m.action}]: {m.text}")
            else:
                lines.append(f"AGENT: {m.text}")
        elif m.role == ROLE_SYSTEM:
            lines.append(f"SYSTEM: {m.text}")
    return "\n".join(lines)
