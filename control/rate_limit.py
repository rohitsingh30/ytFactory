"""Per-IP daily rate limits + a global Azure spend cap.

Anti-abuse for the public chat endpoint. Counters live in Firestore so
they survive Cloud Run cold starts and are shared across instances.

Tiers (signed-in / owner tiers land later when Firebase Auth ships):

| Action       | Anonymous (per IP, per UTC day) |
|--------------|----------------------------------|
| chat message | 20                               |
| confirm      | 1                                |

Plus a global daily Azure-spend cap (env: YTFACTORY_AZURE_DAILY_CAP_USD,
default 5.0). When breached, /api/chat returns 503 with a friendly
"come back tomorrow" body until the next UTC day rolls over.

Both counters reset at UTC midnight (the doc id is the date stamp).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


# Default per-action quotas. Override via env if needed.
_DEFAULTS: dict[str, int] = {
    "chat": int(os.environ.get("YTFACTORY_LIMIT_CHAT_PER_IP", "20")),
    "confirm": int(os.environ.get("YTFACTORY_LIMIT_CONFIRM_PER_IP", "1")),
}

_AZURE_DAILY_CAP_USD = float(os.environ.get("YTFACTORY_AZURE_DAILY_CAP_USD", "5.0"))

# Approximate cost per 1K tokens for gpt-5.3-chat / gpt-4o-mini class.
# Used only to maintain a rough running spend estimate; actual billing
# comes from Azure. Keep conservative — over-estimate to fail closed.
_COST_PER_1K_TOKENS_USD = float(os.environ.get("YTFACTORY_AZURE_COST_PER_1K_TOK_USD", "0.005"))


# ---------------------------------------------------------------------------
# Firestore-backed counters (lazy import — keeps in-memory tests free of GCP)
# ---------------------------------------------------------------------------


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _client():
    from google.cloud import firestore  # noqa: PLC0415 — lazy
    return firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))


def _ip_counter_ref(ip: str, action: str):
    """ratelimits/<date>/ips/<ip>/actions/<action>"""
    db = _client()
    return (db.collection("ratelimits")
              .document(_today_utc_str())
              .collection("ips")
              .document(ip)
              .collection("actions")
              .document(action))


def _spend_doc_ref():
    """ratelimits/<date>/global/spend"""
    db = _client()
    return (db.collection("ratelimits")
              .document(_today_utc_str())
              .collection("global")
              .document("spend"))


# ---------------------------------------------------------------------------
# Pluggable backend so unit tests don't need Firestore
# ---------------------------------------------------------------------------


class _MemoryBackend:
    """Trivial in-process counters; tests reset between cases."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, str, str], int] = {}  # (date, ip, action) → n
        self.spend: dict[str, float] = {}  # date → usd

    def reset(self) -> None:
        self.counters.clear()
        self.spend.clear()

    def incr(self, ip: str, action: str) -> int:
        key = (_today_utc_str(), ip, action)
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def get(self, ip: str, action: str) -> int:
        return self.counters.get((_today_utc_str(), ip, action), 0)

    def add_spend(self, usd: float) -> float:
        d = _today_utc_str()
        self.spend[d] = self.spend.get(d, 0.0) + usd
        return self.spend[d]

    def get_spend(self) -> float:
        return self.spend.get(_today_utc_str(), 0.0)


class _FirestoreBackend:
    def incr(self, ip: str, action: str) -> int:
        from google.cloud import firestore  # noqa: PLC0415
        ref = _ip_counter_ref(ip, action)
        ref.set({"count": firestore.Increment(1), "updated_at": datetime.now(timezone.utc)}, merge=True)
        snap = ref.get()
        return int(snap.to_dict().get("count", 0))

    def get(self, ip: str, action: str) -> int:
        snap = _ip_counter_ref(ip, action).get()
        if not snap.exists:
            return 0
        return int(snap.to_dict().get("count", 0))

    def add_spend(self, usd: float) -> float:
        from google.cloud import firestore  # noqa: PLC0415
        ref = _spend_doc_ref()
        ref.set({"usd": firestore.Increment(usd), "updated_at": datetime.now(timezone.utc)}, merge=True)
        snap = ref.get()
        return float(snap.to_dict().get("usd", 0.0))

    def get_spend(self) -> float:
        snap = _spend_doc_ref().get()
        if not snap.exists:
            return 0.0
        return float(snap.to_dict().get("usd", 0.0))


_BACKEND: _MemoryBackend | _FirestoreBackend | None = None


def get_backend():
    """Backend matches the queue: memory in tests, Firestore in prod."""
    global _BACKEND
    if _BACKEND is None:
        if os.environ.get("YTFACTORY_QUEUE_BACKEND", "memory").lower() == "firestore":
            _BACKEND = _FirestoreBackend()
        else:
            _BACKEND = _MemoryBackend()
    return _BACKEND


def reset_backend() -> None:
    """Tests only."""
    global _BACKEND
    _BACKEND = None


# ---------------------------------------------------------------------------
# Public surface — used by chat_routes
# ---------------------------------------------------------------------------


def client_ip(request: Request) -> str:
    """Best-effort client IP. Cloud Run sets X-Forwarded-For; trust the first hop."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def quota_for(action: Literal["chat", "confirm"]) -> int:
    return _DEFAULTS[action]


def check_and_increment(request: Request, action: Literal["chat", "confirm"]) -> None:
    """Raises 429 if the IP is over its daily quota; else increments and returns.

    Note: get_spend() is checked separately via `check_spend_cap`. Splitting
    means a chat call doesn't get charged for a confirm enqueue, and a 429
    spend-cap response still gets returned cleanly without an unrelated 429
    rate-limit response landing first.
    """
    backend = get_backend()
    ip = client_ip(request)
    quota = quota_for(action)
    used = backend.get(ip, action)
    if used >= quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily limit of {quota} {action} request{'s' if quota != 1 else ''} "
                f"per IP reached. Try again after UTC midnight."
            ),
        )
    backend.incr(ip, action)


def check_spend_cap() -> None:
    """Raises 503 if the global daily Azure spend has crossed the cap."""
    backend = get_backend()
    spent = backend.get_spend()
    if spent >= _AZURE_DAILY_CAP_USD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Daily AI budget of ${_AZURE_DAILY_CAP_USD:.2f} reached. "
                f"Service resumes after UTC midnight."
            ),
        )


def record_token_usage(prompt_tokens: int, completion_tokens: int) -> float:
    """Add a turn's token cost to the daily running total. Returns running USD."""
    total_tokens = prompt_tokens + completion_tokens
    usd = total_tokens / 1000.0 * _COST_PER_1K_TOKENS_USD
    return get_backend().add_spend(usd)


def daily_spend_usd() -> float:
    return get_backend().get_spend()


def daily_cap_usd() -> float:
    return _AZURE_DAILY_CAP_USD
