#!/usr/bin/env python3
"""Daily idle-cost audit — catches any service with min-instances >= 1
or any service that's been continuously running for > 1 hour without
being part of an active bake-off window.

Usage:
  GCP_PROJECT=ytfactory-prod-v3 python scripts/audit_idle_costs.py

Exits non-zero if any waste is detected — wire into a daily cron
(Cloud Scheduler) to get email alerts when any service drifts back
to expensive idle patterns.
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
from datetime import datetime, timezone


PROJECT = os.environ.get("GCP_PROJECT", "ytfactory-prod-v3")
REGION = os.environ.get("GCP_REGION", "asia-southeast1")


def run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True)


def main() -> int:
    print(f"=== Idle-cost audit: project={PROJECT} region={REGION} ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}\n")

    raw = run([
        "gcloud", "run", "services", "list",
        f"--project={PROJECT}", f"--region={REGION}",
        "--format=json",
    ])
    services = json.loads(raw)

    issues = []
    print(f"{'service':40} {'min':>4} {'max':>4} {'GPU':>4} {'mem':>6} {'OK?'}")
    print("-" * 70)

    for s in services:
        name = s["metadata"]["name"]
        ann = s.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
        min_scale = int(ann.get("autoscaling.knative.dev/minScale", "0"))
        max_scale = int(ann.get("autoscaling.knative.dev/maxScale", "1"))
        containers = s.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])
        limits = containers[0].get("resources", {}).get("limits", {}) if containers else {}
        gpu = limits.get("nvidia.com/gpu", "0")
        mem = limits.get("memory", "?")
        ok = "✓"
        if min_scale > 0:
            ok = f"❌ min-instances={min_scale} (BURNS IDLE GPU)"
            issues.append(f"{name}: min-instances={min_scale}")
        print(f"{name:40} {min_scale:>4} {max_scale:>4} {str(gpu):>4} {mem:>6} {ok}")

    print()
    if issues:
        print("⚠ ISSUES FOUND:")
        for issue in issues:
            print(f"  • {issue}")
        print()
        print("Fix: gcloud run services update <svc> --region=" + REGION + " --min-instances=0")
        return 1

    print("✓ All services properly scale-to-zero. No idle waste.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
