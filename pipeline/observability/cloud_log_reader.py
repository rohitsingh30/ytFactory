"""Cross-service Cloud Logging reader for the telemetry dashboard.

Why this exists
---------------

The web-server's in-process shadow buffer
(:class:`pipeline.observability.exporters.BoundedInMemoryLogRecordExporter`)
gives the laptop dashboard a fast, zero-network local view of recent
events. **In production it is structurally near-empty** because the
web-server is a thin BFF — every render, TTS call, image gen, and
upload happens in a *different* process (render-worker JOB, TTS / image
Cloud Run services). The web-server only sees its own emissions.

This module gives the dashboard a *cross-service* read path. When the
active OTel exporter is ``gcp`` and ``GOOGLE_CLOUD_PROJECT`` is set,
the dashboard's :func:`pipeline.telemetry.read_events` consults this
reader before falling back to the in-process buffer. The reader queries
the Cloud Logging API for ``jsonPayload."ytfactory.event"!=""``
records emitted by every service over the requested window, normalises
them back to the legacy ``read_events`` dict shape, and TTL-caches the
result so dashboard polls don't hammer the API.

Format expectations
~~~~~~~~~~~~~~~~~~~

Every log entry that lands in Cloud Logging via
:class:`pipeline.observability.cloud_run_json_exporter.CloudRunStructuredJsonLogExporter`
has shape::

    jsonPayload = {
      "severity": "INFO" | "ERROR" | ...,
      "message": "ytfactory.event",
      "ytfactory.event": "tts_synth",
      "ytfactory.category": "tts",
      "ytfactory.success": true,
      "ytfactory.duration_ms": 1240,
      "ytfactory.channel": "historyrecapped",
      "ytfactory.slug": "aita-001",
      "ytfactory.meta.provider": "cloudrun_chatterbox",
      ...
    }

The reader filters on the presence of ``ytfactory.event`` so unrelated
JSON-payload records (e.g. ad-hoc structured logging from third-party
libraries) are skipped without false positives.

Failure modes
~~~~~~~~~~~~~

The reader NEVER raises — every exception path returns ``None`` so the
caller silently falls back to the in-process buffer. The dashboard
must keep working even when the Cloud Logging API is rate-limited,
ADC is missing, or the ``google-cloud-logging`` package is not
installed (slim cloud build).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional


_logger = logging.getLogger(__name__)


# Telemetry must never block the dashboard. Cap each query at 5 s so a
# slow Cloud Logging call doesn't stall a poll-every-30 s tab.
_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_MAX_RECORDS = 1000
_DEFAULT_TTL_S = 15.0


def _project_id() -> Optional[str]:
    return os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")


def _bool(s: Any) -> bool:
    """Cloud Logging coerces every json bool back to a ``bool``, but
    the legacy structlog path used to write strings; tolerate both."""
    if isinstance(s, bool):
        return s
    if isinstance(s, str):
        return s.lower() in ("true", "1", "yes")
    return bool(s)


def _normalise_payload(payload: Dict[str, Any], received_ts: float) -> Optional[Dict[str, Any]]:
    """Convert one ``jsonPayload`` dict into the legacy
    :func:`pipeline.telemetry.read_events` dict shape. Returns ``None``
    when the payload doesn't look like a ytFactory event."""
    event = payload.get("ytfactory.event")
    if not event:
        return None

    category = payload.get("ytfactory.category", "pipeline")
    # Audit Q2.9 — defaulting absent ytfactory.success to True silently
    # overcounted SUCCESS in dashboard failure-rate calc (every event
    # without an explicit success field looked passing). Default to
    # None so downstream "success vs failure" math has to make an
    # explicit decision; events that genuinely don't carry a success
    # signal should not bias the rate either way.
    raw_success = payload.get("ytfactory.success")
    success = None if raw_success is None else _bool(raw_success)
    job_id = payload.get("ytfactory.job_id")

    duration = payload.get("ytfactory.duration_ms")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = None

    metadata: Dict[str, Any] = {}
    for k, v in payload.items():
        if not isinstance(k, str):
            continue
        if not k.startswith("ytfactory."):
            continue
        if k in (
            "ytfactory.event",
            "ytfactory.category",
            "ytfactory.success",
            "ytfactory.duration_ms",
            "ytfactory.job_id",
        ):
            continue
        if k.startswith("ytfactory.meta."):
            metadata[k[len("ytfactory.meta."):]] = v
        else:
            # ytfactory.channel / ytfactory.slug / ytfactory.run_id /
            # ytfactory.render_kind / etc.
            metadata.setdefault(k[len("ytfactory."):], v)

    return {
        "ts": received_ts,
        "event": event,
        "category": category,
        "success": success,
        "duration_ms": duration,
        "job_id": job_id,
        "metadata": metadata,
    }


