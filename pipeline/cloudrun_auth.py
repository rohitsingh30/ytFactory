"""Shared Cloud Run ID-token helper for ytFactory clients.

Used by:
- `pipeline.tts.cloudrun` (chatterbox + indicf5)
- `pipeline.images.images_cloudrun` (cloudrun_z_image_turbo)

Why per-audience cache:

A Google ID token issued for one Cloud Run service URL is **only**
valid against that URL's audience. With a single global cache, when a
Short calls (a) `cloudrun_chatterbox` then (b) `cloudrun_z_image_turbo`,
the cached token from one URL would get reused against the other →
Cloud Run rejects with 401 "audience claim mismatch". Audience-keyed
cache prevents this.

Token TTL is 50 min (Google ID tokens are valid 60 min; we refresh
early to absorb clock skew between laptop and Cloud Run frontend).

Auth strategy (in order):

1. **Metadata server** — when running INSIDE Cloud Run / GCE / GKE,
   the platform exposes a metadata server at 169.254.169.254 (alias
   ``metadata.google.internal``) that mints audience-scoped ID tokens
   without any CLI. This is the ONLY path that works inside the
   render-worker container, which doesn't ship the gcloud CLI.

2. **gcloud service-account ADC** — laptop dev with a SA key, OR a
   container that DOES have gcloud installed. Audience-scoped.

3. **gcloud user-account ADC** — laptop dev with `gcloud auth login`.
   No audience scoping; works as long as the caller has
   `roles/run.invoker` on the project.

The order matters: the metadata path silently no-ops when not on a
GCP runtime, falling through to the gcloud paths cleanly on laptops.

Disable in tests via env var? The two clients call
`get_id_token(url)` directly — patch this module's
`_subprocess_run` / `_metadata_token` to mock if needed.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)


# Google ID tokens are valid for 1 h. We refresh at 50 min to leave
# margin for clock skew between laptop and Cloud Run frontend.
_TOKEN_TTL_S = 50 * 60

# audience URL → (token, expires_at_unix_ts)
_TOKENS: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Metadata-server path (preferred when running on GCP)
# ---------------------------------------------------------------------------

# Cloud Run / GCE / GKE expose this in every container. Returns 200 + a
# JWT body when called with the right header. Network-scoped to the
# private link-local address; we can't accidentally hit it from a
# laptop unless someone's spoofing 169.254.169.254.
_METADATA_HOST = "metadata.google.internal"
_METADATA_TIMEOUT_S = 1.5


def _on_gcp_runtime() -> bool:
    """Cheap probe: are we inside a GCP runtime that has the metadata server?

    Looks for env vars Cloud Run / GCE / GKE always set. Avoids the
    cost of an HTTP call when we're definitely on a laptop.
    """
    return bool(
        os.environ.get("K_SERVICE")           # Cloud Run service
        or os.environ.get("CLOUD_RUN_JOB")    # Cloud Run Job execution
        or os.environ.get("GAE_SERVICE")      # App Engine
        or os.environ.get("FUNCTION_TARGET")  # Cloud Functions
        or os.environ.get("KUBERNETES_SERVICE_HOST")  # GKE
        or os.path.isfile("/var/run/secrets/kubernetes.io/serviceaccount/token")
    )


def _metadata_token(audience: str) -> Optional[str]:
    """Fetch an audience-scoped ID token from the GCP metadata server.

    Returns the token string on success, ``None`` on failure (so the
    caller can fall through to gcloud paths). Never raises — callers
    treat None as "this path didn't work, try the next one".
    """
    if not _on_gcp_runtime():
        return None
    try:
        import urllib.request  # noqa: PLC0415
        url = (
            f"http://{_METADATA_HOST}/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={audience}"
        )
        req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT_S) as resp:
            if resp.status != 200:
                logger.debug("[cloudrun_auth] metadata HTTP %s for %s",
                             resp.status, audience)
                return None
            return resp.read().decode("ascii").strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("[cloudrun_auth] metadata path failed for %s: %s", audience, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_id_token(audience: str) -> str:
    """Return a Google-issued ID token usable against `audience`.

    Tries (1) the GCP metadata server when running inside Cloud Run /
    GCE / GKE; (2) gcloud SA ADC; (3) gcloud user-account ADC. Tokens
    are cached per-audience for ~50 min so a single render only pays
    the auth roundtrip once per service it talks to.
    """
    now = time.time()
    cached = _TOKENS.get(audience)
    if cached is not None:
        token, expires_at = cached
        if now < expires_at:
            return token

    # Path 1: metadata server (only works on GCP runtime).
    md_token = _metadata_token(audience)
    if md_token:
        _TOKENS[audience] = (md_token, now + _TOKEN_TTL_S)
        return md_token

    # Path 2 + 3: gcloud subprocess.
    cmds = [
        ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
        ["gcloud", "auth", "print-identity-token"],  # user-account fallback
    ]
    last_err: Exception | None = None
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            token = res.stdout.strip()
            _TOKENS[audience] = (token, now + _TOKEN_TTL_S)
            return token
        except FileNotFoundError as e:
            # gcloud binary missing — bubble a clear error if metadata
            # also failed (we're probably in a slim container that
            # forgot the runtime check). Better than the cryptic
            # "[Errno 2] No such file or directory: 'gcloud'" trace.
            last_err = e
            continue
        except subprocess.CalledProcessError as e:
            last_err = e
            stderr = (e.stderr or "").lower()
            # User-account error → try the no-audience form. Anything
            # else (e.g. not logged in) → bubble up after the loop.
            if "invalid account type" not in stderr and "audiences" not in stderr:
                raise
    raise RuntimeError(
        f"could not get a Google ID token for audience={audience!r}: "
        f"metadata server returned no token AND gcloud paths failed "
        f"(last error: {last_err}). "
        f"On a laptop, run `gcloud auth application-default login` and "
        f"`gcloud auth login`. In a Cloud Run container, ensure the "
        f"service account has Service Account Token Creator role on "
        f"itself."
    )


def _reset_cache_for_tests() -> None:
    """Clear the token cache. Test-only hook so unit tests don't
    leak state across cases."""
    _TOKENS.clear()
