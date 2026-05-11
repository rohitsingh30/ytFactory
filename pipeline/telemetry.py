"""Back-compat shim — preserves the legacy ``pipeline.telemetry`` API.

This module used to be a self-contained JSONL writer. As of the OTel
migration (see :mod:`pipeline.observability` + ``docs/telemetry.md``)
all telemetry flows through OTel → Cloud Trace + Cloud Monitoring +
Cloud Logging. The shim keeps every existing call site
(``pipeline.render.shorts``, ``pipeline.compose``, ``pipeline.llm.cli``,
``web.server`` etc.) working without churn:

    from pipeline import telemetry as tlm
    tlm.track("evt", category="...", duration_ms=...)
    with tlm.timed("stage", metadata={...}) as t:
        ...

What changed under the hood
---------------------------

- Spans are real OTel spans (visible in Cloud Trace UI).
- Counters / histograms are real OTel metrics
  (``ytfactory.events`` / ``ytfactory.stage_duration_ms``).
- Logs are OTel log records exported to Cloud Logging.
- :func:`read_events` queries the active log buffer (in-memory in
  tests; Cloud Logging via the dashboard in prod, wired in P6) and
  returns the same dict shape callers expect.

What was removed
----------------

The JSONL backend (``data/telemetry/events-YYYY-MM-DD.jsonl``,
``_today_path``, ``TELEMETRY_DIR``, ``_PARSE_CACHE``, ``_Timer``)
was deleted. Tests that asserted on the file backend were updated to
introspect the OTel in-memory exporters instead.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

# ---------------------------------------------------------------------------
# Graceful fallback when the OTel-backed observability package isn't
# available in this image. The slim cloud build (cloud/web-server/
# Dockerfile + requirements-control.txt) intentionally omits the heavy
# opentelemetry stack — when pipeline.observability fails to import
# (typically because ``import opentelemetry`` ModuleNotFoundError's
# inside .errors), every downstream call here becomes a no-op so the
# rest of the FastAPI app still boots. The on-laptop venv installs the
# full requirements.txt and gets the real OTel pipeline.
#
# This is a TEMPORARY shim during the JSONL→OTel migration. Once
# requirements-control.txt picks up the opentelemetry-* packages the
# stub branch becomes dead code (no behavioural change for anyone who
# actually has the package). Until then it unblocks every cloud
# deploy that rides on top of an in-progress pipeline.observability
# refactor.
try:
    from pipeline import observability as _obs  # type: ignore[assignment]
    _OBSERVABILITY_AVAILABLE = True
except Exception as _e:  # noqa: BLE001
    _OBSERVABILITY_AVAILABLE = False
    logging.getLogger(__name__).info(
        "pipeline.telemetry: pipeline.observability unavailable (%s); "
        "track/timed/emit_span become no-ops in this process.",
        _e,
    )

    class _NoopTimer:
        def add(self, *, metadata=None) -> None: ...
        def fail(self, *_args, **_kwargs) -> None: ...

    class _NoopObs:
        @staticmethod
        def track(*_args, **_kwargs) -> None: ...

        @staticmethod
        def emit_span(*_args, **_kwargs) -> None: ...

        @staticmethod
        @contextmanager
        def timed(*_args, **_kwargs):
            yield _NoopTimer()

        @staticmethod
        def current_bundle():  # noqa: D401
            return None

        @staticmethod
        def init(*_args, **_kwargs) -> None: ...

    _obs = _NoopObs()  # type: ignore[assignment]


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- public ---


def track(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: Optional[int] = None,
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Fire-and-forget event. Same signature as the legacy JSONL writer."""
    _obs.track(
        event,
        category=category,
        success=success,
        duration_ms=duration_ms,
        job_id=job_id,
        metadata=metadata,
    )


