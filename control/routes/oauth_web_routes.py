"""Web OAuth — connect a YouTube channel by clicking a button.

Replaces the laptop-era localhost-callback flow. The user clicks
"Connect" in the studio UI, hits ``/api/oauth/start?account=<slug>``,
gets sent to Google, signs in, and is redirected back to
``/api/oauth/callback`` — which exchanges the code for a token and
stashes it in Firestore under ``oauth_tokens/<account>``.

The token is then read by ``pipeline.upload`` (with a tiny adapter)
the next time a render publishes for that channel.

Why a Firestore stash and not the laptop's
``~/.config/ytfactory/youtube_token_<account>.json``? In cloud, every
container is ephemeral and there's no shared filesystem. Firestore is
the only persistence we have. The same secret is used across the
control-plane and render-worker containers via the
``ChannelToken.load(account)`` helper below.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oauth")

CLIENT_SECRET_PATH = Path(
    os.environ.get(
        "YTFACTORY_CLIENT_SECRET_PATH",
        str(Path.home() / ".config/ytfactory/client_secret.json"),
    )
)

PUBLIC_BASE_URL_ENV = "YTFACTORY_PUBLIC_BASE_URL"  # e.g. https://ytfactory-control-….run.app

SCOPES = " ".join([
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
])

# State token → account map; small TTL. In-memory is fine because the
# OAuth round-trip completes in seconds; the user shouldn't be doing
# multi-replica round-robin during a sign-in.
_PENDING_STATE: dict[str, dict[str, Any]] = {}
_STATE_TTL_S = 600


def _client() -> dict[str, str]:
    """Return the OAuth client_secret as a dict (cloud-friendly).

    Three ways to provide it (first match wins):
      1. ``YTFACTORY_CLIENT_SECRET`` env var — the JSON string itself
         (Cloud Run secret-as-env-var). This is the production path.
      2. ``YTFACTORY_CLIENT_SECRET_PATH`` env var — a file path to read.
      3. ``~/.config/ytfactory/client_secret.json`` — laptop default.
    """
    raw_env = os.environ.get("YTFACTORY_CLIENT_SECRET", "").strip()
    if raw_env:
        try:
            raw = json.loads(raw_env)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=f"YTFACTORY_CLIENT_SECRET env var is not valid JSON: {e}",
            )
    else:
        if not CLIENT_SECRET_PATH.exists():
            raise HTTPException(
                status_code=500,
                detail=(
                    f"OAuth client secret missing. Set YTFACTORY_CLIENT_SECRET "
                    f"env var (cloud) or place client_secret.json at {CLIENT_SECRET_PATH}."
                ),
            )
        raw = json.loads(CLIENT_SECRET_PATH.read_text())
    cs = raw.get("installed") or raw.get("web") or {}
    if not cs.get("client_id") or not cs.get("client_secret"):
        raise HTTPException(status_code=500, detail="OAuth client secret malformed")
    return cs


def _public_base_url(request: Request) -> str:
    """Where the public callback lives. Prefer env, fall back to request's host."""
    explicit = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip().rstrip("/")
    if explicit:
        return explicit
    # Honor X-Forwarded-Proto/Host from the load balancer.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}".rstrip("/")


def _redirect_uri(request: Request) -> str:
    return f"{_public_base_url(request)}/api/oauth/callback"


def _gc(now: float) -> None:
    expired = [k for k, v in _PENDING_STATE.items() if now - v["created_at"] > _STATE_TTL_S]
    for k in expired:
        _PENDING_STATE.pop(k, None)


# ---------------------------------------------------------------------------
# Token storage — Firestore in cloud, falls back to per-account file in dev
# ---------------------------------------------------------------------------


def _firestore_doc(account: str):
    """Return the Firestore document ref for the channel's OAuth token, or None
    when running in memory mode (local dev)."""
    if (os.environ.get("YTFACTORY_QUEUE_BACKEND", "memory").lower() != "firestore"):
        return None
    try:
        from google.cloud import firestore  # noqa: PLC0415
        db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"))
        return db.collection("oauth_tokens").document(account)
    except Exception:  # noqa: BLE001
        logger.warning("firestore client unavailable, falling back to disk", exc_info=True)
        return None


def _file_path(account: str) -> Path:
    return Path(os.environ.get(
        "YTFACTORY_TOKEN_DIR",
        str(Path.home() / ".config/ytfactory"),
    )) / f"youtube_token_{account}.json"


def _secret_mount_path(account: str) -> Path:
    """Cloud Run secret mount: /secrets/youtube-token-<account>/value."""
    return Path("/secrets") / f"youtube-token-{account}" / "value"


def save_token(account: str, payload: dict[str, Any]) -> None:
    """Persist token where the renderer can read it.

    Always writes to disk too so the laptop pipeline keeps working when
    we hand-fix things from the terminal.
    """
    doc = _firestore_doc(account)
    if doc is not None:
        doc.set(payload, merge=True)
    p = _file_path(account)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2))


