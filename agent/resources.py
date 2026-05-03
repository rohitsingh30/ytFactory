"""Probe local capacity and warm-cache state. Sent on every heartbeat.

Stubs for now — the heavy-worker migration (task #8) fills in real probes
for kokoro_warm / mflux_warm / whisper_warm by hooking module-load events.
"""
from __future__ import annotations

import shutil

import psutil  # type: ignore[import-not-found]

from shared.schema import AgentResources


def _gpu_mem_pct() -> float:
    """Best-effort GPU memory usage. MLX shares system memory on Apple Silicon,
    so we report system memory pressure instead — that's the real bound."""
    try:
        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0


def _on_battery() -> bool:
    try:
        b = psutil.sensors_battery()
        return bool(b and not b.power_plugged)
    except Exception:
        return False


def _disk_free_pct() -> float:
    try:
        u = shutil.disk_usage("/")
        return 100.0 * u.free / u.total
    except Exception:
        return 100.0


# Set by the runner when each worker module first loads. Initially all False.
_WARM: dict[str, bool] = {
    "kokoro": False,
    "mflux": False,
    "whisper": False,
    "f5": False,
}


def mark_warm(name: str, warm: bool = True) -> None:
    if name in _WARM:
        _WARM[name] = warm


def snapshot(agent_id: str, local_queue_depth: int = 0) -> AgentResources:
    mem_used = _gpu_mem_pct()
    return AgentResources(
        agent_id=agent_id,
        mlx_free_pct=max(0.0, 100.0 - mem_used),
        gpu_mem_pct=mem_used,
        kokoro_warm=_WARM["kokoro"],
        mflux_warm=_WARM["mflux"],
        whisper_warm=_WARM["whisper"],
        f5_warm=_WARM["f5"],
        on_battery=_on_battery(),
        local_queue_depth=local_queue_depth,
    )
