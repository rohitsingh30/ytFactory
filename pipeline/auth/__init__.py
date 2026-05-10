"""Web user identity — Sign-in-with-Google + per-email allowlist.

Distinct from ``pipeline.upload`` (which manages YouTube channel OAuth
tokens for *publishing*). This package handles *who is allowed to use
the website* — a separate Google OAuth client, separate Firestore
collection, separate session cookie.

Public surface (re-exported by submodule):

* ``oauth_url(state)`` / ``exchange_code(code)`` — Google OAuth web flow
* ``sign_session(email)`` / ``verify_session(cookie)`` — HMAC cookie I/O
* ``get_user(email)`` / ``upsert_user(...)`` / ``approve(email)`` / ``deny(email)`` /
  ``list_pending()`` — Firestore allowlist CRUD
* ``USER_STATUS_APPROVED`` / ``USER_STATUS_PENDING`` / ``USER_STATUS_DENIED``
"""
from .identity import (  # noqa: F401
    USER_STATUS_APPROVED,
    USER_STATUS_DENIED,
    USER_STATUS_PENDING,
    approve,
    deny,
    exchange_code,
    get_user,
    is_admin_email,
    list_pending,
    list_users,
    oauth_url,
    sign_session,
    upsert_user,
    verify_session,
)
