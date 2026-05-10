"""Playwright-based X (Twitter) scraper for ytFactory.

Free-tier X API does not include /2/tweets/search/recent (Basic+ only),
so this scrapes x.com via a real browser session. X walls all guest reads
behind a sign-in modal, so the first run requires interactive login;
subsequent runs reuse the saved auth state at
``~/.config/ytfactory/x_scrape/auth_state.json``.

Output schema (one JSON file):
    {
      "query": "<search string>",
      "scraped_at": "<ISO8601 UTC>",
      "url": "<x.com search URL>",
      "logged_in": <bool>,
      "tweets": [
        {"id", "url", "author", "handle", "text", "posted_at",
         "likes", "retweets", "replies", "views", "is_verified"},
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from playwright.sync_api import Page, TimeoutError as PWTimeoutError, sync_playwright

SESSION_DIR = pathlib.Path.home() / ".config/ytfactory/x_scrape"
SESSION_STATE = SESSION_DIR / "auth_state.json"
CHROME_ROOT = pathlib.Path.home() / "Library/Application Support/Google/Chrome"


def _pull_chrome_cookies(profile_name: str) -> list[dict]:
    """Pull x.com / twitter.com cookies from a real Chrome profile via browser_cookie3
    (handles macOS keychain decryption). Returns Playwright cookie dicts."""
    import browser_cookie3  # type: ignore

    cookie_db = CHROME_ROOT / profile_name / "Cookies"
    if not cookie_db.exists():
        raise SystemExit(f"Chrome cookies DB not found at {cookie_db}")
    out: list[dict] = []
    for domain in ("x.com", "twitter.com"):
        jar = browser_cookie3.chrome(cookie_file=str(cookie_db), domain_name=domain)
        for c in jar:
            same_site = "Lax"
            rest = getattr(c, "_rest", {}) or {}
            ss = rest.get("SameSite") or rest.get("samesite")
            if isinstance(ss, str) and ss.capitalize() in ("Strict", "Lax", "None"):
                same_site = ss.capitalize()
            out.append(
                {
                    "name": c.name,
                    "value": c.value or "",
                    "domain": c.domain,
                    "path": c.path or "/",
                    "secure": bool(c.secure),
                    "httpOnly": bool(rest.get("HttpOnly") or rest.get("httponly")),
                    "sameSite": same_site,
                    "expires": float(c.expires) if c.expires else -1,
                }
            )
    return out

SCRAPE_JS = r"""
() => {
  const parseCount = (s) => {
    if (!s) return 0;
    s = s.trim().replace(/,/g, '');
    const m = s.match(/^([\d.]+)\s*([KMB])?$/i);
    if (!m) return parseInt(s, 10) || 0;
    const n = parseFloat(m[1]);
    const mult = { K: 1e3, M: 1e6, B: 1e9 }[(m[2] || '').toUpperCase()] || 1;
    return Math.round(n * mult);
  };
  const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  return arts.map(a => {
    const userBlock = a.querySelector('[data-testid="User-Name"]');
    const userText = userBlock ? userBlock.innerText.split('\n').filter(Boolean) : [];
    const author = userText[0] || null;
    const handle = userText.find(s => s.startsWith('@')) || null;
    const verifiedSvg = userBlock ? userBlock.querySelector('svg[aria-label*="erified" i]') : null;
    const tt = a.querySelector('[data-testid="tweetText"]');
    const text = tt ? tt.innerText : '';
    const timeEl = a.querySelector('time');
    const posted_at = timeEl ? timeEl.getAttribute('datetime') : null;
    const linkEl = a.querySelector('a[href*="/status/"]');
    const url = linkEl ? new URL(linkEl.getAttribute('href'), location.origin).href : null;
    const idMatch = url ? url.match(/\/status\/(\d+)/) : null;
    const id = idMatch ? idMatch[1] : null;
    const tdc = (sel) => {
      const el = a.querySelector(`[data-testid="${sel}"]`);
      if (!el) return 0;
      const lab = el.getAttribute('aria-label') || el.innerText || '';
      const m = lab.match(/([\d.,]+\s*[KMB]?)/i);
      return parseCount(m ? m[1] : '0');
    };
    let views = 0;
    const viewsLink = a.querySelector('a[href*="/analytics"], a[aria-label*="view" i]');
    if (viewsLink) {
      const lab = viewsLink.getAttribute('aria-label') || viewsLink.innerText || '';
      const m = lab.match(/([\d.,]+\s*[KMB]?)\s*view/i);
      if (m) views = parseCount(m[1]);
    }
    return {
      id, url, author, handle, text, posted_at,
      likes: tdc('like'),
      retweets: tdc('retweet'),
      replies: tdc('reply'),
      views,
      is_verified: !!verifiedSvg,
    };
  }).filter(t => t.id && t.text);
}
"""


def _logged_in(page: Page) -> bool:
    return page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() > 0


def scrape(
    query: str | None,
    out_path: pathlib.Path,
    *,
    tweet_url: str | None = None,
    headless: bool = False,
    max_tweets: int = 25,
    scroll_passes: int = 4,
    interactive_login_timeout: int = 300,
    chrome_profile: str | None = None,
) -> dict:
    if tweet_url:
        url = tweet_url
        query = query or tweet_url
    else:
        if not query:
            raise SystemExit("scrape() requires either query or tweet_url")
        enc = urllib.parse.quote(query)
        url = f"https://x.com/search?q={enc}&src=typed_query&f=top"

    imported_cookies: list[dict] = []
    if chrome_profile:
        imported_cookies = _pull_chrome_cookies(chrome_profile)
        login_cookies = sum(c["name"] in {"auth_token", "ct0", "twid"} for c in imported_cookies)
        print(
            f"[x_scrape] imported {len(imported_cookies)} cookies from Chrome '{chrome_profile}' "
            f"(auth cookies: {login_cookies}/3)",
            file=sys.stderr,
        )
        if login_cookies < 3:
            print(
                "[x_scrape] WARNING: missing one or more of auth_token/ct0/twid — "
                "are you really signed in to x.com in that profile?",
                file=sys.stderr,
            )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx_kwargs = {"viewport": {"width": 1280, "height": 900}}
        if SESSION_STATE.exists() and not chrome_profile:
            ctx_kwargs["storage_state"] = str(SESSION_STATE)
            print(f"[x_scrape] reusing saved auth state from {SESSION_STATE}", file=sys.stderr)
        ctx = browser.new_context(**ctx_kwargs)
        if imported_cookies:
            ctx.add_cookies(imported_cookies)
        page = ctx.new_page()

        print(f"[x_scrape] navigating: {url}", file=sys.stderr)
        # Let chromium settle the initial about:blank before we navigate, otherwise
        # a startup redirect can interrupt our goto.
        page.wait_for_timeout(1500)
        for attempt in range(3):
            try:
                page.goto(url, wait_until="commit", timeout=45000)
                break
            except PWTimeoutError:
                raise
            except Exception as e:
                if "interrupted" in str(e) and attempt < 2:
                    print(f"[x_scrape] goto interrupted ({attempt + 1}/3), retrying", file=sys.stderr)
                    page.wait_for_timeout(1500)
                    continue
                raise
        page.wait_for_timeout(3000)

        if page.locator('article[data-testid="tweet"]').count() == 0:
            print(
                f"[x_scrape] sign-in modal up — sign in inside the browser window "
                f"(timeout {interactive_login_timeout}s); cookies will be saved on success.",
                file=sys.stderr,
            )
            try:
                page.wait_for_selector(
                    'article[data-testid="tweet"]',
                    timeout=interactive_login_timeout * 1000,
                )
            except PWTimeoutError:
                shot = out_path.with_suffix(".diag.png")
                try:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot), full_page=False)
                    print(f"[x_scrape] diag screenshot -> {shot}", file=sys.stderr)
                except Exception:
                    pass

        logged_in = _logged_in(page)
        if logged_in:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(SESSION_STATE))
            print(f"[x_scrape] saved auth state -> {SESSION_STATE}", file=sys.stderr)

        seen: dict[str, dict] = {}
        for i in range(scroll_passes):
            for t in page.evaluate(SCRAPE_JS):
                if t["id"] not in seen:
                    seen[t["id"]] = t
            print(
                f"[x_scrape] pass {i + 1}/{scroll_passes}  unique tweets: {len(seen)}",
                file=sys.stderr,
            )
            if len(seen) >= max_tweets:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1800)

        ctx.close()
        browser.close()

    tweets = sorted(
        seen.values(),
        key=lambda t: (t.get("likes", 0) or 0) + 2 * (t.get("retweets", 0) or 0),
        reverse=True,
    )[:max_tweets]

    payload = {
        "query": query,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "url": url,
        "logged_in": logged_in,
        "tweet_count": len(tweets),
        "tweets": tweets,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[x_scrape] wrote {len(tweets)} tweets -> {out_path}", file=sys.stderr)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scrape viral tweets from x.com — search results or replies under a tweet."
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--query", help='Search query, e.g. "Real Madrid"')
    src.add_argument(
        "--tweet-url",
        help="Tweet URL to fetch + scrape its reply thread (where edgy/funny takes live)",
    )
    ap.add_argument("--out", required=True, type=pathlib.Path, help="Output JSON path")
    ap.add_argument("--headless", action="store_true", help="Headless (only after first interactive login)")
    ap.add_argument("--max", type=int, default=25, help="Max tweets to capture")
    ap.add_argument("--scroll-passes", type=int, default=4, help="Scroll passes")
    ap.add_argument(
        "--login-timeout",
        type=int,
        default=300,
        help="Seconds to wait for interactive login on first run",
    )
    ap.add_argument(
        "--chrome-profile",
        default=None,
        help='Pull cookies from this real Chrome profile (e.g. "Profile 3"). '
        "Skips interactive login and reuses your already-signed-in session.",
    )
    args = ap.parse_args()
    t0 = time.monotonic()
    payload = scrape(
        args.query,
        args.out,
        tweet_url=args.tweet_url,
        headless=args.headless,
        max_tweets=args.max,
        scroll_passes=args.scroll_passes,
        interactive_login_timeout=args.login_timeout,
        chrome_profile=args.chrome_profile,
    )
    print(
        f"[x_scrape] done in {time.monotonic() - t0:.1f}s  tweets={payload['tweet_count']}  "
        f"logged_in={payload['logged_in']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
