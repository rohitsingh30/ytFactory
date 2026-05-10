"""Burner-channel cross-engagement worker.

A "burner channel" is a YouTube channel we have OAuth access to but
which is NOT in `pipeline.schemas.customization.CHANNEL_REGISTRY` (i.e. not part
of the production stable). Burners exist for cross-engagement —
liking + subscribing + watching our production catalog from a fresh
identity to seed views, watch-time, and the YouTube algorithm.

This module:

* Discovers burner channels (intersection of `youtube_token_*.json` and
  `channel_ids.json` minus production registry).
* Drives a Playwright session as the burner — using the burner's
  matching Chrome profile so the YouTube web session is genuine
  (no OAuth cookie injection — YouTube has been hostile to those in
  2026).
* For every video in `pipeline.utils.catalog.list_catalog()`:
   1. Open in a NEW tab (Cmd+click pattern via context.new_page()).
   2. Wait for player to load, click "Like".
   3. Click "Subscribe" if not already subscribed (per channel,
      idempotent — tracked in sidecar).
* Then enters a tab-cycling watch loop: every 20-45s switches focus
  to another open tab, advancing watch-time signal across the catalog.
* Loops forever until `/tmp/burner_engage_<slug>.stop` exists.

State persisted to ``data/burner_engage/<slug>.json`` so the dashboard
can poll the live worker (worker writes; dashboard reads). On graceful
stop the file's `phase` becomes `stopped`. On crash it stays whatever
it was before — the next run starts a fresh job.

We deliberately keep this single-process / single-burner. Concurrent
burners would need separate Playwright contexts, separate stop signals,
and add scheduling complexity that's not in scope yet.
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "burner_engage"
TOKEN_DIR = Path.home() / ".config" / "ytfactory"
PROFILE_MAP_PATH = TOKEN_DIR / "profile_map.json"
SECRETS_DIR = Path("/secrets")
CHROME_USER_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


def _secret_value(name: str) -> Path:
    """Cloud Run secret mount path: /secrets/<name>/value."""
    return SECRETS_DIR / name / "value"


def _resolve_token_path(slug: str) -> Path | None:
    """Cloud-aware: prefer mounted secret, fall back to local file."""
    sec = _secret_value(f"youtube-token-{slug}")
    if sec.exists():
        return sec
    local = TOKEN_DIR / f"youtube_token_{slug}.json"
    if local.exists():
        return local
    return None


def _resolve_profile_map() -> Path | None:
    sec = _secret_value("profile-map")
    if sec.exists():
        return sec
    if PROFILE_MAP_PATH.exists():
        return PROFILE_MAP_PATH
    return None


def _resolve_channel_ids() -> Path | None:
    sec = _secret_value("youtube-channel-ids")
    if sec.exists():
        return sec
    local = TOKEN_DIR / "channel_ids.json"
    if local.exists():
        return local
    return None


# ---------------------------------------------------------------------------
# Burner discovery
# ---------------------------------------------------------------------------


def _production_slugs() -> set[str]:
    from pipeline.schemas.customization import CHANNEL_REGISTRY  # local import to avoid cycle

    return {c["key"] for c in CHANNEL_REGISTRY}


def _channel_ids_registry() -> dict[str, dict]:
    p = _resolve_channel_ids()
    if p is None:
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _account_email_for_token(slug: str) -> str | None:
    """The Google account email behind a token slug.

    OAuth tokens don't store the account email directly. Best signal
    we have is the cached profile_map, populated by upload-via-playwright
    workflows. Fall back to None — caller must handle by asking the
    user or re-mapping via Local State.
    """
    pmap = _resolve_profile_map()
    if pmap is None:
        return None
    try:
        m = json.loads(pmap.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # profile_map.json maps email → "Profile N" (per upload-via-playwright
    # docs). We need the reverse: slug-of-channel → email. Today we have
    # to introspect the YouTube API to figure out which email owns this
    # channel. Use the upload helper if available.
    rec = m.get(slug)
    if isinstance(rec, dict) and rec.get("email"):
        return rec["email"]
    return None


def list_burner_channels() -> list[dict[str, Any]]:
    """Burner = has OAuth token + is in channel_ids.json + NOT production.

    Returns a JSON-friendly list each carrying:
      {slug, channel_id, title, has_token, profile_known, email}
    """
    prod = _production_slugs()
    ids = _channel_ids_registry()
    out: list[dict[str, Any]] = []
    for slug, rec in ids.items():
        if slug in prod:
            continue
        if _resolve_token_path(slug) is None:
            continue
        email = _account_email_for_token(slug)
        out.append({
            "slug": slug,
            "channel_id": rec.get("channel_id"),
            "title": rec.get("title") or slug,
            "discovered_at": rec.get("discovered_at"),
            "has_token": True,
            "email": email,
            "profile_known": email is not None,
        })
    out.sort(key=lambda r: r["slug"])
    return out


# ---------------------------------------------------------------------------
# Chrome profile mapping
# ---------------------------------------------------------------------------


def _email_to_chrome_profile(email: str) -> str | None:
    """Map a Google email to its Chrome `Profile N` directory name.

    Reads Local State (the live one — the file itself is JSON and safe
    to read while Chrome is open; we just don't WRITE to it).
    Returns None if no profile in this Chrome installation is signed
    in to that email.
    """
    state = CHROME_USER_DIR / "Local State"
    if not state.exists():
        return None
    try:
        d = json.loads(state.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cache = d.get("profile", {}).get("info_cache", {})
    for profile_dir, info in cache.items():
        if info.get("user_name", "").lower() == email.lower():
            return profile_dir
    return None


def _stage_profile_copy(slug: str, profile_dir: str) -> Path:
    """DEPRECATED — kept for reference only.

    Earlier version of the worker used `launch_persistent_context` against
    a copy of the live Chrome profile. That path is unworkable on macOS:
    Chrome encrypts cookie values with a key bound to the macOS Keychain
    AND the launching binary fingerprint. A fresh Playwright Chromium
    inherits neither, so all auth cookies decrypt to empty strings and
    the YouTube session is silently lost.

    Replaced with the CDP+bridge pattern from
    `pipeline.cross_engage_via_playwright` — see ``run()`` above.
    """
    raise NotImplementedError(
        "_stage_profile_copy is deprecated; use the CDP+bridge launcher."
    )


# ---------------------------------------------------------------------------
# Sidecar state
# ---------------------------------------------------------------------------


@dataclass
class VideoState:
    video_id: str
    channel: str
    channel_label: str
    title: str
    url: str
    liked: bool = False
    subscribed: bool = False
    tab_open: bool = False
    last_focused_at: str = ""
    watch_seconds: int = 0
    error: str = ""


@dataclass
class EngageState:
    slug: str
    channel_id: str
    started_at: str
    phase: str = "initializing"  # initializing | engaging | watching | stopped | failed | blocked
    last_action_at: str = ""
    last_action_msg: str = ""
    stop_requested: bool = False
    videos: list[VideoState] = field(default_factory=list)


def _state_path(slug: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{slug}.json"


def _stop_path(slug: str) -> Path:
    return Path(f"/tmp/burner_engage_{slug}.stop")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_state(state: EngageState) -> None:
    p = _state_path(state.slug)
    payload = {
        "slug": state.slug,
        "channel_id": state.channel_id,
        "started_at": state.started_at,
        "phase": state.phase,
        "last_action_at": state.last_action_at,
        "last_action_msg": state.last_action_msg,
        "stop_requested": state.stop_requested,
        "videos": [asdict(v) for v in state.videos],
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


def read_state(slug: str) -> dict | None:
    """Public: dashboard polls this to render live status."""
    p = _state_path(slug)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def request_stop(slug: str) -> None:
    """Public: ask the worker (running or not) to stop on its next tick."""
    _stop_path(slug).write_text(_now())


def is_running(slug: str) -> bool:
    """Cheap liveness check — sidecar exists AND last_action recent.

    Defines "alive" as last_action_at within the last 90 seconds. The
    worker's tab cycle ticks every 20-45s so 90s is comfortably above
    the upper bound. Phase=='stopped' or 'failed' always returns False.
    """
    s = read_state(slug)
    if not s:
        return False
    if s.get("phase") in ("stopped", "failed"):
        return False
    last = s.get("last_action_at")
    if not last:
        return False
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() < 90


# ---------------------------------------------------------------------------
# Worker — the actual Playwright loop
# ---------------------------------------------------------------------------


def _bump_action(state: EngageState, msg: str) -> None:
    state.last_action_at = _now()
    state.last_action_msg = msg
    _save_state(state)
    logger.info("[burner:%s] %s", state.slug, msg)


def _maybe_stop(state: EngageState) -> bool:
    if _stop_path(state.slug).exists():
        state.stop_requested = True
        return True
    return False


def _click_like(page) -> bool:
    """DEPRECATED — replaced by `cross_engage_via_playwright._probe_like`
    which has the actual battle-tested selectors + dispatch_event click."""
    raise NotImplementedError("use _probe_like from cross_engage_via_playwright")


def _click_subscribe(page) -> bool:
    """DEPRECATED — replaced by `cross_engage_via_playwright._probe_subscribe`."""
    raise NotImplementedError("use _probe_subscribe from cross_engage_via_playwright")


def _human_pause(lo: float = 1.5, hi: float = 4.0) -> None:
    time.sleep(random.uniform(lo, hi))


def run(slug: str, *, headless: bool = False) -> int:
    """Run the burner-engage worker for a slug. Blocks until stopped."""
    # NOTE: launch path uses the proven CDP+bridge pattern from
    # `pipeline.cross_engage_via_playwright` (real Chrome binary +
    # Chrome-Debug user-data-dir + cookie bridge from live Chrome +
    # Playwright connect_over_cdp). The naive
    # `launch_persistent_context(profile_copy)` path silently loses
    # macOS Keychain cookie decryption — Chrome encrypts cookie values
    # with a per-binary-fingerprint key and a fresh Playwright Chromium
    # can't read them. Use the helpers below instead.
    import subprocess as _subprocess
    from pipeline.cross_engage.cross_engage_via_playwright import (
        bridge_cookies, launch_chrome_for,
        _clear_singleton, _probe_like, _probe_subscribe,
    )
    from playwright.sync_api import sync_playwright  # lazy import

    # Real Chrome may be running. We don't kill it — Chrome-Debug uses
    # its own --user-data-dir so the two coexist. The only thing real
    # Chrome blocks is the cookie BRIDGE (SQLite locks on Cookies DBs);
    # if Chrome is up we skip bridging and rely on whatever Chrome-Debug
    # already has from a prior bridge. If those cookies are stale,
    # YouTube will redirect to sign-in and the worker logs a clear
    # message — at which point the user can quit Chrome and re-Engage.
    def _real_chrome_is_running() -> list[str]:
        ps = _subprocess.run(
            ["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
            capture_output=True, text=True,
        )
        return [p for p in ps.stdout.split() if p.strip()]

    burners = {b["slug"]: b for b in list_burner_channels()}
    if slug not in burners:
        print(f"error: '{slug}' is not a burner channel", file=sys.stderr)
        return 2
    burner = burners[slug]
    if not burner.get("email"):
        print(
            f"error: no email known for burner '{slug}'. "
            f"Add an entry to ~/.config/ytfactory/profile_map.json:\n"
            f'  {{"{slug}": {{"email": "<you>@gmail.com"}} }}',
            file=sys.stderr,
        )
        return 2
    profile_dir = _email_to_chrome_profile(burner["email"])
    if not profile_dir:
        print(
            f"error: no Chrome profile found for {burner['email']}. "
            f"Sign into that account once via real Chrome, then retry.",
            file=sys.stderr,
        )
        return 2

    # Clear any stale stop signal from a previous run
    _stop_path(slug).unlink(missing_ok=True)

    # Build the catalog — frozen at run start so deletions/additions
    # mid-run don't surprise us
    from pipeline.utils.catalog import list_catalog

    catalog = list_catalog()
    if not catalog:
        print("error: catalog empty (no shipped videos to engage with).", file=sys.stderr)
        return 2

    state = EngageState(
        slug=slug,
        channel_id=burner["channel_id"] or "",
        started_at=_now(),
    )
    state.videos = [
        VideoState(
            video_id=e.video_id,
            channel=e.channel,
            channel_label=e.channel_label,
            title=e.title,
            url=e.url,
        )
        for e in catalog
    ]
    _bump_action(state, f"initializing (catalog={len(state.videos)} videos)")

    # Pre-check: if real Chrome is running we SKIP the cookie bridge
    # (SQLite locks would corrupt the copy). Chrome-Debug coexists fine
    # — different --user-data-dir, different process group. Worker uses
    # whatever cookies Chrome-Debug already has.
    chrome_pids = _real_chrome_is_running()
    if chrome_pids:
        _bump_action(
            state,
            f"real Chrome is running (PIDs: {chrome_pids}); "
            f"skipping cookie bridge — using existing Chrome-Debug cookies",
        )

    # Bridge cookies real Chrome → Chrome-Debug for this profile, idempotent.
    # Skip when real Chrome is up (SQLite-lock corruption risk).
    if not chrome_pids:
        _bump_action(state, f"bridging cookies for {profile_dir}…")
        try:
            bridge_cookies(profile_dir)
        except Exception as e:  # noqa: BLE001
            state.phase = "failed"
            _bump_action(state, f"cookie bridge failed: {e}")
            return 1

    # Launch Chrome-Debug with this profile + remote-debugging-port=0,
    # parse the CDP port from stderr.
    work_dir = Path(f"/tmp/burner_engage_work_{slug}")
    work_dir.mkdir(parents=True, exist_ok=True)
    _clear_singleton()

    _bump_action(state, f"launching Chrome (profile={profile_dir})…")
    try:
        chrome_proc, cdp_port = launch_chrome_for(profile_dir, work_dir=work_dir)
    except Exception as e:  # noqa: BLE001
        state.phase = "failed"
        _bump_action(state, f"chrome launch failed: {e}")
        return 1

    # Per-channel "already subscribed" memo so we don't re-click
    subscribed_channels: set[str] = set()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0]  # signed-in context — DO NOT new_context()

            state.phase = "engaging"
            _bump_action(state, "Chrome attached, opening videos…")

            # Phase 1: open + like + subscribe each video in its own tab.
            # Reuse cross_engage_via_playwright._probe_like / _probe_subscribe
            # so we get the same battle-tested selectors and
            # dispatch_event-based click that survives YouTube UI churn.
            for vs in state.videos:
                if _maybe_stop(state):
                    break
                try:
                    page = ctx.new_page()
                    page.set_default_timeout(15000)
                    page.goto(vs.url, wait_until="domcontentloaded", timeout=60000)
                    vs.tab_open = True
                    _human_pause(2.0, 5.0)  # let player initialise

                    if "accounts.google.com" in page.url:
                        vs.error = "redirected to sign-in (cookies stale)"
                        _bump_action(
                            state, f"sign-in redirect on {vs.video_id} — bridge stale, re-bridge & retry"
                        )
                        continue

                    # Dismiss consent modals if any
                    for sel in (
                        "button:has-text('Accept all')",
                        "button:has-text('I agree')",
                    ):
                        try:
                            page.locator(sel).first.click(timeout=1500)
                            time.sleep(0.4)
                        except Exception:
                            continue

                    # LIKE
                    like_state, like_btn = _probe_like(page)
                    if like_state == "liked":
                        vs.liked = True
                        _bump_action(state, f"already-liked {vs.video_id}")
                    elif like_state == "unliked" and like_btn is not None:
                        try:
                            like_btn.dispatch_event("click", timeout=8000)
                            time.sleep(3)
                            state2, _ = _probe_like(page)
                            if state2 == "liked":
                                vs.liked = True
                                _bump_action(state, f"liked {vs.video_id} ({vs.channel})")
                        except Exception as e:  # noqa: BLE001
                            vs.error = f"like-click {type(e).__name__}: {e}"

                    _human_pause()

                    # SUBSCRIBE — only once per channel
                    if vs.channel not in subscribed_channels:
                        sub_state, sub_btn = _probe_subscribe(page)
                        if sub_state == "subscribed":
                            vs.subscribed = True
                            subscribed_channels.add(vs.channel)
                            _bump_action(state, f"already-subscribed to {vs.channel}")
                        elif sub_state == "unsubscribed" and sub_btn is not None:
                            try:
                                sub_btn.scroll_into_view_if_needed(timeout=4000)
                                time.sleep(0.4)
                                sub_btn.click(force=True, timeout=8000)
                                time.sleep(3)
                                state2, _ = _probe_subscribe(page)
                                if state2 == "subscribed":
                                    vs.subscribed = True
                                    subscribed_channels.add(vs.channel)
                                    _bump_action(state, f"subscribed to {vs.channel}")
                            except Exception as e:  # noqa: BLE001
                                vs.error = (vs.error + " | " if vs.error else "") + f"sub-click {type(e).__name__}: {e}"
                        _human_pause()
                except Exception as e:  # noqa: BLE001
                    vs.error = str(e)[:200]
                    _bump_action(state, f"error on {vs.video_id}: {vs.error}")
                _save_state(state)

            if _maybe_stop(state):
                state.phase = "stopped"
                _bump_action(state, "stop requested during engage phase")
                return 0

            # Phase 2: tab-cycling watch loop. Forever until stop signal.
            state.phase = "watching"
            _bump_action(state, "entering watch-loop (focus cycles every 20-45s)")
            pages = [pg for pg in ctx.pages if pg.url.startswith("http")]
            if not pages:
                state.phase = "failed"
                _bump_action(state, "no playable tabs left after engage phase")
                return 1

            page_to_idx: dict[Any, int] = {}
            for pg in pages:
                for i, vs in enumerate(state.videos):
                    if vs.url in pg.url:
                        page_to_idx[pg] = i
                        break

            while True:
                if _maybe_stop(state):
                    state.phase = "stopped"
                    _bump_action(state, "stop signal received — closing browser")
                    break
                pg = random.choice(pages)
                try:
                    pg.bring_to_front()
                    dwell = random.uniform(20.0, 45.0)
                    idx = page_to_idx.get(pg)
                    if idx is not None:
                        vs = state.videos[idx]
                        vs.last_focused_at = _now()
                        vs.watch_seconds += int(dwell)
                        _bump_action(
                            state,
                            f"watching {vs.video_id} ({vs.channel}) for {int(dwell)}s",
                        )
                    end = time.monotonic() + dwell
                    while time.monotonic() < end:
                        if _maybe_stop(state):
                            break
                        time.sleep(2.0)
                except Exception as e:  # noqa: BLE001
                    _bump_action(state, f"tab error (skipping): {e}")
                    pages = [p for p in pages if p is not pg]
                    if not pages:
                        state.phase = "failed"
                        _bump_action(state, "all tabs died")
                        break
    except Exception as e:  # noqa: BLE001
        state.phase = "failed"
        _bump_action(state, f"worker crashed: {e}")
        return 1
    finally:
        # Always tear Chrome down — leaving a Chrome-Debug instance
        # blocks the next run because of the singleton lock.
        time.sleep(2)
        try:
            chrome_proc.terminate()
            chrome_proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                chrome_proc.kill()
            except Exception:
                pass

    if state.phase != "failed":
        state.phase = "stopped"
        _bump_action(state, "worker exited cleanly")
    _stop_path(slug).unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="pipeline.cross_engage.burner_engage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List discovered burner channels")
    rp = sub.add_parser("run", help="Run engage worker for a burner")
    rp.add_argument("slug")
    rp.add_argument("--headless", action="store_true", help="Run Chrome headless (debug only — likely tripping bot detection).")
    sp = sub.add_parser("stop", help="Stop a running engage worker")
    sp.add_argument("slug")
    stp = sub.add_parser("status", help="Print live status JSON for a burner")
    stp.add_argument("slug")
    args = ap.parse_args()

    if args.cmd == "list":
        for b in list_burner_channels():
            mark = "✅" if b["profile_known"] else "⚠️ no email mapping"
            print(f"  {mark}  {b['slug']:20s}  {b['title']}  ({b['channel_id']})")
        return 0
    if args.cmd == "run":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return run(args.slug, headless=args.headless)
    if args.cmd == "stop":
        request_stop(args.slug)
        print(f"stop requested for {args.slug}")
        return 0
    if args.cmd == "status":
        s = read_state(args.slug)
        print(json.dumps(s, indent=2) if s else "(no state)")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
