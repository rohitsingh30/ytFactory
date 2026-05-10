"""Screenshot individual tweets from x.com — used by tweet-reaction Shorts.

Reads a list of tweet URLs (CLI args or stdin), navigates to each, and saves
a clean PNG of just the tweet article element (text + author + media if any).

Auth: reuses ~/.config/ytfactory/x_scrape/auth_state.json (saved by x_scrape.py)
or pulls cookies from a Chrome profile via --chrome-profile.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

import subprocess

from playwright.sync_api import TimeoutError as PWTimeoutError, sync_playwright

from pipeline.social.x_scrape import SESSION_STATE, _pull_chrome_cookies

# JS that locates the media element inside the matched article and returns its
# bbox as fractions of the article — device-scale-factor agnostic.
MEDIA_PROBE_JS = """
(tweetId) => {
  const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const article = arts.find(a => a.querySelector(`a[href*="/status/${tweetId}"]`));
  if (!article) return null;
  const ar = article.getBoundingClientRect();
  const videoEl = article.querySelector('[data-testid="videoPlayer"]') || article.querySelector('video');
  const photoEl = article.querySelector('[data-testid="tweetPhoto"]:not(:has([data-testid="videoPlayer"]))');
  const target = videoEl || photoEl;
  if (!target) return { kind: "none" };
  const r = target.getBoundingClientRect();
  return {
    kind: videoEl ? "video" : "image",
    region_frac: [
      (r.x - ar.x) / ar.width,
      (r.y - ar.y) / ar.height,
      r.width / ar.width,
      r.height / ar.height,
    ],
  };
}
"""


def _download_tweet_video(url: str, out_path: pathlib.Path) -> bool:
    """Run yt-dlp to fetch the tweet's video. Returns True on success.

    Cloud-first: uses the Cloud Run yt-dlp worker so the laptop never
    has to handle bot challenges. Falls back to local yt-dlp on cloud
    unavailability.
    """
    from pipeline.footage import yt_dlp_cloudrun  # noqa: PLC0415 — lazy

    try:
        yt_dlp_cloudrun.download(
            url,
            out_path,
            format_string="best[ext=mp4]/best",
            timeout_s=120,
        )
    except yt_dlp_cloudrun.CloudRunYtDlpFailed as exc:
        print(f"[x_shot] cloud yt-dlp failed for {url}: {exc}", file=sys.stderr)
        return False
    except yt_dlp_cloudrun.CloudRunYtDlpUnavailable as exc:
        print(f"[x_shot] cloud yt-dlp unavailable for {url}: {exc}", file=sys.stderr)
        return False
    return out_path.exists() and out_path.stat().st_size > 1024


def _slug_from_url(url: str) -> str:
    m = re.search(r"/([^/]+)/status/(\d+)", url)
    if not m:
        return re.sub(r"\W+", "_", url)[:48]
    return f"{m.group(1)}_{m.group(2)}"


def _normalize_tweet_url(url: str) -> tuple[str, str]:
    """Strip /photo/N, /video/N, /analytics etc. Returns (canonical_url, tweet_id)."""
    m = re.search(r"https?://(?:x|twitter)\.com/([^/]+)/status/(\d+)", url)
    if not m:
        raise ValueError(f"Not a tweet URL: {url}")
    handle, tid = m.group(1), m.group(2)
    return f"https://x.com/{handle}/status/{tid}", tid


def screenshot_tweets(
    urls: list[str],
    out_dir: pathlib.Path,
    *,
    chrome_profile: str | None = None,
    headless: bool = True,
    dark: bool = True,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies = _pull_chrome_cookies(chrome_profile) if chrome_profile else []

    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx_kwargs = {
            "viewport": {"width": 700, "height": 900},
            "device_scale_factor": 2,
            "color_scheme": "dark" if dark else "light",
        }
        if SESSION_STATE.exists() and not chrome_profile:
            ctx_kwargs["storage_state"] = str(SESSION_STATE)
        ctx = browser.new_context(**ctx_kwargs)
        if cookies:
            ctx.add_cookies(cookies)
        page = ctx.new_page()

        for raw_url in urls:
            try:
                url, tweet_id = _normalize_tweet_url(raw_url)
            except ValueError as e:
                print(f"[x_shot] SKIP     {raw_url}  ({e})", file=sys.stderr)
                results.append({"url": raw_url, "png": None, "error": str(e)})
                continue
            slug = _slug_from_url(url)
            png = out_dir / f"{slug}.png"
            try:
                page.wait_for_timeout(800)
                page.goto(url, wait_until="commit", timeout=30000)
                page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
                # Reply URLs render the parent at top and the target lower; pick by tweet ID.
                article = page.locator(
                    f'article[data-testid="tweet"]:has(a[href*="/status/{tweet_id}"])'
                ).first
                article.wait_for(timeout=8000)
                # Hide X's sticky top bar / "show more replies" banners — their
                # backdrop-filter:blur leaks into the article screenshot otherwise.
                page.add_style_tag(content="""
                    [role='banner'], header[role='banner'] { display: none !important; }
                    [data-testid='primaryColumn'] > div > div > div[style*='position: sticky'],
                    div[style*='position: sticky'] { position: static !important; }
                    * { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }
                """)
                # Let media (images, GIF first frame) actually paint.
                page.wait_for_timeout(2500)
                article.scroll_into_view_if_needed()
                article.screenshot(path=str(png), animations="disabled")
                box = article.bounding_box() or {"width": 0, "height": 0}
                media = page.evaluate(MEDIA_PROBE_JS, tweet_id) or {"kind": "none"}
                video_path = None
                if media.get("kind") == "video":
                    mp4 = out_dir / f"{slug}.mp4"
                    if _download_tweet_video(url, mp4):
                        video_path = str(mp4)
                results.append(
                    {
                        "url": url,
                        "tweet_id": tweet_id,
                        "slug": slug,
                        "png": str(png),
                        "width": int(box["width"]),
                        "height": int(box["height"]),
                        "media_kind": media.get("kind", "none"),
                        "media_region_frac": media.get("region_frac"),
                        "video_path": video_path,
                    }
                )
                tag = media.get("kind", "none")
                vmark = "  +video" if video_path else ""
                print(
                    f"[x_shot] {slug}  {int(box['width'])}x{int(box['height'])}  media={tag}{vmark}  -> {png}",
                    file=sys.stderr,
                )
            except PWTimeoutError:
                print(f"[x_shot] TIMEOUT  {url}", file=sys.stderr)
                results.append({"url": url, "slug": slug, "png": None, "error": "timeout"})
            except Exception as e:
                print(f"[x_shot] FAIL     {url}  {e}", file=sys.stderr)
                results.append({"url": url, "slug": slug, "png": None, "error": str(e)})

        ctx.close()
        browser.close()

    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Screenshot tweets from URLs.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", action="append", help="Tweet URL (repeatable)")
    src.add_argument(
        "--urls-file",
        type=pathlib.Path,
        help="JSON file containing either an array of URLs or {tweets:[{url:...},...]}",
    )
    ap.add_argument("--out-dir", type=pathlib.Path, required=True)
    ap.add_argument("--chrome-profile", default=None, help='e.g. "Profile 3"')
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--light", action="store_true", help="Light theme (default: dark)")
    ap.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=None,
        help="Optional path to write {url, png, width, height} manifest as JSON",
    )
    args = ap.parse_args()

    if args.url:
        urls = list(args.url)
    else:
        data = json.loads(args.urls_file.read_text())
        if isinstance(data, list):
            urls = [u if isinstance(u, str) else u["url"] for u in data]
        elif isinstance(data, dict) and "tweets" in data:
            urls = [t["url"] for t in data["tweets"] if t.get("url")]
        else:
            raise SystemExit(f"Unrecognized urls-file shape: {args.urls_file}")

    t0 = time.monotonic()
    results = screenshot_tweets(
        urls,
        args.out_dir,
        chrome_profile=args.chrome_profile,
        headless=not args.no_headless,
        dark=not args.light,
    )
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(results, indent=2))
        print(f"[x_shot] wrote manifest -> {args.manifest}", file=sys.stderr)
    ok = sum(1 for r in results if r.get("png"))
    print(
        f"[x_shot] done in {time.monotonic() - t0:.1f}s  ok={ok}/{len(results)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
