"""Sign-in-with-Google + per-email Firestore allowlist + signed session.

Kept intentionally small. Three concerns in one module:

1. **Google OAuth web flow** — build the consent URL, exchange the
   callback code for an ID token, extract verified email.
2. **HMAC session cookie** — bind email + issued timestamp into a
   tamper-proof string. Verify on every request.
3. **Firestore allowlist** — ``auth_users/<email>`` doc per known
   user, holding ``status`` (approved / pending / denied),
   ``is_admin``, audit fields.

Admin determination is implicit: any email matching one of the
``YTFACTORY_ADMIN_DOMAINS`` (default ``docx.co.in``) gets
``is_admin=True`` and ``status=approved`` on first sign-in.

Env vars:

* ``YTFACTORY_WEB_OAUTH_CLIENT`` — JSON string of the web OAuth client
  ({"web": {"client_id": "...", "client_secret": "...",
  "redirect_uris": [...]}}). On Cloud Run, mounted from Secret
  Manager. On laptop, optional — falls back to file at
  ``$YTFACTORY_WEB_OAUTH_CLIENT_PATH`` or
  ``~/.config/ytfactory/web_oauth_client.json``.
* ``YTFACTORY_SESSION_SECRET`` — 32+ byte random hex for HMAC. Cookie
  rotation = change this env var. Auto-generated dev fallback if unset.
* ``YTFACTORY_ADMIN_DOMAINS`` — comma-separated. Default
  ``docx.co.in``.
* ``YTFACTORY_ADMIN_EMAILS`` — comma-separated explicit-email allowlist
  (case-insensitive). Empty by default. Use when admins live on a
  shared-tenant domain (e.g. ``gmail.com``) where the domain-match
  rule would over-grant. Either match (domain OR explicit email)
  grants admin.
* ``YTFACTORY_AUTH_REDIRECT_URI`` — the canonical redirect URI used
  in the OAuth flow. Must match one entry in the OAuth client's
  ``redirect_uris``. Default
  ``https://ytfactory-web-7hwnzw7lya-as.a.run.app/api/auth/google/callback``.

  **Single-host invariant.** The host portion of this URI MUST match
  ``YTFACTORY_CANONICAL_HOST`` on the public-facing service (the
  Next.js ``ytfactory-web-next`` middleware enforces this via 308
  host normalization). Cloud Run gives every service two equivalent
  public URLs (project-id-hash form + project-number form); cookies
  are host-bound, so ``yt_oauth_state`` only fires the callback OK
  when the user starts and ends on the same host. See
  ``docs/cloudrun_dual_url_host_normalization.md``.
* ``YTFACTORY_SESSION_TTL_S`` — cookie TTL seconds. Default 7 days.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

USER_STATUS_APPROVED = "approved"
USER_STATUS_PENDING = "pending"
USER_STATUS_DENIED = "denied"

_VALID_STATUSES = (USER_STATUS_APPROVED, USER_STATUS_PENDING, USER_STATUS_DENIED)

_OAUTH_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
_OAUTH_SCOPES = ("openid", "email", "profile")

_DEFAULT_REDIRECT_URI = (
    "https://ytfactory-web-7hwnzw7lya-as.a.run.app/api/auth/google/callback"
)
_DEFAULT_ADMIN_DOMAINS = ("docx.co.in",)
_DEFAULT_TTL_S = 7 * 24 * 3600  # 7 days

_FIRESTORE_COLLECTION = "auth_users"

# Process-local fallback secret for dev (won't survive restart).
_RUNTIME_SECRET = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# OAuth client + session secret loading


def _oauth_client() -> dict:
    """Return the web OAuth client config as
    ``{"client_id", "client_secret", "redirect_uris": [...]}``.

    Source order (first hit wins):
      1. ``YTFACTORY_WEB_OAUTH_CLIENT`` env (JSON string — Cloud Run / Secret Manager)
      2. ``YTFACTORY_WEB_OAUTH_CLIENT_PATH`` env (path to JSON file)
      3. ``~/.config/ytfactory/web_oauth_client.json`` (laptop default)
    """
    raw_env = os.environ.get("YTFACTORY_WEB_OAUTH_CLIENT")
    if raw_env:
        cfg = json.loads(raw_env)
    else:
        path = (
            os.environ.get("YTFACTORY_WEB_OAUTH_CLIENT_PATH")
            or str(Path.home() / ".config/ytfactory/web_oauth_client.json")
        )
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    inner = cfg.get("web") or cfg.get("installed") or cfg
    if "client_id" not in inner or "client_secret" not in inner:
        raise RuntimeError(
            "OAuth client config missing client_id/client_secret. "
            "Set YTFACTORY_WEB_OAUTH_CLIENT or place JSON at "
            "~/.config/ytfactory/web_oauth_client.json"
        )
    return {
        "client_id": inner["client_id"],
        "client_secret": inner["client_secret"],
        "redirect_uris": inner.get("redirect_uris", []),
    }


def _redirect_uri() -> str:
    return (
        os.environ.get("YTFACTORY_AUTH_REDIRECT_URI") or _DEFAULT_REDIRECT_URI
    ).strip()


def _session_secret() -> bytes:
    return (os.environ.get("YTFACTORY_SESSION_SECRET") or _RUNTIME_SECRET).encode()


def _ttl_s() -> int:
    try:
        return int(os.environ.get("YTFACTORY_SESSION_TTL_S", str(_DEFAULT_TTL_S)))
    except ValueError:
        return _DEFAULT_TTL_S


def _admin_domains() -> tuple[str, ...]:
    raw = (os.environ.get("YTFACTORY_ADMIN_DOMAINS") or "").strip()
    if not raw:
        return _DEFAULT_ADMIN_DOMAINS
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip())


def _admin_emails() -> frozenset[str]:
    """Explicit per-email admin allowlist (case-insensitive). Empty by
    default. Combined with ``_admin_domains()`` via OR.
    """
    raw = (os.environ.get("YTFACTORY_ADMIN_EMAILS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(
        e.strip().lower() for e in raw.split(",") if e.strip()
    )


def is_admin_email(email: str) -> bool:
    """Implicit admin via either:
      - email domain in ``YTFACTORY_ADMIN_DOMAINS``, OR
      - explicit email in ``YTFACTORY_ADMIN_EMAILS``.
    """
    if "@" not in email:
        return False
    email_l = email.lower()
    if email_l in _admin_emails():
        return True
    domain = email_l.split("@", 1)[1]
    return domain in _admin_domains()


# ---------------------------------------------------------------------------
# OAuth web flow


def oauth_url(state: str) -> str:
    """Build the Google OAuth consent URL the browser is redirected to.

    ``state`` is a CSRF nonce stored in a transient cookie + verified
    on callback.
    """
    cs = _oauth_client()
    params = {
        "client_id": cs["client_id"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(_OAUTH_SCOPES),
        "access_type": "online",  # we don't need refresh tokens for sign-in
        "prompt": "select_account",
        "state": state,
        # include_granted_scopes lets a user already-consented to other
        # scopes pass through without re-prompting.
        "include_granted_scopes": "true",
    }
    return f"{_OAUTH_AUTH_URI}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Exchange the OAuth callback ``code`` for an ID token; verify it;
    return the email + email_verified claims.

    Raises on any failure (HTTP error, invalid token, unverified email).
    """
    cs = _oauth_client()
    body = {
        "code": code,
        "client_id": cs["client_id"],
        "client_secret": cs["client_secret"],
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    resp = httpx.post(_OAUTH_TOKEN_URI, data=body, timeout=15.0)
    if resp.status_code != 200:
        raise RuntimeError(
            f"OAuth token exchange failed: {resp.status_code} {resp.text[:200]}"
        )
    payload = resp.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise RuntimeError("OAuth response missing id_token")

    claims = _decode_id_token(id_token, audience=cs["client_id"])
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise RuntimeError("ID token missing email claim")
    if not claims.get("email_verified"):
        raise RuntimeError(f"Google reports email {email!r} is NOT verified")
    return {
        "email": email,
        "name": claims.get("name") or "",
        "picture": claims.get("picture") or "",
    }


def _decode_id_token(token: str, *, audience: str | None = None) -> dict:
    """Decode and CRYPTOGRAPHICALLY VERIFY a Google ID token's payload.

    Audit S1.1 — pre-fix this trusted Google's signature without
    verification on the assumption that "the channel is TLS, we
    control the client_secret". That's wrong: anyone who can MITM
    TLS, intercept a redirect, replay a leaked code-exchange
    response, or interpose a tampered id_token by ANY means could
    mint claims like ``email=admin@docx.co.in`` and
    ``email_verified=true`` — which combined with auto-admin via
    ``YTFACTORY_ADMIN_DOMAINS`` is full takeover.

    Now uses ``google.oauth2.id_token.verify_oauth2_token`` which:
    - fetches Google's JWKS from the v3/certs endpoint (with
      transport-layer caching);
    - verifies the RS256 signature against Google's published keys;
    - validates iss is one of accounts.google.com /
      https://accounts.google.com;
    - validates aud == client_id (so a token minted for a different
      OAuth client can't be replayed against ours);
    - validates exp/iat windows.

    Falls back to the legacy unverified base64 decode ONLY when the
    google-auth lib isn't installable (lab/test env without
    network), with a loud warning. Production deployments MUST have
    google-auth available — verify the import succeeds at boot.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise RuntimeError("Malformed id_token (expected three segments)")

    if audience:
        try:
            from google.auth.transport import requests as ga_requests
            from google.oauth2 import id_token as ga_id_token
        except ImportError:  # coverage: only triggers in environments without google-auth
            logger.warning(
                "google-auth not installed; falling back to UNVERIFIED "
                "id_token decode. This is a security regression — install "
                "google-auth in any prod environment."
            )
        else:
            try:
                claims = ga_id_token.verify_oauth2_token(
                    token,
                    ga_requests.Request(),
                    audience=audience,
                )
            except ValueError as e:
                # google-auth raises ValueError on every check failure
                # (bad sig, expired, wrong audience, wrong issuer).
                raise RuntimeError(f"id_token verification failed: {e}") from e
            return claims

    # Audience-less path is only used by the legacy decode-only path
    # (kept for back-compat with callers that don't have client_id).
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


# ---------------------------------------------------------------------------
# Session cookie (HMAC over ``email|issued``)


def sign_session(email: str, *, now_s: int | None = None) -> str:
    issued = now_s if now_s is not None else int(time.time())
    payload = f"{email}|{issued}"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest()
    sig_b = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{payload}|{sig_b}"


def verify_session(cookie: str | None) -> Optional[str]:
    """Return the email if cookie is valid and unexpired, else None."""
    if not cookie or "|" not in cookie:
        return None
    try:
        email, issued_s, sig = cookie.rsplit("|", 2)
    except ValueError:
        return None
    payload = f"{email}|{issued_s}"
    expect = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest()
    expect_b = base64.urlsafe_b64encode(expect).decode().rstrip("=")
    if not hmac.compare_digest(expect_b, sig):
        return None
    try:
        issued = int(issued_s)
    except ValueError:
        return None
    if (time.time() - issued) > _ttl_s():
        return None
    return email or None


# ---------------------------------------------------------------------------
# Firestore allowlist


def _firestore():
    """Lazy-import Firestore client — keeps test imports fast."""
    from google.cloud import firestore  # noqa: PLC0415  # pragma: no cover

    return firestore.Client()  # pragma: no cover


def _doc_ref(email: str):
    db = _firestore()
    return db.collection(_FIRESTORE_COLLECTION).document(email.lower())


# ---------------------------------------------------------------------------
# Audit Q2.34 — get_user TTL cache.
#
# Pre-fix the auth middleware called ``get_user(email)`` on every
# authenticated request, which hit Firestore once per request. Polled
# dashboards (chat, jobs, telemetry) hit ~6 reads/sec/tab; a few open
# tabs alone burned through the per-replica Firestore quota and
# inflated the GCP bill. Now cache per email with a tiny TTL (default
# 60 s). The trade-off: a freshly-promoted user has to wait up to TTL
# seconds for their session to see the new status. That's fine — admin
# promotion via /api/admin/users immediately calls invalidate_user_cache
# below to bust the entry, and the user re-loads after seeing the
# email arrive.
#
# Override the TTL via YTFACTORY_USER_CACHE_TTL_S=0 to disable.
# ---------------------------------------------------------------------------

import time as _time
import threading as _threading

_USER_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_USER_CACHE_LOCK = _threading.Lock()
_USER_CACHE_TTL_DEFAULT_S = 60.0


def _user_cache_ttl_s() -> float:
    raw = os.environ.get("YTFACTORY_USER_CACHE_TTL_S", "").strip()
    if not raw:
        return _USER_CACHE_TTL_DEFAULT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _USER_CACHE_TTL_DEFAULT_S


def invalidate_user_cache(email: Optional[str] = None) -> None:
    """Bust the get_user cache for ``email`` (or all entries when None).

    Call this from any code that mutates a user doc (admin
    approve/deny, status change) so the cached value doesn't shadow
    the change for up to TTL seconds.
    """
    with _USER_CACHE_LOCK:
        if email is None:
            _USER_CACHE.clear()
        else:
            _USER_CACHE.pop(email.lower(), None)


def get_user(email: str) -> Optional[dict]:
    """Return user doc as dict, or None if not registered.

    Audit Q2.34 — cached for ``YTFACTORY_USER_CACHE_TTL_S`` seconds
    (default 60). Mutators must call ``invalidate_user_cache(email)``.
    """
    key = email.lower()
    ttl = _user_cache_ttl_s()
    if ttl > 0:
        now = _time.time()
        with _USER_CACHE_LOCK:
            cached = _USER_CACHE.get(key)
        if cached is not None:
            cached_at, value = cached
            if (now - cached_at) <= ttl:
                return value
    snap = _doc_ref(email).get()
    if not snap.exists:
        result: Optional[dict] = None
    else:
        data = snap.to_dict() or {}
        data["email"] = key
        result = data
    if ttl > 0:
        with _USER_CACHE_LOCK:
            _USER_CACHE[key] = (_time.time(), result)
    return result


def upsert_user(
    email: str,
    *,
    name: str = "",
    picture: str = "",
    request_note: str = "",
) -> dict:
    """Idempotent first-sign-in. Auto-approves admin domains.

    Returns the resulting user doc.
    """
    email = email.lower()
    ref = _doc_ref(email)
    snap = ref.get()
    now_iso = _now_iso()

    if snap.exists:
        existing = snap.to_dict() or {}
        # Backfill display fields if missing; never demote status.
        update: dict[str, Any] = {"last_seen_at": now_iso}
        if name and not existing.get("name"):
            update["name"] = name
        if picture and not existing.get("picture"):
            update["picture"] = picture
        if update:  # pragma: no branch
            ref.update(update)
            # Audit Q2.34 — bust the cache so the next get_user
            # picks up the backfilled name / picture / last_seen_at.
            invalidate_user_cache(email)
        existing.update(update)
        existing["email"] = email
        return existing

    # First sign-in.
    if is_admin_email(email):
        status = USER_STATUS_APPROVED
        is_admin = True
        approved_at: str | None = now_iso
        approved_by: str | None = "system:admin_domain"
    else:
        status = USER_STATUS_PENDING
        is_admin = False
        approved_at = None
        approved_by = None

    doc = {
        "email": email,
        "name": name,
        "picture": picture,
        "status": status,
        "is_admin": is_admin,
        "request_note": request_note,
        "requested_at": now_iso,
        "approved_at": approved_at,
        "approved_by": approved_by,
        "last_seen_at": now_iso,
    }
    ref.set(doc)
    # Audit Q2.34 — bust the get_user cache on first sign-in too.
    invalidate_user_cache(email)
    return doc


def approve(email: str, *, by: str) -> dict:
    return _set_status(email, USER_STATUS_APPROVED, by=by)


def deny(email: str, *, by: str) -> dict:
    return _set_status(email, USER_STATUS_DENIED, by=by)


def _set_status(email: str, status: str, *, by: str) -> dict:
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {_VALID_STATUSES}")
    ref = _doc_ref(email)
    snap = ref.get()
    if not snap.exists:
        raise LookupError(f"user {email!r} not found")
    update = {
        "status": status,
        "approved_at": _now_iso() if status == USER_STATUS_APPROVED else None,
        "approved_by": by if status == USER_STATUS_APPROVED else None,
        "decision_at": _now_iso(),
        "decision_by": by,
    }
    ref.update(update)
    out = snap.to_dict() or {}
    out.update(update)
    out["email"] = email.lower()
    # Audit Q2.34 — bust the get_user cache so the change is
    # visible to subsequent auth-middleware lookups immediately.
    invalidate_user_cache(email)
    return out


def list_pending() -> list[dict]:
    return list_users(status=USER_STATUS_PENDING)


def list_users(*, status: Optional[str] = None) -> list[dict]:
    """List all users, optionally filtered by status. Newest-first."""
    db = _firestore()
    q = db.collection(_FIRESTORE_COLLECTION)
    if status is not None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        q = q.where(filter=_field_eq("status", status))
    snaps = q.stream()
    rows = []
    for s in snaps:
        d = s.to_dict() or {}
        d["email"] = s.id
        rows.append(d)
    rows.sort(key=lambda d: d.get("requested_at") or "", reverse=True)
    return rows


def _field_eq(field: str, value: Any):
    """Build a Firestore filter compatible with both v1 and v2 SDK shapes."""
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: PLC0415

        return FieldFilter(field, "==", value)
    except ImportError:  # pragma: no cover
        return None  # caller falls back to legacy where(field, "==", value)


# ---------------------------------------------------------------------------
# Helpers


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
