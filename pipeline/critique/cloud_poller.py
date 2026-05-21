"""Laptop-side cloud-render critic daemon.

The cloud render-worker (``cloud/render-worker-v2/entrypoint.py``)
writes ``critique.verdict = "UNGATED"`` because the vision-bearing
critic in :mod:`pipeline.llm.critic` requires the ``claude`` CLI with
``add_dirs`` + ``allowed_tools=["Read"]`` — neither available to the
Azure-OpenAI-backed worker. Without a SHIP verdict the upload gate
refuses the render, so cloud-rendered Shorts have been queueing up
in Firestore unpublished.

This daemon closes the loop: it polls Firestore for ``status=done``
jobs whose ``critique.verdict`` is missing or ``UNGATED``, downloads
the mp4 + beats.json from GCS to a local cache, runs
:func:`pipeline.llm.critic.critique_short` (which is laptop-CLI-vision-
aware), and writes the verdict + axes back to the SAME job doc.

The daemon is intentionally:

* Idempotent — once a job has a SHIP/FIX/BLOCK verdict, the poller
  skips it on every subsequent iteration.
* Self-throttling — minimum 60 s between polls so we never hot-loop.
* Forgiving — a critic raise marks the job's ``critique.error`` field
  and a 1-hour cooldown gate keeps us from re-trying the same broken
  job on every iteration.
* Local-only side effects — downloads land under
  ``~/.cache/ytfactory/cloud_critic/<jid>/``. The mp4 is deleted
  after the critic completes; the score.json + frames are kept for
  debugging.

Public API
----------

* :func:`poll_and_critique_loop` — the main entry. Wired by the CLI
  shim at ``scripts/cloud_critic_loop.py`` and the launchd plist at
  ``control/com.ytfactory.cloud-critic.plist``.

* :func:`critique_one_job` — single-job pass; exported for unit tests.

Firestore schema touched
------------------------

Read::

    jobs/<jid> = {
        status: "done",
        short_uri: "gs://bucket/jobs/<jid>/video/short.mp4",
        critique: {verdict: "UNGATED", ...},
        artifacts: {beats: {uri: "gs://bucket/jobs/<jid>/beats/beats.json"}},
        channel: "...",
        slug: "...",
    }

Write::

    jobs/<jid>.critique = {
        verdict: "SHIP" | "FIX" | "BLOCK",
        axes: {...},
        score: int,
        one_line_take: "...",
        critiqued_at: "<iso8601>",
        critiqued_by: "laptop_cloud_poller",
    }

Or on failure::

    jobs/<jid>.critique.error = "<short error message>"
    jobs/<jid>.critique.errored_at = "<iso8601>"
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_logger = logging.getLogger(__name__)


# Verdicts we treat as "needs the laptop critic to run". Anything else
# (SHIP / FIX / BLOCK) is considered already-graded.
_UNGATED_VERDICTS: frozenset[Optional[str]] = frozenset({None, "", "UNGATED"})

# How long we wait before re-attempting a job whose previous critic
# call raised. Without this gate, a job whose mp4 is corrupt would be
# retried every 60 s forever.
_ERROR_RETRY_COOLDOWN_S = 3600  # 1 hour

# Local cache layout
_CACHE_ROOT_ENV = "YTFACTORY_CLOUD_CRITIC_CACHE"


# ---------------------------------------------------------------------------
# Cache + GCS helpers
# ---------------------------------------------------------------------------


def _cache_root() -> Path:
    """Per-user cache dir for downloaded mp4s + critic output."""
    override = os.environ.get(_CACHE_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "ytfactory" / "cloud_critic"


def _job_cache_dir(job_id: str) -> Path:
    return _cache_root() / job_id


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://bucket/path/to/file`` → ``("bucket", "path/to/file")``.

    Raises ``ValueError`` on malformed URIs. We never silently fall back
    to a default bucket — that would mask a worker-side schema bug.
    """
    if not uri or not isinstance(uri, str) or not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    rest = uri[len("gs://"):]
    if "/" not in rest:
        raise ValueError(f"gs:// URI missing object path: {uri!r}")
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"gs:// URI missing bucket or key: {uri!r}")
    return bucket, key