def track_stage_done(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: Optional[int] = None,
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Stage-completion event. Distinct surface from :func:`track` so
    callers (and ``unittest.mock.patch.object``) can mock the two
    independently — preserved verbatim from the legacy module so
    existing test mocks (``m.tlm.track_stage_done.call_count``) still
    work.
    """
    _obs.track(
        event,
        category=category,
        success=success,
        duration_ms=duration_ms,
        job_id=job_id,
        metadata=metadata,
    )


def emit_span(
    name: str,
    *,
    duration_ms: int,
    success: bool = True,
    category: str = "pipeline",
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a span with a pre-measured duration.

    Use when the caller already has its own ``t0/t1`` (e.g. the
    renderer's :func:`_record_stage_done` helper). This adds a span
    visible in Cloud Trace alongside the existing ``track`` /
    ``track_stage_done`` event log + counter, without forcing the
    caller to convert to a :func:`timed` block.
    """
    _obs.emit_span(
        name,
        duration_ms=duration_ms,
        success=success,
        category=category,
        job_id=job_id,
        metadata=metadata,
    )


@contextmanager
def timed(
    event: str,
    *,
    category: str = "pipeline",
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Iterator["_TimerHandle"]:
    """Context-manager span/timer. Yields a handle compatible with the
    legacy ``_Timer`` API (``.add(metadata=...)`` and ``.end(...)``)
    so existing call sites keep working.

    On exception, marks the span ERROR and re-raises.
    """
    with _obs.timed(event, category=category, job_id=job_id,
                    metadata=metadata) as inner:
        wrapper = _TimerHandle(inner)
        try:
            yield wrapper
        finally:
            # If the caller invoked ``.end()`` explicitly, the underlying
            # OTel span was already adjusted. We still let the OTel
            # context-manager close + emit on exit (idempotent).
            pass


def read_events(
    *,
    since_ts: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Return events newer than ``since_ts`` from the active OTel log
    buffer, normalised to the legacy dict shape:

    .. code-block:: python

        {
            "ts": float,           # seconds since epoch
            "event": str,
            "category": str,
            "success": bool,
            "duration_ms": int | None,
            "job_id": str | None,
            "metadata": dict,
        }

    Backed by the in-process bounded shadow log buffer
    (:class:`pipeline.observability.exporters.BoundedInMemoryLogRecordExporter`)
    in ``console`` / ``gcp`` / ``otlp`` modes, and by the test
    in-memory exporter in ``inmemory`` mode. ``none`` mode returns
    ``[]``. Disable the shadow buffer via
    ``YTFACTORY_TELEMETRY_BUFFER_DISABLE=1`` (then this returns
    ``[]`` everywhere except ``inmemory``).

    Caveat: shadow buffer is per-process. The Cloud Run web-server
    instance only sees its own emissions; cross-instance and
    cross-service visibility (e.g. render-worker JOB events) still
    requires Cloud Logging queries — see ``docs/telemetry.md`` for
    the deep-link path.

    Returns events sorted oldest → newest. ``limit`` (when set) keeps
    the most recent N. The returned list is a fresh list — callers
    are free to mutate.
    """
    bundle = _obs.current_bundle()
    if bundle is None:
        # Not initialised yet — boot in whatever mode the env says
        # (inmemory in tests; console / gcp otherwise).
        _obs.init()
        bundle = _obs.current_bundle()
    if bundle is None:
        return []

    if bundle.log_inmemory is None:
        # ``none`` mode (or ``YTFACTORY_TELEMETRY_BUFFER_DISABLE=1`` in
        # any non-inmemory mode) — there's no in-process buffer to
        # read from. The dashboard will surface an "unconfigured"
        # hint via /api/telemetry/init_status.
        return []

    # Force-flush the BatchLogRecordProcessor so test assertions see
    # the latest writes. Cheap when buffer is empty.
    try:
        if bundle.log_processor is not None:
            bundle.log_processor.force_flush()
    except Exception:  # noqa: BLE001
        pass

    out: list[dict] = []
    for ld in bundle.log_inmemory.get_finished_logs():
        rec = ld.log_record
        body = rec.body if isinstance(rec.body, dict) else {}
        ts = (rec.timestamp or 0) / 1_000_000_000  # ns → s
        if since_ts is not None and ts < since_ts:
            continue
        # Merge any context-provided attributes (channel / slug /
        # render_kind / etc) back INTO the legacy ``metadata`` dict
        # the dashboard reads. The OTel side stores those as span +
        # log attributes (``ytfactory.channel``); the legacy
        # ``read_events`` shape kept them under ``metadata``. Without
        # this the dashboard's per-channel rollup shows everything
        # under "?".
        meta = dict(body.get("metadata") or {})
        for k, v in (rec.attributes or {}).items():
            if not isinstance(k, str):
                continue
            if not k.startswith("ytfactory."):
                continue
            short = k[len("ytfactory."):]
            # Skip the few attrs the dashboard already gets via
            # top-level fields, and the per-meta nesting prefix.
            if short in (
                "event", "category", "success", "duration_ms",
                "job_id",
            ):
                continue
            if short.startswith("meta."):
                meta.setdefault(short[len("meta."):], v)
            else:
                meta.setdefault(short, v)
        out.append({
            "ts": ts,
            "event": body.get("event"),
            "category": body.get("category", "pipeline"),
            "success": bool(body.get("success", True)),
            "duration_ms": body.get("duration_ms"),
            "job_id": body.get("job_id"),
            "metadata": meta,
        })
    out.sort(key=lambda r: r["ts"])
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out


def percentile(values: Iterable[float], q: float) -> float:
    """Linear-interpolated percentile (kept identical to the legacy
    implementation so dashboard math doesn't shift).
    """
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    if q <= 0:
        return xs[0]
    if q >= 1:
        return xs[-1]
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


# ---------------------------------------------------------- legacy handle ---


class _TimerHandle:
    """Adapter that exposes the legacy ``_Timer`` API on top of the new
    :class:`pipeline.observability.telemetry._TimerHandle`.

    Why it exists: the old module's ``timed`` yielded an instance with
    ``.add(metadata=...)`` and ``.end(success=..., metadata=...)``. A
    handful of legacy call sites use both. The OTel handle has the
    same ``.add()``/``.fail()`` surface but no ``.end()``. Wrapping
    here is cheaper than touching every old call site.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner) -> None:
        self._inner = inner

    def add(self, *, metadata: Optional[dict[str, Any]] = None) -> None:
        self._inner.add(metadata=metadata)

    def end(
        self,
        *,
        success: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Legacy explicit-end. The OTel context-manager will still
        close the span on exit; we simply mutate state on the handle.
        Idempotent — second call is a no-op.
        """
        if metadata:
            self._inner.add(metadata=metadata)
        if not success:
            self._inner.fail("legacy .end(success=False)")


__all__ = [
    "emit_span",
    "percentile",
    "read_events",
    "timed",
    "track",
    "track_stage_done",
]
