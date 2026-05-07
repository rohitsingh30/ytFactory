"""Cloud Run GPU image-gen provider — talks to the
``ytfactory-image-*`` Cloud Run services in asia-southeast1.

Counterpart to ``cloud/image-flux2-klein/server.py`` (and a future
``cloud/image-z-image-turbo/server.py``). Same function signature as
``pipeline.images._generate_z_image_turbo`` etc. so the dispatcher in
``pipeline.images.generate`` can swap any local provider for a
``cloudrun_<model>`` provider by changing the provider string only.

**Key divergence from the TTS client (`pipeline.tts.cloudrun`):
render-level circuit breaker.** Each Short renders ~30 images on the
critical path. If the cloud service is down and we per-image fall
back to local mflux, that's 30 × ~120 s timeout = 1 hour added to
the wall-clock for a single Short. Instead, the FIRST CloudRunUnavailable
in a render trips a module-level breaker → all subsequent calls in
the same process skip cloud entirely. Renderer entry points call
``reset_circuit_breaker()`` at the top of every render to clear the
flag.

**HTTP client: ``requests`` (not ``urllib``).** Earlier rev of this
file used ``urllib.request.urlopen``; that revealed a class of bug
during canary 2026-05-07 where cold-load + idle TCP timeouts on the
Cloud Run LB severed the laptop's connection mid-request, but
``urllib``'s blocking read didn't notice — the client hung until its
own 900 s timeout fired even when the server had returned 200 OK
minutes earlier. ``requests`` uses a fresh ``requests.Session()``
per call, no connection-pool reuse across stale TCP, plus an
explicit retry on connection-reset errors.

Env vars:

* ``CLOUDRUN_IMAGE_FLUX2_KLEIN_URL`` — the FLUX.2 [klein] 4B service
  URL (e.g. ``https://ytfactory-image-flux2-klein-...run.app``).
* ``CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL`` — the Z-Image-Turbo 6B service
  URL. Optional today (Z-Image cloud cold-load reliability still
  WIP, see memory/feedback_zimage_cloudrun_coldload_stall.md and
  P3.5 todo).
* ``CLOUDRUN_IMAGE_TIMEOUT`` — per-call HTTP timeout in seconds.
  Default 900 (15 min, generous because a cold instance pays
  ~5-7 min cold-load before responding to /generate).
* ``CLOUDRUN_IMAGE_DISABLE_FALLBACK`` — set to ``1`` to make cloud
  failures hard-error instead of falling back to local mflux. Use
  in canary to surface cloud bugs.
* ``CLOUDRUN_IMAGE_FALLBACK_MODE`` — ``once_per_render`` (default,
  trip the breaker on first failure) or ``per_image`` (legacy
  per-call fallback; only for debugging).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

from pipeline.cloudrun_auth import get_id_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- base URL


def _service_url(model: str) -> str:
    """Return the Cloud Run service URL for `model`.

    Each image model lives in its own Cloud Run service (separate
    container, separate dep tree, separate quota slot), so we route
    per-model:

    * flux2_klein     → CLOUDRUN_IMAGE_FLUX2_KLEIN_URL
    * z_image_turbo   → CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL
    * (future) qwen_image, hidream
    """
    per_model = {
        "flux2_klein":   "CLOUDRUN_IMAGE_FLUX2_KLEIN_URL",
        "z_image_turbo": "CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL",
        "qwen_image":    "CLOUDRUN_IMAGE_QWEN_URL",
        "hidream":       "CLOUDRUN_IMAGE_HIDREAM_URL",
    }
    if model not in per_model:
        raise ValueError(
            f"unknown cloudrun image model: {model!r}. "
            f"Valid: {sorted(per_model)}"
        )
    env_var = per_model[model]
    url = os.environ.get(env_var, "").strip().rstrip("/")
    if not url:
        raise CloudRunUnavailable(
            f"cloudrun_{model} requires {env_var} to be set in .env. "
            f"Without it the cloud path is unavailable; the caller's "
            f"fallback path will activate."
        )
    return url


def _timeout_s() -> int:
    """Default 900s (15 min). The FLUX.2 klein cold-load measured
    7-8 min on Cloud Run L4 from GCS Fuse + an actual 4-step inference
    runs ~3 s, so 900s gives a comfortable 6-7 min margin even on cold
    instances. Render entry points should ALSO call
    `pipeline.images.warmup("cloudrun_flux2_klein")` early so the first
    real /generate hits a warm container — see canary 2026-05-07
    where the default 600s tripped exactly at the cold-load envelope."""
    try:
        return int(os.environ.get("CLOUDRUN_IMAGE_TIMEOUT", "900"))
    except ValueError:
        return 900


def _fallback_disabled() -> bool:
    return os.environ.get("CLOUDRUN_IMAGE_DISABLE_FALLBACK", "").strip() in ("1", "true")


def _fallback_mode() -> str:
    """`once_per_render` (default) or `per_image`."""
    mode = os.environ.get("CLOUDRUN_IMAGE_FALLBACK_MODE", "").strip().lower()
    if mode in ("once_per_render", "per_image"):
        return mode
    return "once_per_render"


# ----------------------------------------------------- circuit breaker

# `_CLOUD_DISABLED_THIS_RENDER` is set to True on the first
# CloudRunUnavailable we see in `once_per_render` mode. Renderer
# entry points call `reset_circuit_breaker()` at the top of every
# render to clear it, so it's per-render not per-process.
_BREAKER_LOCK = threading.Lock()
_CLOUD_DISABLED_THIS_RENDER: bool = False
_BREAKER_REASON: str | None = None


def reset_circuit_breaker() -> None:
    """Clear the per-render circuit breaker. Call at the top of
    every render entry point (make_shorts, render/long_form,
    render/footage_only, render/sports_doc) so a previous render's
    cloud failure doesn't carry over to the next one."""
    global _CLOUD_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if _CLOUD_DISABLED_THIS_RENDER:
            logger.info(
                "cloudrun image circuit breaker reset (was tripped: %s)",
                _BREAKER_REASON,
            )
        _CLOUD_DISABLED_THIS_RENDER = False
        _BREAKER_REASON = None