def load_token(account: str) -> dict[str, Any] | None:
    doc = _firestore_doc(account)
    if doc is not None:
        snap = doc.get()
        if snap.exists:
            return snap.to_dict()
    p = _file_path(account)
    if p.exists():
        return json.loads(p.read_text())
    sp = _secret_mount_path(account)
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/start")
async def start(
    request: Request,
    account: str = Query(..., description="Channel slug (e.g. rhymetimejunction)"),
    return_to: str = Query("/app/channels", description="Where to send the user after success"),
) -> RedirectResponse:
    """Kicks off the OAuth flow. Redirects the user to Google.

    Use case: link in the web UI like
    ``<a href="/api/oauth/start?account=rhymetimejunction">Connect</a>``.
    """
    cs = _client()
    state = secrets.token_urlsafe(32)
    _PENDING_STATE[state] = {
        "account": account,
        "return_to": return_to,
        "created_at": time.time(),
    }
    _gc(time.time())

    params = {
        "response_type": "code",
        "client_id": cs["client_id"],
        "redirect_uri": _redirect_uri(request),
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Google redirects here. Exchange code → token → save → bounce back to UI."""
    if error:
        return _html_done(error_msg=f"Google returned error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")
    pending = _PENDING_STATE.pop(state, None)
    if pending is None:
        raise HTTPException(status_code=400, detail="invalid or expired state")
    account = pending["account"]
    return_to = pending["return_to"] or "/app/channels"

    cs = _client()
    redirect_uri = _redirect_uri(request)

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": cs["client_id"],
                "client_secret": cs["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        logger.error("token exchange failed: %s %s", r.status_code, r.text[:500])
        return _html_done(error_msg=f"Token exchange failed: {r.status_code}")

    tok = r.json()
    expires_at = time.time() + int(tok.get("expires_in", 3600))

    payload = {
        "account": account,
        "token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": cs["client_id"],
        "client_secret": cs["client_secret"],
        "scopes": (tok.get("scope") or SCOPES).split(),
        "expiry": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(expires_at)),
        "universe_domain": "googleapis.com",
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    save_token(account, payload)
    has_refresh = bool(payload["refresh_token"])
    logger.info("oauth: stored token account=%s refresh=%s", account, has_refresh)

    return _html_done(success=True, account=account, return_to=return_to,
                      has_refresh=has_refresh)


@router.get("/status")
async def status(account: str = Query(...)) -> dict:
    """Quick read for the UI to know whether a channel has a token cached."""
    tok = load_token(account)
    if not tok:
        return {"account": account, "connected": False}
    return {
        "account": account,
        "connected": True,
        "has_refresh": bool(tok.get("refresh_token")),
        "issued_at": tok.get("issued_at"),
        "expiry": tok.get("expiry"),
        "scopes": tok.get("scopes", []),
    }


@router.delete("/disconnect")
async def disconnect(account: str = Query(...)) -> dict:
    """Forget the cached token for a channel — does NOT revoke at Google."""
    doc = _firestore_doc(account)
    if doc is not None:
        doc.delete()
    p = _file_path(account)
    if p.exists():
        p.unlink()
    return {"account": account, "connected": False}


def _html_done(
    success: bool = False,
    error_msg: str | None = None,
    account: str = "",
    return_to: str = "/app/channels",
    has_refresh: bool = False,
) -> HTMLResponse:
    """Tiny self-contained completion page that auto-bounces the user
    back to the studio. Avoids loading the full Next.js bundle for this
    one screen."""
    if success:
        title = "Channel connected"
        body = f"""
          <h1>Channel connected</h1>
          <p><strong>{account}</strong> is now linked. Refresh token: {'yes' if has_refresh else 'NO — first sign-in only issues one. If missing, revoke at <a href=\"https://myaccount.google.com/connections\">myaccount.google.com/connections</a> and reconnect.'}</p>
          <p>Returning you to the studio…</p>
          <script>setTimeout(() => location.href = {json.dumps(return_to)}, 1500);</script>
        """
        status_code = 200
    else:
        title = "Connection failed"
        body = f"""
          <h1>Connection failed</h1>
          <p>{error_msg or 'Unknown error.'}</p>
          <p><a href=\"{return_to}\">Back to studio</a></p>
        """
        status_code = 400
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"><title>{title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      background: #0a0a0a; color: #fafafa;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", system-ui, sans-serif;
      display: grid; place-items: center; min-height: 100vh; margin: 0;
      padding: 2rem;
    }}
    main {{
      max-width: 460px; padding: 2rem; border-radius: 12px;
      border: 1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.03);
    }}
    h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 1rem; letter-spacing: -0.011em; }}
    p {{ color: rgba(255,255,255,.7); font-size: 14px; line-height: 1.55; }}
    a {{ color: #a78bfa; }}
  </style>
</head><body><main>{body}</main></body></html>
"""
    return HTMLResponse(content=html, status_code=status_code)
