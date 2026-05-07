"""Cloud Run GPU TTS provider — talks to ``ytfactory-tts`` Cloud Run
service in asia-southeast1.

This is the laptop-side counterpart of ``cloud/tts/server.py``. Same
function signatures as ``pipeline.tts.f5._synth_f5_tts`` etc. so the
dispatcher in ``pipeline.audio.synthesize`` can swap any local ``f5_tts``
call for ``cloudrun_f5`` by changing the provider string only.

**Auto-fallback to local.** If the cloud service is unreachable (DNS
fail, 5xx, timeout >120 s), the call transparently falls back to the
local synth function for the same model. This means a Cloud Run outage
or quota exhaustion never blocks a render — we just pay the local
M2 Max wall-clock instead.

**Auth.** Cloud Run service is deployed `--no-allow-unauthenticated`.
We attach a Google ID token from the gcloud Application Default
Credentials. The token's audience MUST be the service URL (NOT the API
endpoint) for Cloud Run to accept it.

Env vars:

* ``CLOUDRUN_TTS_URL`` — required. The full Cloud Run service URL,
  e.g. ``https://ytfactory-tts-767262167641.asia-southeast1.run.app``.
  Set in the laptop's ``.env`` (sourced by the renderer entrypoints).
* ``CLOUDRUN_TTS_COSYVOICE_URL`` — optional. Separate URL for the
  ``ytfactory-tts-cosyvoice`` Cloud Run service (CosyVoice 2 lives in
  its own image — see ``cloud/tts-cosyvoice/`` for why). If set, the
  ``cloudrun_cosyvoice`` provider routes here instead of
  ``CLOUDRUN_TTS_URL``. If unset, requests fall through to
  ``CLOUDRUN_TTS_URL`` (which today returns 400 for cosyvoice — useful
  to detect mis-configuration during rollout).
* ``CLOUDRUN_TTS_TIMEOUT`` — optional. Per-call timeout in seconds.
  Default 180. Long-form chunks rarely need >30 s on L4 warm; the
  large headroom covers cold-start image-pull + model-load (~40 s)
  plus a generous safety margin.
* ``CLOUDRUN_TTS_DISABLE_FALLBACK`` — optional. Set to ``1`` to make
  cloud failures hard-error instead of falling back to local. Useful
  in tests where you want to confirm the cloud path.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ----------------------------------------------------------- token caching
#
# Lifted to `pipeline.cloudrun_auth` 2026-05-07 — see that module for
# the per-audience cache rationale (multi-service rollout requires
# audience-scoped tokens, not the original single global cache). This
# module re-exports `_get_id_token` as a thin wrapper so any existing
# import paths keep working.


from pipeline.cloudrun_auth import get_id_token as _get_id_token  # noqa: E402, F401


# ---------------------------------------------------------------- base URL


def _service_url(*, model: str | None = None) -> str:
    """Return the Cloud Run service URL for a given model.

    Each model lives in its own Cloud Run service (separate container,
    separate dep tree, separate quota slot), so we route per-model:

    * f5          → CLOUDRUN_TTS_F5_URL (or legacy CLOUDRUN_TTS_URL)
    * higgs       → CLOUDRUN_TTS_HIGGS_URL
    * cosyvoice   → CLOUDRUN_TTS_COSYVOICE_URL
    * chatterbox  → CLOUDRUN_TTS_CHATTERBOX_URL
    * indicparler → CLOUDRUN_TTS_INDICPARLER_URL
    * indicf5     → CLOUDRUN_TTS_INDICF5_URL

    Each service-specific env falls back to CLOUDRUN_TTS_URL if unset
    so a one-service deployment still works.
    """
    per_model = {
        "f5":          "CLOUDRUN_TTS_F5_URL",
        "higgs":       "CLOUDRUN_TTS_HIGGS_URL",
        "cosyvoice":   "CLOUDRUN_TTS_COSYVOICE_URL",
        "chatterbox":  "CLOUDRUN_TTS_CHATTERBOX_URL",
        "indicparler": "CLOUDRUN_TTS_INDICPARLER_URL",
        "indicf5":     "CLOUDRUN_TTS_INDICF5_URL",
    }
    if model in per_model:
        specific = os.environ.get(per_model[model], "").strip().rstrip("/")
        if specific:
            return specific
    url = os.environ.get("CLOUDRUN_TTS_URL", "").strip().rstrip("/")
    if not url:
        env_hint = per_model.get(model or "f5", "CLOUDRUN_TTS_URL")
        raise RuntimeError(
            f"cloudrun_{model or 'f5'} provider requires {env_hint} (or "
            f"CLOUDRUN_TTS_URL as fallback) to be set. "
            f"Add it to .env and re-source, or `unset` to force fall back "
            f"to local TTS providers."
        )
    return url


def _timeout_s() -> int:
    try:
        return int(os.environ.get("CLOUDRUN_TTS_TIMEOUT", "180"))
    except ValueError:
        return 180


def _fallback_disabled() -> bool:
    return os.environ.get("CLOUDRUN_TTS_DISABLE_FALLBACK", "").strip() in ("1", "true")


# --------------------------------------------------------------- HTTP call


class CloudRunUnavailable(RuntimeError):
    """Raised when the cloud service can't satisfy the request and the
    caller should fall back to the local provider."""


def _post_synth(payload: dict) -> dict:
    """POST to /synth, return parsed JSON. Raises CloudRunUnavailable
    on connection error / timeout / 5xx so the caller can fall back."""
    url = _service_url(model=payload.get("model"))
    token = _get_id_token(url)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=f"{url}/synth",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx (bad input) → re-raise so caller sees the real error.
        # 5xx (server crash) → CloudRunUnavailable so fallback kicks in.
        if 500 <= e.code < 600:
            raise CloudRunUnavailable(f"cloud /synth {e.code}: {e.read()[:300]!r}") from e
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CloudRunUnavailable(f"cloud /synth network error: {e}") from e


def _materialise_wav(resp: dict, out_path: Path) -> Path:
    """Write the WAV to ``out_path`` from either the inline base64
    field or the GCS URI in the response."""
    if "output_inline" in resp and resp["output_inline"]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(resp["output_inline"]))
        return out_path
    gcs_uri = resp.get("output_gcs")
    if not gcs_uri:
        raise RuntimeError(
            f"cloud /synth response had neither output_inline nor "
            f"output_gcs: keys={list(resp)!r}"
        )
    # Streaming GCS download via gcloud (no extra dep on the laptop).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gcloud", "storage", "cp", gcs_uri, str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


# -------------------------------------------------------- public synth API


def _read_ref_b64(ref_audio_path: str) -> str:
    """Load reference WAV from disk and base64-encode for the wire."""
    return base64.b64encode(Path(ref_audio_path).read_bytes()).decode("ascii")


def _synth_cloudrun(
    *,
    model: str,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """Generic Cloud Run dispatcher used by every cloudrun_<model>
    wrapper below. ref_audio_text required for f5/cosyvoice; optional
    for the rest (matches each model's local-side contract)."""
    payload = {
        "model": model,
        "text": text,
        "ref_audio_b64": _read_ref_b64(ref_audio_path),
        "ref_text": ref_audio_text,
        "speed": speed,
        "seed": seed,
        "output": "inline",
    }
    t0 = time.time()
    resp = _post_synth(payload)
    _materialise_wav(resp, out_path)
    logger.info(
        "cloudrun_%s ok: chars=%d audio=%.2fs cloud_wall=%.2fs "
        "rtf=%.2f e2e_wall=%.2fs",
        model, len(text), resp.get("duration_s", 0.0),
        resp.get("wall_s", 0.0), resp.get("rtf", 0.0),
        time.time() - t0,
    )
    return out_path


def _synth_cloudrun_f5(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """F5-TTS via Cloud Run. Falls back to local F5-MLX on cloud
    failure unless ``CLOUDRUN_TTS_DISABLE_FALLBACK=1``.

    Output WAV is byte-different from the local MLX path (different
    inference runtime), but voice character should be perceptually
    identical because the cloud uses the same checkpoint
    (F5TTS_Base) and same flow-matching params (ode_method=rk4,
    nfe_step=8, cfg_strength=2.0, sway_sampling_coef=-1.0).
    """
    try:
        return _synth_cloudrun(
            model="f5", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning(
            "cloudrun_f5 unavailable (%s); falling back to local f5_tts", e,
        )
        from pipeline.tts.f5 import _synth_f5_tts

        return _synth_f5_tts(
            text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
        )


def _synth_cloudrun_higgs(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """Higgs Audio v2 via Cloud Run.

    On cloud failure, fall back to **local F5-TTS** (per laptop-fallback
    policy 2026-05-06: "F5 for all on laptop"). Higgs has no realistic
    local equivalent (PyTorch-only, ~20× real-time on M2 Max MPS),
    so F5 is the practical degradation path.
    """
    try:
        return _synth_cloudrun(
            model="higgs", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning(
            "cloudrun_higgs unavailable (%s); falling back to local F5-TTS "
            "(per laptop-fallback policy 2026-05-06)", e,
        )
        from pipeline.tts.f5 import _synth_f5_tts

        return _synth_f5_tts(
            text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
        )


def _synth_cloudrun_cosyvoice(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """CosyVoice 2 via Cloud Run.

    On cloud failure, fall back to **local F5-TTS** (per laptop-fallback
    policy 2026-05-06). CosyVoice isn't installed in the laptop venv
    (deepspeed/torch dep tree fights), so F5 is the only viable local
    path.

    NOTE: CosyVoice 2 0.5B does NOT speak Hindi (proven 2026-05-05).
    Use cloudrun_indicparler / kokoro hf_alpha for Hindi instead.
    """
    if not ref_audio_text:
        raise ValueError(
            "cloudrun_cosyvoice requires ref_audio_text "
            "(transcript of the ref WAV)"
        )
    try:
        return _synth_cloudrun(
            model="cosyvoice", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning(
            "cloudrun_cosyvoice unavailable (%s); falling back to local F5-TTS "
            "(per laptop-fallback policy 2026-05-06)", e,
        )
        from pipeline.tts.f5 import _synth_f5_tts

        return _synth_f5_tts(
            text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
        )


def _synth_cloudrun_indicparler(
    text: str,
    ref_audio_path: str | None,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
    description: str | None = None,
) -> Path:
    """Indic Parler-TTS via Cloud Run.

    On cloud failure, fall back to **local Kokoro hf_alpha** (per
    laptop-fallback policy 2026-05-06: "kokoro for hindutava-animated").
    Hindi has no F5 local equivalent — Kokoro is the only Hindi TTS
    that runs on the laptop.

    Uses ``description`` (natural-language voice spec) instead of
    ``ref_audio_path``. Indic Parler is description-driven.
    """
    payload_text = text
    payload_desc = description or (
        "A clear, expressive Indian female voice with moderate pace. "
        "Recording is high quality."
    )
    try:
        return _synth_cloudrun(
            model="indicparler", text=payload_text,
            ref_audio_path=ref_audio_path or "",  # not used by indicparler
            ref_audio_text=payload_desc,  # piggyback the description channel
            out_path=out_path, speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning(
            "cloudrun_indicparler unavailable (%s); falling back to local "
            "Kokoro hf_alpha (per laptop-fallback policy 2026-05-06)", e,
        )
        from pipeline.tts.kokoro import _synth_kokoro

        return _synth_kokoro(
            text=text, voice="hf_alpha", out_path=out_path, speed=speed,
        )


def _synth_cloudrun_indicf5(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """AI4Bharat IndicF5 via Cloud Run.

    F5-TTS architecture fine-tuned on 1417h of curated Indian speech;
    11 Indic languages including Hindi, Bengali, Tamil, etc.
    Voice-clone style — REQUIRES ref_audio_path + ref_audio_text.

    On cloud failure, fall back to **local Kokoro hf_alpha** (per
    laptop-fallback policy 2026-05-06: only on-laptop Hindi voice).
    Kokoro is preset-voice (no clone), so the fallback loses voice
    identity but stays in Hindi.
    """
    if not ref_audio_text:
        raise ValueError(
            "cloudrun_indicf5 requires ref_audio_text "
            "(transcript of the ref WAV; used for prosody anchoring)"
        )
    try:
        return _synth_cloudrun(
            model="indicf5", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning(
            "cloudrun_indicf5 unavailable (%s); falling back to local "
            "Kokoro hf_alpha (per laptop-fallback policy 2026-05-06)", e,
        )
        from pipeline.tts.kokoro import _synth_kokoro

        return _synth_kokoro(
            text=text, voice="hf_alpha", out_path=out_path, speed=speed,
        )


def _synth_cloudrun_chatterbox(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float,
    seed: int | None = None,
) -> Path:
    """Chatterbox via Cloud Run.

    On cloud failure, fall back to **local F5-TTS** (NOT local Chatterbox).

    Why F5 instead of local Chatterbox:
      * User policy 2026-05-06: "for laptop we keep F5 for all" — local
        Chatterbox is too slow on M2 Max MPS (~6-13× real-time) for
        production use. F5-MLX is the canonical laptop TTS.
      * The fallback exists to keep renders unblocked during a Cloud Run
        outage; voice quality match is acceptable degradation.
      * Local Chatterbox provider remains available via explicit
        ``tts_provider: chatterbox`` in a YAML, but is not the cloud
        fallback target.

    Local F5 ignores ``ref_audio_text`` for chunked synthesis — the
    contract still passes through.
    """
    try:
        return _synth_cloudrun(
            model="chatterbox", text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path,
            speed=speed, seed=seed,
        )
    except CloudRunUnavailable as e:
        if _fallback_disabled():
            raise
        logger.warning(
            "cloudrun_chatterbox unavailable (%s); falling back to local F5-TTS "
            "(per laptop-fallback policy 2026-05-06)", e,
        )
        from pipeline.tts.f5 import _synth_f5_tts

        return _synth_f5_tts(
            text=text, ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text, out_path=out_path, speed=speed,
        )
