"""UI-driven cross-channel engagement (like + subscribe) via Playwright.

The Data API counterpart is `pipeline.research.cross_engage` (uses
videos.rate + subscriptions.insert — both 50 quota units each, blocked
when the project's daily 10K-unit pool is exhausted).

This module drives YouTube's web UI through a Chrome-Debug instance
attached via CDP (mirrors the upload path; see
``docs/playwright_with_signed_in_chrome.md``). It costs ZERO Data API
quota and works any time of day.

For each sibling profile (every Chrome-Debug profile except the source):
- Bridge cookies real Chrome → Chrome-Debug if stale
- Launch Chrome --profile-directory + remote-debugging-port=0
- Connect Playwright via CDP
- Navigate to the video URL
- Click Like (force=True; YouTube's like button often fails actionability checks
  near the viewport edge — see e2e_2026_05_08_stage6.md for the empirical fix)
- Click Subscribe (the source channel's button on the watch page)
- Verify both state transitions
- Close

Used by the `/upload-via-playwright` skill's Stage 6.
"""
from __future__ import annotations
import json, pathlib, re, subprocess, time
from typing import Iterable

HOME = pathlib.Path.home()
REAL_CHROME = HOME / "Library/Application Support/Google/Chrome"
CHROME_DEBUG = HOME / "Library/Application Support/Google/Chrome-Debug"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

SUBSCRIBE_SELECTORS = (
    "ytd-subscribe-button-renderer button",
    "yt-subscribe-button-view-model button",
    "button[aria-label*='Subscribe to' i]",
    "ytd-watch-metadata #subscribe-button-shape button",
)
LIKE_SELECTORS = (
    "button[aria-label*='like this video' i][aria-pressed]",
    "button[aria-label*='Like this' i]",
    "ytd-toggle-button-renderer #like-button button",
    "like-button-view-model button",
)
COOKIE_FILES = (
    "Cookies", "Cookies-journal", "Login Data", "Login Data-journal",
    "Web Data", "Web Data-journal", "Preferences",
)
NETWORK_COOKIE_FILES = ("Cookies", "Cookies-journal")


def discover_profiles(real_only: bool = False) -> list[str]:
    """List 'Profile N' dirs that exist in real Chrome (or Chrome-Debug if not real_only)."""
    base = REAL_CHROME if real_only else CHROME_DEBUG
    return sorted([
        p.name for p in base.iterdir()
        if p.is_dir() and re.fullmatch(r"Profile \d+", p.name)
    ])


def profile_email_map() -> dict[str, str]:
    """Read real Chrome's Local State and return {profile_dir: email}."""
    ls = json.loads((REAL_CHROME / "Local State").read_text())
    return {
        k: v["user_name"]
        for k, v in ls["profile"]["info_cache"].items()
        if v.get("user_name")
    }


def bridge_cookies(profile: str, *, dst_dir: pathlib.Path | None = None) -> None:
    """Selective copy real Chrome → Chrome-Debug for one profile. Idempotent.

    ``dst_dir`` defaults to the canonical ``Chrome-Debug`` user-data-dir.
    Pass a different path to bridge into a sibling instance (e.g.
    ``Chrome-Debug-engage``) so concurrent runs don't fight over the
    same SingletonLock. Each instance still inherits the live signed-in
    cookies from real Chrome the same way.
    """
    src = REAL_CHROME
    dst = dst_dir or CHROME_DEBUG
    src_p = src / profile
    dst_p = dst / profile
    if not src_p.exists():
        raise FileNotFoundError(f"Real Chrome {profile!r} not found at {src_p}")
    (dst_p / "Network").mkdir(parents=True, exist_ok=True)
    # Top-level Local State has the os_crypt encrypted key — must be in sync.
    if (src / "Local State").exists():
        (dst / "Local State").write_bytes((src / "Local State").read_bytes())
    for f in COOKIE_FILES:
        s = src_p / f
        if s.exists():
            (dst_p / f).write_bytes(s.read_bytes())
    for f in NETWORK_COOKIE_FILES:
        s = src_p / "Network" / f
        if s.exists():
            (dst_p / "Network" / f).write_bytes(s.read_bytes())