def _trip_breaker(reason: str) -> None:
    global _CLOUD_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if not _CLOUD_DISABLED_THIS_RENDER:
            logger.warning(
                "cloudrun image circuit breaker TRIPPED — falling back "
                "to local mflux for the rest of this render (reason: %s)",
                reason,
            )
            _CLOUD_DISABLED_THIS_RENDER = True
            _BREAKER_REASON = reason


def _breaker_open() -> bool:
    with _BREAKER_LOCK:
        return _CLOUD_DISABLED_THIS_RENDER


# --------------------------------------------------------------- HTTP call


class CloudRunUnavailable(RuntimeError):
    """Raised when the cloud service can't satisfy the request and
    the caller should fall back to the local provider (or trip the
    breaker, if `once_per_render` mode)."""


def _post_generate(url: str, payload: dict) -> dict:
    """POST to /generate, return parsed JSON. Raises
    CloudRunUnavailable on connection error / timeout / 5xx so the
    caller can fall back.

    Retries ONCE on any connection error (ConnectionError /
    ChunkedEncodingError / ReadTimeout) — the most common failure
    mode is a stale TCP connection severed by Cloud Run's LB during
    a long cold-load wait, and a fresh connection then succeeds.
    Each call uses a fresh `requests.Session` so we never re-use a
    pooled connection across stage boundaries.
    """
    token = get_id_token(url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = _timeout_s()
    last_err: Exception | None = None
    for attempt in (1, 2):
        sess = requests.Session()
        try:
            resp = sess.post(
                f"{url}/generate", data=json.dumps(payload),
                headers=headers, timeout=timeout,
            )
            if 500 <= resp.status_code < 600:
                raise CloudRunUnavailable(
                    f"cloud /generate {resp.status_code}: "
                    f"{resp.text[:300]!r}"
                )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            # 4xx (caller bug — bad input) → re-raise without fallback.
            raise
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
        ) as e:
            last_err = e
            logger.warning(
                "cloudrun /generate attempt %d/2 failed (%s); %s",
                attempt, type(e).__name__,
                "retrying with fresh connection" if attempt == 1
                else "giving up → CloudRunUnavailable",
            )
        except CloudRunUnavailable:
            raise  # already wrapped, no retry
        finally:
            sess.close()
    raise CloudRunUnavailable(
        f"cloud /generate network error after 2 attempts: {last_err}"
    ) from last_err


