#!/usr/bin/env python3
"""Daily Cloud Run admin-panel snapshot.

Run from launchd (``com.ytfactory.cloud-snapshot.plist``) at 02:00
local each day, and from the panel's "Refresh" button on demand. Pulls
health + cost + deploys, persists JSON under ``data/_bench/cloud_*/``.

CLI:
    python scripts/cloud_daily_snapshot.py             # full snapshot
    python scripts/cloud_daily_snapshot.py --dry-run   # query, don't write
    python scripts/cloud_daily_snapshot.py --quiet     # no stdout
    python scripts/cloud_daily_snapshot.py --cost-days 60
    python scripts/cloud_daily_snapshot.py --skip cost --skip deploys
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Repo root on sys.path so this can run via `python scripts/...` from
# any cwd (launchd runs under /).
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pipeline.cloud import cost, deploys, health, snapshot  # noqa: E402


def _setup_logging(quiet: bool) -> None:
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Run the queries but don't write snapshot files.")
    p.add_argument("--quiet", action="store_true",
                   help="Only log warnings and above.")
    p.add_argument("--cost-days", type=int, default=30)
    p.add_argument("--project", default="ytfactory-prod-v3")
    p.add_argument("--billing-account-id",
                   help="Override BILLING_ACCOUNT_ID env (BigQuery export table suffix).")
    p.add_argument(
        "--skip", action="append", default=[],
        choices=["health", "cost", "deploys"],
        help="Skip a section (repeatable).",
    )
    args = p.parse_args(argv)

    _setup_logging(args.quiet)
    log = logging.getLogger("cloud_daily_snapshot")

    sections: dict[str, str | dict] = {}

    if "health" not in args.skip:
        if args.dry_run:
            rows = health.sweep()
            sections["health"] = {"summary": health.summary(rows), "rows": len(rows)}
        else:
            sections["health"] = str(snapshot.snapshot_health())

    if "cost" not in args.skip:
        if args.dry_run:
            window = cost.fetch_cost_window(days=args.cost_days,
                                            billing_account_id=args.billing_account_id)
            sections["cost"] = {
                "available": window.available,
                "reason": window.reason,
                "total_today": window.total_today,
                "total_mtd": window.total_mtd,
            }
        else:
            sections["cost"] = str(snapshot.snapshot_cost(
                days=args.cost_days,
                billing_account_id=args.billing_account_id,
            ))

    if "deploys" not in args.skip:
        if args.dry_run:
            rows = deploys.collect(project=args.project)
            sections["deploys"] = {"rows": len(rows)}
        else:
            sections["deploys"] = str(snapshot.snapshot_deploys(project=args.project))

    log.info("done: %s", sections)
    if not args.quiet:
        json.dump({"dry_run": args.dry_run, "sections": sections}, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
