"""Lightweight file-backed telemetry for the ytFactory pipeline.

Adapted from the shofferAi pattern (apps/web/lib/telemetry.ts), but
file-based since this project has no Postgres. Every event is appended
as one JSON line to ``data/telemetry/events.jsonl``. The web server
reads + aggregates that file on demand for the /api/telemetry/* views.

Why JSONL: append-only, crash-safe, trivially tail-able for debugging,
and a single user / single host means we never need a real time-series DB.
File rotates daily into ``events-YYYY-MM-DD.jsonl`` so a long-running
host doesn't grow one unbounded file; ``read_events`` globs both.

Use::

    from pipeline import telemetry as tlm
    tlm.track("stage_done", category="pipeline", duration_ms=1234,
              metadata={"niche": "aita", "stage": "tts"})

    with tlm.timed("stage", metadata={"stage": "image"}) as t:
        ...                       # do work
        t.add(metadata={"n": 7})  # extra fields recorded on .end()
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
TELEMETRY_DIR = Path(
    os.environ.get("YTFACTORY_TELEMETRY_DIR")
    or (_PROJECT_ROOT / "data" / "telemetry")
)

_LOCK = threading.Lock()


def _today_path() -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return TELEMETRY_DIR / f"events-{day}.jsonl"


def track(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget event write. Never raises — if disk is full or
    the directory is read-only we print a warning and move on, since
    pipeline correctness must not depend on telemetry.
    """
    record = {
        "ts": time.time(),
        "event": event,
        "category": category,
        "success": success,
        "duration_ms": duration_ms,
        "job_id": job_id,
        "metadata": metadata or {},
    }
    try:
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with _today_path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:
        print(f"[telemetry] write failed for {category}/{event}: {e}",
              file=sys.stderr, flush=True)


def track_stage_done(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Stage-completion telemetry — distinct surface from :func:`track`
    so renderers can emit per-stage events (tts/asr/image_gen/compose)
    without inflating the per-attempt event counts that downstream
    tests assert on.

    Behaviour-wise this is just :func:`track`; the separate name lets
    callers (and ``unittest.mock.patch.object``) distinguish stage
    rollups from per-attempt events without filtering by ``event``
    string.
    """
    track(
        event,
        category=category,
        success=success,
        duration_ms=duration_ms,
        job_id=job_id,
        metadata=metadata,
    )


class _Timer:
    def __init__(self, event: str, *, category: str, job_id: str | None,
                 metadata: dict[str, Any]) -> None:
        self._event = event
        self._category = category
        self._job_id = job_id
        self._metadata = dict(metadata)
        self._start = time.time()
        self._ended = False

    def add(self, *, metadata: dict[str, Any] | None = None) -> None:
        """Merge extra fields into the metadata that will be recorded on end."""
        if metadata:
            self._metadata.update(metadata)

    def end(self, *, success: bool = True,
            metadata: dict[str, Any] | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        if metadata:
            self._metadata.update(metadata)
        track(
            self._event,
            category=self._category,
            success=success,
            duration_ms=int((time.time() - self._start) * 1000),
            job_id=self._job_id,
            metadata=self._metadata,
        )


@contextmanager
def timed(
    event: str,
    *,
    category: str = "pipeline",
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[_Timer]:
    """Context manager wrapping ``track`` with auto-timing.

    Records ``success=False`` if the with-block raises, then re-raises.
    """
    t = _Timer(event, category=category, job_id=job_id, metadata=metadata or {})
    try:
        yield t
        t.end(success=True)
    except BaseException as e:
        t.add(metadata={"error": f"{type(e).__name__}: {e}"[:300]})
        t.end(success=False)
        raise


# ---- read side ---------------------------------------------------------

# Cache parsed shards by (path, mtime, size) so the dashboard doesn't
# re-parse every events-*.jsonl file on every /api/telemetry/* hit.
# A historical shard (yesterday or older) is parsed exactly once for
# the lifetime of the server; today's shard is re-parsed only when
# its mtime/size moves (i.e. when a new event was actually appended).
#
# Bug it fixes: pre-fix, six dashboard routes call read_events() per
# request, each one walks every shard from disk + json.loads every
# line. With ~3 000 events/day × 7 days = ~21 000 events parsed per
# request, on the asyncio event loop, blocking every other in-flight
# request (see d2-async-disk-io). Even after d2 wraps the call in a
# thread, the wasted work is real — disk + CPU per dashboard poll.
#
# Memory cost is bounded: ~21 000 small dicts × ~250 bytes = ~5 MB,
# trivial. Cache hit is a ref copy, not a deep copy — callers MUST
# treat the returned list as read-only (read_events docstring updated).
_PARSE_CACHE: dict[Path, tuple[float, int, list[dict]]] = {}
_PARSE_CACHE_LOCK = threading.Lock()


def _parse_shard(path: Path) -> list[dict]:
    """Parse one events-*.jsonl shard, with an mtime-keyed cache."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = (st.st_mtime, st.st_size)
    with _PARSE_CACHE_LOCK:
        cached = _PARSE_CACHE.get(path)
        if cached is not None and (cached[0], cached[1]) == key:
            return cached[2]
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE[path] = (st.st_mtime, st.st_size, out)
    return out


def read_events(
    *,
    since_ts: float | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read events newer than ``since_ts`` from all JSONL shards.

    Returns events sorted oldest → newest. ``limit`` (when set) keeps the
    most recent N. Malformed lines are skipped silently.

    The returned list is **shared with the in-memory parse cache** —
    callers must treat it as read-only. Sort/filter/copy as needed.
    """
    if not TELEMETRY_DIR.exists():
        return []
    files = sorted(TELEMETRY_DIR.glob("events-*.jsonl"))
    out: list[dict] = []
    for f in files:
        for rec in _parse_shard(f):
            if since_ts is not None and rec.get("ts", 0) < since_ts:
                continue
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0))
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out


def percentile(values: Iterable[float], q: float) -> float:
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
