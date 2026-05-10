"""Drive a burner brand account through the production catalog —
like + subscribe via Playwright in an **attached** Chrome session.

Sibling to ``pipeline.create_burner_channel`` (channel + OAuth) and
``pipeline.cross_engage.burner_engage.run`` (the long-running engage daemon). This
script is the **one-shot, attach-to-running-Chrome** variant the user
asked for after seeing the burner-create flow:

* ATTACH to a running Chrome-Debug on the host's Chrome profile (or
  launch + bridge if none) — same coexistence pattern as
  ``pipeline.cross_engage.create_burner_channel.find_running_chrome_debug``.
* SWITCH the active YouTube channel context to the **burner brand
  account** via the avatar → "Switch account" submenu. This is
  required: rsinghtomar54's Chrome Profile 1 hosts many brand accounts
  (newrtrudaj personal, plus burners), and any like/subscribe action
  is attributed to whichever channel is currently active.
* For each video in ``pipeline.utils.catalog.list_catalog()`` (the production
  catalog), navigate to its ``/watch?v=…`` URL, **like**, and
  **subscribe** to the source channel.
* NEVER kill Chrome at the end (per the user's "do not close" rule).

Why "attached" matters: the original ``pipeline.cross_engage.burner_engage.run``
always launches a fresh Chrome-Debug AND tears it down at the end.
That blocks the user's parallel work in the same window AND wipes any
live YouTube session in Chrome-Debug each launch (via the destructive
cookie bridge). This script reuses whatever Chrome-Debug is already up.

Why no OAuth requirement: engagement actions (like, subscribe) are
purely UI-driven via Playwright. The Google API path (which IS what
needs OAuth) is blocked anyway during quota exhaustion. So we accept
any burner that's in ``channel_ids.json`` + ``profile_map.json``,
regardless of token state.

Usage
-----
::

    .venv/bin/python -m pipeline.cross_engage.cross_engage_burner_attached \\
        --slug zgsbhqszdheo

    # First 5 catalog videos only (smoke test):
    .venv/bin/python -m pipeline.cross_engage.cross_engage_burner_attached \\
        --slug zgsbhqszdheo --limit 5

Flags
-----
* ``--slug``        — burner identifier (must exist in channel_ids.json).
* ``--email``       — host Google account; default ``rsinghtomar54@gmail.com``.
* ``--profile``     — override Chrome profile dir; auto from email otherwise.
* ``--limit N``     — only engage with the first N catalog videos.
* ``--include-self``— include the burner's OWN brand-account videos
                      (rare; default skips so you don't self-engage).
* ``--no-subscribe``— like only, skip subscribe.
* ``--no-like``     — subscribe only, skip like.
* ``--dry-run``     — open each video, probe state, but don't click.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import sys
import time

from pipeline.cross_engage.cross_engage_via_playwright import (
    _clear_singleton,
    _probe_like,
    _probe_subscribe,
    bridge_cookies,
    launch_chrome_for,
    profile_email_map,
)
from pipeline.cross_engage.create_burner_channel import (
    CHANNEL_IDS_PATH,
    PROFILE_MAP_PATH,
    _load_json,
    find_running_chrome_debug,
    resolve_profile,
)

DEFAULT_EMAIL = "rsinghtomar54@gmail.com"
DEFAULT_WORK_DIR = pathlib.Path("/tmp/pw-engage-burner")
# Sibling user-data-dir so engage Chrome runs as a SEPARATE PROCESS,
# fully independent from the user's main Chrome-Debug window. Each
# instance needs its own --user-data-dir to avoid SingletonLock conflicts.
DEFAULT_USER_DATA_DIR = pathlib.Path.home() / "Library/Application Support/Google/Chrome-Debug-engage"
CANONICAL_CHROME_DEBUG = pathlib.Path.home() / "Library/Application Support/Google/Chrome-Debug"


# ── coexistence: harvest cookies from the user's signed-in Chrome via CDP ─

CANONICAL_CHROME_DEBUG = pathlib.Path.home() / "Library/Application Support/Google/Chrome-Debug"


def clone_chrome_debug_to_engage_udd(profile: str, *, src_udd: pathlib.Path, dst_udd: pathlib.Path) -> None:
    """Mirror the live Chrome-Debug Profile dir to a fresh sibling
    user-data-dir, so the engage Chrome inherits all profile metadata
    (avatar, account info, channels, settings — not just cookies).

    Skips files Chrome locks while running; surviving subset is enough
    for Chrome to launch as 'this person' and pick up the signed-in
    state once we top off cookies via CDP after launch.

    The final live cookies are injected separately via
    ``harvest_cookies_via_cdp`` + ``ctx.add_cookies`` since the on-disk
    cookies SQLite is locked by the running source Chrome.
    """
    import shutil
    src_p = src_udd / profile
    dst_p = dst_udd / profile
    if not src_p.exists():
        raise FileNotFoundError(f"source profile {src_p} not found")
    dst_p.mkdir(parents=True, exist_ok=True)
    # Top-level Local State (encryption key, profile registry).
    if (src_udd / "Local State").exists():
        try: (dst_udd / "Local State").write_bytes((src_udd / "Local State").read_bytes())
        except OSError as e: print(f"  ⚠ Local State copy: {e}", flush=True)
    # Per-profile files. Use copytree-with-ignore so we don't choke on
    # locked SQLite files.
    for entry in src_p.iterdir():
        target = dst_p / entry.name
        try:
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True, ignore_dangling_symlinks=True)
            else:
                shutil.copy2(entry, target)
        except (OSError, shutil.Error):
            # Locked / missing files — skip, we have what we need.
            continue


def harvest_cookies_via_cdp(src_port: int) -> list[dict]:
    """Read cookies from a Chrome instance running with a remote-debugging port.

    Read-only — doesn't open tabs, doesn't navigate, doesn't disrupt the
    user's session. Disconnect-only on exit (CDP-attached ``browser.close()``
    severs the WebSocket; it does NOT terminate the underlying Chrome
    process).

    The cookie SQLite file is LOCKED while the source Chrome runs, so
    file-level bridge_cookies() can't read it. The CDP ``Storage.getCookies``
    method serves them straight from Chrome's in-memory jar — no DB
    access needed.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        src = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{src_port}")
        try:
            cookies = src.contexts[0].cookies()
        finally:
            try: src.close()  # disconnect only — does NOT kill the source Chrome
            except Exception: pass
    return list(cookies)


