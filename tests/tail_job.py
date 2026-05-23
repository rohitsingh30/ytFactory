"""Tail a queued render job via the signed-in Chrome-Debug on :9222.

Emits one stdout line per status/stage change; exits when status becomes
terminal (`done` or `failed`). Designed for `Monitor` so each line is a
chat notification.

Usage:
    .venv/bin/python tests/tail_job.py \\
        --base-url https://ytfactory-web-next-e67vyhiy6a-as.a.run.app \\
        --job-id 900e7ce1c76e4322b640b925fc0a81f1
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--job-id", required=True)
    p.add_argument("--cdp", default="http://127.0.0.1:9222")
    p.add_argument("--max-s", type=float, default=3600.0)
    args = p.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0]
        # Reuse an existing page if one is open; otherwise open a fresh tab.
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # Navigate to the render page once so the operator sees it too.
        target_url = f"{args.base_url}/app/render/{args.job_id}"
        if page.url != target_url:
            page.goto(target_url, wait_until="domcontentloaded")

        last_signature: str | None = None
        start = time.time()
        while time.time() - start < args.max_s:
            try:
                r = page.request.get(
                    f"{args.base_url}/api/jobs/{args.job_id}", timeout=10_000,
                )
                if r.status != 200:
                    print(f"  poll HTTP {r.status}", flush=True)
                    time.sleep(5)
                    continue
                body = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"  poll error: {type(e).__name__}: {e}", flush=True)
                time.sleep(5)
                continue

            status = body.get("status") or "?"
            stage = body.get("stage") or "?"
            sig = f"{status}/{stage}"
            if sig != last_signature:
                last_signature = sig
                print(f"  status={status} stage={stage}", flush=True)

            if status in ("done", "failed"):
                print(json.dumps({
                    "job_id": args.job_id,
                    "status": status,
                    "stage": stage,
                    "short_uri": body.get("short_uri"),
                    "youtube_url": body.get("youtube_url"),
                    "error": body.get("error"),
                }), flush=True)
                return 0 if status == "done" else 2

            time.sleep(8)

        print(f"timed out after {args.max_s}s", flush=True)
        return 3


if __name__ == "__main__":
    sys.exit(main())
