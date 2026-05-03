"""Cross-side type envelopes used by control plane, light workers, and the laptop agent.

All values are JSON-safe so they round-trip through Firestore + HTTP.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskKind(str, Enum):
    # Light (cloud-side) tasks
    SCRIPT = "script"
    CRITIC = "critic"
    CAST = "cast"
    REWRITE = "rewrite"
    PULL_STORY = "pull_story"
    WIKI_RESEARCH = "wiki_research"
    YOUTUBE_UPLOAD = "youtube_upload"
    # Heavy (laptop-side) tasks
    IMAGES = "images"
    TTS = "tts"
    ASR = "asr"
    COMPOSE = "compose"
    FOOTAGE = "footage"
    # Smoke / no-op
    NOOP = "noop"


HEAVY_KINDS: frozenset[str] = frozenset(
    {TaskKind.IMAGES, TaskKind.TTS, TaskKind.ASR, TaskKind.COMPOSE, TaskKind.FOOTAGE}
)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


class TaskEnvelope(BaseModel):
    """A single unit of work in the queue."""

    task_id: str
    job_id: str
    kind: TaskKind
    payload: dict[str, Any] = Field(default_factory=dict)
    input_uri: str | None = None
    output_uri: str | None = None

    status: TaskStatus = TaskStatus.QUEUED
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3

    error: str | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class JobEnvelope(BaseModel):
    """A user-facing render job; spawns one or more tasks."""

    job_id: str
    channel: str
    proposal: dict[str, Any]
    owner_uid: str | None = None  # Firebase UID; None for anonymous
    status: Literal["pending", "running", "done", "failed"] = "pending"
    short_uri: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AgentResources(BaseModel):
    """Snapshot of the laptop's current capacity. Sent on every heartbeat."""

    agent_id: str
    mlx_free_pct: float = 100.0
    gpu_mem_pct: float = 0.0
    kokoro_warm: bool = False
    mflux_warm: bool = False
    whisper_warm: bool = False
    f5_warm: bool = False
    on_battery: bool = False
    local_queue_depth: int = 0


class HeartbeatRequest(BaseModel):
    resources: AgentResources


class HeartbeatResponse(BaseModel):
    ok: bool = True
    server_time: datetime = Field(default_factory=_utcnow)


class LeaseRequest(BaseModel):
    agent_id: str
    caps: list[TaskKind]
    lease_ttl_s: int = 300  # 5 min default


class LeaseResponse(BaseModel):
    """One leased task or instructions to wait."""

    task: TaskEnvelope | None = None
    wait_s: int = 30


class AckRequest(BaseModel):
    agent_id: str
    status: Literal["ok", "error"]
    output_uri: str | None = None
    error: str | None = None


class AckResponse(BaseModel):
    ok: bool = True
