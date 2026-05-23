"""Local dev control plane — minimal FastAPI app for end-to-end protocol tests.

The full production server (which mounts chat, jobs, telemetry, static UI) is
``web/server.py`` (the ``ytfactory-web`` Cloud Run service). This file
exists so the agent + lease protocol AND the cloud-cutover route set
(``control/routes/*``) can be exercised without dragging the legacy
monolith in.

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

# All routers from control/routes/ — the canonical location.
# Production server (web/server.py) uses the same set; dev + prod now
# share the same route surface.
#
# chat_routes is the only one that still lives at control/chat_routes.py
# because it owns shared helpers (_enqueue_render_job, ConfirmResponse)
# that control/scheduler.py + control/render_routes.py depend on.
# A future cleanup pass moves chat_routes to control/routes/ once those
# cross-imports are migrated.
from control.chat_routes import router as chat_router

from control.routes.agent_routes import router as agent_router
from control.routes.dashboard_routes import router as dashboard_router
from control.routes.niche_routes import router as niche_router
from control.routes.scheduler_routes import router as scheduler_router

from control.routes.channels_routes import router as channels_router_v2
from control.routes.cloud_routes import router as cloud_router_v2
from control.routes.music_routes import router as music_router_v2
from control.routes.niche_specs_routes import router as niche_specs_router_v2
from control.routes.render_routes import router as render_router_v2
from control.routes.song_sample_routes import router as song_sample_router_v2
from control.routes.telemetry_routes import router as telemetry_router_v2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "web" / "static"

app = FastAPI(title="ytFactory control (dev)")

# Auto-instrument every route → an OTel HTTP span (named after the
# route template, e.g. ``GET /api/render/{job_id}``). Spans nest with
# any pipeline-side ``obs.timed`` calls inside the handler so the
# dashboard's trace explorer shows the full request → render pipeline
# in one waterfall. Idempotent: safe to call again on hot reload.
from pipeline import observability as _obs  # noqa: E402

_obs.instrument_fastapi(app)
_obs.instrument_outbound_http()
_obs.install_http_identity_middleware(app)
# Mount the v2 (control/routes/*) routers FIRST so their concrete paths
# win over any legacy variant with the same prefix.
app.include_router(channels_router_v2)
app.include_router(cloud_router_v2)
# Per-channel niche spec REST + AI auto-generate (POST .../niches/draft).
# The channel page's "Add a new niche" dialog hits this; without it the
# Generate button 404s even when the dev server is up.
app.include_router(niche_specs_router_v2)
app.include_router(render_router_v2)
app.include_router(music_router_v2)
app.include_router(song_sample_router_v2)
app.include_router(telemetry_router_v2)
# Legacy routers — additive; they bring agent/chat/dashboard/niche/scheduler.
app.include_router(agent_router)
app.include_router(chat_router)
app.include_router(dashboard_router)
app.include_router(niche_router)
app.include_router(scheduler_router)


@app.on_event("startup")
async def _startup_sim_worker() -> None:
    """Auto-start the sim worker when ``YTFACTORY_SIM_WORKER=1`` (default
    on for local dev). Drains queued RENDER_SHORT tasks via the canned
    placeholder mp4 — keeps the e2e test independent of the cloud render
    job. Safe in prod: ``YTFACTORY_SIM_WORKER=0`` skips entirely.
    """
    try:
        from control.core.sim_worker import start_sim_worker  # noqa: PLC0415
        start_sim_worker()
    except Exception:  # noqa: BLE001
        # Sim worker is optional — never block server startup on it.
        pass

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "yes"}


# no-cache forces ETag/304 revalidation on every load so a fresh
# Cloud Run deploy reaches users on a normal reload — no ⌘⇧R needed.
# Applied to every static-HTML entry point. Cheap: 304 responses are
# tiny and the browser's disk cache still serves the body when valid.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
async def index() -> FileResponse:
    """Public landing page — hero, live stats, channel cards, CTAs to
    /studio + /dashboard. Marketing surface for the project."""
    return FileResponse(STATIC_DIR / "landing.html", headers=_NO_CACHE)


@app.get("/studio")
async def studio() -> FileResponse:
    """Operator UI (the legacy /, with right-docked chat panel + render flow)."""
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/chat")
async def chat_only_page() -> FileResponse:
    """Standalone chat-only page (no operator UI). Useful for embeds."""
    return FileResponse(STATIC_DIR / "chat.html", headers=_NO_CACHE)

