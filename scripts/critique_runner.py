#!/usr/bin/env python
"""Critique-runner CLI entry — start the laptop daemon.

Usage:

    # Start the runner (blocks). Defaults to:
    #   - repo_root = current dir (must be a git repo, must be clean)
    #   - branch    = current HEAD branch (typically `main`)
    #   - remote    = `origin`
    python scripts/critique_runner.py

    # Or via the Makefile:
    make critique-runner

Env knobs (all optional):

    YTFACTORY_CRITIQUE_AGENT_TIMEOUT_S
        Per-agent-turn cap. Default 1800 (30 min).
    YTFACTORY_CRITIQUE_GATE_TIMEOUT_S
        Per-gate-suite cap. Default 600 (10 min).
    YTFACTORY_CRITIQUE_POLL_INTERVAL_S
        Firestore polling cadence between critiques. Default 2.0.
    YTFACTORY_CRITIQUE_MAX_FAILED_TURNS
        How many gate failures before the runner gives up. Default 3.
    YTFACTORY_CRITIQUE_REMOTE / _BRANCH
        Override the push remote / branch.
    GOOGLE_CLOUD_PROJECT
        Firestore project — defaults to ``ytfactory-prod-v2``.

The runner refuses to start if the working tree has uncommitted
changes (the agent's diff has to be isolated). Stash or commit your
local work first.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Allow running directly without `pip install -e .`
REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT_FALLBACK))

from pipeline.critique import runner as runner_mod  # noqa: E402


def _firestore_client():
    from google.cloud import firestore  # noqa: PLC0415
    return firestore.Client(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"),
    )


def _build_config(args: argparse.Namespace) -> runner_mod.RunnerConfig:
    return runner_mod.RunnerConfig(
        repo_root=Path(args.repo_root).resolve(),
        push_remote=args.remote,
        push_branch=args.branch,
        max_failed_turns=int(os.environ.get("YTFACTORY_CRITIQUE_MAX_FAILED_TURNS", 3)),
        agent_timeout_s=int(os.environ.get("YTFACTORY_CRITIQUE_AGENT_TIMEOUT_S", 1800)),
        gate_timeout_s=int(os.environ.get("YTFACTORY_CRITIQUE_GATE_TIMEOUT_S", 600)),
        poll_interval_s=float(os.environ.get("YTFACTORY_CRITIQUE_POLL_INTERVAL_S", 2.0)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT_FALLBACK),
        help="Path to the ytFactory git repo (default: parent of this script).",
    )
    parser.add_argument(
        "--remote",
        default=os.environ.get("YTFACTORY_CRITIQUE_REMOTE", "origin"),
        help="Push remote (default: origin).",
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("YTFACTORY_CRITIQUE_BRANCH", "main"),
        help="Push branch (default: main).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Process at most one critique then exit (smoke-test mode).",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
        help="DEBUG | INFO | WARNING (default: INFO).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("critique-runner")

    config = _build_config(args)
    if not (config.repo_root / ".git").exists():
        logger.error("repo_root %s is not a git repo (no .git dir)", config.repo_root)
        return 2

    client = _firestore_client()
    stop_event = threading.Event()

    def _handle_sigint(signum, frame):  # noqa: ARG001
        logger.info("received signal %s, shutting down after current critique", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    if args.once:
        # Smoke-test: poll once, claim the first queued critique if
        # any, process it, then exit. Useful for `make critique-once`.
        queued = (
            client.collection("critiques")
            .where("status", "==", runner_mod.STATUS_QUEUED)
            .order_by("created_at")
            .limit(1)
            .get()
        )
        if not queued:
            logger.info("no queued critique to process; exiting")
            return 0
        runner_mod.process_one_critique(client, queued[0].id, config)
        return 0

    runner_mod.run_forever(client, config, stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
