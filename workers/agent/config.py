"""Agent runtime configuration — read once at startup from env."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    control_url: str
    auth_token: str
    heartbeat_interval_s: float
    lease_caps: tuple[str, ...]
    lease_ttl_s: int
    parallelism: int  # number of concurrent lease loops on this agent

    @classmethod
    def from_env(cls) -> "AgentConfig":
        token = os.environ.get("YTFACTORY_AGENT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "YTFACTORY_AGENT_TOKEN not set — same value as the control plane env."
            )
        # Default agent_id to the laptop's hostname so multi-machine setups Just Work.
        agent_id = os.environ.get("YTFACTORY_AGENT_ID") or socket.gethostname()
        url = os.environ.get(
            "YTFACTORY_CONTROL_URL",
            "https://ytfactory-control-767262167641.us-central1.run.app",
        ).rstrip("/")
        caps = tuple(
            c.strip() for c in os.environ.get(
                "YTFACTORY_AGENT_CAPS",
                # v1: laptop runs both heavy AND light tasks. Once light
                # workers move to Cloud Run jobs, drop them from this list.
                "render_short,youtube_upload,research_handoff,images,tts,asr,compose,footage,noop",
            ).split(",") if c.strip()
        )
        return cls(
            agent_id=agent_id,
            control_url=url,
            auth_token=token,
            heartbeat_interval_s=float(os.environ.get("YTFACTORY_HEARTBEAT_S", "15")),
            lease_caps=caps,
            lease_ttl_s=int(os.environ.get("YTFACTORY_LEASE_TTL_S", "300")),
            parallelism=int(os.environ.get("YTFACTORY_AGENT_PARALLELISM", "2")),
        )