def _gcs_download(uri: str, dest: Path) -> Path:
    """Download a ``gs://`` URI to ``dest``. Returns ``dest``.

    Lazy import keeps the daemon importable on machines without
    ``google-cloud-storage`` installed for unit-test setUp.
    """
    bucket_name, key = _parse_gs_uri(uri)
    from google.cloud import storage  # noqa: PLC0415 — lazy

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
    client = storage.Client(project=project)
    blob = client.bucket(bucket_name).blob(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(dest))
    return dest


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------


def _firestore_client(project_id: str):
    from google.cloud import firestore  # noqa: PLC0415 — lazy
    return firestore.Client(project=project_id)


def _scan_ungated_jobs(client, *, limit: int = 50) -> list[Any]:
    """Return job snapshots that look like they need the critic.

    We can't combine ``where("status", "==", "done")`` with ``where(
    "critique.verdict", "in", [None, "UNGATED"])`` in a single
    Firestore query (composite index + ``in`` with ``None`` is awkward),
    so we filter ``critique.verdict`` in Python after the status query.
    """
    snaps = (
        client.collection("jobs")
        .where("status", "==", "done")
        .limit(limit)
        .stream()
    )
    out: list[Any] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        critique = data.get("critique") or {}
        verdict = critique.get("verdict")
        if verdict not in _UNGATED_VERDICTS:
            continue
        out.append(snap)
    return out


