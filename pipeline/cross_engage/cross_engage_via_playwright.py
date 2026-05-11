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
import urllib.error
import urllib.request
from typing import Iterable


def _cdp_alive(port: int, *, timeout: float = 2.0) -> bool:
    """Probe ``http://127.0.0.1:<port>/json/version`` to confirm CDP is live
    AND served by Chrome (not by some other CDP-speaking host).

    Used by ``launch_chrome_for`` to filter out the lsof-discovered
    candidate ports that aren't actually Chrome's CDP. There are two
    classes of false positive on a developer laptop:

    1. Non-CDP localhost listeners — e.g. mongod on :27017, postgres
       on :5432. Filtered by the HTTP 200 check.
    2. **Other CDP-speaking processes** — node.js / Electron app
       debuggers (VS Code, Slack, Discord, …) ALSO answer
       ``/json/version`` with a 200 because the V8 inspector implements
       the same endpoint. They list themselves as
       ``"Browser": "node.js/vXX"`` instead of
       ``"Browser": "Chrome/XX"``. Without checking the Browser field,
       Playwright's ``connect_over_cdp`` connects to node.js and
       fails with ``Invalid URL: undefined`` because node's ``/json``
       returns Electron targets, not Chrome browser targets. Validated
       bug 2026-05-11.

    Defined locally rather than imported from
    ``create_burner_channel`` because that module pulls in playwright
    + the upload chain on import — too heavy for the worker's launch
    path. Tiny duplication, big import-graph win.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout,
        ) as r:
            if r.status != 200:
                return False
            payload = json.loads(r.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    browser = str(payload.get("Browser", ""))
    # Chrome's Browser string is "Chrome/<version>" (e.g.
    # "Chrome/147.0.7727.139"). Edge ships "Edge/<version>" and shares
    # the chromium CDP — accept it too in case someone runs the worker
    # with --chrome-bin pointing at an Edge install. Anything else
    # (node.js, electron, headless_shell stripped builds) is rejected.
    return browser.startswith("Chrome/") or browser.startswith("Edge/")

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
# IMPORTANT — Dislike misclassification, fixed 2026-05-10 (re-applied
# 2026-05-11 after the original edit was reverted by an overlapping
# patch).
#
# The pre-fix selectors used `aria-label*='like this video' i` (case-
# insensitive substring), which ALSO matches `aria-label='Dislike this
# video'` because "like this video" is a substring of "Dislike this
# video". On /watch the DOM order made `.first` return the Like button;
# on /shorts the order is reversed and `.first` returned the Dislike
# button. `_probe_like` then reported `("unliked", dislike_btn)` and
# the engage loop CLICKED DISLIKE on every Short — both the original
# 2026-05-10 evening run AND the re-occurrence after the second
# regression on 2026-05-11.
#
# Fix: lead with a single universal selector that works on /watch AND
# /shorts AND can never match Dislike (the `:not(dislike-button-view-
# model button)` clause excludes the adjacent dislike-button-view-
# model's child button). Each fallback also carries a `:not([aria-
# label*='dislike' i])` guard for defence-in-depth.
LIKE_SELECTORS = (
    # Universal — works on /watch AND /shorts. The Like button on
    # Shorts has aria-label="No likes" / "<N> likes" (a count display
    # that doubles as the toggle); on /watch it's "like this video
    # along with N other people". aria-pressed reflects user state on
    # both. The :not() excludes the sibling dislike-button-view-model.
    "like-button-view-model button:not(dislike-button-view-model button)",
    # Legacy /watch fallbacks — keep for back-compat with older YouTube
    # revisions where the view-model markup hasn't rolled out. Both
    # carry explicit Dislike guards so the misclassification cannot
    # recur even if a future YouTube refresh narrows the universal
    # selector.
    "button[aria-label*='like this video' i][aria-pressed]:not([aria-label*='dislike' i])",
    "ytd-toggle-button-renderer #like-button button:not([aria-label*='dislike' i])",
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


def launch_chrome_for(
    profile: str,
    *,
    work_dir: pathlib.Path,
    user_data_dir: pathlib.Path | None = None,
    headless: bool = False,
) -> tuple[subprocess.Popen, str]:
    """Launch Chrome-Debug with the given profile + remote-debugging-port=0.
    Returns (proc, cdp_port). Caller must terminate the proc.

    ``user_data_dir`` defaults to the canonical ``Chrome-Debug`` directory.
    Pass a different path (e.g. ``Chrome-Debug-engage``) to spin up a
    completely independent Chrome instance that coexists with the
    canonical one — needed when the user is actively using their
    Chrome-Debug window and we don't want to disrupt them by stealing
    focus / opening tabs there.

    ``headless=True`` adds ``--headless=new`` (modern headless, Chrome
    109+) plus a fixed ``--window-size=1366,900`` so YouTube's
    responsive layout matches the desktop SPA selectors the avatar /
    channel-switcher flow relies on. Headless still respects
    ``--profile-directory`` and reads cookies from the live user-data-dir
    natively (no extra bridging needed). Useful when the user is
    actively using their windowed Chrome and a second visible Chrome
    would steal focus / be disruptive.

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
    args = [
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
        # Mute every tab globally. The cross-engage Shorts loop opens
        # 50+ tabs concurrently; un-muted, the audio decoders saturate
        # the user's CPU and slow page.goto() to 10+ s per tab. YouTube
        # still counts views from muted plays, so watch-time signal is
        # unaffected. Validated 2026-05-10.
        "--mute-audio",
    ]
    if headless:
        args += [
            # Chrome 109+ "new" headless renders the same DOM as
            # windowed mode (the legacy headless lite-rendering would
            # have broken the avatar / channel-switcher selectors).
            "--headless=new",
            # YouTube's SPA renders different chrome layouts at
            # different viewport sizes; pin a desktop width so the
            # avatar #avatar-btn selector + Switch-account submenu
            # render the same way they do in our regular dev runs.
            "--window-size=1366,900",
        ]
    args.append("about:blank")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w"),
        start_new_session=True,
    )
    # CDP-port discovery — prefer the stderr-text scrape because it's
    # the ONLY method that's guaranteed to return THIS chrome process'
    # port. The lsof fallback was added for a launchd context where
    # stderr was lost, but on a multi-Chrome laptop it can return a
    # SIBLING chrome's port (validated bug 2026-05-11: a fresh chrome
    # with --user-data-dir=Chrome-Debug-profile_1 reported port 54888
    # via lsof, which actually belonged to the canonical Chrome-Debug
    # PID 86647 — Playwright then connect_over_cdp'd to the WRONG
    # browser, hung 180 s on the busy canonical's CDP target list,
    # and aborted. Chrome.app on macOS appears to leak file descriptors
    # across sibling browser PIDs in some boot races, so lsof on a
    # fresh PID can surface a sibling's listener.)
    #
    # New strategy:
    #   - Wait UP TO 30 s for the stderr-text scrape (chrome usually
    #     prints "DevTools listening on ws://..." within 1-3 s; 30 s
    #     covers ridiculously loaded laptops).
    #   - Only fall back to lsof if stderr is STILL empty after that
    #     deadline. The lsof candidate must (a) respond to /json/version
    #     AND (b) be different from any port already discovered for a
    #     DIFFERENT chrome PID — the latter check is approximate (we
    #     don't track sibling chromes here) but the long stderr wait
    #     should make the fallback fire only in the launchd-stderr-
    #     missing edge case, where there's no sibling to confuse it
    #     with.
    cdp_port = None
    stderr_deadline = time.time() + 30.0
    while time.time() < stderr_deadline:
        time.sleep(0.3)
        try:
            stderr_text = stderr_path.read_text()
        except OSError:
            stderr_text = ""
        m = re.search(r"ws://127\.0\.0\.1:(\d+)", stderr_text)
        if m:
            cdp_port = m.group(1)
            break
    if not cdp_port:
        # Stderr genuinely empty — last-ditch lsof fallback. Same as
        # before but with the explicit caveat that it may pick a
        # sibling chrome's port on this laptop.
        for _ in range(10):
            time.sleep(0.3)
            try:
                ls = subprocess.run(
                    ["lsof", "-p", str(proc.pid), "-iTCP", "-sTCP:LISTEN", "-n"],
                    capture_output=True, text=True, timeout=2,
                )
                for line in ls.stdout.splitlines():
                    if "LISTEN" not in line or "127.0.0.1" not in line:
                        continue
                    pm = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if not pm:
                        continue
                    candidate = pm.group(1)
                    if _cdp_alive(int(candidate), timeout=1.5):
                        cdp_port = candidate
                        break
                if cdp_port:
                    break
            except Exception:  # noqa: BLE001
                pass
    if not cdp_port:
        proc.kill()
        raise RuntimeError(f"no CDP port for {profile}\n{stderr_path.read_text()}")
    # Final safety: even when the stderr scrape wins, double-check the
    # port is alive. A printed-but-not-yet-listening port is a known
    # Chrome startup race (rare, but stalls Playwright's connect for
    # the full timeout).
    deadline = time.time() + 8.0
    while time.time() < deadline and not _cdp_alive(int(cdp_port), timeout=0.5):
        time.sleep(0.2)
    if not _cdp_alive(int(cdp_port), timeout=1.5):
        proc.kill()
        raise RuntimeError(
            f"chrome announced CDP on port {cdp_port} but /json/version "
            f"never responded; stderr:\n{stderr_path.read_text()}"
        )
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
    user_data_dir: pathlib.Path | None = None,
) -> dict:
    """Visit `video_url` from `profile` and click Like + Subscribe if applicable.
    Returns a result dict with per-action status.

    ``user_data_dir`` lets the caller pass a per-profile sibling UDD so
    multiple profiles can engage concurrently in their own Chrome
    processes (each Chrome locks its own UDD; without per-profile dirs
    they'd fight over the canonical Chrome-Debug SingletonLock and
    serialise). Defaults to the canonical ``Chrome-Debug`` for back-compat.
    """
    from playwright.sync_api import sync_playwright

    print(f"\n[{profile}] launching Chrome", flush=True)
    udd = user_data_dir or CHROME_DEBUG
    _clear_singleton(udd)
    proc, port = launch_chrome_for(profile, work_dir=work_dir, user_data_dir=udd)
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
    max_workers: int | None = None,
) -> list[dict]:
    """Cycle every sibling profile (real-Chrome - source) through engage_from_profile.

    **2026-05-10: parallelised.** Each profile gets its own per-profile
    sibling Chrome user-data-dir (``Chrome-Debug-engage-<profile>``), so
    Chromes can run concurrently without fighting over the canonical
    Chrome-Debug SingletonLock. The pattern is the same one
    ``cross_engage_burner_attached.py:557`` already uses for burner
    engagement. Cap at 4 workers by default (M2 Max RAM ceiling for
    concurrent Chromiums); override via ``max_workers`` or env
    ``YTFACTORY_CROSS_ENGAGE_WORKERS``.

    Pre-fix this was sequential — the largest line item in the per-render
    wall-clock budget (3-5 min for 5 channels). Parallelising at 4 cuts
    that to ~1-1.5 min on a typical 5-channel fanout.
    """
    import os
    import concurrent.futures as _cf

    work_dir = work_dir or pathlib.Path("/tmp/pw-cross-engage")
    work_dir.mkdir(parents=True, exist_ok=True)
    # No more global Chrome-closed assertion: per-profile sibling UDDs
    # mean each Chrome is independent, and concurrency-by-design needs
    # the user's main Chrome to stay open.

    if profiles is None:
        all_real = discover_profiles(real_only=True)
        profiles = [p for p in all_real if p != source_profile]
    profiles = list(profiles)

    email_map = profile_email_map()
    print(f"[fanout] source: {source_profile} ({email_map.get(source_profile, '?')})")
    print(f"[fanout] siblings ({len(profiles)}):")
    for p in profiles:
        print(f"  {p:12} → {email_map.get(p, '?')}")

    if max_workers is None:
        try:
            max_workers = int(os.environ.get("YTFACTORY_CROSS_ENGAGE_WORKERS", "4"))
        except ValueError:
            max_workers = 4
    max_workers = max(1, min(max_workers, len(profiles) or 1))
    print(f"[fanout] max_workers={max_workers}")

    def _engage_one(profile: str) -> dict:
        # Per-profile sibling UDD so this Chrome doesn't lock the
        # canonical Chrome-Debug user-data-dir. Mirrors
        # cross_engage_burner_attached.py:557.
        engage_udd = pathlib.Path.home() / "Library/Application Support/Google" / (
            f"Chrome-Debug-engage-{profile.replace(' ', '_').lower()}"
        )
        if bridge_cookies_first:
            try:
                bridge_cookies(profile, dst_dir=engage_udd)
            except Exception as e:
                return {"profile": profile, "errors": [f"bridge {type(e).__name__}: {e}"]}
        try:
            return engage_from_profile(
                profile, video_url, work_dir=work_dir, user_data_dir=engage_udd,
            )
        except Exception as e:
            return {"profile": profile, "errors": [f"engage {type(e).__name__}: {e}"]}

    results: list[dict] = []
    if max_workers == 1 or len(profiles) <= 1:
        for profile in profiles:
            results.append(_engage_one(profile))
    else:
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_for = {ex.submit(_engage_one, p): p for p in profiles}
            for fut in _cf.as_completed(future_for):
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append({"profile": future_for[fut], "errors": [f"future {type(e).__name__}: {e}"]})

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
