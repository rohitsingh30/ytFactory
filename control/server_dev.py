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

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from control.agent_routes import router as agent_router
from control.chat_routes import router as chat_router
from control.niche_routes import router as niche_router

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "web" / "static"

app = FastAPI(title="ytFactory control (dev)")
app.include_router(agent_router)
app.include_router(chat_router)
app.include_router(niche_router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "yes"}


@app.get("/")
async def index() -> FileResponse:
    """Default landing page = the operator UI with the right-docked chat panel.

    The standalone chat-only page is still reachable at /chat for embed use.
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/chat")
async def chat_only_page() -> FileResponse:
    """Standalone chat-only page (no operator UI). Useful for embeds."""
    return FileResponse(STATIC_DIR / "chat.html")
