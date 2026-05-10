"""Re-export of :mod:`pipeline.cloudrun_auth`.

Two copies of this module existed before 2026-05-10 — one at
``pipeline/cloudrun_auth.py`` (used by TTS) and one at
``pipeline/cloud/cloudrun_auth.py`` (used by the image client). They
drifted: the metadata-server path that lets the cloud worker auth
without the gcloud CLI was added to the top-level copy first, and the
image client's stale copy crashed the cake-orch-v3 smoke run with
``FileNotFoundError: 'gcloud'``.

Going forward this module is a pure re-export of the canonical one.
Imports from either path resolve to the same code.
"""
from __future__ import annotations

from pipeline.cloudrun_auth import (  # noqa: F401
    get_id_token,
    _on_gcp_runtime,
    _metadata_token,
    _reset_cache_for_tests,
    _TOKEN_TTL_S,
    _METADATA_HOST,
    _TOKENS,
)
