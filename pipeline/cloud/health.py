"""Live health sweep across all Cloud Run services we own.

Hits each service's ``/readyz`` (TTS + image — they emit
``warm_s`` + ``cold_loaded`` + ``model_repo``) or ``/healthz`` (infra)
in parallel via :mod:`concurrent.futures` (urllib in worker threads —
no asyncio dep, plays nicely with FastAPI sync handlers).

Classification (green / yellow / red):

* **green** — HTTP 200 within timeout, ``cold_loaded`` is False
  (or the field is absent for non-GPU services), latency under
  the rolling p95 by less than 2x.
* **yellow** — HTTP 200 but cold (``cold_loaded`` true), or latency
  2-5x p95, or warm_s exceeds the per-service ``warm_s_max``.
* **red** — non-200, network error, timeout, or latency > 5x p95.

A service that's not configured (URL empty) gets status ``unconfigured``
so the panel can show it muted instead of dropping it.

The rolling baseline lives at ``data/_bench/cloud_health_baseline.json``
and is updated by :mod:`pipeline.cloud.snapshot` once per day.

Note (2026-05-10 perf pass — see ``docs/web_perf_pass_2026_05_10.md``):
the request-level handler at ``control/routes/cloud_routes.py::cloud_health``
wraps :func:`sweep` in a 5 s in-process TTL cache. Health is volatile,
but multi-tab × 30 s poll cadence multiplies the work. 5 s is shorter
than any meaningful service-state change (services recover/die on
Cloud Run scale events, both >> 5 s) and collapses N-tab × M-poll work
to a single sweep. ``sweep`` itself stays uncached so callers needing
real-time data (cron snapshot writer, ad-hoc CLI) bypass via direct
import.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .services import Service, ServiceKind, list_services
from .. import observability as _obs

logger = logging.getLogger(__name__)

# Where the rolling p95 + warm baseline lives. Cross-channel state, so
# under data/ per docs/channel_layout.md.
BASELINE_PATH = Path(__file__).resolve().parents[2] / "data" / "_bench" / "cloud_health_baseline.json"

# Per-service hard ceilings on cold-load time (seconds). Anything above
# trips yellow even if the call succeeded. Sourced from docs/cloudrun_*.md.
DEFAULT_WARM_S_MAX: dict[str, float] = {
    "z-image-turbo": 1500.0,  # 15-25 min cold; baked-weights production model
    "chatterbox": 300.0,
    "indicf5": 240.0,
}

# Per-call HTTP timeout for /readyz. Health is a fast probe — if it
# can't answer in 15s we want to call it red, not block the panel.
HTTP_TIMEOUT_S = 15.0


@dataclass
class HealthRow:
    """One service's current health snapshot."""

    short: str
    name: str
    kind: str
    url: str
    health_url: str
    status: str  # green | yellow | red | unconfigured
    http_status: Optional[int] = None
    latency_ms: Optional[float] = None
    warm_s: Optional[float] = None
    cold_loaded: Optional[bool] = None
    model_repo: Optional[str] = None
    error: Optional[str] = None
    flags: list[str] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)


def _load_baseline() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text())
    except Exception:
        logger.warning("baseline at %s is corrupt, ignoring", BASELINE_PATH)
        return {}


def _maybe_id_token(audience: str) -> Optional[str]:
    """Best-effort Cloud Run ID token. Returns None if auth not available."""
    try:
        from .cloudrun_auth import get_id_token  # type: ignore  # noqa: PLC0415

        return get_id_token(audience)
    except Exception as e:  # noqa: BLE001
        logger.debug("id-token unavailable for %s: %s", audience, e)
        return None


