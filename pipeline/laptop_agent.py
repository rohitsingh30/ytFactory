"""Laptop agent — long-polls the cloud control plane for Chrome-bound work.

Cloud Run can't drive your signed-in Chrome (Playwright YT upload,
burner-channel engagement, X-cookie scraping). This agent fills that
gap: it runs on the laptop, long-polls `POST /agent/lease`, executes
the task locally, ACKs back.

Two task kinds it claims:
  - PLAYWRIGHT_UPLOAD  — drives Chrome to upload an mp4 via studio.youtube.com
  - BURNER_ENGAGE      — drives Chrome to like/sub via burner profiles

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
import socket
import sys
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
    "https://ytfactory-control-7hwnzw7lya-as.a.run.app",
)
TOKEN_FILE = Path.home() / ".config" / "ytfactory" / "agent_token"
AGENT_ID = os.environ.get("YTFACTORY_AGENT_ID") or f"laptop-{socket.gethostname()}"

CAPS = ["playwright_upload", "burner_engage"]
LEASE_TTL_S = 600  # 10 min — enough for a YT upload + post-publish steps
HEARTBEAT_INTERVAL_S = 60


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


def _post(path: str, body: dict, *, timeout: float = 35) -> dict:
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
        }, timeout=10)
    except Exception as e:
        logger.warning("heartbeat failed: %s", e)


def _claim_task() -> dict | None:
    """Long-poll for one matching task. Returns None on timeout."""
    try:
        resp = _post("/agent/lease", {
            "agent_id": AGENT_ID,
            "caps": CAPS,
            "lease_ttl_s": LEASE_TTL_S,
        }, timeout=35)
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
        _post(f"/agent/ack/{task_id}", {
            "agent_id": AGENT_ID,
            "status": "ok" if ok else "failed",
            "output_uri": output_uri,
            "error": error,
        }, timeout=15)
    except Exception as e:
        logger.error("ack failed for %s: %s", task_id, e)


# ---------------------------------------------------------------------------
# Task executors
# ---------------------------------------------------------------------------

def _execute(task: dict) -> tuple[bool, str | None, str | None]:
    """Execute the task. Returns (ok, output_uri, error)."""
    kind = task.get("kind", "")
    payload = task.get("payload", {}) or {}
    logger.info("executing task %s kind=%s", task.get("task_id"), kind)
    try:
        if kind == "playwright_upload":
            return _exec_playwright_upload(payload)
        if kind == "burner_engage":
            return _exec_burner_engage(payload)
        return False, None, f"unsupported task kind on laptop: {kind}"
    except Exception as e:
        logger.exception("task %s failed", task.get("task_id"))
        return False, None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[:800]}"


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


def _exec_burner_engage(p: dict) -> tuple[bool, str | None, str | None]:
    """Spawn the engage worker for a burner profile in the background.

    Required payload keys:
      slug:       burner channel slug (matches youtube-token-<slug> secret)

    The worker is a long-lived loop (engage all videos, then enter the
    tab-cycling watch phase forever until the user clicks Stop). We
    intentionally fire-and-forget here:

      * Spawn the subprocess detached (``start_new_session=True``).
      * Pipe stdout/stderr to a per-slug log file under
        ``data/burner_engage/logs/<slug>.log`` (same convention the old
        cloud subprocess.Popen path used).
      * Ack the task as ``ok`` immediately so the lease doesn't expire
        and the cloud queue marks the task DONE in seconds rather than
        keeping it LEASED for hours.

    The UI tracks per-burner progress via the GCS-backed state file
    (see ``pipeline.cross_engage.burner_engage._save_state`` /
    ``read_state``), NOT via task-status polling, so a fast ack is the
    correct UX here.
    """
    import subprocess  # noqa: PLC0415

    slug = p.get("slug")
    if not slug:
        return False, None, "missing payload.slug"

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
        return False, None, f"failed to spawn burner_engage worker: {e}"

    logger.info(
        "laptop_agent: spawned burner_engage worker pid=%s slug=%s mode=%s log=%s",
        proc.pid, slug, mode, log_path,
    )
    return True, None, None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    logger.info("laptop agent starting; agent_id=%s control=%s caps=%s",
                AGENT_ID, CONTROL_URL, CAPS)
    last_hb = 0.0
    while True:
        now = time.time()
        if now - last_hb > HEARTBEAT_INTERVAL_S:
            _heartbeat()
            last_hb = now
        task = _claim_task()
        if task is None:
            # Long-poll already waited up to 30s; small extra back-off so we
            # don't hammer the control plane on auth failure.
            time.sleep(1)
            continue
        ok, out_uri, err = _execute(task)
        _ack(task["task_id"], ok=ok, output_uri=out_uri, error=err)


if __name__ == "__main__":
    run()
