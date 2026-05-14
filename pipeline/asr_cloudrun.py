"""Cloud Run ASR client — talks to ``ytfactory-asr-whisper``.

Laptop-side counterpart of ``cloud/asr-whisper/server.py``. Used by:

* :class:`pipeline.render.timeline.asr_beats.AsrBeats` — short engine
  timeline build.
* :class:`pipeline.render.timeline.asr_anchors.AsrAnchors` — long engine
  timeline build.

Both delegate via :func:`align_via_cloud` which:

1. Uploads the narration wav to a temp GCS object (or inlines it as
   base64 for files < 5 MB) so the cloud service doesn't need disk
   access to the laptop.
2. POSTs ``/align`` with ``{wav_url, anchors?, mode}``.
3. Returns the parsed segment list as
   ``list[pipeline.render.contracts.Segment]``.
4. On any failure (DNS, 5xx, timeout > CLOUDRUN_ASR_TIMEOUT) raises
   :class:`pipeline.cloud.errors.CloudRunUnavailable` so the caller
   can fall back to local whisper.

Same shape as :mod:`pipeline.tts.cloudrun` and
:mod:`pipeline.images_cloudrun` — cloud→laptop fallback is the
universal pattern.

Env vars
--------

- ``CLOUDRUN_ASR_URL`` — required for cloud routing. Full Cloud Run
  service URL (e.g. ``https://ytfactory-asr-whisper-...run.app``).
  When unset, :func:`align_via_cloud` raises ``CloudRunUnavailable``
  immediately so callers fall back without paying network cost.
- ``CLOUDRUN_ASR_TIMEOUT`` — optional per-call timeout in seconds.
  Default 600 (whisper on long narration can take ~5 min).
- ``CLOUDRUN_ASR_DISABLE_FALLBACK`` — set to ``1`` in tests where
  you want cloud failures to hard-error instead of triggering local
  fallback at the call site.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import requests

from pipeline.render.contracts import Segment

_logger = logging.getLogger(__name__)


class CloudRunAsrUnavailable(RuntimeError):
    """Raised when the cloud ASR service is unreachable / unconfigured.

    The asr_beats / asr_anchors plugins catch this and fall back to
    local ``pipeline.asr.transcribe`` (whisper_mlx).
    """


def _service_url() -> str:
    url = os.environ.get("CLOUDRUN_ASR_URL", "").strip()
    if not url:
        raise CloudRunAsrUnavailable(
            "CLOUDRUN_ASR_URL not set — cloud ASR routing disabled. "
            "Caller should fall back to local whisper. Deploy the service "
            "via cloud/asr-whisper/deploy.sh and set the env var to enable."
        )
    return url.rstrip("/")


def _timeout_s() -> float:
    raw = os.environ.get("CLOUDRUN_ASR_TIMEOUT", "600").strip()
    try:
        return float(raw)
    except ValueError:
        return 600.0


def _id_token_for(audience: str) -> str:
    """Reuse the cloud-run auth helper used by tts/cloudrun + image/cloudrun."""
    from pipeline.cloudrun_auth import get_id_token  # noqa: PLC0415
    return get_id_token(audience=audience)


def align_via_cloud(
    narration_wav: Path,
    *,
    mode: str = "beats",
    anchors: list[str] | None = None,
    language: str | None = None,
    max_words_per_beat: int = 8,
) -> list[Segment]:
    """Call the Cloud Run ASR /align endpoint.

    Returns a list of :class:`pipeline.render.contracts.Segment`.
    Raises :class:`CloudRunAsrUnavailable` on any cloud-side failure
    so the caller (typically a TimelineBuilder plugin) can fall back
    to local whisper.

    Args:
        narration_wav: Path to the narration wav on the laptop.
        mode: One of "beats" / "anchors" / "words".
        anchors: For ``mode=anchors``, the list of authored section/
            chapter title texts to anchor against the narration.
        language: Force a specific language code (en / hi / …). Default
            = whisper auto-detect.
        max_words_per_beat: For ``mode=beats``, words-per-beat cap before
            forcing a split. Default 8 matches shorts.py historical
            behavior.
    """
    base_url = _service_url()
    align_url = f"{base_url}/align"

    if not narration_wav.exists():
        raise FileNotFoundError(f"narration wav not found: {narration_wav}")

    payload: dict[str, Any] = {
        "mode": mode,
        "max_words_per_beat": max_words_per_beat,
    }
    if anchors:
        payload["anchors"] = list(anchors)
    if language:
        payload["language"] = language

    # File-size routing: < 5 MB goes inline (one fewer round-trip);
    # larger files upload to GCS first.
    size = narration_wav.stat().st_size
    if size < 5 * 1024 * 1024:
        payload["narration_wav_b64"] = base64.b64encode(
            narration_wav.read_bytes()
        ).decode("ascii")
    else:
        payload["narration_wav_url"] = _upload_to_gcs(narration_wav)

    headers = {
        "Authorization": f"Bearer {_id_token_for(base_url)}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            align_url,
            json=payload,
            headers=headers,
            timeout=_timeout_s(),
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        _logger.warning("cloud ASR call failed (%s) — caller will fall back",
                        type(exc).__name__)
        raise CloudRunAsrUnavailable(
            f"cloud ASR unreachable ({type(exc).__name__}: {exc})"
        ) from exc

    if resp.status_code != 200:
        # 4xx is a real error (bad request); 5xx is a server-side issue
        # that should trigger fallback. 4xx callers should fix the
        # request rather than fall back, so re-raise with detail.
        body = resp.text[:500]
        if 500 <= resp.status_code < 600:
            raise CloudRunAsrUnavailable(
                f"cloud ASR HTTP {resp.status_code}: {body}"
            )
        raise RuntimeError(
            f"cloud ASR HTTP {resp.status_code} (non-retryable): {body}"
        )

    data = resp.json()
    return [
        Segment(
            start_s=float(s["start_s"]),
            end_s=float(s["end_s"]),
            text=s.get("text", ""),
            anchor_id=str(s.get("anchor_id", f"seg_{i:03d}")),
            kind=s.get("kind", mode if mode != "words" else "word"),
        )
        for i, s in enumerate(data.get("segments", []))
    ]


def _upload_to_gcs(narration_wav: Path) -> str:
    """Upload the wav to GCS and return ``gs://...`` URL.

    Uses the per-job temp bucket the existing renderer pipeline already
    writes to (``YTFACTORY_BUCKET`` env or ``YTFACTORY_STATE_BUCKET``).
    Object lifecycle is handled by the bucket's ttl policy — we don't
    explicitly delete after the call.
    """
    bucket_name = (
        os.environ.get("YTFACTORY_BUCKET")
        or os.environ.get("YTFACTORY_STATE_BUCKET")
    )
    if not bucket_name:
        raise CloudRunAsrUnavailable(
            "no YTFACTORY_BUCKET / YTFACTORY_STATE_BUCKET env set — "
            "can't upload large wav to cloud ASR. Either set the env or "
            "use a smaller wav (< 5 MB will inline as base64)."
        )
    from google.cloud import storage  # noqa: PLC0415
    import hashlib  # noqa: PLC0415

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    fingerprint = hashlib.sha256(narration_wav.read_bytes()).hexdigest()[:12]
    blob_path = f"asr-input/{fingerprint}.wav"
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(str(narration_wav))
    return f"gs://{bucket_name}/{blob_path}"


__all__ = ["align_via_cloud", "CloudRunAsrUnavailable"]
