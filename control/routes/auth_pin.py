"""Single-user PIN session — gates ``/app/*`` and write endpoints.

Tiny session-cookie flow for the single-tenant product. Stays
intentionally minimal because the multi-tenant SaaS plan
(``docs/multitenant_saas_plan.md``) replaces this with Sign-in-with-Google
OIDC when we get there.

Wire-up:
- Set ``YTFACTORY_OPERATOR_PIN`` in the control-plane env. Anything
  else means "no auth" (dev/local default — keeps `make` tests fast).
- ``POST /api/auth/login`` — submit PIN, receive an HttpOnly session
  cookie.
- ``POST /api/auth/logout`` — clear the cookie.
- ``GET /api/auth/whoami`` — { logged_in: bool }.
- The Next.js middleware (web-next) calls ``/api/auth/whoami`` and
  redirects unauthenticated users to ``/login``.
- Backend write endpoints (``/api/render``, ``/api/chat/confirm``,
  ``/api/jobs/{id}/publish``, ``/api/channels/{ch}/defaults``) gain a
  ``Depends(require_pin)`` once enabled.

Cookie design:
- Name: ``yt_session``
- Value: ``HMAC_SHA256(secret, "issued=<unix_ts>")``
  → no DB lookup; rotate by changing ``YTFACTORY_SESSION_SECRET``.
- Lifetime: 7 days (configurable via ``YTFACTORY_SESSION_TTL_S``).
- Flags: HttpOnly, Secure (in prod), SameSite=Lax.
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import logging
import os
import secrets
import time

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")

PIN_ENV = "YTFACTORY_OPERATOR_PIN"
SECRET_ENV = "YTFACTORY_SESSION_SECRET"
TTL_ENV = "YTFACTORY_SESSION_TTL_S"
COOKIE_NAME = "yt_session"

# Process-local fallback — generated once if no env secret is set so dev
# sessions work but don't survive restart. Production SHOULD set the env.
_RUNTIME_SECRET = secrets.token_hex(32)


def auth_enabled() -> bool:
    """No PIN configured → no gate. Default for dev."""
    return bool((os.environ.get(PIN_ENV) or "").strip())


def _secret() -> bytes:
    return (os.environ.get(SECRET_ENV) or _RUNTIME_SECRET).encode()


def _ttl_s() -> int:
    try:
        return int(os.environ.get(TTL_ENV, "604800"))  # 7 days
    except ValueError:
        return 604800


def _sign(payload: str) -> str:
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _make_token(now_s: int | None = None) -> str:
    issued = now_s or int(time.time())
    payload = f"issued={issued}"
    sig = _sign(payload)
    return f"{payload}.{sig}"


def _verify_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:  # pragma: no cover — rsplit(".", 1) with "." in token always yields 2 parts
        return False
    if not hmac.compare_digest(_sign(payload), sig):
        return False
    if not payload.startswith("issued="):
        return False
    try:
        issued = int(payload.split("=", 1)[1])
    except (IndexError, ValueError):
        return False
    return (time.time() - issued) <= _ttl_s()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_pin(yt_session: str | None = Cookie(default=None)) -> None:
    """Raises 401 unless a valid session cookie is present.

    No-op when no PIN is configured (dev convenience)."""
    if not auth_enabled():
        return
    if not _verify_token(yt_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    pin: str


class WhoamiResponse(BaseModel):
    logged_in: bool
    auth_required: bool


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    if not auth_enabled():
        return {"logged_in": True, "auth_required": False, "note": "auth disabled in dev"}
    expected = (os.environ.get(PIN_ENV) or "").strip()
    if not hmac.compare_digest(body.pin.strip(), expected):
        # Tiny rate-limit hint — don't leak. Brute-force protection is
        # the multi-tenant story (Firebase Auth handles it natively).
        logger.warning(
            "PIN login failed from ip=%s",
            getattr(request.client, "host", "?"),
        )
        raise HTTPException(status_code=401, detail="invalid pin")
    response.set_cookie(
        COOKIE_NAME,
        _make_token(),
        max_age=_ttl_s(),
        httponly=True,
        samesite="lax",
        secure=os.environ.get("YTFACTORY_COOKIE_SECURE", "0") == "1",
    )
    return {"logged_in": True, "auth_required": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"logged_in": False, "auth_required": auth_enabled()}


@router.get("/whoami", response_model=WhoamiResponse)
async def whoami(yt_session: str | None = Cookie(default=None)) -> WhoamiResponse:
    return WhoamiResponse(
        logged_in=(not auth_enabled()) or _verify_token(yt_session),
        auth_required=auth_enabled(),
    )