# ── pre-flight ─────────────────────────────────────────────────────────────

def list_known_burners() -> list[dict]:
    """Burners we can engage from = channel_ids.json ∩ profile_map.json,
    minus production CHANNEL_REGISTRY. Looser than burner_engage.list_burner_channels
    (which also requires OAuth tokens) — pure-UI engagement doesn't need a token."""
    from pipeline.schemas.customization import CHANNEL_REGISTRY  # local import to avoid cycle
    prod = {c["key"] for c in CHANNEL_REGISTRY}
    ids = _load_json(CHANNEL_IDS_PATH, {})
    pmap = _load_json(PROFILE_MAP_PATH, {})
    out: list[dict] = []
    for slug, rec in ids.items():
        if slug in prod:
            continue
        email = (pmap.get(slug) or {}).get("email")
        if not email:
            continue
        out.append({
            "slug": slug,
            "channel_id": rec.get("channel_id"),
            "title": rec.get("title") or slug,
            "email": email,
        })
    return sorted(out, key=lambda r: r["slug"])


def resolve_burner(slug: str | None) -> dict:
    burners = list_known_burners()
    if not burners:
        raise SystemExit(
            "no burners registered. Spin one up with:\n"
            "  .venv/bin/python -m pipeline.cross_engage.create_burner_channel"
        )
    if slug:
        match = next((b for b in burners if b["slug"] == slug), None)
        if not match:
            raise SystemExit(
                f"burner {slug!r} not in channel_ids.json. Known: "
                f"{[b['slug'] for b in burners]}"
            )
        return match
    if len(burners) == 1:
        return burners[0]
    # Multiple burners, no slug — make the user pick.
    raise SystemExit(
        "multiple burners registered; pass --slug to pick one:\n"
        + "\n".join(f"  - {b['slug']:24s} {b['email']}" for b in burners)
    )


# ── account switching in attached Chrome ──────────────────────────────────

def _open_avatar_menu(page) -> None:
    avatar = page.locator("button#avatar-btn, button[aria-label*='Account menu' i]").first
    avatar.wait_for(state="visible", timeout=10000)
    avatar.click(timeout=5000)
    time.sleep(1.0)


