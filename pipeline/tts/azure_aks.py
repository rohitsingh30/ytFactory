"""Azure AKS GPU TTS provider — talks to the cloud_azure cluster in
``australiaeast``. Parallel to ``pipeline.tts.cloudrun`` (GCP) so the
existing Cloud Run path is untouched and remains primary today.

Channels opt in to Azure by setting ``tts_provider: azure_<model>`` in
their YAML; without that flip, the dispatcher in ``pipeline.audio``
keeps using ``cloudrun_<model>`` and Azure is dormant.

**3-tier fallback chain:**

    azure_<model>  —[fail]→  cloudrun_<model>  —[fail]→  local f5_tts

The first two tiers are TLS-fronted GPU services in different clouds; if
both go down the laptop's local F5-MLX path takes over. To pin tests to
a specific tier set ``AZURE_TTS_DISABLE_FALLBACK=1`` (Azure-only) or
``CLOUDRUN_TTS_DISABLE_FALLBACK=1`` (GCP-only); leave both unset for
production behavior.

Env vars:

* ``AZURE_TTS_F5_URL`` / ``AZURE_TTS_HIGGS_URL`` /
  ``AZURE_TTS_CHATTERBOX_URL`` / ``AZURE_TTS_COSYVOICE_URL`` /
  ``AZURE_TTS_INDICPARLER_URL`` / ``AZURE_TTS_INDICF5_URL`` —
  ingress-routed URLs from ``cloud_azure/print_envs.sh``.
* ``AZURE_API_KEY`` — shared secret in ``X-API-Key`` header.
* ``AZURE_TTS_TIMEOUT`` — per-call timeout (s). Default 240 (more
  generous than Cloud Run's 180 because KEDA cold-start on Spot can
  add ~60 s on top of the model's normal load).
* ``AZURE_TTS_DISABLE_FALLBACK`` — ``1`` → hard-error on Azure failure
  instead of trying GCP next.
* ``AZURE_TLS_VERIFY`` — ``0`` to skip cert verify (self-signed nip.io).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

import requests

from pipeline.utils.azure_auth import AzureKeyMissing, get_api_key, tls_verify

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- base URL


_PER_MODEL_ENV = {
    "f5":          "AZURE_TTS_F5_URL",
    "higgs":       "AZURE_TTS_HIGGS_URL",
    "cosyvoice":   "AZURE_TTS_COSYVOICE_URL",
    "chatterbox":  "AZURE_TTS_CHATTERBOX_URL",
    "indicparler": "AZURE_TTS_INDICPARLER_URL",
    "indicf5":     "AZURE_TTS_INDICF5_URL",
}


class AzureUnavailable(RuntimeError):
    """Cloud Azure can't satisfy the request → caller should try GCP next."""


def _service_url(model: str) -> str:
    env_name = _PER_MODEL_ENV.get(model)
    if env_name is None:
        raise ValueError(f"unknown Azure TTS model: {model!r}. "
                         f"valid: {sorted(_PER_MODEL_ENV)}")
    url = os.environ.get(env_name, "").strip().rstrip("/")
    if not url:
        raise AzureUnavailable(
            f"azure_{model} requires {env_name} (from cloud_azure/print_envs.sh)"
        )
    return url


def _timeout_s() -> int:
    try:
        return int(os.environ.get("AZURE_TTS_TIMEOUT", "240"))
    except ValueError:
        return 240


def _fallback_disabled() -> bool:
    return os.environ.get("AZURE_TTS_DISABLE_FALLBACK", "").strip() in ("1", "true")


# --------------------------------------------------------------- HTTP call


def _post_synth(payload: dict) -> dict:
    """POST /synth, raise AzureUnavailable on connection error / 5xx."""
    base = _service_url(payload["model"])
    try:
        api_key = get_api_key()
    except AzureKeyMissing as e:
        raise AzureUnavailable(str(e)) from e
    try:
        with requests.Session() as s:
            r = s.post(
                f"{base}/synth",
                json=payload,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                timeout=_timeout_s(),
                verify=tls_verify(),
            )
        if 500 <= r.status_code < 600:
            raise AzureUnavailable(f"azure /synth {r.status_code}: {r.text[:300]!r}")
        r.raise_for_status()
        return r.json()
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
        raise AzureUnavailable(f"azure /synth network error: {e}") from e


