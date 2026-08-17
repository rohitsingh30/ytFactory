"""Context — the artifact bus + injected cross-cutting services.

Stages read/write artifacts here, and receive telemetry / storage / config from here
too — so no stage reaches for an import-time global (this is what kills the
service-locator anti-pattern the old pipeline had). Everything a stage needs to touch
the outside world arrives through this one object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pipeline.core.contracts import RenderSpec


@runtime_checkable
class Telemetry(Protocol):
    """Injected telemetry. ``stage`` is a context manager emitting start/end/failed."""

    def stage(self, name: str) -> Any: ...
    def event(self, name: str, **meta: Any) -> None: ...


@runtime_checkable
class Storage(Protocol):
    """Injected artifact I/O. The ONLY place GCS/local paths are built."""

    def put(self, local: Path, key: str) -> str: ...
    def get(self, key: str, local: Path) -> Path: ...


@runtime_checkable
class Config(Protocol):
    """Injected configuration. ALL service URLs / paths / constants come from here —
    stage code contains zero hardcoded URLs or paths."""

    def service_url(self, name: str) -> str: ...          # e.g. "tts_chatterbox" -> https://...
    def voice_ref_path(self, voice_id: str) -> Path: ...   # resolves voice_refs root
    def channel_yaml(self, channel: str) -> dict[str, Any]: ...


@dataclass
class Context:
    """Carries one render's spec, injected services, and the produced-artifact bus."""

    spec: RenderSpec
    telemetry: Telemetry
    storage: Storage
    config: Config
    work_dir: Path
    _artifacts: dict[str, Any] = field(default_factory=dict)

    def get(self, kind: str) -> Any:
        if kind not in self._artifacts:
            raise KeyError(
                f"Context.get: artifact '{kind}' not produced yet — check stage order"
            )
        return self._artifacts[kind]

    def put(self, kind: str, artifact: Any) -> None:
        self._artifacts[kind] = artifact

    def has(self, kind: str) -> bool:
        return kind in self._artifacts
