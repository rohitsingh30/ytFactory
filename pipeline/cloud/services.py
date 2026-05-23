"""Canonical Cloud Run service catalog — single source of truth.

Everything else in :mod:`pipeline.cloud` (health, cost, deploys, warm,
snapshot) and :mod:`control.routes.cloud_routes` imports from here.
Keeps the service list out of ad-hoc dicts scattered across the
codebase.

Source priority for the URL of each service:

1. ``CLOUDRUN_<KIND>_<NAME>_URL`` env var (the canonical place — the
   same vars the renderer / TTS pipeline read).
2. The hard-coded fallback in :data:`_SERVICES` — kept in sync with
   ``cloud/warm_{tts,image}_services.sh`` and ``cloud/<svc>/deploy.sh``.

A service whose URL resolves to empty string is still listed so the
admin panel can flag it as ``unconfigured`` rather than silently
hiding it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class ServiceKind(str, Enum):
    """High-level grouping for the admin panel filter chips."""

    TTS = "tts"
    IMAGE = "image"
    VIDEO = "video"
    INFRA = "infra"


@dataclass(frozen=True)
class Service:
    """One Cloud Run service we care about in the admin panel."""

    name: str  # ytfactory-tts-chatterbox
    short: str  # chatterbox  (the suffix we show in the UI)
    kind: ServiceKind
    env_var: Optional[str]  # CLOUDRUN_TTS_CHATTERBOX_URL
    fallback_url: str
    healthz_path: str  # "/readyz" for GPU services, "/healthz" for infra
    is_job: bool = False  # render-worker-v2 is a Job, not a Service
    notes: str = ""

    @property
    def url(self) -> str:
        """Resolved URL — env var takes precedence over fallback."""
        if self.env_var:
            v = (os.environ.get(self.env_var) or "").strip()
            if v:
                return v
        return self.fallback_url

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def health_url(self) -> str:
        if not self.url:
            return ""
        return self.url.rstrip("/") + self.healthz_path


_REGION = "asia-southeast1"

# Keep this list aligned with ``cloud/`` directories. When you add a new
# service, add a row here AND a deploy.sh under cloud/<name>/.
_SERVICES: tuple[Service, ...] = (
    # --- TTS (GPU L4) ---
    Service(
        name="ytfactory-tts-chatterbox",
        short="chatterbox",
        kind=ServiceKind.TTS,
        env_var="CLOUDRUN_TTS_CHATTERBOX_URL",
        fallback_url="",
        healthz_path="/readyz",
        notes="Default English Shorts voice — see docs/cloudrun_tts.md.",
    ),
    Service(
        name="ytfactory-tts-indicf5",
        short="indicf5",
        kind=ServiceKind.TTS,
        env_var="CLOUDRUN_TTS_INDICF5_URL",
        fallback_url="",
        healthz_path="/readyz",
        notes="Hindi production lane.",
    ),
    # --- Image (GPU L4) ---
    # Z-Image-Turbo is the SOLE production image model (Q67, ADR-024).
    Service(
        name="ytfactory-image-z-image-turbo",
        short="z-image-turbo",
        kind=ServiceKind.IMAGE,
        env_var="CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL",
        fallback_url="",
        healthz_path="/readyz",
        notes="Sole production image model — see CLAUDE.md.",
    ),
    # --- Video / heavy workers ---
    Service(
        name="ytfactory-render-worker-v2",
        short="render-worker-v2",
        kind=ServiceKind.VIDEO,
        env_var=None,
        fallback_url="",  # Job, not Service — no /healthz; introspected via gcloud
        healthz_path="",
        is_job=True,
        notes="Cloud Run JOB (one execution per render). Health = recent execution success rate.",
    ),
    Service(
        name="ytfactory-clone-video-worker",
        short="clone-video-worker",
        kind=ServiceKind.VIDEO,
        env_var="CLOUDRUN_CLONE_VIDEO_URL",
        fallback_url="",
        healthz_path="/healthz",
    ),
    Service(
        name="ytfactory-editing-agent",
        short="editing-agent",
        kind=ServiceKind.VIDEO,
        env_var="CLOUDRUN_EDITING_AGENT_URL",
        fallback_url="",
        healthz_path="/readyz",
        notes="Cinematic editor (polish + assemble-clips/stills/mixed). See docs/editing_agent.md and cloud/editing-agent/.",
    ),
    # --- Infra (CPU services) ---
    Service(
        name="ytfactory-web-server",
        short="web-server",
        kind=ServiceKind.INFRA,
        env_var="CLOUDRUN_WEB_SERVER_URL",
        fallback_url="",
        healthz_path="/healthz",
    ),
    Service(
        name="ytfactory-web-next",
        short="web-next",
        kind=ServiceKind.INFRA,
        env_var="CLOUDRUN_WEB_NEXT_URL",
        fallback_url="",
        healthz_path="/healthz",
    ),
)


def list_services(
    *,
    kind: Optional[ServiceKind] = None,
    configured_only: bool = False,
) -> list[Service]:
    """Return the canonical service list, optionally filtered."""
    out: Iterable[Service] = _SERVICES
    if kind is not None:
        out = (s for s in out if s.kind == kind)
    if configured_only:
        out = (s for s in out if s.configured)
    return list(out)


def get_service(short_or_name: str) -> Optional[Service]:
    """Look up by either ``short`` (chatterbox) or ``name`` (ytfactory-tts-chatterbox)."""
    needle = short_or_name.strip()
    for s in _SERVICES:
        if s.short == needle or s.name == needle:
            return s
    return None