def switch_to_burner_brand(page, work_dir: pathlib.Path, *, burner: dict) -> bool:
    """Click avatar → "Switch account" → row matching the burner.

    Idempotent: probes which channel is currently active first via
    ``studio.youtube.com/`` redirect (it lands on
    ``studio.youtube.com/channel/<active_UC>``). If that's already the
    burner, short-circuits.

    Returns True if the burner is now (or was already) the active
    YouTube context.
    """
    target_uc = burner["channel_id"]
    ctx = page.context

    def _read_active_uc(timeout_s: float = 12.0) -> str | None:
        # IMPORTANT: open a FRESH page for the verify. Studio's SPA
        # caches the channel context per-tab — re-using the page that
        # just did the switch click reads the OLD UC indefinitely
        # (validated bug 2026-05-09). A new page hits the server fresh
        # and gets the just-switched context.
        verify = ctx.new_page()
        verify.set_default_timeout(15000)
        try:
            verify.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=30000)
            try:
                verify.wait_for_url(re.compile(r"/channel/UC[A-Za-z0-9_-]{20,}"), timeout=int(timeout_s * 1000))
            except Exception:
                pass
            # Studio shows a "Welcome to YouTube Studio" first-run modal
            # for never-visited brand accounts. Doesn't affect the URL,
            # but log the dismissal anyway so the screenshot trail is
            # cleaner if we ever come back to debug.
            for sel in (
                "ytcp-button:has-text('Continue')",
                "tp-yt-paper-button:has-text('Continue')",
            ):
                try:
                    verify.locator(sel).first.click(timeout=2000)
                    time.sleep(0.5)
                    break
                except Exception:
                    continue
            m = re.search(r"/channel/(UC[A-Za-z0-9_-]{20,})", verify.url)
            return m.group(1) if m else None
        finally:
            try: verify.close()
            except Exception: pass

    active_uc = _read_active_uc()
    if active_uc == target_uc:
        print(f"  → already active as burner {burner['slug']!r} ({target_uc})", flush=True)
        return True
    print(f"  → active is {active_uc!r}; need to switch to {target_uc!r}", flush=True)

    active_uc = _read_active_uc()
    if active_uc == target_uc:
        print(f"  → already active as burner {burner['slug']!r} ({target_uc})", flush=True)
        return True
    print(f"  → active is {active_uc!r}; need to switch to {target_uc!r}", flush=True)

    page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(2.0)
    _open_avatar_menu(page)
    page.screenshot(path=str(work_dir / "10-avatar-menu.png"))

    switch = page.locator("text=Switch account").first
    if not switch.is_visible(timeout=4000):
        page.screenshot(path=str(work_dir / "10-no-switch-account.png"))
        raise SystemExit("'Switch account' menu item not found.")
    switch.click(timeout=5000)
    time.sleep(1.5)
    page.screenshot(path=str(work_dir / "11-switch-submenu.png"))

    # Each row in the submenu has the channel name as visible text.
    # The row container is <ytd-account-item-renderer>; clicking inner
    # text spans (yt-formatted-string) misses the click target.
    target_text = burner["title"]
    # Match on the row container, not the inner text spans, so the
    # click registers on the right element. Validated 2026-05-09.
    target = page.locator(
        f"ytd-account-item-renderer:has(yt-formatted-string#channel-title:text-is({json.dumps(target_text)}))"
    ).first
    if target.count() == 0:
        # Fallback: looser substring match (in case the title got
        # truncated or YouTube switched to ellipsis).
        target = page.locator(
            f"ytd-account-item-renderer:has-text({json.dumps(target_text)})"
        ).first
    if not target.is_visible(timeout=4000):
        page.screenshot(path=str(work_dir / "11-burner-row-not-found.png"))
        raise SystemExit(
            f"burner row {target_text!r} not found in switch-account submenu. "
            f"Maybe the brand-account name on YouTube differs from "
            f"channel_ids.json title — check {work_dir}/11-switch-submenu.png"
        )
    target.click(timeout=5000)
    print(f"  → clicked '{target_text}'; waiting for context switch", flush=True)
    time.sleep(4.0)
    try: page.wait_for_load_state("networkidle", timeout=12000)
    except Exception: pass

    active_uc = _read_active_uc()
    if active_uc != target_uc:
        page.screenshot(path=str(work_dir / "12-switch-failed.png"))
        print(f"  ⚠ switch verification failed; studio shows {active_uc!r}", flush=True)
        return False
    print(f"  ✓ switched to {burner['slug']} ({target_uc})", flush=True)
    return True


