"""Shared Cloud Run ID-token helper for ytFactory clients.

Used by:
- `pipeline.tts.cloudrun` (6 TTS services)
- `pipeline.images_cloudrun` (2 image services: cloudrun_flux2_klein,
  cloudrun_z_image_turbo; future cloudrun_qwen_image, cloudrun_hidream)

Why per-audience cache (vs the original global single-token cache that
used to live in `pipeline.tts.cloudrun`):

A Google ID token issued for one Cloud Run service URL is **only**
valid against that URL's audience. With a single global cache, when a
Short calls (a) `cloudrun_chatterbox` then (b) `cloudrun_flux2_klein`,
the cached `--audiences=<chatterbox-url>` token gets reused against
the FLUX URL → Cloud Run rejects with 401 "audience claim mismatch".
The original TTS code escaped this only because every TTS variant
within a single render typically targeted the same TTS service URL —
the multi-service multi-audience case never came up.

Image services break that assumption. Audience-keyed cache fixes it.

Token TTL is 50 min (Google ID tokens are valid 60 min; we refresh
early to absorb clock skew between laptop and Cloud Run frontend).

Auth strategy: tries the service-account ADC path first
(`--audiences=<url>` scoped). Falls back to user-account ADC
(no audience scoping; works as long as caller has `roles/run.invoker`
or higher on the project). The order matters because user-account
ADC errors out with "Invalid account type" on the audience-scoped
form, so we detect that and retry without `--audiences`.

Disable in tests via env var? The two clients call
`get_id_token(url)` directly — patch this module's
`_subprocess_run` to mock if needed. No env knob needed today.
"""
from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger(__name__)


# Google ID tokens are valid for 1 h. We refresh at 50 min to leave
# margin for clock skew between laptop and Cloud Run frontend.
_TOKEN_TTL_S = 50 * 60

# audience URL → (token, expires_at_unix_ts)
_TOKENS: dict[str, tuple[str, float]] = {}


def get_id_token(audience: str) -> str:
    """Return a Google-issued ID token usable against `audience`.

    Service-account ADC supports `--audiences=<service-url>` to scope
    the token. User-account ADC (laptop dev) does not — the issued
    token has no custom audience and Cloud Run accepts it as long as
    the user has `roles/run.invoker` (or higher, e.g. project owner).
    We try the service-account path first; if it errors with
    "Invalid account type", fall back to the user-account path.

    Tokens cached per-audience for ~50 min. Concurrent callers may
    each spend the gcloud subprocess cost on the same audience the
    first time around — fine, the gcloud call is ~200ms.
    """
    now = time.time()
    cached = _TOKENS.get(audience)
    if cached is not None:
        token, expires_at = cached
        if now < expires_at:
            return token

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
        except subprocess.CalledProcessError as e:
            last_err = e
            stderr = (e.stderr or "").lower()
            # User-account error → try the no-audience form. Anything
            # else (e.g. not logged in) → bubble up after the loop.
            if "invalid account type" not in stderr and "audiences" not in stderr:
                raise
    raise RuntimeError(
        f"could not get a gcloud ID token for audience={audience!r}; "
        f"tried both audience-scoped and plain. Last error: {last_err}"
    )


def _reset_cache_for_tests() -> None:
    """Clear the token cache. Test-only hook so unit tests don't
    leak state across cases."""
    _TOKENS.clear()
