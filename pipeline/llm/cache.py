"""Content-addressable artifact cache for the orchestrator.

Why
---
A render touches ~30 images, ~5–10 LLM calls, ~1 long TTS pass, plus
ASR + compose. When ONE artifact is broken (one image-judge fail, one
script_check retry), we want to invalidate ONLY that artifact's cache
key and reuse everything else. Bazel-style content-addressable storage
gives us that for free: the cache key is a hash of every input that
goes into producing the artifact, so an unchanged input → identical
key → cache hit; a changed input → different key → cache miss.

Two backends share one interface:

- ``LocalCache`` — laptop dev. Cache lives under
  ``~/.cache/ytfactory/`` by default; tests override via ``root=``.
- ``GCSCache`` — cloud render-worker. Cache lives at
  ``gs://<YTFACTORY_CACHE_BUCKET>/cache/``; defaults to
  ``ytfactory-prod-v2-cache`` so that any worker, in any execution,
  can hit the same cache. Critical for cost: a redo of beat #14 today
  reuses 29 images that any prior render already paid for.

Usage from a contract
---------------------
::

    key = cache.key_for(stage="prompts", channel=ctx.channel,
                        channel_cfg=ctx.channel_cfg,
                        upstream={"script": script, "cast": cast},
                        sub_index=14)
    hit = cache.get(key)
    if hit is not None:
        return hit
    out = do_work(...)
    cache.put(key, out)
    return out

The ``run_stage`` helper in :mod:`pipeline.llm.orchestrator` will read
the cache automatically once contracts implement ``cache_key()`` —
that wiring lands in Phase 7 (pipeline runner). For now contracts can
opt in by calling ``cache.get`` / ``cache.put`` directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hashing — single source of truth for cache keys
# ---------------------------------------------------------------------------


def canonicalize(obj: Any) -> str:
    """Deterministic JSON for hashing.

    Sorted keys, no whitespace, default str fallback. Tuples treated
    as lists. Cuts the false-miss rate from "obj equal but stringified
    differently" to ~zero.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def key_for(
    *,
    stage: str,
    channel: str,
    channel_cfg: dict | None,
    upstream: dict | None = None,
    sub_index: int | None = None,
    extra: dict | None = None,
) -> str:
    """Compose a content-addressable cache key.

    Hashed inputs:
      - stage name (so two stages can't collide)
      - channel + channel_cfg (rule changes invalidate everything)
      - upstream artifacts dict (any upstream change invalidates this stage)
      - sub_index (per-beat granularity)
      - extra (provider, model, anything else)
    """
    h = hashlib.sha256()
    h.update(stage.encode())
    h.update(b"|")
    h.update(channel.encode())
    h.update(b"|")
    h.update(canonicalize(channel_cfg or {}).encode())
    h.update(b"|")
    h.update(canonicalize(upstream or {}).encode())
    h.update(b"|")
    h.update(b"None" if sub_index is None else str(sub_index).encode())
    h.update(b"|")
    h.update(canonicalize(extra or {}).encode())
    return h.hexdigest()[:32]   # 128-bit prefix is plenty


# ---------------------------------------------------------------------------
# Backend interface + records
# ---------------------------------------------------------------------------


@dataclass
class CacheRecord:
    """One artifact in the cache.

    ``output`` is the JSON-serialisable payload (script.json, cast.json,
    one image prompt, etc.). ``binary_uri`` is set for stages that
    produce file artifacts (images, audio); points at the GCS / FS path
    where the binary lives.
    """
    output: Any
    binary_uri: str | None = None
    metadata: dict | None = None


class CacheBackend(ABC):
    """Pluggable storage. The orchestrator only sees this interface."""

    @abstractmethod
    def get(self, key: str) -> CacheRecord | None: ...

    @abstractmethod
    def put(self, key: str, record: CacheRecord) -> None: ...

    @abstractmethod
    def has(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove an entry. No-op if missing.

        Used by the critic-FIX cascade to force a stage to re-execute
        even when its upstream hash hasn't changed (e.g. critic asks
        for a re-rewrite of the same source story but with different
        emphasis — the cache key would otherwise be identical).
        """
        ...


# ---------------------------------------------------------------------------
# Local FS backend (laptop dev + tests)
# ---------------------------------------------------------------------------


class LocalCache(CacheBackend):
    """File-system cache rooted at ``root`` (default ``~/.cache/ytfactory``)."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = Path.home() / ".cache" / "ytfactory"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Two-level fanout to avoid 100k files in one dir.
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> CacheRecord | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("[cache] corrupt entry at %s — treating as miss", p)
            return None
        return CacheRecord(
            output=data.get("output"),
            binary_uri=data.get("binary_uri"),
            metadata=data.get("metadata"),
        )

    def put(self, key: str, record: CacheRecord) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "output": record.output,
            "binary_uri": record.binary_uri,
            "metadata": record.metadata or {},
        }))
        os.replace(tmp, p)  # atomic on the same filesystem

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# GCS backend (cloud render-worker)
# ---------------------------------------------------------------------------