# ── per-video engage ──────────────────────────────────────────────────────

def _human_pause(lo: float = 1.0, hi: float = 2.5) -> None:
    """Random pause to mimic human reading time + give YouTube's SPA
    a beat to settle between actions."""
    time.sleep(random.uniform(lo, hi))


def _new_window_page(browser, ctx, url: str, timeout_ms: int = 15000):
    """Open ``url`` in a brand-new Chrome WINDOW (not a tab) via raw CDP.

    Chrome's ``Target.createTarget`` with ``newWindow=True`` pops a
    separate top-level window in the same browser process. Sharing the
    process means we get the same signed-in cookies/profile state for
    free; the new top-level window means our automation never steals
    focus from windows the user is interacting with (e.g. a match
    they're watching in the same Chrome).

    Returns a Playwright ``Page`` for the new window's tab.
    """
    session = browser.new_browser_cdp_session()
    try:
        target = session.send("Target.createTarget", {"url": url, "newWindow": True})
    finally:
        try: session.detach()
        except Exception: pass
    target_id = target["targetId"]

    # Wait for ctx.pages to surface the new page corresponding to that target.
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for p in ctx.pages:
            try:
                if getattr(p, "_impl_obj", None) and getattr(p._impl_obj, "_initializer", {}).get("targetId") == target_id:
                    return p
            except Exception:
                pass
            # Fallback: match by URL prefix (works for our /watch?v= form).
            try:
                if url.split("?", 1)[0] in p.url:
                    return p
            except Exception:
                continue
        time.sleep(0.2)
    # Last resort — return the most recently created page.
    return ctx.pages[-1] if ctx.pages else None



    time.sleep(random.uniform(lo, hi))  # pragma: no cover  # dead code after return


def engage_video(
    page,
    *,
    video: dict,
    do_like: bool,
    do_subscribe: bool,
    dry_run: bool,
    subscribed_channels: set[str],
) -> dict:
    """Open a watch URL and like + subscribe (one-shot). Returns a per-video result dict."""
    res = {
        "video_id": video["video_id"],
        "channel": video["channel"],
        "title": video["title"][:80],
        "like": "skipped",
        "subscribe": "skipped",
        "errors": [],
    }
    # Use canonical /watch?v=<id> form (not /shorts/) — watch page
    # exposes both like + subscribe in the stable selector lanes.
    watch_url = f"https://www.youtube.com/watch?v={video['video_id']}"
    print(f"\n  → {video['video_id']}  [{video['channel']}]  {video['title'][:60]!r}", flush=True)

    try:
        page.goto(watch_url, wait_until="domcontentloaded", timeout=60000)
        # YouTube hydrates the player + like/sub buttons asynchronously
        # AFTER domcontentloaded — a 2-4s wait isn't enough; the buttons
        # are in DOM but un-pressable. wait_for_selector on the visible
        # like button is the right gate.
        try:
            page.wait_for_selector(
                "button[aria-label*='like this video' i][aria-pressed]",
                state="attached", timeout=15000,
            )
        except Exception:
            pass
        _human_pause(3.0, 5.0)
    except Exception as e:
        res["errors"].append(f"goto {type(e).__name__}: {e}")
        return res

    if "accounts.google.com" in page.url:
        res["errors"].append("redirected to sign-in (cookies stale or video access-restricted)")
        return res

    # Detect "Video unavailable" — happens for stale test renders that
    # YouTube took down. Don't waste retries on like/sub probes.
    try:
        if page.locator("text='Video unavailable'").first.is_visible(timeout=2000):
            res["like"] = "unavailable"
            res["subscribe"] = "unavailable"
            print("     ✗ video unavailable (skipping)", flush=True)
            return res
    except Exception:
        pass

    # Dismiss YouTube's rotating consent modals if any
    for sel in ("button:has-text('Accept all')", "button:has-text('I agree')"):
        try:
            page.locator(sel).first.click(timeout=1500)
            time.sleep(0.4)
        except Exception:
            continue

    # ── LIKE ─────────────────────────────────────────────────────────────
    if do_like:
        # Probe-with-retry: the imported _probe_like uses .first and a
        # 2s timeout per selector. On slow renders the first match's
        # aria-pressed is still null (not 'false'/'true'), tripping the
        # 'unknown' branch. Retrying gives the SPA time to settle.
        state, btn = "unknown", None
        for attempt in range(4):
            state, btn = _probe_like(page)
            if state in ("liked", "unliked"):
                break
            time.sleep(2)
        print(f"     like state: {state}", flush=True)
        if state == "liked":
            res["like"] = "already_liked"
        elif state == "unliked" and btn is not None:
            if dry_run:
                res["like"] = "DRY_RUN_would_click"
            else:
                try:
                    # dispatch_event survives the theater-mode hidden-sibling
                    # actionability failures (validated 2026-05-08 in
                    # cross_engage_via_playwright Stage 6).
                    btn.dispatch_event("click", timeout=8000)
                    time.sleep(4)
                    state2, _ = _probe_like(page)
                    res["like"] = "OK" if state2 == "liked" else f"FAIL_state_after_click={state2}"
                except Exception as e:
                    res["errors"].append(f"like-click {type(e).__name__}: {e}")
                    res["like"] = "FAIL_exception"
        else:
            res["like"] = f"FAIL_no_button (state={state})"
        _human_pause()

    # ── SUBSCRIBE — once per source channel ─────────────────────────────
    if do_subscribe and video["channel"] not in subscribed_channels:
        state, btn = "unknown", None
        for attempt in range(4):
            state, btn = _probe_subscribe(page)
            if state in ("subscribed", "unsubscribed"):
                break
            time.sleep(2)
        print(f"     sub state: {state}", flush=True)
        if state == "subscribed":
            res["subscribe"] = "already_subscribed"
            subscribed_channels.add(video["channel"])
        elif state == "unsubscribed" and btn is not None:
            if dry_run:
                res["subscribe"] = "DRY_RUN_would_click"
            else:
                try:
                    btn.scroll_into_view_if_needed(timeout=4000)
                    time.sleep(0.4)
                    btn.click(force=True, timeout=8000)
                    time.sleep(4)
                    state2, _ = _probe_subscribe(page)
                    if state2 == "subscribed":
                        res["subscribe"] = "OK"
                        subscribed_channels.add(video["channel"])
                    else:
                        res["subscribe"] = f"FAIL_state_after_click={state2}"
                except Exception as e:
                    res["errors"].append(f"sub-click {type(e).__name__}: {e}")
                    res["subscribe"] = "FAIL_exception"
        else:
            res["subscribe"] = f"FAIL_no_button (state={state})"
        _human_pause()
    elif do_subscribe:
        res["subscribe"] = "skipped_channel_already_subscribed_this_run"

    return res