def _entry_timestamp_seconds(entry: Any) -> float:
    """Pull a unix-seconds timestamp from a Cloud Logging entry. The
    REST shape uses ``timestamp`` (ISO-8601 string); the Python client
    object exposes ``timestamp`` as a ``datetime``. Tolerate both."""
    ts = getattr(entry, "timestamp", None)
    if ts is None and isinstance(entry, dict):
        ts = entry.get("timestamp")
    if ts is None:
        return time.time()
    if hasattr(ts, "timestamp"):
        try:
            return float(ts.timestamp())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            # Cloud Logging timestamps are RFC3339 with optional 'Z'.
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            pass
    return time.time()


def _entry_payload(entry: Any) -> Optional[Dict[str, Any]]:
    """Return the ``jsonPayload`` dict from a Cloud Logging entry."""
    payload = getattr(entry, "payload", None)
    if isinstance(payload, dict):
        return payload
    if isinstance(entry, dict):
        jp = entry.get("jsonPayload")
        if isinstance(jp, dict):
            return jp
        # Some clients use 'payload' too.
        if isinstance(entry.get("payload"), dict):
            return entry["payload"]
    return None


class CloudLoggingEventReader:
    """Thread-safe TTL-cached reader for ytFactory events in Cloud
    Logging. Singleton-per-process via :func:`get_reader`.

    Design notes:

    * **TTL cache** — multiple dashboard tabs polling the same window
      hit the same cache key. Default 15 s; override via
      ``YTFACTORY_TELEMETRY_CLOUDLOG_TTL``.
    * **Bounded result size** — per-window records capped at
      :data:`_DEFAULT_MAX_RECORDS`. Override via
      ``YTFACTORY_TELEMETRY_CLOUDLOG_LIMIT``.
    * **Defensive** — every exception path returns ``None``, the
      caller falls back to the in-process buffer.
    """

    def __init__(
        self,
        *,
        project_id: str,
        ttl_s: float = _DEFAULT_TTL_S,
        max_records: int = _DEFAULT_MAX_RECORDS,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        client_factory: Any = None,
    ) -> None:
        self._project_id = project_id
        self._ttl_s = ttl_s
        self._max_records = max_records
        self._timeout_s = timeout_s
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = threading.Lock()
        self._cache: Dict[float, tuple[float, List[Dict[str, Any]]]] = {}

    # -- client construction (lazy + one-shot) -----------------------

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        # Lazy import — keeps the laptop dev dashboard working when
        # google-cloud-logging is not installed.
        try:
            import google.cloud.logging as gcl  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"google-cloud-logging unavailable: {e}",
            )
        self._client = gcl.Client(project=self._project_id)
        return self._client

    # -- public API --------------------------------------------------

    def read(
        self,
        *,
        since_ts: float,
    ) -> Optional[List[Dict[str, Any]]]:
        """Return events newer than ``since_ts`` (unix seconds), or
        ``None`` on any failure.

        Identical (since_ts → result) pairs hit the TTL cache so a
        dashboard polling every 30 s with the same window doesn't pay
        a network round trip every time.
        """
        # Bucket the cache key to coarse 5 s slots so refresh-every-1s
        # tabs share entries.
        cache_key = round(since_ts / 5.0) * 5.0
        now = time.time()

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                cached_at, cached_events = cached
                if now - cached_at < self._ttl_s:
                    return list(cached_events)

        try:
            events = self._fetch(since_ts=since_ts)
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "Cloud Logging telemetry read failed: %s", e,
            )
            return None

        with self._lock:
            self._cache[cache_key] = (now, events)
            # Bound the cache to keep memory predictable.
            if len(self._cache) > 64:
                oldest = sorted(self._cache.items(),
                                key=lambda kv: kv[1][0])[: len(self._cache) - 64]
                for k, _ in oldest:
                    self._cache.pop(k, None)

        return list(events)

    def invalidate(self) -> None:
        """Drop the TTL cache. Used by tests."""
        with self._lock:
            self._cache.clear()

    # -- internals ---------------------------------------------------

    def _fetch(self, *, since_ts: float) -> List[Dict[str, Any]]:
        """Run the Cloud Logging query and normalise the entries.

        Filter: ``jsonPayload."ytfactory.event"!=""`` plus a timestamp
        lower bound. The Logs Explorer language documents the
        ``timestamp >= "RFC3339"`` syntax explicitly.
        """
        from datetime import datetime, timezone

        client = self._get_client()
        # Pad by 60 s to compensate for ingestion lag — we'd rather
        # have a few duplicates than miss a record.
        ts_iso = datetime.fromtimestamp(
            max(0.0, since_ts - 60.0), tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        filter_ = (
            # Audit T1.3 — render-worker-v2 is a Cloud Run JOB
            # (resource.type="cloud_run_job"), not a revision; the
            # original "cloud_run_revision"-only filter dropped every
            # span the worker emits. Match both shapes so we cover
            # both Cloud Run services AND Cloud Run jobs.
            '(resource.type="cloud_run_revision" OR '
            'resource.type="cloud_run_job") AND '
            'jsonPayload."ytfactory.event"!="" AND '
            f'timestamp >= "{ts_iso}"'
        )

        out: List[Dict[str, Any]] = []
        deadline = time.time() + self._timeout_s
        for entry in client.list_entries(
            filter_=filter_,
            order_by="timestamp desc",
            page_size=min(1000, self._max_records),
        ):
            if time.time() > deadline:
                _logger.debug(
                    "Cloud Logging telemetry read hit %.1fs deadline",
                    self._timeout_s,
                )
                break
            payload = _entry_payload(entry)
            if payload is None:
                continue
            ts = _entry_timestamp_seconds(entry)
            if ts < since_ts:
                continue
            event = _normalise_payload(payload, ts)
            if event is None:
                continue
            out.append(event)
            if len(out) >= self._max_records:
                break

        # Sort oldest → newest (legacy contract).
        out.sort(key=lambda e: e["ts"])
        return out


# -- module-level singleton + accessor -----------------------------------


_READER_LOCK = threading.Lock()
_READER: Optional[CloudLoggingEventReader] = None
_DISABLED_REASON: Optional[str] = None


def _ttl_from_env() -> float:
    raw = os.environ.get("YTFACTORY_TELEMETRY_CLOUDLOG_TTL", "").strip()
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TTL_S


def _limit_from_env() -> int:
    raw = os.environ.get("YTFACTORY_TELEMETRY_CLOUDLOG_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_MAX_RECORDS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_RECORDS


def _disabled_via_env() -> bool:
    val = os.environ.get(
        "YTFACTORY_TELEMETRY_CLOUDLOG_DISABLE", "",
    ).strip().lower()
    return val in {"1", "true", "yes", "on"}


def get_reader() -> Optional[CloudLoggingEventReader]:
    """Return a process-wide reader, lazily constructed.

    Returns ``None`` (with a one-shot warning) when:

    * ``GOOGLE_CLOUD_PROJECT`` / ``GCP_PROJECT`` is not set.
    * ``YTFACTORY_TELEMETRY_CLOUDLOG_DISABLE=1`` is set (escape hatch).
    * The reader has previously failed to construct (e.g.
      ``google-cloud-logging`` not installed) — we cache the failure
      so dashboard polls don't keep re-attempting.
    """
    global _READER, _DISABLED_REASON
    if _disabled_via_env():
        return None
    project = _project_id()
    if not project:
        return None
    with _READER_LOCK:
        if _DISABLED_REASON is not None:
            return None
        if _READER is not None and _READER._project_id == project:
            return _READER
        try:
            _READER = CloudLoggingEventReader(
                project_id=project,
                ttl_s=_ttl_from_env(),
                max_records=_limit_from_env(),
            )
            return _READER
        except Exception as e:  # noqa: BLE001
            _DISABLED_REASON = str(e)
            _logger.warning(
                "CloudLoggingEventReader unavailable, disabling cross-"
                "service dashboard read: %s", e,
            )
            return None


def reset_for_tests() -> None:
    """Clear singleton + disabled flag — required between tests that
    inject different mock factories."""
    global _READER, _DISABLED_REASON
    with _READER_LOCK:
        _READER = None
        _DISABLED_REASON = None


__all__ = [
    "CloudLoggingEventReader",
    "_normalise_payload",
    "_entry_payload",
    "_entry_timestamp_seconds",
    "get_reader",
    "reset_for_tests",
]
