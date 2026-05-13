"""Direct Playwright CDP-attach driver for the signed-in Chrome on :9222.

No MCP intermediation — just attach, drive, screenshot. Lets the agent
operate the dashboard end-to-end without depending on MCP's flaky
config-reload behavior.

Usage (interactive shell):
    .venv/bin/python scripts/_drive_signed_in.py <command> [args...]

Commands:
    nav <url>                  navigate active page to URL
    snap [filename]            screenshot to /tmp/snap.png (or filename)
    click <text>               click element matching visible text
    text <ref> "the value"     type text into element matching ref
    eval "<js>"                run JS in page, print result
    title                      print page title + url
    wait <secs>                wait N seconds
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


CDP = "http://127.0.0.1:9222"


def _active_page(context):
    """Pick the most-recently-active page that's a real http(s) URL."""
    pages = context.pages
    real = [p for p in pages if (p.url or "").startswith(("http://", "https://"))]
    return real[-1] if real else (pages[-1] if pages else context.new_page())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[0]
    args = argv[1:]
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        # Connecting attaches to the existing instance; default context is the
        # signed-in profile. Use first context.
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = _active_page(ctx)

        if cmd == "nav":
            url = args[0]
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.5)  # let post-DCL JS hydrate
            print(f"OK navigated → {page.url}")
        elif cmd == "snap":
            out = Path(args[0]) if args else Path("/tmp/snap.png")
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=False)
            print(f"OK saved {out} ({out.stat().st_size} bytes)")
        elif cmd == "snap_full":
            out = Path(args[0]) if args else Path("/tmp/snap_full.png")
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=True)
            print(f"OK saved {out} ({out.stat().st_size} bytes)")
        elif cmd == "title":
            print(f"URL:   {page.url}")
            print(f"TITLE: {page.title()}")
        elif cmd == "wait":
            time.sleep(float(args[0]))
            print("OK")
        elif cmd == "click":
            sel = args[0]
            # Try Playwright's text= selector first, then role-based
            try:
                page.locator(f"text={sel}").first.click(timeout=10000)
            except Exception:
                # Fall back to a CSS-ish selector if user passed one
                page.locator(sel).first.click(timeout=10000)
            time.sleep(1)
            print(f"OK clicked {sel!r} → now at {page.url}")
        elif cmd == "click_role":
            role, name = args[0], args[1]
            page.get_by_role(role, name=name).click(timeout=10000)
            time.sleep(1)
            print(f"OK clicked role={role!r} name={name!r} → now at {page.url}")
        elif cmd == "fill":
            sel, value = args[0], args[1]
            page.locator(sel).first.fill(value, timeout=10000)
            print(f"OK filled {sel!r}")
        elif cmd == "fill_label":
            label, value = args[0], args[1]
            page.get_by_label(label).fill(value, timeout=10000)
            print(f"OK filled label={label!r}")
        elif cmd == "type":
            sel, value = args[0], args[1]
            page.locator(sel).first.click()
            page.keyboard.type(value, delay=20)
            print(f"OK typed into {sel!r}")
        elif cmd == "press":
            page.keyboard.press(args[0])
            print(f"OK pressed {args[0]}")
        elif cmd == "eval":
            js = args[0]
            result = page.evaluate(js)
            print(json.dumps(result, indent=2, default=str))
        elif cmd == "tabs":
            for i, pg in enumerate(ctx.pages):
                print(f"[{i}] {pg.url[:90]}")
        elif cmd == "select_tab":
            ctx.pages[int(args[0])].bring_to_front()
            print("OK")
        elif cmd == "html":
            out = Path(args[0]) if args else Path("/tmp/page.html")
            out.write_text(page.content())
            print(f"OK wrote {out} ({out.stat().st_size} bytes)")
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            print(__doc__, file=sys.stderr)
            return 2

        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
