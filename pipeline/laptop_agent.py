"""Laptop agent — long-polls the cloud control plane for Chrome-bound work.

Cloud Run can't drive your signed-in Chrome (Playwright YT upload,
burner-channel engagement, X-cookie scraping). This agent fills that
gap: it runs on the laptop, long-polls `POST /agent/lease`, executes
the task locally, ACKs back.

Two task kinds it claims:
  - PLAYWRIGHT_UPLOAD  — drives Chrome to upload an mp4 via studio.youtube.com
  - BURNER_ENGAGE      — drives Chrome to like/sub via burner profiles
  - CREATE_BURNER      — drives Chrome to spin up a new YouTube brand account

Run manually:
    .venv/bin/python -m pipeline.laptop_agent

Recommended: launch via the included launchd plist
(`~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist`) which
keeps it alive across reboots / sleep cycles via KeepAlive.

Auth: requires `YTFACTORY_AGENT_TOKEN` matching the cloud control
plane's env var. Stored in `~/.config/ytfactory/agent_token` (mode 600).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

logger = logging.getLogger("laptop_agent")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CONTROL_URL = os.environ.get(
    "YTFACTORY_CONTROL_URL",
    "https://ytfactory-web-7hwnzw7lya-as.a.run.app",
)
TOKEN_FILE = Path.home() / ".config" / "ytfactory" / "agent_token"
AGENT_ID = os.environ.get("YTFACTORY_AGENT_ID") or f"laptop-{socket.gethostname()}"

# Capabilities advertised to the cloud queue.
#
# 2026-05-12: ``playwright_upload`` removed — the executor still raises
# NotImplementedError, and with parallel leasing it would rapidly drain any
# queued upload tasks into instant ack-failures (which until today were
# silently 422-ing because we sent status="failed" instead of the schema's
# "error", leaving them LEASED forever — that's the same bug that produced
# the 158-task zombie pile we just reaped).
CAPS = ["burner_engage", "create_burner"]

LEASE_TTL_S = 600  # 10 min — enough for a YT upload + post-publish steps
HEARTBEAT_INTERVAL_S = 60

# Per-kind in-flight caps. Each worker holds a slot for the full lifetime of
# the spawned subprocess (NOT just the Popen call) so we don't accidentally
# launch hundreds of Chrome instances during a 168-task burst. ``burner_engage``
# in subscribe_only mode runs ~1-3 min per burner; ``create_burner`` is a
# multi-minute Playwright flow that must serialise per Chrome profile.
#
# Tunable via env so we can dial up if the laptop is comfortable, or down if
# Chrome contention causes flakes.
DEFAULT_KIND_CAPS = {
    "burner_engage": int(os.environ.get("YTFACTORY_AGENT_CAP_BURNER_ENGAGE", "4")),
    "create_burner": int(os.environ.get("YTFACTORY_AGENT_CAP_CREATE_BURNER", "1")),
    "playwright_upload": int(os.environ.get("YTFACTORY_AGENT_CAP_PLAYWRIGHT_UPLOAD", "1")),
}

# Number of lease-loop worker threads. Each one independently long-polls for
# work, so this caps how many concurrent /agent/lease calls we make. Should be
# >= sum of per-kind caps so a stalled task in one kind doesn't starve another.
NUM_WORKERS = int(os.environ.get("YTFACTORY_AGENT_WORKERS", "5"))

# Tracks how many child subprocesses are currently running per kind. Workers
# refuse to lease for a kind that's already at cap. Updated under
# _CHILDREN_LOCK so the count stays consistent across worker threads.
_CHILDREN_LOCK = threading.Lock()
_active_by_kind: dict[str, int] = {k: 0 for k in DEFAULT_KIND_CAPS}

# Set by the SIGTERM handler so worker threads stop claiming new tasks. They
# continue to drain in-flight spawns + acks for a clean shutdown.
_SHUTDOWN = threading.Event()


def _token() -> str:
    """Bearer token for the cloud control plane.

    Uses gcloud OIDC — Google Frontend validates against run.invoker
    IAM. Works for user accounts (no --audiences) and service accounts
    (with --audiences). Falls back to YTFACTORY_AGENT_TOKEN if the
    target is localhost (dev mode).
    """
    if CONTROL_URL.startswith(("http://localhost", "http://127.0.0.1")):
        tok = os.environ.get("YTFACTORY_AGENT_TOKEN")
        if tok:
            return tok
        if TOKEN_FILE.exists():
            return TOKEN_FILE.read_text().strip()
        raise SystemExit(
            f"YTFACTORY_AGENT_TOKEN not set in env, and {TOKEN_FILE} missing. "
            f"For local dev, generate with `openssl rand -hex 32`."
        )
    import subprocess  # noqa: PLC0415
    cached = _ID_TOKEN_CACHE.get("user")
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        # User account: omit --audiences (gcloud rejects it for users).
        # The token's email claim is what Cloud Run IAM uses to match
        # run.invoker bindings against `user:rohittomar@docx.co.in` or
        # `domain:docx.co.in`.
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-identity-token"],
            text=True, stderr=subprocess.PIPE, timeout=15,
        ).strip()
    except FileNotFoundError as e:
        raise SystemExit(
            "gcloud CLI not found; required to authenticate against cloud "
            f"control plane at {CONTROL_URL}. Install gcloud or set "
            "YTFACTORY_CONTROL_URL=http://127.0.0.1:8766 for laptop dev."
        ) from e
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"gcloud auth print-identity-token failed: {e.stderr}"
        ) from e
    if not tok:
        raise SystemExit("empty ID token from gcloud")
    # User-account ID tokens last ~1h. Cache 50 min.
    _ID_TOKEN_CACHE["user"] = (tok, time.time() + 50 * 60)
    return tok


_ID_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _post(path: str, body: dict, *, timeout: float = 65) -> dict:
    """POST to the control plane.

    Default timeout (65 s) is comfortably wider than the cloud's 30 s lease
    long-poll deadline + TLS handshake + Cloud Run cold-start variance, so
    transient cloud latency doesn't surface as ``read timeout`` here.
    """
    url = CONTROL_URL.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_token()}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _heartbeat() -> None:
    try:
        _post("/agent/heartbeat", {
            "resources": {
                "agent_id": AGENT_ID,
                "caps": CAPS,
            },
        }, timeout=20)
    except Exception as e:
        logger.warning("heartbeat failed: %s", e)


def _claim_task(caps: list[str]) -> dict | None:
    """Long-poll for one matching task. Returns None on timeout.

    ``caps`` is the per-call subset of the agent's CAPS (kinds that still
    have local capacity). Passing a narrowed cap list lets the cloud skip
    matching tasks for kinds we can't actually run right now.
    """
    if not caps:
        # All kinds at local cap → nothing for us to do, skip the long-poll.
        return None
    try:
        resp = _post("/agent/lease", {
            "agent_id": AGENT_ID,
            "caps": caps,
            "lease_ttl_s": LEASE_TTL_S,
        }, timeout=65)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            logger.error("auth rejected — refresh YTFACTORY_AGENT_TOKEN")
            time.sleep(60)
            return None
        logger.warning("lease HTTP %s: %s", e.code, e.read()[:200])
        time.sleep(5)
        return None
    except Exception as e:
        logger.warning("lease failed: %s", e)
        time.sleep(5)
        return None
    return resp.get("task")


def _ack(task_id: str, *, ok: bool, output_uri: str | None = None,
         error: str | None = None) -> None:
    try:
        # The cloud schema expects Literal["ok", "error"] — sending "failed"
        # used to silently 422 and leave tasks LEASED, which is what produced
        # the 158 stuck-LEASED zombies we reaped on 2026-05-12.
        _post(f"/agent/ack/{task_id}", {
            "agent_id": AGENT_ID,
            "status": "ok" if ok else "error",
            "output_uri": output_uri,
            "error": error,
        }, timeout=20)
    except Exception as e:
        logger.error("ack failed for %s: %s", task_id, e)


# ---------------------------------------------------------------------------
# Task executors
#
# Each executor returns (ok, output_uri, error, child) where ``child`` is the
# spawned subprocess (or None for synchronous tasks). The worker thread holds
# the per-kind capacity slot until ``child.wait()`` returns, which is what
# gives us real Chrome-instance concurrency control — without it, the agent
# would Popen 168 burner_engage children in seconds and torch the laptop.
# ---------------------------------------------------------------------------

def _execute(task: dict) -> tuple[bool, str | None, str | None, Any]:
    """Execute the task. Returns (ok, output_uri, error, child_proc_or_None)."""
    kind = task.get("kind", "")
    payload = task.get("payload", {}) or {}
    logger.info("executing task %s kind=%s", task.get("task_id"), kind)
    try:
        if kind == "playwright_upload":
            ok, out, err = _exec_playwright_upload(payload)
            return ok, out, err, None
        if kind == "burner_engage":
            return _exec_burner_engage(payload)
        if kind == "create_burner":
            return _exec_create_burner(payload)
        return False, None, f"unsupported task kind on laptop: {kind}", None
    except Exception as e:
        logger.exception("task %s failed", task.get("task_id"))
        return False, None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[:800]}", None


def _exec_playwright_upload(p: dict) -> tuple[bool, str | None, str | None]:
    """Drive Chrome to upload an mp4 to YouTube Studio.

    Required payload keys:
      mp4_uri:    gs://... source file (downloaded locally first)
      title:      str
      description: str
      tags:       list[str] (optional)
      visibility: 'public' | 'unlisted' | 'private'
      account:    'mystoriesanimated' (which Chrome profile to use)
    Optional:
      thumbnail_uri:  gs://... custom thumbnail
    Returns:
      output_uri = the YouTube video URL on success.
    """
    from pipeline.upload.upload import upload_short_via_playwright  # type: ignore  # noqa
    # Falls back to documenting the gap — real upload-via-playwright
    # is in the .claude/skills/upload-via-playwright/SKILL.md and is
    # invoked by the agent code path when this entry-point is implemented.
    raise NotImplementedError(
        "playwright_upload executor not implemented yet — see "
        "/.claude/skills/upload-via-playwright/SKILL.md for the manual "
        "Chrome-driven flow that this should automate."
    )


def _exec_burner_engage(p: dict) -> tuple[bool, str | None, str | None, Any]:
    """Spawn the engage worker for a burner profile in the background.

    Required payload keys:
      slug:       burner channel slug (matches youtube-token-<slug> secret)

    Subscribe-only mode runs ~1-3 min per burner (visit each unique source
    channel, click Subscribe, exit). Like/watch modes run for hours via the
    tab-cycling watch loop. Either way, the spawned subprocess is detached
    from the agent's lifetime — if launchd restarts the agent, the worker
    keeps running until it hits its stop signal.

    Returns the Popen so the calling worker thread can hold its per-kind
    capacity slot until the subprocess exits — that's what bounds concurrent
    Chrome instances on the laptop.
    """
    import subprocess  # noqa: PLC0415

    slug = p.get("slug")
    if not slug:
        return False, None, "missing payload.slug", None

    repo_root = Path(__file__).resolve().parent.parent
    log_dir = repo_root / "data" / "burner_engage" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{slug}.log"
    log_fp = log_path.open("a", buffering=1)
    log_fp.write(f"\n\n=== laptop-agent spawn @ {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n")
    log_fp.flush()

    # Engagement mode (added 2026-05-11). Default keeps the original
    # like+sub+watch-loop behaviour for any task enqueued before the
    # cloud route started passing this field.
    mode = p.get("mode") or "like_subscribe_view"

    cmd = [
        sys.executable, "-m", "pipeline.cross_engage.burner_engage",
        "run", str(slug), "--mode", mode,
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    # The cloud cutover (2026-05-09) moved per-channel uploads/ records
    # into GCS. The worker reads the catalog (every shipped video to
    # engage with) via pipeline.utils.catalog.list_catalog, which now
    # honours YTFACTORY_STATE_BUCKET for the GCS-backed read path. The
    # laptop's launchd plist doesn't normally export this env, so we
    # default it here — that way the worker sees the production catalog
    # whether or not the user remembered to set it. Override-friendly:
    # any pre-set value in the plist wins.
    env.setdefault("YTFACTORY_STATE_BUCKET", "ytfactory-prod-v2-state")
    env.setdefault("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            # Fully detach: the agent's own lifetime is independent of
            # the worker's. If launchd restarts the agent (or we're
            # killed for any reason), the burner_engage worker keeps
            # running until it hits its stop signal.
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, None, f"failed to spawn burner_engage worker: {e}", None

    logger.info(
        "laptop_agent: spawned burner_engage worker pid=%s slug=%s mode=%s log=%s",
        proc.pid, slug, mode, log_path,
    )
    return True, None, None, proc


def _exec_create_burner(p: dict) -> tuple[bool, str | None, str | None, Any]:
    """Spawn one ``pipeline.cross_engage.create_burner_channel`` invocation.

    Optional payload keys:
      email:        host Google account that hosts the new brand-account
                    channel. Defaults to the CLI's DEFAULT_EMAIL.
      display_name: explicit display name. Default is a random
                    realistic-looking burner name (matches the existing
                    burner aesthetic — see random_burner_name in the CLI).
      slug:         explicit local identifier. Derived from display_name
                    if omitted.
      oauth:        bool, default True. False appends --no-oauth so the
                    channel is created but not OAuth'd.

    Multi-minute Playwright flow (Chrome boot + form fill + handle resolution
    + create-button + UC-id wait + optional OAuth). Returns the Popen so the
    worker can hold its per-kind slot — ``create_burner`` is capped to 1 by
    default because parallel Chrome attaches against the same Google account
    confuse the create flow (refusal screens, OTP prompts, OAuth corruption).
    """
    import subprocess  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parent.parent
    log_dir = repo_root / "data" / "burner_engage" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "create_burner.log"
    log_fp = log_path.open("a", buffering=1)
    log_fp.write(
        f"\n\n=== laptop-agent create_burner spawn @ "
        f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} payload={p} ===\n"
    )
    log_fp.flush()

    cmd = [sys.executable, "-m", "pipeline.cross_engage.create_burner_channel"]
    email = (p.get("email") or "").strip()
    if email:
        cmd += ["--email", email]
    display_name = (p.get("display_name") or "").strip()
    if display_name:
        cmd += ["--display-name", display_name]
    slug = (p.get("slug") or "").strip()
    if slug:
        cmd += ["--slug", slug]
    if not p.get("oauth", True):
        cmd.append("--no-oauth")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("YTFACTORY_STATE_BUCKET", "ytfactory-prod-v2-state")
    env.setdefault("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, None, f"failed to spawn create_burner_channel: {e}", None

    logger.info(
        "laptop_agent: spawned create_burner_channel pid=%s payload=%s log=%s",
        proc.pid, p, log_path,
    )
    return True, None, None, proc


# ---------------------------------------------------------------------------
# Worker pool + main loop
# ---------------------------------------------------------------------------

def _worker_loop(worker_id: int, kind_sems: dict[str, threading.BoundedSemaphore]) -> None:
    """One independent lease → spawn → ack → wait-for-child loop.

    Holds a per-kind capacity slot for the FULL lifetime of the spawned
    subprocess. That's what bounds concurrent Chrome instances on the
    laptop — without it, ``run()`` could rip through hundreds of queued
    burner_engage tasks in seconds and spawn a Chrome instance for each.

    On SIGTERM (``_SHUTDOWN.set()``), workers stop claiming new tasks but
    finish their current spawn+ack so the lease isn't orphaned.
    """
    logger.info("worker %d started", worker_id)
    while not _SHUTDOWN.is_set():
        # Pick the first kind with free capacity. CAPS order matters: we
        # prefer ``burner_engage`` (the bulk-throughput kind) over the rarer
        # ``create_burner``, so during a Subscribe-All burst all workers
        # converge on the high-volume kind first.
        acquired_kind: str | None = None
        for kind in CAPS:
            sem = kind_sems.get(kind)
            if sem is None:
                continue
            if sem.acquire(blocking=False):
                acquired_kind = kind
                break
        if acquired_kind is None:
            # All kinds we serve are at local cap. Sleep briefly with
            # shutdown awareness, then retry.
            _SHUTDOWN.wait(2)
            continue

        try:
            # Narrow the lease cap list so we don't get a kind we can't run.
            task = _claim_task([acquired_kind])
            if task is None:
                continue  # release slot via finally, retry
            with _CHILDREN_LOCK:
                _active_by_kind[acquired_kind] += 1
            try:
                ok, out_uri, err, child = _execute(task)
                _ack(task["task_id"], ok=ok, output_uri=out_uri, error=err)
                if child is not None:
                    try:
                        # Block until the spawned Chrome subprocess exits.
                        # This is what enforces real per-kind concurrency.
                        child.wait()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("worker %d: child wait failed: %s", worker_id, e)
            finally:
                with _CHILDREN_LOCK:
                    _active_by_kind[acquired_kind] = max(0, _active_by_kind[acquired_kind] - 1)
        finally:
            kind_sems[acquired_kind].release()
    logger.info("worker %d stopping (shutdown signal)", worker_id)


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):  # noqa: ARG001
        logger.info("signal %s received → graceful shutdown", signum)
        _SHUTDOWN.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run() -> None:
    logger.info(
        "laptop agent starting; agent_id=%s control=%s caps=%s workers=%d kind_caps=%s",
        AGENT_ID, CONTROL_URL, CAPS, NUM_WORKERS,
        {k: DEFAULT_KIND_CAPS[k] for k in CAPS if k in DEFAULT_KIND_CAPS},
    )

    _install_signal_handlers()

    # One BoundedSemaphore per CAPS kind. Workers acquire-before-lease, so
    # the cap is enforced even before we hit the cloud queue.
    kind_sems: dict[str, threading.BoundedSemaphore] = {}
    for kind in CAPS:
        cap = DEFAULT_KIND_CAPS.get(kind, 1)
        kind_sems[kind] = threading.BoundedSemaphore(cap)

    # Worker threads are daemon so they don't keep the process alive past a
    # shutdown signal — but the SIGTERM handler sets _SHUTDOWN first, giving
    # each worker a chance to finish its current spawn+ack.
    workers = [
        threading.Thread(
            target=_worker_loop,
            args=(i, kind_sems),
            name=f"agent-worker-{i}",
            daemon=True,
        )
        for i in range(NUM_WORKERS)
    ]
    for w in workers:
        w.start()

    # Send an initial heartbeat so the cloud sees the agent alive even if
    # workers immediately get blocked on long-polls. Then beat at the
    # configured interval until shutdown.
    _heartbeat()
    last_hb = time.time()
    while not _SHUTDOWN.is_set():
        now = time.time()
        if now - last_hb > HEARTBEAT_INTERVAL_S:
            _heartbeat()
            last_hb = now
        # Short wait so SIGTERM is responsive.
        _SHUTDOWN.wait(2)

    # Give workers a brief window to finish their current task. They each
    # still have the per-task lease TTL on the cloud side, so a half-finished
    # spawn will get reaped by the cloud reaper rather than leaking.
    deadline = time.time() + 10
    for w in workers:
        remaining = max(0.1, deadline - time.time())
        w.join(timeout=remaining)
    logger.info("laptop agent stopped")


if __name__ == "__main__":
    run()