def _materialise_png(resp: dict, out_path: Path) -> Path:
    """Write the PNG to `out_path` from either the inline base64
    field or the GCS URI in the response."""
    if "output_inline" in resp and resp["output_inline"]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(resp["output_inline"]))
        return out_path
    gcs_uri = resp.get("output_gcs")
    if not gcs_uri:
        raise RuntimeError(
            f"cloud /generate response had neither output_inline nor "
            f"output_gcs: keys={list(resp)!r}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gcloud", "storage", "cp", gcs_uri, str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


# -------------------------------------------------------- public generate API


def _generate_cloudrun(
    *,
    model: str,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Generic Cloud Run dispatcher used by every cloudrun_<model>
    wrapper below. Same signature as the local `_generate_z_image_turbo`
    / `_generate_mflux` so the orchestrator can pass through unchanged."""
    url = _service_url(model)
    payload = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "seed": seed,
        "output": "inline",
    }
    t0 = time.time()
    resp = _post_generate(url, payload)
    _materialise_png(resp, out_path)
    logger.info(
        "cloudrun_%s ok: prompt_len=%d %sx%s steps=%d "
        "cloud_wall=%.2fs e2e_wall=%.2fs%s",
        model, len(prompt), resp.get("width", "?"), resp.get("height", "?"),
        resp.get("steps", steps),
        resp.get("wall_s", 0.0), time.time() - t0,
        " (cold)" if resp.get("cold_loaded") else "",
    )
    return out_path


# ------------------------------------------------------- per-model wrappers


def _generate_cloudrun_flux2_klein(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """FLUX.2 [klein] 4B via Cloud Run.

    Falls back to local mflux Z-Image-Turbo on cloud failure unless
    `CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` (canary use).

    Per the render-level circuit breaker policy: the FIRST cloud
    failure in a render trips the breaker and all subsequent calls
    skip cloud immediately, so a 30-image Short with a cloud outage
    pays at most ONE timeout (~10 min) instead of 30 × timeout.

    Why fall back to z_image_turbo (mflux) and not flux Schnell:
    z_image_turbo is the stable local-laptop default we ship today
    and produces output stylistically close to FLUX.2 klein. mflux
    Flux Schnell exists but is slower and used by only one variant
    (mystoriesanimated/variants/tifu.yaml).
    """
    if _breaker_open():
        logger.info(
            "cloudrun_flux2_klein: breaker open this render → "
            "skipping cloud, going straight to local z_image_turbo"
        )
        return _local_fallback(
            prompt=prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )

    try:
        return _generate_cloudrun(
            model="flux2_klein", prompt=prompt, seed=seed,
            out_path=out_path, width=width, height=height,
            steps=_clamp_steps(steps, lo=2, hi=8, default=4),
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        if _fallback_mode() == "once_per_render":
            _trip_breaker(str(e))
        logger.warning(
            "cloudrun_flux2_klein unavailable (%s); falling back to "
            "local z_image_turbo (mflux)", e,
        )
        return _local_fallback(
            prompt=prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )


def _generate_cloudrun_z_image_turbo(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Z-Image-Turbo 6B via Cloud Run.

    On cloud failure, falls back to **local mflux Z-Image-Turbo**
    (same model, MLX runtime). Voice / per-pixel output bytes will
    differ between cloud-diffusers and local-mflux but
    style/character lock is preserved.

    Same circuit-breaker policy as `_generate_cloudrun_flux2_klein`.
    """
    if _breaker_open():
        logger.info(
            "cloudrun_z_image_turbo: breaker open this render → "
            "skipping cloud, going straight to local z_image_turbo"
        )
        return _local_fallback(
            prompt=prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )

    try:
        return _generate_cloudrun(
            model="z_image_turbo", prompt=prompt, seed=seed,
            out_path=out_path, width=width, height=height,
            steps=_clamp_steps(steps, lo=4, hi=12, default=9),
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        if _fallback_mode() == "once_per_render":
            _trip_breaker(str(e))
        logger.warning(
            "cloudrun_z_image_turbo unavailable (%s); falling back to "
            "local z_image_turbo (mflux)", e,
        )
        return _local_fallback(
            prompt=prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )


def _clamp_steps(steps: int, *, lo: int, hi: int, default: int) -> int:
    """Clamp step count to the model's valid range; fall back to
    `default` if outside. Keeps the wire payload safe even if the
    caller passes weird numbers (e.g. legacy 4-step value when we
    deploy a model that needs 8+ steps)."""
    if steps < lo or steps > hi:
        logger.info(
            "cloudrun image: clamping steps=%d into [%d, %d] → using %d",
            steps, lo, hi, default,
        )
        return default
    return steps


def _local_fallback(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Call the local `_generate_z_image_turbo` (mflux). Lazy-imported
    to avoid pulling MLX into every process that touches this
    module (e.g. the web server)."""
    from pipeline.images import _generate_z_image_turbo
    return _generate_z_image_turbo(
        prompt=prompt, seed=seed, out_path=out_path,
        width=width, height=height, steps=steps,
    )


# ----------------------------------------------------------------- warmup


def _readyz(model: str) -> dict:
    """GET /readyz on a service. Used to pay the cold-load tax
    BEFORE a render starts. Returns the server's JSON response.

    Uses requests (not urllib) so a Cloud Run LB severance during
    the long cold-load doesn't leave us hanging on a dead socket
    (see _post_generate docstring for the urllib post-mortem)."""
    url = _service_url(model)
    token = get_id_token(url)
    sess = requests.Session()
    try:
        resp = sess.get(
            f"{url}/readyz",
            headers={"Authorization": f"Bearer {token}"},
            timeout=900,  # cold-load can be 5-7 min on FLUX, longer on Z-Image
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        sess.close()


def warmup(model: str) -> threading.Thread:
    """Fire-and-forget background-thread warmup of `model`. Returns
    the thread so the caller can `.join(timeout=...)` if it wants
    to. Failures are logged but don't propagate — warmup is best-
    effort; the real /generate call surfaces any actual problem."""
    def _go() -> None:
        try:
            t0 = time.time()
            r = _readyz(model)
            logger.info(
                "cloudrun_%s warmup ok: cold=%s warm_s=%s "
                "boot_uptime_s=%s e2e_wall=%.2fs",
                model, r.get("cold_loaded"), r.get("warm_s"),
                r.get("boot_uptime_s"), time.time() - t0,
            )
        except CloudRunUnavailable as e:
            logger.info(
                "cloudrun_%s warmup skipped — service URL unset (%s)",
                model, e,
            )
        except Exception as e:
            logger.warning(
                "cloudrun_%s warmup failed: %s — actual /generate "
                "call will surface the real error or fall back",
                model, e,
            )

    t = threading.Thread(target=_go, daemon=True, name=f"cloudrun-warmup-{model}")
    t.start()
    return t
