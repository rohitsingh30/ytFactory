"""End-to-end smoke for a single channel via the /app/create wizard.

Connects to a *signed-in* Chrome over CDP (port 9222 by default — boot via
`scripts/playwright_mcp_signed_in.sh`), drives the 3-step wizard, then
polls `/api/jobs/<id>` until terminal status. One channel per invocation;
loop the script to cover multiple channels.

Example:
    .venv/bin/python tests/e2e_smoke.py \\
        --channel mystoriesanimated \\
        --variant aita \\
        --base-url https://ytfactory-web-next-e67vyhiy6a-as.a.run.app
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PWTimeoutError, sync_playwright


@dataclass
class SmokeResult:
    job_id: str | None
    status: str
    short_uri: str | None
    youtube_url: str | None
    elapsed_s: float
    error: str | None = None


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[smoke {ts}] {msg}", flush=True)


def drive_wizard(page: Page, base_url: str, channel: str, variant: str) -> str:
    """Walk the 3 wizard steps; return the queued job_id."""
    log(f"navigating to {base_url}/app/create")
    page.goto(f"{base_url}/app/create", wait_until="domcontentloaded")

    if "/login" in page.url:
        raise RuntimeError(
            f"redirected to {page.url} — Chrome-Debug profile is not signed in "
            f"to an allowlisted account"
        )

    # --- Step 1: Mode → click "Channel-focused" card -----------------
    log("step 1/3: clicking Channel-focused mode card")
    page.get_by_role("button", name="Choose a channel").click(timeout=10_000)

    # --- Step 2: Channel → click the target channel card -------------
    log(f"step 2/3: picking channel '{channel}'")
    # ChannelHeroCard renders the channel label (e.g. "MyStoriesAnimated").
    # Match case-insensitively on the key with hyphens/underscores stripped
    # since the visible label may differ (e.g. "My Stories Animated").
    page.locator(f'[data-channel-key="{channel}"], [data-key="{channel}"]').first.click(
        timeout=5_000
    ) if page.locator(f'[data-channel-key="{channel}"]').count() else None
    # Fallback: text-based pick. Match the channelLabel form.
    if not page.locator(":scope >> visible=true").count() or "/app/create" not in page.url:
        pass  # noop — we're still on the channel step; the click below handles it
    # Robust path: click the card whose accessible name contains the channel slug.
    candidates = [
        channel,
        channel.replace("animated", ""),
        channel.replace("recapped", ""),
        channel.replace("decoded", ""),
        channel.replace("junction", ""),
        channel.replace("pulse", ""),
    ]
    clicked = False
    for cand in candidates:
        try:
            page.get_by_role("button").filter(has_text=cand).first.click(timeout=2_500)
            clicked = True
            break
        except PWTimeoutError:
            continue
    if not clicked:
        # Final fallback: try the hero card by data attribute set by ChannelHeroCard
        page.locator("button, a").filter(has_text=channel).first.click(timeout=5_000)

    # Footer Continue → step 3
    log("step 2/3: clicking Continue")
    page.get_by_role("button", name="Continue").click(timeout=5_000)

    # --- Step 3: Customize+Review -----------------------------------
    # 3a. Pick the niche/variant (NicheBubbleRow pills)
    log(f"step 3/3: picking niche '{variant}'")
    # Variant cards are <button aria-pressed=...> with text label OR title attr.
    # Try by text first (label often equals the variant key for many channels).
    variant_clicked = False
    for sel in (variant, variant.replace("_", " "), variant.upper()):
        try:
            page.get_by_role("button", name=sel, exact=False).first.click(timeout=2_000)
            variant_clicked = True
            break
        except PWTimeoutError:
            continue
    if not variant_clicked:
        log(f"  warn: couldn't find variant pill for '{variant}' — using default")

    # 3b. Auto-generate topic
    log("step 3/3: clicking Auto-generate topic")
    page.get_by_role("button", name="Auto-generate topic").click(timeout=10_000)
    log("step 3/3: waiting for topic to populate...")
    # The textarea is the only <textarea> in the topic card.
    textarea = page.locator("textarea").first
    for _ in range(120):  # 60s — LLM brainstorm can be slow
        val = textarea.input_value()
        if val and val.strip():
            log(f"  topic: {val[:90]}")
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("auto-generate didn't populate the topic textarea after 60s")

    # 3c. Submit → "Render Short" button in the sticky footer
    submit_btn = page.get_by_role("button", name="Render Short")
    # Wait for the button to be enabled (canAdvance flips true once
    # required fields are set; pull-source seeds source_kind/source_ref).
    for _ in range(20):
        try:
            if submit_btn.is_enabled():
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    else:
        page.screenshot(path="/tmp/e2e_smoke_submit_disabled.png", full_page=True)
        raise RuntimeError(
            "Render Short button stayed disabled — required fields missing. "
            "Screenshot at /tmp/e2e_smoke_submit_disabled.png"
        )
    log("step 3/3: clicking Render Short")
    submit_btn.click(timeout=10_000)

    # Wait for router push to /app/render/<id>. If the client-side
    # redirect takes too long (>120s), fall back to polling
    # /api/jobs?channel=... for the most recently queued job — the
    # server has the truth even if the SPA hasn't navigated yet.
    log("waiting for redirect to /app/render/...")
    redirected = False
    for _ in range(240):  # 120s
        if "/app/render/" in page.url:
            redirected = True
            break
        time.sleep(0.5)
    if not redirected:
        log("no redirect after 120s — polling /api/jobs for the queued job")
        try:
            r = page.request.get(
                f"{base_url}/api/jobs?channel={channel}&limit=1", timeout=10_000,
            )
            if r.status == 200:
                body = r.json()
                jobs = body.get("jobs") or body.get("items") or []
                if jobs:
                    job_id = jobs[0].get("job_id") or jobs[0].get("id")
                    if job_id:
                        log(f"  recovered job_id from /api/jobs: {job_id}")
                        return job_id
        except Exception as exc:  # noqa: BLE001
            log(f"  /api/jobs fallback failed: {type(exc).__name__}: {exc}")
        # Capture diagnostic context.
        page.screenshot(path="/tmp/e2e_smoke_no_redirect.png", full_page=True)
        toasts: list[str] = []
        try:
            for el in page.locator("[data-sonner-toast], li[role='status']").all():
                t = el.inner_text().strip()
                if t:
                    toasts.append(t)
        except Exception:  # noqa: BLE001
            pass
        toast_blob = " | ".join(toasts) or "(no toast captured)"
        raise RuntimeError(
            f"never redirected to /app/render/... after 120s; still at "
            f"{page.url}. Toasts: {toast_blob}. "
            f"Screenshot at /tmp/e2e_smoke_no_redirect.png"
        )

    job_id = page.url.rstrip("/").rsplit("/", 1)[-1]
    log(f"job_id: {job_id}")
    return job_id


def tail_job(page: Page, base_url: str, job_id: str, timeout_s: float) -> SmokeResult:
    """Poll /api/jobs/<id> via the page's auth context until terminal."""
    start = time.time()
    last_stage: str | None = None
    while time.time() - start < timeout_s:
        try:
            r = page.request.get(f"{base_url}/api/jobs/{job_id}", timeout=10_000)
            if r.status != 200:
                log(f"  jobs poll: HTTP {r.status}")
                time.sleep(5)
                continue
            body = r.json()
        except Exception as e:  # noqa: BLE001
            log(f"  jobs poll: {type(e).__name__}: {e}")
            time.sleep(5)
            continue
        status = body.get("status") or "?"
        stage = body.get("stage") or "?"
        if stage != last_stage:
            log(f"  stage={stage} status={status}")
            last_stage = stage
        if status in ("done", "failed"):
            elapsed = time.time() - start
            return SmokeResult(
                job_id=job_id,
                status=status,
                short_uri=body.get("short_uri"),
                youtube_url=body.get("youtube_url"),
                elapsed_s=elapsed,
                error=body.get("error"),
            )
        time.sleep(8)
    return SmokeResult(
        job_id=job_id, status="timeout", short_uri=None, youtube_url=None,
        elapsed_s=time.time() - start,
        error=f"did not reach terminal status within {timeout_s}s",
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True, help="web-next URL (Cloud Run)")
    p.add_argument("--channel", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--cdp", default="http://127.0.0.1:9222")
    p.add_argument("--timeout-s", type=float, default=1800.0,
                   help="job-tail timeout in seconds (default 30min)")
    args = p.parse_args()

    log(f"connecting to {args.cdp}")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0]
        page = ctx.new_page()
        t0 = time.time()
        keep_open = False
        try:
            job_id = drive_wizard(page, args.base_url, args.channel, args.variant)
            result = tail_job(page, args.base_url, job_id, args.timeout_s)
        except Exception as e:  # noqa: BLE001
            log(f"FATAL: {type(e).__name__}: {e}")
            result = SmokeResult(
                job_id=None, status="error", short_uri=None, youtube_url=None,
                elapsed_s=time.time() - t0, error=f"{type(e).__name__}: {e}",
            )
            # Keep the failed tab + browser open so the operator can inspect.
            keep_open = True
        if not keep_open:
            # Only close on clean runs; never close on error.
            page.close()
        # Always disconnect from CDP cleanly — does NOT close the Chrome instance.
        # (Connecting via connect_over_cdp makes .close() a disconnect, not a kill.)

    print()
    print(json.dumps({
        "channel": args.channel, "variant": args.variant,
        "job_id": result.job_id, "status": result.status,
        "short_uri": result.short_uri, "youtube_url": result.youtube_url,
        "elapsed_s": round(result.elapsed_s, 1), "error": result.error,
    }, indent=2))

    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
