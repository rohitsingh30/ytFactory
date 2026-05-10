"""Website-native render dispatch helper for ytFactory skills.

Skills (the AI authoring layer — `/make-mystories-short`,
`/make-cosmos-short`, etc.) used to bash-exec ``scripts/make_shorts.py``
directly. That coupled every skill invocation to whatever shell env
the skill happened to land in (PYTHONPATH, cwd, gcloud account, …),
which broke at every project migration. The 2026-05-09
``ytfactory-prod`` → ``ytfactory-prod-v2`` cutover surfaced this once
too often.

The website-native pattern instead lets the skill POST a render
request to the **cloud** ytfactory-web service, which spawns the
renderer (cloud render-worker JOB) in its own already-correct env.
This module is the client-side glue:

    from pipeline.cloud.skill_dispatch import render_via_website
    result = render_via_website(
        channel_yaml="mystoriesanimated/variants/aita_animated.yaml",
        script_path="mystoriesanimated/reddit_amitheasshole/scripts/<slug>.json",
        renderer="shorts",
    )
    print(result["mp4_path"])

CLI form (skills usually invoke this from Bash):

    python -m pipeline.cloud.skill_dispatch render \\
        --channel mystoriesanimated/variants/aita_animated.yaml \\
        --script mystoriesanimated/reddit_amitheasshole/scripts/<slug>.json
    # blocks, polls every 5s, prints the final mp4 path on success.

Auth:
    ytfactory-web is deployed --no-allow-unauthenticated. Every request
    needs an Authorization: Bearer <id-token> header. The token is
    fetched from gcloud Application Default Credentials at first use
    and cached for ~50 minutes. No bearer token is sent to localhost
    targets (safe-LAN dev mode).

Env knobs:
    YTFACTORY_WEBSITE_URL — defaults to the cloud ytfactory-web service.
        Override to http://localhost:8765 for local dev (currently
        documented, though local web/server.py is being retired in the
        2026-05-09 nuclear cleanup).
    YTFACTORY_DISPATCH_POLL_S — defaults to 5
    YTFACTORY_DISPATCH_TIMEOUT_S — defaults to 7200 (2 hr)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Cloud ytfactory-web is the canonical target. Phase 4 consolidation
# (2026-05-10) folded ytfactory-control's routers (agent lease, scheduler
# tick, state API, niche schema validation, modular channel/voice/niche
# routers) INTO web/server.py — ytfactory-web is now the single deploy.
# To opt back into a local dev instance:
#   YTFACTORY_WEBSITE_URL=http://localhost:8765
#   PYTHONPATH=. .venv/bin/uvicorn web.server:app --port 8765
DEFAULT_WEBSITE_URL = "https://ytfactory-web-7hwnzw7lya-as.a.run.app"
WEBSITE_URL = os.environ.get("YTFACTORY_WEBSITE_URL", DEFAULT_WEBSITE_URL)
DEFAULT_POLL_S = float(os.environ.get("YTFACTORY_DISPATCH_POLL_S", "5"))
DEFAULT_TIMEOUT_S = float(os.environ.get("YTFACTORY_DISPATCH_TIMEOUT_S", "7200"))


class WebsiteUnreachableError(RuntimeError):
    """Website at YTFACTORY_WEBSITE_URL did not answer."""


_ID_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_ID_TOKEN_TTL_S = 50 * 60  # refresh ~10 min before expiry


def _is_localhost(url: str) -> bool:
    return any(url.startswith(p) for p in ("http://localhost", "http://127.0.0.1"))


def _get_id_token(audience: str) -> str:
    """Fetch a Google ID token for the given Cloud Run service URL.

    Cached for ~50 min. Uses ``gcloud auth print-identity-token`` which
    works for both user accounts and service accounts under ADC.
    """
    cached = _ID_TOKEN_CACHE.get(audience)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        token = subprocess.check_output(
            [
                "gcloud", "auth", "print-identity-token",
                "--audiences=" + audience,
            ],
            text=True,
            stderr=subprocess.PIPE,
            timeout=15,
        ).strip()
    except FileNotFoundError as e:
        raise WebsiteUnreachableError(
            f"gcloud CLI not found; needed to authenticate against "
            f"cloud ytfactory-web ({audience}). Install: brew install "
            f"google-cloud-sdk, OR set YTFACTORY_WEBSITE_URL to a "
            f"localhost target."
        ) from e
    except subprocess.CalledProcessError as e:
        raise WebsiteUnreachableError(
            f"gcloud auth print-identity-token failed for "
            f"audience={audience}: {e.stderr}"
        ) from e
    if not token:
        raise WebsiteUnreachableError(
            f"empty ID token from gcloud for audience={audience}"
        )
    _ID_TOKEN_CACHE[audience] = (token, time.time() + _ID_TOKEN_TTL_S)
    return token


def _auth_headers(url: str) -> dict[str, str]:
    """Inject Bearer ID token unless the target is localhost (dev)."""
    if _is_localhost(url):
        return {}
    # Audience for Cloud Run is the service base URL (no path).
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    audience = f"{parts.scheme}://{parts.netloc}"
    return {"Authorization": f"Bearer {_get_id_token(audience)}"}


def _post_json(path: str, body: dict, *, timeout: float = 30) -> dict:
    url = f"{WEBSITE_URL.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_headers(url))
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        raise WebsiteUnreachableError(
            f"website not reachable at {WEBSITE_URL}. "
            f"Default target is the cloud ytfactory-web service. "
            f"For local dev: set YTFACTORY_WEBSITE_URL=http://localhost:8765 "
            f"and start `PYTHONPATH=. .venv/bin/uvicorn web.server:app "
            f"--host 127.0.0.1 --port 8765` from /Users/rohit/ytFactory. "
            f"underlying: {e}"
        ) from e
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"POST {url} → HTTP {e.code}: {body}") from e


def _get_json(path: str, *, timeout: float = 30) -> dict:
    url = f"{WEBSITE_URL.rstrip('/')}{path}"
    headers = _auth_headers(url)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        raise WebsiteUnreachableError(
            f"website not reachable at {WEBSITE_URL}: {e}"
        ) from e


def submit_render(
    channel_yaml: str | None = None,
    script_path: str | None = None,
    *,
    cmd: list[str] | None = None,
    extra_args: list[str] | None = None,
    label: str | None = None,
) -> str:
    """POST a render request to the website. Returns the job_id.

    Two modes:
      1. SHORTS convenience: pass `channel_yaml=` + `script_path=`. Internally
         rewritten to ``cmd=[scripts/make_shorts.py, --channel, ..., --script, ...]``.
      2. GENERIC: pass `cmd=[...]` directly. The first element is the
         renderer entry script (must be in the website's whitelist —
         see `_ALLOWED_RENDER_CMDS` in web/server.py). The skill knows
         exactly what flags it wants.

    `extra_args` is forwarded to the renderer in both modes.
    """
    payload: dict[str, Any] = {}
    if cmd:
        payload["cmd"] = list(cmd) + list(extra_args or [])
    elif channel_yaml and script_path:
        payload["channel_yaml"] = str(channel_yaml)
        payload["script_path"] = str(script_path)
        payload["extra_args"] = list(extra_args or [])
    else:
        raise ValueError(
            "submit_render requires either cmd=[...] or "
            "(channel_yaml=, script_path=)"
        )
    if label:
        payload["label"] = label
    out = _post_json("/api/jobs/from_script", payload)
    job_id = out.get("job_id")
    if not job_id:
        raise RuntimeError(f"unexpected response from /api/jobs/from_script: {out}")
    return job_id


def get_job(job_id: str) -> dict:
    return _get_json(f"/api/jobs/from_script/{job_id}")


def wait_for_job(
    job_id: str,
    *,
    poll_s: float = DEFAULT_POLL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    print_progress: bool = False,
) -> dict:
    """Poll until the job state is 'done' / 'done_no_mp4_found' / 'failed'.
    Returns the final job dict. Raises TimeoutError on deadline."""
    deadline = time.time() + timeout_s
    last_log_len = 0
    while time.time() < deadline:
        rec = get_job(job_id)
        if print_progress:
            tail = rec.get("log_tail", "")
            new = tail[last_log_len:]
            if new:
                sys.stdout.write(new)
                sys.stdout.flush()
                last_log_len = len(tail)
        if rec.get("state") in ("done", "done_no_mp4_found", "failed"):
            return rec
        time.sleep(poll_s)
    raise TimeoutError(
        f"render job {job_id} did not finish within {timeout_s:.0f}s"
    )


def render_via_website(
    channel_yaml: str | None = None,
    script_path: str | None = None,
    *,
    cmd: list[str] | None = None,
    extra_args: list[str] | None = None,
    label: str | None = None,
    poll_s: float = DEFAULT_POLL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    print_progress: bool = False,
) -> dict:
    """Submit a render and block until it finishes. Returns the job dict."""
    job_id = submit_render(
        channel_yaml=channel_yaml,
        script_path=script_path,
        cmd=cmd,
        extra_args=extra_args,
        label=label,
    )
    if print_progress:
        print(f"[dispatch] submitted job {job_id} → polling every {poll_s:.0f}s")
    return wait_for_job(
        job_id, poll_s=poll_s, timeout_s=timeout_s, print_progress=print_progress
    )


# ---------------------------------------------------------------- CLI ---
# Skills invoke this from Bash:
#   .venv/bin/python -m pipeline.cloud.skill_dispatch render \
#       --channel <channel.yaml> --script <script.json>


def _cli_render(args: argparse.Namespace) -> int:
    cmd: list[str] | None = None
    if args.cmd:
        cmd = [args.cmd]
    elif args.channel and args.script:
        cmd = None  # use shorts convenience mode
    else:
        print(
            "ERROR: pass either (--channel + --script) or --cmd <renderer.py>",
            file=sys.stderr,
        )
        return 2
    try:
        rec = render_via_website(
            channel_yaml=args.channel if not cmd else None,
            script_path=args.script if not cmd else None,
            cmd=cmd,
            extra_args=args.extra_args or [],
            label=args.label,
            poll_s=args.poll_s,
            timeout_s=args.timeout_s,
            print_progress=not args.quiet,
        )
    except WebsiteUnreachableError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except TimeoutError as e:
        print(f"TIMEOUT: {e}", file=sys.stderr)
        return 3

    state = rec.get("state")
    mp4 = rec.get("mp4_path")
    if state == "done" and mp4:
        print(f"\n✓ render complete: {mp4}")
        return 0
    if state == "done_no_mp4_found":
        print(
            f"\n⚠ renderer exit_code=0 but no mp4 located on disk. "
            f"Job log: {rec.get('log_path')}",
            file=sys.stderr,
        )
        return 1
    print(
        f"\n✗ render failed: state={state} error={rec.get('error')!r}\n"
        f"  log: {rec.get('log_path')}",
        file=sys.stderr,
    )
    return 1


def _cli_status(args: argparse.Namespace) -> int:
    rec = get_job(args.job_id)
    print(json.dumps(rec, indent=2, default=str))
    return 0


def _cli_list(args: argparse.Namespace) -> int:
    out = _get_json(f"/api/jobs/from_script?limit={args.limit}")
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pipeline.cloud.skill_dispatch",
        description="Website-native render dispatcher for ytFactory skills.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser(
        "render",
        help="Submit a render and wait for it.",
        description=(
            "Two forms:\n"
            "  Shorts convenience:  --channel <yaml> --script <json>\n"
            "  Generic:             --cmd <script.py> -- --flag1 v1 --flag2 v2 ...\n"
            "Use generic when calling a non-shorts renderer "
            "(render_long_form / render_footage_only / render_long_form_doc / "
            "render_split_screen / render_tweet_reaction)."
        ),
    )
    pr.add_argument("--channel", help="Path to channel YAML (shorts mode).")
    pr.add_argument("--script", help="Path to script JSON (shorts mode).")
    pr.add_argument(
        "--cmd",
        help=(
            "Renderer entry script (e.g. historyrecapped/scripts/render_long_form.py). "
            "Pass renderer flags AFTER -- (everything after -- is forwarded)."
        ),
    )
    pr.add_argument(
        "--label",
        help="Optional human label for the dashboard list.",
    )
    pr.add_argument(
        "--extra-args",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra args forwarded to the renderer CLI (place after --).",
    )
    pr.add_argument("--poll-s", type=float, default=DEFAULT_POLL_S)
    pr.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    pr.add_argument("--quiet", action="store_true", help="Suppress live log tail.")
    pr.set_defaults(fn=_cli_render)

    ps = sub.add_parser("status", help="Print one job's snapshot as JSON.")
    ps.add_argument("job_id")
    ps.set_defaults(fn=_cli_status)

    pl = sub.add_parser("list", help="List recent script jobs.")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(fn=_cli_list)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
