"""Cloud Run JOB execution reconciler — fixes silent-death blind spot
(B3b in 2026-05-23 docket; the "known gap" cited at
``control/routes/render_routes.py:732-743``).

The render-worker-v2 JOB writes ``status=rendering`` on the
``jobs/<id>`` Firestore doc when it starts, then updates ``stage`` on
every transition. If the worker crashes or is SIGKILLed (Cloud Run
``--task-timeout``, OOM, host eviction, Python interpreter crash, …),
the Firestore doc is frozen at ``rendering`` forever — the dashboard
shows a perpetual "Working" spinner and the user has no way to know
the render is dead without tailing Cloud Run logs.

The B3a SIGTERM handler (``cloud/render-worker-v2/entrypoint.py``)
catches the soft-termination path. This reconciler is the safety net
for everything SIGTERM cannot catch: SIGKILL, OOM kill, kernel panic,
network partition between the worker and Firestore, etc.

Strategy per tick:

  1. List Firestore ``jobs/*`` with ``status IN (queued, rendering,
     dispatching, pending)`` AND ``updated_at < now - max_age_minutes``
     (default 120m = 2× the worker's 60m task-timeout). The age filter
     keeps the Cloud Run Admin API call volume bounded.
  2. For each candidate, look up the JOB execution via
     ``cloud_execution`` (set by
     ``control/routes/render_routes.py:404``).
  3. Fetch the live Execution proto and decide:
        * ``completion_time`` unset → execution still running. DON'T
          touch — the worker may just be in a long compose stage that
          doesn't update Firestore for minutes at a time.
        * ``completion_time`` set + ``failed_count >= 1`` → mark job
          ``status=failed`` with an explanatory error.
        * ``completion_time`` set + ``cancelled_count >= 1`` → mark
          job ``status=cancelled``.
        * ``completion_time`` set + ``succeeded_count >= 1`` but
          Firestore still says ``rendering`` → worker died after
          completing the render-worker JOB but before writing
          ``status=done``. Rare, but mark failed with a "succeeded
          but job doc never updated" message so the operator can
          investigate.
  4. NotFound on the Execution → it was evicted (Cloud Run's 60-day
     retention) but Firestore still has the doc. Mark failed.

This module is structured as a single ``reconcile_stuck_jobs()`` entry
point that:

  * Returns a structured summary dict (counts + per-job actions) for
    the route and the plist cron to log.
  * Never raises — Firestore / Cloud Run blips degrade to "skipped
    this tick, try again next tick" so a transient SDK failure can't
    leave dead jobs unsweepable.
  * Supports ``dry_run=True`` so an admin can preview what WOULD be
    marked failed before the first cron run.

Wiring:

  * ``POST /api/admin/reconcile_jobs`` (control/routes/render_routes.py)
    — manual trigger for now-tick.
  * ``com.ytfactory.job-reconciler.plist`` LaunchAgent on the laptop
    runs this every 5 min in production until we move the control
    plane to Cloud Run.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Default candidacy window: anything older than 2× the worker's
# task-timeout (60m) is fair game for reconciliation. Tighter than
# this and we'd reconcile genuinely-still-running jobs whose worker
# happens to be in a long compose stage without progress writes.
DEFAULT_MAX_AGE_MINUTES = int(
    os.environ.get("YTFACTORY_RECONCILER_MAX_AGE_MIN", "120")
)

# Hard cap on candidates per tick — protects against a Firestore index
# blowup that returns 10k rows from gridlocking the loop.
DEFAULT_BATCH_SIZE = int(
    os.environ.get("YTFACTORY_RECONCILER_BATCH_SIZE", "50")
)

# Statuses that are reconcilable. Terminal statuses (done / failed /
# cancelled) are never touched — the reconciler only fixes the
# "silently stuck mid-pipeline" class of bug.
_RECONCILABLE_STATUSES = ("queued", "pending", "dispatching", "rendering", "running")


def reconcile_stuck_jobs(
    *,
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One reconciliation pass over Firestore ``jobs/*``.

    Args:
      max_age_minutes: candidates must have ``updated_at`` older than
        now - this. Default 120m.
      batch_size: hard cap on candidates per tick.
      dry_run: if True, log what would be done but don't write to
        Firestore.

    Returns:
      ``{"scanned": int, "candidates": int, "marked_failed": int,
         "marked_cancelled": int, "still_running": int,
         "no_execution_ref": int, "execution_lookup_failed": int,
         "actions": [{job_id, action, reason}, ...],
         "dry_run": bool}``

    Never raises. Per-job errors are caught + counted so a single bad
    doc can't stop the loop.
    """
    summary: dict[str, Any] = {
        "scanned": 0,
        "candidates": 0,
        "marked_failed": 0,
        "marked_cancelled": 0,
        "marked_succeeded_but_unwritten": 0,
        "still_running": 0,
        "no_execution_ref": 0,
        "execution_lookup_failed": 0,
        "execution_not_found": 0,
        "actions": [],
        "dry_run": dry_run,
        "error": None,
    }

    try:
        from google.cloud import firestore  # noqa: PLC0415
    except ImportError as exc:
        summary["error"] = f"firestore SDK unavailable: {exc}"
        logger.warning("reconciler: %s", summary["error"])
        return summary

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3")
    try:
        db = firestore.Client(project=project)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"firestore client init failed: {exc}"
        logger.warning("reconciler: %s", summary["error"])
        return summary

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    candidates: list[tuple[str, dict]] = []
    # Query each status separately rather than using `where IN` to avoid
    # the "in" + "<" composite-index requirement (one extra index per
    # status pair otherwise). 5 small queries < 1 big composite.
    for status in _RECONCILABLE_STATUSES:
        try:
            q = (
                db.collection("jobs")
                .where("status", "==", status)
                .where("updated_at", "<", cutoff)
                .limit(batch_size)
            )
            for doc in q.stream():
                d = doc.to_dict() or {}
                d["job_id"] = doc.id
                candidates.append((doc.id, d))
                summary["scanned"] += 1
                if len(candidates) >= batch_size:
                    break
        except Exception as exc:  # noqa: BLE001
            # Most likely a composite-index 400 the first time. Log + skip
            # this status; other statuses can still be reconciled.
            logger.warning(
                "reconciler: firestore query failed for status=%s: %s "
                "(needs composite index updated_at + status)",
                status, exc,
            )
            continue
        if len(candidates) >= batch_size:
            break

    summary["candidates"] = len(candidates)
    if not candidates:
        logger.debug("reconciler: no candidates older than %s min", max_age_minutes)
        return summary

    # Lazy-import the executions client — keeps test stubs from paying
    # the gRPC import cost.
    try:
        from google.cloud import run_v2  # noqa: PLC0415
        exec_client = run_v2.ExecutionsClient()
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"run_v2 client init failed: {exc}"
        logger.warning("reconciler: %s", summary["error"])
        return summary

    for job_id, doc in candidates:
        action = _reconcile_one(
            job_id, doc, db=db, exec_client=exec_client, dry_run=dry_run,
        )
        summary["actions"].append(action)
        key = action["action"]
        if key in summary:
            summary[key] += 1

    logger.info(
        "reconciler: scanned=%d candidates=%d marked_failed=%d "
        "marked_cancelled=%d still_running=%d no_execution_ref=%d "
        "lookup_failed=%d not_found=%d dry_run=%s",
        summary["scanned"], summary["candidates"], summary["marked_failed"],
        summary["marked_cancelled"], summary["still_running"],
        summary["no_execution_ref"], summary["execution_lookup_failed"],
        summary["execution_not_found"], dry_run,
    )
    return summary


