#!/usr/bin/env python
"""Laptop-side cloud-critic daemon — CLI entry.

The cloud render worker (Azure-OpenAI) cannot run our vision-bearing
critic (claude-CLI-only). This daemon polls Firestore for finished
cloud renders whose ``critique.verdict`` is ``UNGATED``/missing,
downloads the mp4 + beats.json, runs the laptop critic, and writes
the verdict back to the same Firestore doc. The upload gate downstream
respects the new SHIP/FIX/BLOCK verdict.

Usage
-----

    # Run forever (default 60 s between polls):
    python scripts/cloud_critic_loop.py

    # One pass + exit (smoke-test):
    python scripts/cloud_critic_loop.py --max-iters=1

    # Verbose:
    python scripts/cloud_critic_loop.py --log-level=DEBUG

Env knobs:

    GOOGLE_CLOUD_PROJECT
        Firestore project; defaults to ``ytfactory-prod-v3``.
    YTFACTORY_CLOUD_CRITIC_INTERVAL_S
        Poll interval. Floors at 60 s; default 60.
    YTFACTORY_CLOUD_CRITIC_CACHE
        Override the local cache dir (default ``~/.cache/ytfactory/cloud_critic``).

Install (auto-start at login via launchd):

    cp control/com.ytfactory.cloud-critic.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.ytfactory.cloud-critic.plist
    # Verify:
    launchctl list | grep ytfactory.cloud-critic
    tail -f ~/Library/Logs/ytfactory/cloud_critic.stdout.log

Uninstall:

    launchctl unload ~/Library/LaunchAgents/com.ytfactory.cloud-critic.plist
    rm ~/Library/LaunchAgents/com.ytfactory.cloud-critic.plist
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

# Allow running directly without `pip install -e .`
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from pipeline.critique import cloud_poller  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n", 1)[0],
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3"),
        help="GCP project hosting Firestore (default: ytfactory-prod-v3).",
    )
    parser.add_argument(
        "--interval-s", type=int,
        default=int(os.environ.get("YTFACTORY_CLOUD_CRITIC_INTERVAL_S", 60)),
        help="Seconds between polls (floored at 60; default 60).",
    )
    parser.add_argument(
        "--max-iters", type=int, default=None,
        help="If set, exit after N iterations. Used by --once + unit tests.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Alias for --max-iters=1.",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
        help="DEBUG | INFO | WARNING | ERROR (default INFO).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("cloud-critic-loop")

    max_iters = 1 if args.once else args.max_iters
    stop_event = threading.Event()
    cloud_poller.install_sigint_handler(stop_event)

    logger.info(
        "starting cloud-critic loop project=%s interval_s=%s max_iters=%s",
        args.project_id, args.interval_s, max_iters,
    )
    iters = cloud_poller.poll_and_critique_loop(
        project_id=args.project_id,
        interval_s=args.interval_s,
        max_iters=max_iters,
        stop_event=stop_event,
    )
    logger.info("loop exited after %d iteration(s)", iters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
