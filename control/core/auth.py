"""Auth for control-plane endpoints.

Two layers:

- **Agent auth**: shared bearer token in `YTFACTORY_AGENT_TOKEN` env. The
  laptop agent sends `Authorization: Bearer <token>` on every call. Simple
  and good enough for v1; rotate via Secret Manager later.
- **User auth**: Firebase ID token (Google sign-in). Anonymous users get
  no token; rate-limited by IP. Stub for now — wired in task #12.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status

_AGENT_TOKEN_ENV = "YTFACTORY_AGENT_TOKEN"


def _expected_agent_token() -> str:
    token = os.environ.get(_AGENT_TOKEN_ENV)
    if not token:
        raise RuntimeError(
            f"{_AGENT_TOKEN_ENV} not set. Generate one with `openssl rand -hex 32` "
            f"and put it in both the control-plane env and the agent env."
        )
    return token


def require_agent(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: raises 401 unless Authorization: Bearer <agent_token>.

    On Cloud Run (K_SERVICE env set by the runtime), trust IAM instead —
    Google Frontend already validated the caller's OIDC token against the
    run.invoker binding before the request reached us. The OIDC token
    consumes the Authorization header; we can't ALSO require an
    app-level Bearer there.
    """
    if os.environ.get("K_SERVICE"):
        return
    expected = _expected_agent_token()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid agent token")