# ── entry point ───────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> dict:
    burner = resolve_burner(args.slug)
    print(f"[engage-burner] burner: {burner['slug']!r} ({burner['title']!r}) UC={burner['channel_id']}", flush=True)
    print(f"[engage-burner] host:   {burner['email']}", flush=True)

    profile = resolve_profile(burner["email"], args.profile)
    print(f"[engage-burner] profile: {profile}", flush=True)

    work_dir = (args.work_dir / burner["slug"]).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"[engage-burner] work:    {work_dir}", flush=True)

    # Catalog
    from pipeline.utils.catalog import list_catalog
    catalog = list_catalog()
    if args.limit:
        catalog = catalog[: args.limit]
    if not args.include_self:
        catalog = [v for v in catalog if v.channel_label != burner["title"]]
    if not catalog:
        raise SystemExit("catalog empty after filters; nothing to engage with.")
    print(f"[engage-burner] catalog: {len(catalog)} videos to engage with", flush=True)

    # Coexistence: launch a SEPARATE Chrome process on a sibling
    # user-data-dir. Three steps:
    #   1. cp -R the full live Profile dir from canonical Chrome-Debug
    #      (covers avatar, channels, settings; locked SQLite cookies
    #      come over stale or skipped).
    #   2. Launch Chrome on the sibling udd.
    #   3. Harvest LIVE cookies from canonical Chrome-Debug via CDP
    #      (read-only — no tabs opened in user's window) and inject
    #      via dst-CDP. Fills in whatever the on-disk copy missed.
    # Per-host engage Chrome — Chrome-Debug-engage-profile_<N>. This
    # lets us engage burners on rs54 (Profile 1), rs3011 (Profile 3),
    # iitkgp (Profile 4) etc. concurrently in separate Chrome processes.
    # Each Chrome can only be signed in to one Google account at a
    # time, but switches between brand accounts of the SAME Google
    # account via the channel-switcher in-flow.
    if args.user_data_dir == DEFAULT_USER_DATA_DIR:
        engage_udd = pathlib.Path.home() / "Library" / "Application Support" / "Google" / f"Chrome-Debug-engage-{profile.replace(' ', '_').lower()}"
    else:
        engage_udd = args.user_data_dir

    proc = None
    attached = False
    if not args.force_fresh:
        existing = find_running_chrome_debug(profile, user_data_dir=str(engage_udd))
        if existing is not None:
            attach_pid, port = existing
            print(f"[engage-burner] attaching to running engage-Chrome PID={attach_pid} CDP=ws://127.0.0.1:{port} (udd={engage_udd.name})", flush=True)
            attached = True

    if not attached:
        # Source live cookies from the canonical per-host Chrome-Debug-profile_<N>
        # IF that's running (the create-burner spree leaves these alive),
        # else from the canonical Chrome-Debug, else bridge from real
        # Chrome on disk (last resort; may be stale).
        host_canonical = pathlib.Path.home() / "Library/Application Support/Google" / f"Chrome-Debug-{profile.replace(' ', '_').lower()}"
        canonical_running = (
            find_running_chrome_debug(profile, user_data_dir=str(host_canonical))
            or find_running_chrome_debug(profile, user_data_dir=str(CANONICAL_CHROME_DEBUG))
        )
        harvested: list[dict] = []
        if canonical_running is not None:
            src_pid, src_port = canonical_running
            print(f"[engage-burner] cloning Profile {profile!r} from running canonical PID={src_pid}", flush=True)
            src_udd = host_canonical if host_canonical.exists() else CANONICAL_CHROME_DEBUG
            clone_chrome_debug_to_engage_udd(profile, src_udd=src_udd, dst_udd=engage_udd)
            print(f"[engage-burner] harvesting cookies via CDP (read-only)", flush=True)
            try:
                harvested = harvest_cookies_via_cdp(src_port)
                print(f"  → harvested {len(harvested)} cookies", flush=True)
            except Exception as e:
                print(f"  ⚠ cookie harvest failed: {type(e).__name__}: {e}", flush=True)
        else:
            print(f"[engage-burner] no live canonical Chrome; bridging from real Chrome on disk", flush=True)
            bridge_cookies(profile, dst_dir=engage_udd)

        _clear_singleton(engage_udd)
        proc, port = launch_chrome_for(profile, work_dir=work_dir, user_data_dir=engage_udd)
        print(f"[engage-burner] launched Chrome PID={proc.pid} CDP=ws://127.0.0.1:{port} (udd={engage_udd.name})", flush=True)

        if harvested:
            from playwright.sync_api import sync_playwright as _sp_inject
            with _sp_inject() as pw_inj:
                dst = pw_inj.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                try:
                    dst.contexts[0].add_cookies(harvested)
                    print(f"  → injected {len(harvested)} cookies into engage Chrome", flush=True)
                finally:
                    try: dst.close()
                    except Exception: pass

    from playwright.sync_api import sync_playwright
    results: list[dict] = []
    subscribed: set[str] = set()
    summary: dict = {
        "slug": burner["slug"],
        "channel_id": burner["channel_id"],
        "host_email": burner["email"],
        "profile": profile,
        "n_catalog": len(catalog),
        "switched_to_burner": False,
        "results": results,
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]  # signed-in context — DO NOT new_context()
            page = ctx.new_page()
            page.set_default_timeout(15000)

            switched = switch_to_burner_brand(page, work_dir, burner=burner)
            summary["switched_to_burner"] = switched
            if not switched:
                raise SystemExit(
                    f"could not switch to burner {burner['slug']!r} as active "
                    f"YouTube context. Engagement would land on the wrong "
                    f"channel — aborting before doing harm."
                )

            for i, v in enumerate(catalog, 1):
                print(f"\n[{i}/{len(catalog)}]", flush=True)
                vd = {
                    "video_id": v.video_id,
                    "channel": v.channel,
                    "title": v.title,
                    "url": v.url,
                }
                # Engagement runs in a SEPARATE Chrome process (different
                # user-data-dir) — see the launch block above. This means
                # ctx.new_page() opens a tab in OUR Chrome, not the user's.
                vpage = ctx.new_page()
                vpage.set_default_timeout(15000)
                try:
                    r = engage_video(
                        vpage,
                        video=vd,
                        do_like=not args.no_like,
                        do_subscribe=not args.no_subscribe,
                        dry_run=args.dry_run,
                        subscribed_channels=subscribed,
                    )
                    results.append(r)
                finally:
                    try: vpage.close()
                    except Exception: pass
                summary_path = work_dir / "result.json"
                summary_path.write_text(json.dumps(summary, indent=2))

    finally:
        # Coexistence: NEVER kill Chrome we attached to. Even when WE
        # launched it, leave it running so the next run can attach.
        if proc is not None:
            print(f"[engage-burner] launched Chrome PID {proc.pid} left running for the next attach.", flush=True)

    # Final summary
    n_liked = sum(1 for r in results if r["like"] in ("OK", "already_liked"))
    n_subbed = sum(1 for r in results if r["subscribe"] in ("OK", "already_subscribed"))
    n_err = sum(1 for r in results if r["errors"])
    print()
    print(f"✅ engagement done — {n_liked}/{len(results)} liked  ·  {n_subbed} subs ok  ·  {n_err} errors")
    print(f"   per-video summary → {work_dir / 'result.json'}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--slug", default=None, help="Burner identifier (must exist in channel_ids.json). Required when ≥2 burners are registered AND --all-burners is not set.")
    ap.add_argument("--all-burners", action="store_true", help="Cycle through every registered burner sequentially. Mutually exclusive with --slug.")
    ap.add_argument("--email", default=DEFAULT_EMAIL, help=f"Override host Google account (default: {DEFAULT_EMAIL}). The burner must be hosted under this email.")
    ap.add_argument("--personal-handle", default="newrtrudaj", help="Unused — kept for back-compat. The brand-account switcher uses the burner's title from channel_ids.json.")
    ap.add_argument("--profile", default=None, help="Override Chrome profile dir; auto-resolved from --email otherwise.")
    ap.add_argument("--work-dir", type=pathlib.Path, default=DEFAULT_WORK_DIR, help="Per-run artifacts (screenshots, result.json) land in <work-dir>/<slug>/.")
    ap.add_argument("--user-data-dir", type=pathlib.Path, default=DEFAULT_USER_DATA_DIR, help=f"Chrome user-data-dir for this engage instance. Default {DEFAULT_USER_DATA_DIR.name!r} is a SIBLING of the canonical Chrome-Debug — keeps your main Chrome-Debug window completely untouched (separate process, separate tabs).")
    ap.add_argument("--limit", type=int, default=0, help="Only engage with the first N catalog videos (0 = all).")
    ap.add_argument("--include-self", action="store_true", help="Include videos whose source channel matches the burner. Default: skip (don't self-engage).")
    ap.add_argument("--no-like", action="store_true", help="Skip the Like step (subscribe only).")
    ap.add_argument("--no-subscribe", action="store_true", help="Skip the Subscribe step (like only).")
    ap.add_argument("--force-fresh", action="store_true", help="Always launch a new Chrome-Debug. Default detects + attaches to a running one.")
    ap.add_argument("--dry-run", action="store_true", help="Probe like/subscribe state per video but don't click.")
    args = ap.parse_args()

    if args.all_burners and args.slug:
        raise SystemExit("--all-burners and --slug are mutually exclusive.")

    if args.all_burners:
        burners = list_known_burners()
        if not burners:
            raise SystemExit("no burners registered.")
        all_summaries: list[dict] = []
        n_failed = 0
        for i, b in enumerate(burners, 1):
            print(f"\n{'='*60}\n[{i}/{len(burners)}] burner {b['slug']!r}\n{'='*60}", flush=True)
            args.slug = b["slug"]
            try:
                summary = run(args)
                all_summaries.append(summary)
                if not summary.get("switched_to_burner"):
                    n_failed += 1
            except Exception as e:
                print(f"[engage-burner] ✗ {b['slug']} failed: {type(e).__name__}: {e}", flush=True)
                all_summaries.append({"slug": b["slug"], "errors": [str(e)]})
                n_failed += 1
        agg_path = args.work_dir / "all-burners-summary.json"
        agg_path.write_text(json.dumps(all_summaries, indent=2))
        print(f"\n[engage-burner] all-burners done: {len(burners) - n_failed}/{len(burners)} OK → {agg_path}")
        return 0 if n_failed == 0 else 1

    summary = run(args)
    return 0 if summary["switched_to_burner"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