def _probe_one(svc: Service, timeout_s: float = HTTP_TIMEOUT_S) -> HealthRow:
    """Hit one service's healthz endpoint and return a parsed HealthRow."""
    row = HealthRow(
        short=svc.short,
        name=svc.name,
        kind=svc.kind.value,
        url=svc.url,
        health_url=svc.health_url,
        status="unconfigured",
    )

    if svc.is_job:
        row.status = "unconfigured"
        row.error = "Cloud Run JOB — health surfaced via gcloud executions, not /healthz"
        row.flags.append("job")
        return row

    if not svc.url or not svc.healthz_path:
        return row

    started = time.perf_counter()
    headers = {"User-Agent": "ytfactory-cloud-health/1"}
    token = _maybe_id_token(svc.url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(svc.health_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            row.http_status = resp.status
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            row.latency_ms = round(elapsed_ms, 1)
            body = resp.read(8192)
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                if "warm_s" in payload:
                    try:
                        row.warm_s = float(payload["warm_s"])
                    except (TypeError, ValueError):
                        pass
                if "cold_loaded" in payload:
                    row.cold_loaded = bool(payload["cold_loaded"])
                if "model_repo" in payload:
                    row.model_repo = str(payload["model_repo"])[:200]
    except urllib.error.HTTPError as e:
        row.http_status = e.code
        row.error = f"HTTP {e.code}"
        row.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    except urllib.error.URLError as e:
        row.error = f"URLError: {e.reason}"
    except TimeoutError:
        row.error = f"timeout after {timeout_s:.0f}s"
    except Exception as e:  # noqa: BLE001 — we never want one bad probe to take the panel down
        row.error = f"{type(e).__name__}: {e}"

    row.status = _classify(svc, row)
    return row


def _classify(svc: Service, row: HealthRow) -> str:
    """Apply the green/yellow/red rules (see module docstring)."""
    # Hard reds.
    if row.error and row.http_status is None:
        return "red"
    if row.http_status is not None and row.http_status >= 500:
        return "red"
    if row.http_status is not None and row.http_status >= 400:
        return "yellow"

    # Cold or above the warm ceiling → yellow.
    cold_max = DEFAULT_WARM_S_MAX.get(svc.short)
    if row.cold_loaded:
        row.flags.append("cold_loaded")
        return "yellow"
    if cold_max is not None and row.warm_s is not None and row.warm_s > cold_max:
        row.flags.append(f"warm_s>{cold_max:g}")
        return "yellow"

    # TODO 2026-05-10: when probe times out (no http_status, error
    # contains "timeout") AND svc.kind == IMAGE, return "yellow" +
    # flag "probe_timeout_likely_cold" instead of falling through to
    # "red" via the network-error rule above. Image cold-load is
    # 5-7 min (FLUX) / 15-25 min (Z-Image), well over the 15 s probe
    # ceiling — calling those red is misleading. See
    # `.claude/skills/update-docs/learnings/_index.md` 2026-05-10
    # entry; escalate to CLASS-OF-BUG on second observation.

    # Latency drift vs baseline.
    baseline = _load_baseline().get(svc.short, {})
    p95_ms = baseline.get("latency_p95_ms")
    if p95_ms and row.latency_ms:
        if row.latency_ms > p95_ms * 5:
            row.flags.append("latency>5x_p95")
            return "red"
        if row.latency_ms > p95_ms * 2:
            row.flags.append("latency>2x_p95")
            return "yellow"

    return "green"


@_obs.traced("cloud.health.sweep", category="cloud",
             capture=["parallelism", "timeout_s"])
def sweep(
    services: Optional[list[Service]] = None,
    *,
    parallelism: int = 12,
    timeout_s: float = HTTP_TIMEOUT_S,
) -> list[HealthRow]:
    """Probe every (configured) service in parallel and return rows."""
    targets = services if services is not None else list_services()
    if not targets:
        return []

    results: list[HealthRow] = []
    with ThreadPoolExecutor(max_workers=min(parallelism, len(targets))) as pool:
        futs = {pool.submit(_probe_one, svc, timeout_s): svc for svc in targets}
        for fut in as_completed(futs):
            results.append(fut.result())

    # Deterministic order: kind then short.
    results.sort(key=lambda r: (r.kind, r.short))
    return results


def sweep_dict(**kwargs: Any) -> list[dict[str, Any]]:
    """Same as :func:`sweep` but returns plain dicts (JSON-friendly)."""
    return [asdict(r) for r in sweep(**kwargs)]


def summary(rows: list[HealthRow]) -> dict[str, int]:
    """Aggregate counts, useful for the panel header strip."""
    counts = {"green": 0, "yellow": 0, "red": 0, "unconfigured": 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts
