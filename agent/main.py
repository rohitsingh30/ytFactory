"""Laptop agent entry point.

Two coroutines run concurrently:
- `_heartbeat_loop`: POSTs /agent/heartbeat every config.heartbeat_interval_s.
- `_lease_loop`: long-polls /agent/lease, dispatches each leased task to a
  worker via runner.run, then POSTs /agent/ack/{id}.

Outbound HTTPS only — the laptop opens no listening ports.
"""
from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from agent import resources, runner
import workers.heavy  # noqa: F401 — registers heavy worker functions
from agent.config import AgentConfig
from shared.schema import (
    AckRequest,
    HeartbeatRequest,
    LeaseRequest,
    LeaseResponse,
    TaskKind,
)

logger = logging.getLogger("ytfactory.agent")

_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0


def _auth_headers(cfg: AgentConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.auth_token}"}


async def _heartbeat_loop(cfg: AgentConfig, client: httpx.AsyncClient, stop: asyncio.Event) -> None:
    backoff = _BACKOFF_INITIAL_S
    while not stop.is_set():
        snap = resources.snapshot(cfg.agent_id)
        try:
            r = await client.post(
                f"{cfg.control_url}/agent/heartbeat",
                json=HeartbeatRequest(resources=snap).model_dump(mode="json"),
                headers=_auth_headers(cfg),
                timeout=10.0,
            )
            r.raise_for_status()
            backoff = _BACKOFF_INITIAL_S
        except Exception as e:  # noqa: BLE001
            logger.warning("heartbeat failed: %s; backoff=%.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(_BACKOFF_MAX_S, backoff * 2)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.heartbeat_interval_s)
        except asyncio.TimeoutError:
            pass


async def _lease_one(cfg: AgentConfig, client: httpx.AsyncClient) -> bool:
    """Lease + run + ack one task. Returns True if a task was processed."""
    caps = []
    for c in cfg.lease_caps:
        try:
            caps.append(TaskKind(c))
        except ValueError:
            logger.warning("ignoring unknown cap %r", c)
    if not caps:
        return False

    body = LeaseRequest(agent_id=cfg.agent_id, caps=caps, lease_ttl_s=cfg.lease_ttl_s)
    r = await client.post(
        f"{cfg.control_url}/agent/lease",
        json=body.model_dump(mode="json"),
        headers=_auth_headers(cfg),
        timeout=45.0,  # > server's 30s long-poll
    )
    r.raise_for_status()
    parsed = LeaseResponse.model_validate(r.json())
    if parsed.task is None:
        return False

    task = parsed.task
    logger.info("leased %s kind=%s", task.task_id, task.kind.value)
    ok, out_uri, err = await runner.run(task)
    ack = AckRequest(
        agent_id=cfg.agent_id,
        status="ok" if ok else "error",
        output_uri=out_uri,
        error=err,
    )
    r2 = await client.post(
        f"{cfg.control_url}/agent/ack/{task.task_id}",
        json=ack.model_dump(mode="json"),
        headers=_auth_headers(cfg),
        timeout=10.0,
    )
    r2.raise_for_status()
    logger.info("acked %s ok=%s err=%s", task.task_id, ok, err)
    return True


async def _lease_loop(cfg: AgentConfig, client: httpx.AsyncClient, stop: asyncio.Event) -> None:
    backoff = _BACKOFF_INITIAL_S
    while not stop.is_set():
        try:
            ran = await _lease_one(cfg, client)
            backoff = _BACKOFF_INITIAL_S
            if not ran:
                # Server returned no work — short pause before next long-poll to
                # avoid tight loop if connection itself fails fast.
                await asyncio.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            logger.warning("lease loop error: %s; backoff=%.1fs", e, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(_BACKOFF_MAX_S, backoff * 2)


async def run_agent(cfg: AgentConfig | None = None) -> None:
    cfg = cfg or AgentConfig.from_env()
    logger.info("agent starting: id=%s caps=%s control=%s", cfg.agent_id, cfg.lease_caps, cfg.control_url)
    stop = asyncio.Event()

    def _on_signal() -> None:
        logger.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # not available on Windows

    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            _heartbeat_loop(cfg, client, stop),
            _lease_loop(cfg, client, stop),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_agent())


if __name__ == "__main__":
    main()
