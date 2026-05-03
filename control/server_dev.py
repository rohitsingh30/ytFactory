"""Local dev control plane — minimal FastAPI app for end-to-end protocol tests.

The full production server (which mounts chat, jobs, telemetry, static UI) is
built in task #10 by porting web/server.py. This file exists so the agent
+ lease protocol can be exercised without dragging the legacy monolith in.

Run:
    YTFACTORY_AGENT_TOKEN=$(openssl rand -hex 32) \
    YTFACTORY_QUEUE_BACKEND=memory \
    .venv/bin/uvicorn control.server_dev:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

from fastapi import FastAPI

from control.agent_routes import router as agent_router

app = FastAPI(title="ytFactory control (dev)")
app.include_router(agent_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "yes"}