def _materialise_wav(resp: dict, out_path: Path) -> Path:
    """Inline-only — Azure ingress doesn't have GCS read permission, so
    server.py always returns base64 inline. (cloudrun.py supports GCS
    URIs because the Cloud Run service has a per-project bucket.)"""
    if "output_inline" not in resp or not resp["output_inline"]:
        raise RuntimeError(
            f"azure /synth response missing output_inline; keys={list(resp)!r}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(resp["output_inline"]))
    return out_path


# -------------------------------------------------------- public synth API


def _read_ref_b64(p: str) -> str:
    return base64.b64encode(Path(p).read_bytes()).decode("ascii")


def _synth_azure(
    *,
    model: str,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """Generic Azure dispatcher — same payload shape as cloudrun.
    server.py is byte-identical between cloud/<svc>/ and cloud_azure/<svc>/.
    """
    ref_b64 = _read_ref_b64(ref_audio_path) if ref_audio_path else ""
    payload = {
        "model": model,
        "text": text,
        "ref_audio_b64": ref_b64,
        "ref_text": ref_audio_text,
        "speed": speed,
        "seed": seed,
        "output": "inline",
    }
    t0 = time.time()
    resp = _post_synth(payload)
    _materialise_wav(resp, out_path)
    logger.info(
        "azure_%s ok: chars=%d audio=%.2fs cloud_wall=%.2fs e2e_wall=%.2fs",
        model, len(text), resp.get("duration_s", 0.0), resp.get("wall_s", 0.0),
        time.time() - t0,
    )
    return out_path


# -------------------------------------------------- per-model wrappers


def _with_fallback_to_cloudrun(
    *,
    azure_model: str,
    cloudrun_fn,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None,
) -> Path:
    """Try Azure first; on AzureUnavailable, fall through to the GCP
    Cloud Run wrapper which itself falls back to local."""
    try:
        return _synth_azure(
            model=azure_model, text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except AzureUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning("azure_%s unavailable (%s); falling through to GCP",
                       azure_model, e)
        return cloudrun_fn(
            text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )


def _synth_azure_f5(text, ref_audio_path, ref_audio_text, out_path, speed, seed=None):
    from pipeline.tts.cloudrun import _synth_cloudrun_f5
    return _with_fallback_to_cloudrun(
        azure_model="f5", cloudrun_fn=_synth_cloudrun_f5,
        text=text, ref_audio_path=ref_audio_path, ref_audio_text=ref_audio_text,
        out_path=out_path, speed=speed, seed=seed,
    )


def _synth_azure_higgs(text, ref_audio_path, ref_audio_text, out_path, speed, seed=None):
    from pipeline.tts.cloudrun import _synth_cloudrun_higgs
    return _with_fallback_to_cloudrun(
        azure_model="higgs", cloudrun_fn=_synth_cloudrun_higgs,
        text=text, ref_audio_path=ref_audio_path, ref_audio_text=ref_audio_text,
        out_path=out_path, speed=speed, seed=seed,
    )


def _synth_azure_chatterbox(text, ref_audio_path, ref_audio_text, out_path, speed, seed=None):
    from pipeline.tts.cloudrun import _synth_cloudrun_chatterbox
    return _with_fallback_to_cloudrun(
        azure_model="chatterbox", cloudrun_fn=_synth_cloudrun_chatterbox,
        text=text, ref_audio_path=ref_audio_path, ref_audio_text=ref_audio_text,
        out_path=out_path, speed=speed, seed=seed,
    )


def _synth_azure_cosyvoice(text, ref_audio_path, ref_audio_text, out_path, speed, seed=None):
    from pipeline.tts.cloudrun import _synth_cloudrun_cosyvoice
    return _with_fallback_to_cloudrun(
        azure_model="cosyvoice", cloudrun_fn=_synth_cloudrun_cosyvoice,
        text=text, ref_audio_path=ref_audio_path, ref_audio_text=ref_audio_text,
        out_path=out_path, speed=speed, seed=seed,
    )


def _synth_azure_indicparler(text, ref_audio_path, ref_audio_text, out_path, speed, seed=None):
    from pipeline.tts.cloudrun import _synth_cloudrun_indicparler
    return _with_fallback_to_cloudrun(
        azure_model="indicparler", cloudrun_fn=_synth_cloudrun_indicparler,
        text=text, ref_audio_path=ref_audio_path, ref_audio_text=ref_audio_text,
        out_path=out_path, speed=speed, seed=seed,
    )


def _synth_azure_indicf5(text, ref_audio_path, ref_audio_text, out_path, speed, seed=None):
    from pipeline.tts.cloudrun import _synth_cloudrun_indicf5
    return _with_fallback_to_cloudrun(
        azure_model="indicf5", cloudrun_fn=_synth_cloudrun_indicf5,
        text=text, ref_audio_path=ref_audio_path, ref_audio_text=ref_audio_text,
        out_path=out_path, speed=speed, seed=seed,
    )
