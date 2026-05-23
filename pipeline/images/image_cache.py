"""GCS-backed image cache for the image-gen pipeline.

Caches generated PNGs keyed by ``sha256(prompt + seed + model_version
+ W×H + steps)`` under ``gs://ytfactory-prod-v3-cache/images/<hash>.png``.

Lookup is HEAD-then-download; on hit, the helper RE-PUTS the PNG to
refresh its creation date (smart TTL — frequently-hit entries survive
the 30-day GCS lifecycle rule applied to the bucket).

Cache is shared across all channels — same prompt+seed produces the
same PNG regardless of which channel requested it. ``model_version``
captures the model identity so a future model swap automatically
invalidates the entire cache (different hash component).

guidance_scale is NOT in the key. Z-Image-Turbo is CFG-distilled and
the server hard-clamps guidance_scale=0.0 regardless of input — there
is no variance to capture. If a future non-CFG-distilled model is
plugged in, bumping ``model_version`` is sufficient invalidation.

Usage::

    from pipeline.images.image_cache import generate_with_cache

    generate_with_cache(
        prompt=prompt,
        style_prefix=style,
        seed=seed,
        out_path=png_path,
        width=W, height=H, steps=9, provider="cloudrun_z_image_turbo",
    )

When the cache is unavailable (no bucket env, missing google-cloud-
storage, ADC failure), the helper transparently falls through to the
underlying ``pipeline.images.images.generate`` — never blocks a render
on cache infrastructure.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Cache bucket — defaults to the prod artifacts bucket. Override via
# YTFACTORY_IMAGE_CACHE_BUCKET to point at a dedicated cache bucket.
_DEFAULT_CACHE_BUCKET = "ytfactory-prod-v3-cache"

# Model-version identifier baked into the cache key. Bump when the
# model weights / inference pipeline changes in a way that affects
# output bytes for the same prompt+seed (e.g. checkpoint swap, server
# code change to scheduler / dtype / offload mode).
MODEL_VERSION = "z-image-turbo-2026-05"


def _cache_bucket() -> str:
    return os.environ.get("YTFACTORY_IMAGE_CACHE_BUCKET", _DEFAULT_CACHE_BUCKET)


def cache_key(
    *,
    prompt: str,
    seed: int,
    width: int,
    height: int,
    steps: int,
    model_version: str = MODEL_VERSION,
) -> str:
    """Return the 64-hex SHA-256 cache key for a generation request."""
    payload = "\x1f".join([
        f"v={model_version}",
        f"w={int(width)}",
        f"h={int(height)}",
        f"steps={int(steps)}",
        f"seed={int(seed)}",
        f"prompt={prompt}",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_storage_client():
    try:
        from google.cloud import storage  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    except Exception:  # noqa: BLE001
        return None


def _try_cache_hit(blob, out_path: Path) -> bool:
    """Download blob to out_path. Returns True on success."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(out_path))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_cache: download failed for %s: %s", blob.name, exc)
        return False


def _refresh_blob_creation_date(blob) -> None:
    """Re-PUT the blob with the same bytes to reset its creation date.

    GCS lifecycle rules look at `age since creation` for deletion, so
    hot entries die at 30 days even if hit daily. Re-PUTing on hit
    keeps frequently-used entries alive. Cheap (~50ms) — single round-
    trip to GCS.
    """
    try:
        # Re-uploading the same content via reload() + rewrite() is the
        # idiomatic way to refresh creation date without re-downloading
        # the bytes locally. Implementation: rewrite to itself.
        blob.rewrite(blob)
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: cache HIT already returned the PNG to the caller.
        # If the refresh fails, the entry just ages naturally.
        logger.debug("image_cache: refresh failed for %s: %s", blob.name, exc)


def generate_with_cache(
    *,
    prompt: str,
    style_prefix: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
    provider: str,
) -> Path:
    """Drop-in replacement for ``pipeline.images.images.generate`` with
    GCS cache lookup. On miss, generates normally + uploads to cache.
    On hit, downloads + refreshes blob creation date.
    """
    # Build the cache key from the final prompt (after style_prefix
    # is appended). This matches what the generator actually sends.
    final_prompt = (prompt or "").strip()
    if style_prefix:
        final_prompt = f"{final_prompt}\n\nStyle: {style_prefix.strip()}"

    key = cache_key(
        prompt=final_prompt, seed=seed, width=width, height=height, steps=steps,
    )

    client = _get_storage_client()
    blob = None
    bucket_name = _cache_bucket()
    if client is not None:
        try:
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(f"images/{key}.png")
            if blob.exists():
                if _try_cache_hit(blob, out_path):
                    logger.info(
                        "image_cache HIT: key=%s out=%s",
                        key[:12], out_path.name,
                    )
                    _refresh_blob_creation_date(blob)
                    return out_path
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image_cache: HEAD failed (%s) — falling through to generate",
                exc,
            )
            blob = None

    # MISS — generate normally then upload.
    from pipeline.images.images import generate as _real_generate  # noqa: PLC0415
    _real_generate(
        prompt=prompt,
        style_prefix=style_prefix,
        seed=seed,
        out_path=out_path,
        width=width,
        height=height,
        steps=steps,
        provider=provider,
    )
    if blob is not None:
        try:
            blob.upload_from_filename(str(out_path), content_type="image/png")
            logger.info(
                "image_cache MISS-then-store: key=%s out=%s",
                key[:12], out_path.name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image_cache: upload failed for key=%s: %s", key[:12], exc,
            )
    return out_path


__all__ = ["generate_with_cache", "cache_key", "MODEL_VERSION"]