def assert_chrome_closed() -> None:
    out = subprocess.run(
        ["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
        capture_output=True, text=True,
    )
    pids = [p for p in out.stdout.split() if p.strip()]
    if pids:
        raise RuntimeError(
            f"Chrome still running (PIDs: {pids}). Close it first."
        )


def _clear_singleton(dst_dir: pathlib.Path | None = None) -> None:
    base = dst_dir or CHROME_DEBUG
    for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (base / f).unlink(missing_ok=True)


def launch_chrome_for(profile: str, *, work_dir: pathlib.Path, user_data_dir: pathlib.Path | None = None) -> tuple[subprocess.Popen, str]:
    """Launch Chrome-Debug with the given profile + remote-debugging-port=0.
    Returns (proc, cdp_port). Caller must terminate the proc.

    ``user_data_dir`` defaults to the canonical ``Chrome-Debug`` directory.
    Pass a different path (e.g. ``Chrome-Debug-engage``) to spin up a
    completely independent Chrome instance that coexists with the
    canonical one — needed when the user is actively using their
    Chrome-Debug window and we don't want to disrupt them by stealing
    focus / opening tabs there.

    ``start_new_session=True`` puts Chrome in its own POSIX session so it
    survives the Python parent's exit — required for ``--keep-open``
    flows in callers like ``pipeline.create_burner_channel`` where the
    user needs to interact with the browser AFTER the script ends. Cross-
    engage / upload paths terminate the proc explicitly anyway, so the
    new-session behaviour is a strict superset.
    """
    udd = user_data_dir or CHROME_DEBUG
    work_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = work_dir / "chrome.stderr"
    stderr_path.write_text("")
    proc = subprocess.Popen(
        [
            CHROME_BIN,
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={udd}",
            f"--profile-directory={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=AutomationControlled",
            "--noerrdialogs", "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w"),
        start_new_session=True,
    )
    cdp_port = None
    for _ in range(40):
        time.sleep(0.3)
        m = re.search(r"ws://127\.0\.0\.1:(\d+)", stderr_path.read_text())
        if m:
            cdp_port = m.group(1); break
    if not cdp_port:
        proc.kill()
        raise RuntimeError(f"no CDP port for {profile}\n{stderr_path.read_text()}")
    return proc, cdp_port


def _probe_like(page) -> tuple[str, object]:
    for sel in LIKE_SELECTORS:
        try:
            btn = page.locator(sel).first
            pressed = btn.get_attribute("aria-pressed", timeout=2000)
            if pressed == "true":
                return "liked", btn
            if pressed == "false":
                return "unliked", btn
        except Exception:
            continue
    return "unknown", None


def _probe_subscribe(page) -> tuple[str, object]:
    for sel in SUBSCRIBE_SELECTORS:
        try:
            btn = page.locator(sel).first
            txt = (btn.text_content(timeout=2000) or "").strip().lower()
            label = (btn.get_attribute("aria-label", timeout=1000) or "").lower()
            combined = f"{txt} {label}"
            if "subscribed" in combined or "unsubscribe" in combined:
                return "subscribed", btn
            if "subscribe" in combined:
                return "unsubscribed", btn
        except Exception:
            continue
    return "unknown", None


def engage_from_profile(
    profile: str,
    video_url: str,
    *,
    work_dir: pathlib.Path,
    do_like: bool = True,
    do_subscribe: bool = True,
) -> dict:
    """Visit `video_url` from `profile` and click Like + Subscribe if applicable.
    Returns a result dict with per-action status."""
    from playwright.sync_api import sync_playwright

    print(f"\n[{profile}] launching Chrome", flush=True)
    _clear_singleton()
    proc, port = launch_chrome_for(profile, work_dir=work_dir)
    result = {"profile": profile, "video": video_url, "like": None, "subscribe": None, "errors": []}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(15000)

            print(f"[{profile}] → {video_url}", flush=True)
            page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-01.png"))

            if "accounts.google.com" in page.url:
                result["errors"].append("redirected to sign-in (cookies stale or video access-restricted)")
                page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-signin.png"))
                return result

            for sel in (
                "button:has-text('Accept all')",
                "button:has-text('I agree')",
                "tp-yt-paper-button:has-text('Accept all')",
            ):
                try:
                    page.locator(sel).first.click(timeout=2000)
                    time.sleep(0.5)
                except Exception:
                    continue

            # ── LIKE ─────────────────────────────────────────────────────────
            if do_like:
                state, btn = _probe_like(page)
                print(f"[{profile}] like state: {state}", flush=True)
                if state == "liked":
                    result["like"] = "already_liked"
                elif state == "unliked":
                    try:
                        # Skip Playwright's scroll/visibility checks entirely.
                        # YouTube's like button is often hidden behind theater-
                        # mode siblings; dispatch_event delivers the click via
                        # the DOM regardless of visibility.
                        btn.dispatch_event("click", timeout=8000)
                    except Exception as e:
                        result["errors"].append(f"like-click {type(e).__name__}: {e}")
                    time.sleep(5)  # YouTube debounce + UI re-render
                    state2, _ = _probe_like(page)
                    if state2 == "liked":
                        result["like"] = "OK"
                        print(f"[{profile}] ✓ liked", flush=True)
                    else:
                        result["like"] = f"FAIL_state_after_click={state2}"
                        page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-likefail.png"))
                else:
                    result["like"] = "FAIL_no_button"
                    page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-like-no-btn.png"))

            # ── SUBSCRIBE ────────────────────────────────────────────────────
            if do_subscribe:
                state, btn = _probe_subscribe(page)
                print(f"[{profile}] sub state: {state}", flush=True)
                if state == "subscribed":
                    result["subscribe"] = "already_subscribed"
                elif state == "unsubscribed":
                    try:
                        btn.scroll_into_view_if_needed(timeout=4000)
                        time.sleep(0.5)
                        btn.click(force=True, timeout=8000)
                    except Exception as e:
                        result["errors"].append(f"sub-click {type(e).__name__}: {e}")
                    time.sleep(4)
                    state2, _ = _probe_subscribe(page)
                    if state2 == "subscribed":
                        result["subscribe"] = "OK"
                        print(f"[{profile}] ✓ subscribed", flush=True)
                    else:
                        result["subscribe"] = f"FAIL_state_after_click={state2}"
                        page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-subfail.png"))
                else:
                    result["subscribe"] = "FAIL_no_button"
                    page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-sub-no-btn.png"))

            page.screenshot(path=str(work_dir / f"{profile.replace(' ', '_')}-final.png"))
    finally:
        time.sleep(2)
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        time.sleep(2)

    return result


def fanout(
    video_url: str,
    *,
    source_profile: str,
    work_dir: pathlib.Path | None = None,
    profiles: Iterable[str] | None = None,
    bridge_cookies_first: bool = True,
) -> list[dict]:
    """Cycle every sibling profile (real-Chrome - source) through engage_from_profile.
    Sequential — Chrome-Debug locks the user-data-dir per-process."""
    work_dir = work_dir or pathlib.Path("/tmp/pw-cross-engage")
    work_dir.mkdir(parents=True, exist_ok=True)
    assert_chrome_closed()

    if profiles is None:
        all_real = discover_profiles(real_only=True)
        profiles = [p for p in all_real if p != source_profile]

    email_map = profile_email_map()
    print(f"[fanout] source: {source_profile} ({email_map.get(source_profile, '?')})")
    print(f"[fanout] siblings ({len(list(profiles))}):")
    profiles = list(profiles)  # materialize for re-iteration
    for p in profiles:
        print(f"  {p:12} → {email_map.get(p, '?')}")

    results = []
    for profile in profiles:
        if bridge_cookies_first:
            try:
                bridge_cookies(profile)
            except Exception as e:
                results.append({"profile": profile, "errors": [f"bridge {type(e).__name__}: {e}"]})
                continue
        try:
            results.append(engage_from_profile(profile, video_url, work_dir=work_dir))
        except Exception as e:
            results.append({"profile": profile, "errors": [f"engage {type(e).__name__}: {e}"]})

    summary_path = work_dir / "fanout-summary.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\n[fanout] summary → {summary_path}")
    return results


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Video URL (watch?v= form)")
    ap.add_argument("--source-profile", required=True, help="Profile that uploaded the video (will be skipped)")
    ap.add_argument("--work-dir", default="/tmp/pw-cross-engage")
    args = ap.parse_args()
    results = fanout(
        args.url,
        source_profile=args.source_profile,
        work_dir=pathlib.Path(args.work_dir),
    )
    print("\n" + "=" * 60)
    print("Summary")
    for r in results:
        like = r.get("like", "skipped")
        sub = r.get("subscribe", "skipped")
        errs = "; ".join(r.get("errors", []))
        print(f"  {r['profile']:12}  like={like:25}  sub={sub:25}  errs={errs or '—'}")