def _on_error_cooldown(data: dict[str, Any], *, now: float) -> bool:
    """Return True if this job had a critic error in the last
    :data:`_ERROR_RETRY_COOLDOWN_S` seconds. Used to avoid hammering
    the same broken job every loop."""
    crit = data.get("critique") or {}
    if not crit.get("error"):
        return False
    errored_at = crit.get("errored_at")
    if not errored_at:
        # Error set but no timestamp — treat as on cooldown (don't retry
        # blindly; an operator should clear the field manually).
        return True
    try:
        ts = _dt.datetime.fromisoformat(errored_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    age = now - ts.timestamp()
    return age < _ERROR_RETRY_COOLDOWN_S


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Per-job critique
# ---------------------------------------------------------------------------


@dataclass
class JobOutcome:
    """Result of one :func:`critique_one_job` call. Used by the loop's
    per-iteration counters + by unit tests to assert routing."""
    job_id: str
    status: str  # "critiqued" | "skipped" | "failed"
    verdict: Optional[str] = None
    reason: Optional[str] = None


def critique_one_job(
    client,
    snap: Any,
    *,
    cache_root: Optional[Path] = None,
    critique_fn: Optional[Callable[..., dict]] = None,
    download_fn: Optional[Callable[[str, Path], Path]] = None,
    now_fn: Callable[[], float] = time.time,
) -> JobOutcome:
    """Critique a single job. Pure-function shape (no global state)
    so tests can drive each branch directly.

    Parameters
    ----------
    client
        Firestore client (only used for ``snap.reference`` updates).
    snap
        Firestore DocumentSnapshot for a ``jobs/<id>`` doc.
    cache_root
        Override the cache root. Defaults to :func:`_cache_root`.
    critique_fn
        Override the critic call. Defaults to
        :func:`pipeline.llm.critic.critique_short`. Tests inject a
        stub.
    download_fn
        Override GCS download. Defaults to :func:`_gcs_download`.
    now_fn
        Clock injection point.

    Returns
    -------
    JobOutcome
        See dataclass docstring.
    """
    job_id = snap.id
    data = snap.to_dict() or {}
    doc_ref = snap.reference

    # Cooldown guard — see _on_error_cooldown.
    if _on_error_cooldown(data, now=now_fn()):
        return JobOutcome(
            job_id=job_id, status="skipped", reason="error_cooldown"
        )

    short_uri = data.get("short_uri")
    if not short_uri:
        # Worker said status=done but never wrote short_uri. This is a
        # worker bug, not our fault — flag the job so the operator can
        # see it on the dashboard, then move on.
        _logger.warning("job %s: status=done but no short_uri", job_id)
        try:
            doc_ref.set(
                {
                    "critique": {
                        "error": "no short_uri on done job",
                        "errored_at": _utcnow_iso(),
                    }
                },
                merge=True,
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("job %s: also failed to mark error: %s", job_id, e)
        return JobOutcome(
            job_id=job_id, status="skipped", reason="no_short_uri"
        )

    # Resolve beats.json URI from the artifacts map. The worker writes
    # this via emit_artifact(kind="beats", ...) after the asr_beats /
    # asr_anchors stage. Without beats.json the critic raises (the
    # prompt references the beat-by-beat ground truth).
    artifacts = data.get("artifacts") or {}
    beats_entry = artifacts.get("beats") or {}
    beats_uri = beats_entry.get("uri")
    if not beats_uri:
        _logger.warning("job %s: no beats artifact URI; skipping", job_id)
        try:
            doc_ref.set(
                {
                    "critique": {
                        "error": "no beats artifact",
                        "errored_at": _utcnow_iso(),
                    }
                },
                merge=True,
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("job %s: also failed to mark error: %s", job_id, e)
        return JobOutcome(
            job_id=job_id, status="skipped", reason="no_beats_uri"
        )

    slug = data.get("slug") or job_id

    root = (cache_root or _cache_root()) / job_id
    root.mkdir(parents=True, exist_ok=True)
    # critic.py expects beats.json directly under cache_dir
    cache_dir = root / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_dir = root / "critic"
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = root / "short.mp4"
    beats_path = cache_dir / "beats.json"

    download = download_fn or _gcs_download
    crit_fn = critique_fn
    if crit_fn is None:
        # Lazy import — keeps test importability sane.
        from pipeline.llm import critic as _critic_mod  # noqa: PLC0415
        crit_fn = _critic_mod.critique_short

    try:
        download(short_uri, mp4_path)
        download(beats_uri, beats_path)
        result = crit_fn(
            slug=slug,
            mp4_path=mp4_path,
            cache_dir=cache_dir,
            out_dir=out_dir,
        )
    except Exception as e:  # noqa: BLE001
        _logger.exception("job %s: critic call failed: %s", job_id, e)
        try:
            doc_ref.set(
                {
                    "critique": {
                        "error": f"{type(e).__name__}: {e}"[:512],
                        "errored_at": _utcnow_iso(),
                    }
                },
                merge=True,
            )
        except Exception as e2:  # noqa: BLE001
            _logger.warning(
                "job %s: also failed to mark error: %s", job_id, e2
            )
        # Clean up the (possibly partial) mp4 so we don't leak disk.
        if mp4_path.exists():
            try:
                mp4_path.unlink()
            except OSError:
                pass
        return JobOutcome(
            job_id=job_id, status="failed", reason=str(e)[:120]
        )

    verdict = result.get("verdict") if isinstance(result, dict) else None
    axes = result.get("axes") if isinstance(result, dict) else None
    score = result.get("score") if isinstance(result, dict) else None
    one_line = (
        result.get("one_line_take") if isinstance(result, dict) else None
    )

    update_doc = {
        "critique": {
            "verdict": verdict,
            "axes": axes,
            "score": score,
            "one_line_take": one_line,
            "critiqued_at": _utcnow_iso(),
            "critiqued_by": "laptop_cloud_poller",
            # Clear any prior error so the operator can see the latest
            # state without stale error chatter.
            "error": None,
            "errored_at": None,
        }
    }
    try:
        doc_ref.set(update_doc, merge=True)
    except Exception as e:  # noqa: BLE001
        _logger.exception("job %s: failed to write critique back: %s", job_id, e)
        return JobOutcome(
            job_id=job_id, status="failed", reason=f"firestore-write: {e}"[:120]
        )

    # Clean up the mp4 (large; we have the score.json now) but keep the
    # frames/ dir + score.json so the operator can debug a FIX/BLOCK
    # locally without re-downloading.
    if mp4_path.exists():
        try:
            mp4_path.unlink()
        except OSError:
            pass

    _logger.info(
        "job %s: critiqued verdict=%s score=%s slug=%s",
        job_id, verdict, score, slug,
    )
    return JobOutcome(
        job_id=job_id, status="critiqued", verdict=verdict
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


@dataclass
class _LoopStats:
    """Per-iteration counters surfaced via logs (and returned in
    tests so we can assert routing without parsing log lines)."""
    scanned: int = 0
    critiqued: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[JobOutcome] = field(default_factory=list)


def _one_iteration(
    client,
    *,
    cache_root: Optional[Path] = None,
    critique_fn: Optional[Callable[..., dict]] = None,
    download_fn: Optional[Callable[[str, Path], Path]] = None,
) -> _LoopStats:
    """Single poll → critique pass. Exposed for unit tests."""
    stats = _LoopStats()
    try:
        snaps = _scan_ungated_jobs(client)
    except Exception as e:  # noqa: BLE001
        _logger.exception("scan failed: %s", e)
        return stats

    stats.scanned = len(snaps)
    for snap in snaps:
        outcome = critique_one_job(
            client, snap,
            cache_root=cache_root,
            critique_fn=critique_fn,
            download_fn=download_fn,
        )
        stats.outcomes.append(outcome)
        if outcome.status == "critiqued":
            stats.critiqued += 1
        elif outcome.status == "failed":
            stats.failed += 1
        else:
            stats.skipped += 1
    return stats


def poll_and_critique_loop(
    *,
    project_id: str = "ytfactory-prod-v2",
    interval_s: int = 60,
    max_iters: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    client: Any = None,
    cache_root: Optional[Path] = None,
    critique_fn: Optional[Callable[..., dict]] = None,
    download_fn: Optional[Callable[[str, Path], Path]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Run the poll → critique → write-back loop forever (or for
    ``max_iters`` iterations in tests).

    Parameters
    ----------
    project_id
        GCP project hosting Firestore. Defaults to the prod project.
    interval_s
        Floor between polls. **Never** less than 60 s — we don't want
        to hammer Firestore reads when there's no work. Values <60
        get clamped up with a warning.
    max_iters
        If set, the loop exits after this many iterations. Used by
        unit tests + the ``--once`` CLI flag.
    stop_event
        Threading event for graceful SIGINT/SIGTERM shutdown. Loop
        checks before every sleep + every iteration.
    client, cache_root, critique_fn, download_fn, sleep_fn
        Test injection points. Production calls leave them None.

    Returns
    -------
    int
        Number of iterations actually run. Useful for assertions.
    """
    if interval_s < 60:
        _logger.warning(
            "interval_s=%s clamped up to 60 (don't hot-loop)", interval_s
        )
        interval_s = 60

    if stop_event is None:
        stop_event = threading.Event()

    if client is None:
        client = _firestore_client(project_id)

    iters = 0
    while True:
        if stop_event.is_set():
            _logger.info("stop_event set; exiting loop")
            return iters

        iters += 1
        t0 = time.time()
        stats = _one_iteration(
            client,
            cache_root=cache_root,
            critique_fn=critique_fn,
            download_fn=download_fn,
        )
        elapsed = time.time() - t0
        _logger.info(
            "iter %d: scanned=%d critiqued=%d failed=%d skipped=%d "
            "in %.1fs",
            iters, stats.scanned, stats.critiqued,
            stats.failed, stats.skipped, elapsed,
        )

        if max_iters is not None and iters >= max_iters:
            return iters

        # Sleep in 1-second slices so SIGINT can interrupt promptly.
        remaining = interval_s
        while remaining > 0 and not stop_event.is_set():
            slice_s = min(1.0, remaining)
            sleep_fn(slice_s)
            remaining -= slice_s


def install_sigint_handler(stop_event: threading.Event) -> None:
    """Wire SIGINT + SIGTERM to set ``stop_event``. Idempotent."""

    def _handler(signum, _frame):  # noqa: ARG001
        _logger.info("received signal %s; requesting shutdown", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


__all__ = [
    "JobOutcome",
    "critique_one_job",
    "install_sigint_handler",
    "poll_and_critique_loop",
]