class GCSCache(CacheBackend):
    """GCS-backed cache. Lazy-imports ``google-cloud-storage`` so the
    laptop path doesn't need it.
    """

    def __init__(self, bucket: str, prefix: str = "cache/") -> None:
        from google.cloud import storage  # noqa: PLC0415
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._prefix = prefix.rstrip("/") + "/"

    def _blob_name(self, key: str) -> str:
        return f"{self._prefix}{key[:2]}/{key}.json"

    def get(self, key: str) -> CacheRecord | None:
        blob = self._bucket.blob(self._blob_name(key))
        if not blob.exists():
            return None
        try:
            data = json.loads(blob.download_as_text())
        except Exception:  # noqa: BLE001
            logger.warning("[cache] corrupt GCS entry at %s — treating as miss",
                           blob.name)
            return None
        return CacheRecord(
            output=data.get("output"),
            binary_uri=data.get("binary_uri"),
            metadata=data.get("metadata"),
        )

    def put(self, key: str, record: CacheRecord) -> None:
        blob = self._bucket.blob(self._blob_name(key))
        blob.upload_from_string(
            json.dumps({
                "output": record.output,
                "binary_uri": record.binary_uri,
                "metadata": record.metadata or {},
            }),
            content_type="application/json",
        )

    def has(self, key: str) -> bool:
        return self._bucket.blob(self._blob_name(key)).exists()

    def delete(self, key: str) -> None:
        try:
            self._bucket.blob(self._blob_name(key)).delete()
        except Exception:  # noqa: BLE001 — be tolerant; absence is fine
            pass


# ---------------------------------------------------------------------------
# Default backend selection
# ---------------------------------------------------------------------------


_DEFAULT: CacheBackend | None = None


def get_default_cache() -> CacheBackend:
    """Resolve the cache backend from env once per process.

    Order:
      1. ``YTFACTORY_CACHE_DISABLED=1`` → :class:`NoopCache` (renders
         like the cache isn't there; useful for benchmarking).
      2. ``YTFACTORY_CACHE_BACKEND=gcs`` and
         ``YTFACTORY_CACHE_BUCKET`` set → :class:`GCSCache`.
      3. ``YTFACTORY_CACHE_BACKEND=local`` or unset → :class:`LocalCache`
         under ``YTFACTORY_CACHE_DIR`` or ``~/.cache/ytfactory``.

    Cloud render-worker entrypoint should set
    ``YTFACTORY_CACHE_BACKEND=gcs`` +
    ``YTFACTORY_CACHE_BUCKET=ytfactory-prod-v2-cache`` so all worker
    executions share the same cache and one-beat regen costs $0 in
    cloud-image-service GPU time.
    """
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT

    if os.environ.get("YTFACTORY_CACHE_DISABLED", "0").lower() in ("1", "true", "yes"):
        _DEFAULT = NoopCache()
        return _DEFAULT

    backend = os.environ.get("YTFACTORY_CACHE_BACKEND", "local").lower()
    if backend == "gcs":
        bucket = os.environ.get("YTFACTORY_CACHE_BUCKET")
        if not bucket:
            logger.warning(
                "[cache] YTFACTORY_CACHE_BACKEND=gcs but YTFACTORY_CACHE_BUCKET "
                "not set — falling back to LocalCache"
            )
            _DEFAULT = LocalCache()
        else:
            _DEFAULT = GCSCache(bucket=bucket)
    else:
        root = os.environ.get("YTFACTORY_CACHE_DIR")
        _DEFAULT = LocalCache(root=root) if root else LocalCache()
    return _DEFAULT


def reset_default_cache() -> None:
    """Tests only — drop the cached default so a new env can apply."""
    global _DEFAULT
    _DEFAULT = None


class NoopCache(CacheBackend):
    """Cache that never hits. Useful for benchmarking + tests."""

    def get(self, key: str) -> CacheRecord | None: return None
    def put(self, key: str, record: CacheRecord) -> None: pass
    def has(self, key: str) -> bool: return False
    def delete(self, key: str) -> None: pass