def _reconcile_one(
    job_id: str,
    doc: dict,
    *,
    db,
    exec_client,
    dry_run: bool,
) -> dict[str, str]:
    """Reconcile one stuck job. Returns an action dict for the summary."""
    exec_name = doc.get("cloud_execution") or doc.get("cloudrun_execution")
    if not exec_name:
        # Worker died before _enqueue_render_job wrote cloud_execution
        # (between trigger_render_job and the Firestore update — narrow
        # window). Mark failed since we can't verify execution state.
        reason = (
            f"no cloud_execution ref on Firestore doc (last stage="
            f"{doc.get('stage')!r}); worker died before dispatch "
            f"writeback — cannot verify execution state, marking failed"
        )
        if not dry_run:
            _safe_update(db, job_id, {
                "status": "failed",
                "stage": doc.get("stage") or "dispatch",
                "error": (
                    "Reconciler: no cloud_execution ref on job doc and "
                    "updated_at is stale; assuming worker died before "
                    "dispatch writeback completed."
                ),
                "reconciled_at": datetime.now(timezone.utc),
            })
        return {
            "job_id": job_id,
            "action": "no_execution_ref",
            "reason": reason,
        }

    # Fetch the live Execution.
    try:
        execution = exec_client.get_execution(name=exec_name)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "NotFound" in name or "404" in str(exc):
            # Cloud Run keeps execution records for 60 days — beyond
            # that they're evicted and the Firestore doc is orphaned.
            if not dry_run:
                _safe_update(db, job_id, {
                    "status": "failed",
                    "stage": doc.get("stage") or "unknown",
                    "error": (
                        f"Reconciler: Cloud Run Execution {exec_name} "
                        f"not found (likely evicted after 60-day "
                        f"retention). Job doc orphaned."
                    ),
                    "reconciled_at": datetime.now(timezone.utc),
                })
            return {
                "job_id": job_id,
                "action": "execution_not_found",
                "reason": f"Execution {exec_name} not found",
            }
        logger.warning(
            "reconciler: get_execution(%s) failed: %s — leaving job=%s "
            "alone, will retry next tick",
            exec_name, exc, job_id,
        )
        return {
            "job_id": job_id,
            "action": "execution_lookup_failed",
            "reason": f"{name}: {exc}",
        }

    completion_time = getattr(execution, "completion_time", None)
    failed_count = int(getattr(execution, "failed_count", 0) or 0)
    succeeded_count = int(getattr(execution, "succeeded_count", 0) or 0)
    cancelled_count = int(getattr(execution, "cancelled_count", 0) or 0)
    running_count = int(getattr(execution, "running_count", 0) or 0)

    # Proto fields default to "Timestamp(seconds=0)" when unset — must
    # check the seconds value too, not just truthiness.
    completion_seconds = (
        getattr(completion_time, "seconds", 0) if completion_time else 0
    )

    if completion_seconds == 0:
        # Execution still running OR queued. running_count==1 means a
        # task is actively executing (worker is just not writing
        # Firestore progress). Don't touch.
        return {
            "job_id": job_id,
            "action": "still_running",
            "reason": (
                f"execution still in progress "
                f"(running_count={running_count}, completion_time unset)"
            ),
        }

    # Execution completed. Decide which terminal state to apply to
    # the Firestore doc.
    if failed_count >= 1:
        err = (
            f"Reconciler: Cloud Run Execution {exec_name} reported "
            f"failed_count={failed_count} (likely SIGKILL / OOM / "
            f"task-timeout). Firestore doc was stuck at "
            f"status={doc.get('status')!r}, stage={doc.get('stage')!r}. "
            f"Marking failed so the dashboard surfaces it."
        )
        if not dry_run:
            _safe_update(db, job_id, {
                "status": "failed",
                "stage": doc.get("stage") or "unknown",
                "error": err,
                "reconciled_at": datetime.now(timezone.utc),
            })
        return {
            "job_id": job_id,
            "action": "marked_failed",
            "reason": err,
        }

    if cancelled_count >= 1:
        if not dry_run:
            _safe_update(db, job_id, {
                "status": "cancelled",
                "stage": doc.get("stage") or "unknown",
                "error": (
                    f"Reconciler: Execution {exec_name} was cancelled "
                    f"(cancelled_count={cancelled_count})."
                ),
                "reconciled_at": datetime.now(timezone.utc),
            })
        return {
            "job_id": job_id,
            "action": "marked_cancelled",
            "reason": f"cancelled_count={cancelled_count}",
        }

    if succeeded_count >= 1:
        # Execution completed cleanly but the worker never wrote
        # status=done — extremely rare (would mean the worker died
        # AFTER all stages but BEFORE the final _update_job at the
        # end of _main_from_firestore). Mark failed so the operator
        # can investigate; the mp4 may still be in GCS.
        if not dry_run:
            _safe_update(db, job_id, {
                "status": "failed",
                "stage": doc.get("stage") or "unknown",
                "error": (
                    f"Reconciler: Cloud Run Execution {exec_name} "
                    f"succeeded (succeeded_count={succeeded_count}) "
                    f"but the worker never wrote status=done. mp4 may "
                    f"exist at gs://.../jobs/{job_id}/ — check before "
                    f"re-rendering."
                ),
                "reconciled_at": datetime.now(timezone.utc),
            })
        return {
            "job_id": job_id,
            "action": "marked_succeeded_but_unwritten",
            "reason": "execution succeeded but no done writeback",
        }

    # Defensive — none of the counts are positive but completion_time
    # is set. Shouldn't happen per the Cloud Run docs but be safe.
    return {
        "job_id": job_id,
        "action": "still_running",
        "reason": (
            f"completion_time set but counts are all zero "
            f"(succeeded={succeeded_count} failed={failed_count} "
            f"cancelled={cancelled_count}); skipping to be safe"
        ),
    }


def _safe_update(db, job_id: str, fields: dict) -> None:
    """Best-effort Firestore set. Errors are logged but don't propagate."""
    try:
        db.collection("jobs").document(job_id).set(fields, merge=True)
    except Exception:  # noqa: BLE001
        logger.exception("reconciler: failed to update job=%s", job_id)
