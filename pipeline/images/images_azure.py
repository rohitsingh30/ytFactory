"""Azure AKS GPU image-gen provider — parallel to ``pipeline.images_cloudrun``.

Channels opt in by setting ``image_provider: azure_<model>`` in their
YAML; otherwise the dispatcher keeps using ``cloudrun_<model>`` and the
Azure cluster is dormant.

**3-tier fallback chain (per render):**

    azure_<model>  —[fail]→  cloudrun_<model>  —[fail]→  local mflux

Both Azure and Cloud Run have render-level circuit breakers — once Azure
trips in a render, every subsequent call goes straight to Cloud Run. If
Cloud Run also trips, the local mflux fallback handles it. The breaker is
reset at the top of each render entry point (``make_short``,
``render`` for footage_only, ``render_long_form``, ``render_sports_doc``).

Env vars: ``AZURE_IMAGE_FLUX2_KLEIN_URL`` / ``AZURE_IMAGE_Z_IMAGE_TURBO_URL`` /
``AZURE_IMAGE_QWEN_URL`` / ``AZURE_IMAGE_HIDREAM_URL``,
``AZURE_API_KEY``, ``AZURE_IMAGE_TIMEOUT`` (default 900),
``AZURE_IMAGE_DISABLE_FALLBACK``, ``AZURE_TLS_VERIFY``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

from pipeline.utils.azure_auth import AzureKeyMissing, get_api_key, tls_verify

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- base URL


_PER_MODEL_ENV = {
    "flux2_klein":   "AZURE_IMAGE_FLUX2_KLEIN_URL",
    "z_image_turbo": "AZURE_IMAGE_Z_IMAGE_TURBO_URL",
    "qwen_image":    "AZURE_IMAGE_QWEN_URL",
    "hidream":       "AZURE_IMAGE_HIDREAM_URL",
}


class AzureImageUnavailable(RuntimeError):
    """Azure cloud unavailable; caller should fall through to GCP."""


def _service_url(model: str) -> str:
    env = _PER_MODEL_ENV.get(model)
    if env is None:
        raise ValueError(f"unknown Azure image model: {model!r}. "
                         f"valid: {sorted(_PER_MODEL_ENV)}")
    url = os.environ.get(env, "").strip().rstrip("/")
    if not url:
        raise AzureImageUnavailable(
            f"azure_{model} requires {env} (from cloud_azure/print_envs.sh)"
        )
    return url


def _timeout_s() -> int:
    try:
        return int(os.environ.get("AZURE_IMAGE_TIMEOUT", "900"))
    except ValueError:
        return 900


def _fallback_disabled() -> bool:
    return os.environ.get("AZURE_IMAGE_DISABLE_FALLBACK", "").strip() in ("1", "true")


# ------------------------------------------------ per-render circuit breaker

_BREAKER_LOCK = threading.Lock()
_AZURE_DISABLED_THIS_RENDER: bool = False
_BREAKER_REASON: str | None = None


def reset_circuit_breaker() -> None:
    """Clear the per-render breaker. Call at the top of every render
    entry point — same pattern as ``images_cloudrun.reset_circuit_breaker``.
    """
    global _AZURE_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if _AZURE_DISABLED_THIS_RENDER:
            logger.info("azure image circuit breaker reset (was: %s)", _BREAKER_REASON)
        _AZURE_DISABLED_THIS_RENDER = False
        _BREAKER_REASON = None


def _trip_breaker(reason: str) -> None:
    global _AZURE_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if not _AZURE_DISABLED_THIS_RENDER:
            _AZURE_DISABLED_THIS_RENDER = True
            _BREAKER_REASON = reason
            logger.warning("azure image circuit breaker TRIPPED: %s", reason)


def _is_breaker_tripped() -> bool:
    with _BREAKER_LOCK:
        return _AZURE_DISABLED_THIS_RENDER


# --------------------------------------------------------------- HTTP call


def _post_generate(payload: dict, model: str) -> dict:
    base = _service_url(model)
    try:
        api_key = get_api_key()
    except AzureKeyMissing as e:
        raise AzureImageUnavailable(str(e)) from e
    try:
        with requests.Session() as s:
            r = s.post(
                f"{base}/generate",
                json=payload,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                timeout=_timeout_s(),
                verify=tls_verify(),
            )
        if 500 <= r.status_code < 600:
            raise AzureImageUnavailable(
                f"azure /generate {r.status_code}: {r.text[:300]!r}"
            )
        r.raise_for_status()
        return r.json()
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
        raise AzureImageUnavailable(f"azure /generate network error: {e}") from e


def _materialise_png(resp: dict, out_path: Path) -> Path:
    if "output_inline" not in resp or not resp["output_inline"]:
        raise RuntimeError(
            f"azure /generate response missing output_inline; keys={list(resp)!r}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(resp["output_inline"]))
    return out_path


# ------------------------------------------------ public per-model API


def _generate_azure(
    *,
    model: str,
    prompt: str,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
    seed: int | None = None,
    cfg: float | None = None,
    **extras,
) -> Path:
    payload = {
        "model": model, "prompt": prompt,
        "width": width, "height": height,
        "steps": steps, "seed": seed,
        "cfg": cfg, "output": "inline", **extras,
    }
    t0 = time.time()
    resp = _post_generate(payload, model)
    _materialise_png(resp, out_path)
    logger.info(
        "azure_%s ok: %dx%d steps=%d cloud_wall=%.2fs e2e_wall=%.2fs",
        model, width, height, steps,
        resp.get("wall_s", 0.0), time.time() - t0,
    )
    return out_path


def _with_fallback(
    *,
    azure_model: str,
    cloudrun_fn,
    prompt: str,
    out_path: Path,
    **kwargs,
) -> Path:
    """Try Azure (subject to circuit breaker); on failure trip the
    breaker and call the GCP cloudrun fn (which has its own breaker +
    local fallback)."""
    if _is_breaker_tripped():
        return cloudrun_fn(prompt=prompt, out_path=out_path, **kwargs)
    try:
        return _generate_azure(
            model=azure_model, prompt=prompt, out_path=out_path, **kwargs,
        )
    except AzureImageUnavailable as e:
        _trip_breaker(str(e))
        if _fallback_disabled():
            raise
        logger.warning("azure_%s unavailable (%s); falling through to GCP",
                       azure_model, e)
        return cloudrun_fn(prompt=prompt, out_path=out_path, **kwargs)


def _generate_azure_flux2_klein(prompt, out_path, width, height, steps, seed=None, cfg=None, **kw):
    from pipeline.images.images_cloudrun import _generate_cloudrun_flux2_klein
    return _with_fallback(
        azure_model="flux2_klein", cloudrun_fn=_generate_cloudrun_flux2_klein,
        prompt=prompt, out_path=out_path,
        width=width, height=height, steps=steps, seed=seed, cfg=cfg, **kw,
    )


def _generate_azure_z_image_turbo(prompt, out_path, width, height, steps, seed=None, cfg=None, **kw):
    from pipeline.images.images_cloudrun import _generate_cloudrun_z_image_turbo
    return _with_fallback(
        azure_model="z_image_turbo", cloudrun_fn=_generate_cloudrun_z_image_turbo,
        prompt=prompt, out_path=out_path,
        width=width, height=height, steps=steps, seed=seed, cfg=cfg, **kw,
    )


def _generate_azure_qwen_image(prompt, out_path, width, height, steps, seed=None, cfg=None, **kw):
    from pipeline.images.images_cloudrun import _generate_cloudrun_qwen_image
    return _with_fallback(
        azure_model="qwen_image", cloudrun_fn=_generate_cloudrun_qwen_image,
        prompt=prompt, out_path=out_path,
        width=width, height=height, steps=steps, seed=seed, cfg=cfg, **kw,
    )


def _generate_azure_hidream(prompt, out_path, width, height, steps, seed=None, cfg=None, **kw):
    from pipeline.images.images_cloudrun import _generate_cloudrun_hidream
    return _with_fallback(
        azure_model="hidream", cloudrun_fn=_generate_cloudrun_hidream,
        prompt=prompt, out_path=out_path,
        width=width, height=height, steps=steps, seed=seed, cfg=cfg, **kw,
    )


def warmup(model: str) -> None:
    """Best-effort GET /healthz to nudge KEDA scale-up before the first
    real request. Mirrors images_cloudrun.warmup contract."""
    try:
        base = _service_url(model)
    except (ValueError, AzureImageUnavailable):
        return
    try:
        api_key = get_api_key()
    except AzureKeyMissing:
        return
    try:
        requests.get(
            f"{base}/healthz",
            headers={"X-API-Key": api_key},
            timeout=10, verify=tls_verify(),
        )
    except Exception:
        pass
