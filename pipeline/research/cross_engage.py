"""Cross-channel engagement — every owned channel likes/subscribes/views
every other owned channel's uploads.

Design:

- **Sibling discovery** is derived from the on-disk OAuth token cache
  (``~/.config/ytfactory/youtube_token_<account>.json``). Any account
  with a cached token is a sibling. Adding a new channel = OAuthing it
  once via ``pipeline.upload.authenticate(account=...)``.

- **Channel-ID registry** at ``~/.config/ytfactory/channel_ids.json``
  maps each account name → that account's YouTube channel ID. Resolved
  lazily via ``channels.list(mine=True)`` and cached so we don't burn
  quota on every upload.

- **Three engagement actions**:
    1. ``subscribe_pair(home_account, target_channel_id)`` — one-time
       sub. Idempotent (catches subscriptionDuplicate).
    2. ``like_video(home_account, video_id)`` — videos.rate("like").
       Idempotent at YouTube (re-rating same value is a no-op).
    3. ``play_view(video_url, duration_s=45)`` — Playwright headless
       Chromium tab, --mute-audio, navigates to the watch URL, sits for
       ~45s (YouTube counts a view at ~30s of playback), closes. No
       auth — anonymous view.

- **Hook**: ``engage_after_upload(uploader_account, video_id)`` is called
  from ``pipeline.upload.upload_short`` after a successful upload. It
  fires likes from every sibling + a single anonymous Playwright view,
  in a daemon thread so the upload return doesn't block.

Quota cost (per upload):
    - 1 channels.list per uncached sibling (~once per channel ever): 1u
    - N likes (videos.rate): 50u each → 4 siblings = 200u
    - 0u for the Playwright view (no API call)
    Total per upload: ~200u. Daily quota is 10k by default.

Subscribes are one-time at setup, not per-upload — run
``python -m pipeline.cross_engage subscribe-all`` once after adding a
new channel.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.upload import CONFIG_DIR, _token_path, authenticate


CHANNEL_IDS_PATH = CONFIG_DIR / "channel_ids.json"

# Accounts to skip even if a token file exists (e.g. dev placeholders,
# backup tokens). The .fluted-backup suffix is filtered separately.
_EXCLUDE_ACCOUNTS = {"default"}


def _is_real_token(path: Path) -> bool:
    name = path.name
    if not name.startswith("youtube_token_"):
        return False
    if not name.endswith(".json"):
        return False
    if ".fluted-backup" in name:
        return False
    return True


def list_sibling_accounts() -> list[str]:
    """All ytFactory accounts with a cached OAuth token.

    Returned in stable sorted order so subscribe-all and engagement
    fan-out are deterministic across runs.

    **Audit T1.7 — cloud-blind discovery.** Pre-fix this only
    listed laptop ``CONFIG_DIR`` (``~/.config/ytfactory/``). On
    Cloud Run that dir doesn't exist → returned ``[]`` → the whole
    cross-engagement layer was silently dead in cloud-upload mode
    (no likes, no subscribes, no Playwright views from siblings).
    Now also enumerates ``/secrets/youtube-token-*/value`` Cloud
    Run secret mounts (per CLAUDE.md "every per-channel OAuth
    refresh token lives at /secrets/youtube-token-<account>/value")
    and unions both sources so the same code works on laptop and
    on Cloud Run without env switching.
    """
    accounts: set[str] = set()
    if CONFIG_DIR.exists():
        for p in CONFIG_DIR.glob("youtube_token_*.json"):
            if not _is_real_token(p):
                continue
            accounts.add(p.stem[len("youtube_token_"):])
    # Audit T1.7: also enumerate Cloud Run secret mounts.
    secrets_root = Path("/secrets")
    if secrets_root.exists():
        for p in secrets_root.glob("youtube-token-*"):
            if not p.is_dir():
                continue
            if not (p / "value").exists():
                continue
            account = p.name[len("youtube-token-"):]
            if account:
                accounts.add(account)
    return sorted(a for a in accounts if a not in _EXCLUDE_ACCOUNTS)


# ---- channel-ID registry -----------------------------------------------


def _load_registry() -> dict[str, dict[str, Any]]:
    if not CHANNEL_IDS_PATH.exists():
        return {}
    try:
        return json.loads(CHANNEL_IDS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_registry() -> dict[str, dict[str, Any]]:
    """Public alias for ``_load_registry``.

    Audit S1.9 — cross-module callers (``web/server.py::youtube_auth_status``)
    were reaching into the underscore-prefixed helper, which is brittle:
    a refactor that renames or restructures the registry storage would
    silently break those importers. The public alias gives external
    callers a stable name; the underscore version stays as the internal
    in-module entry point so existing tests that monkey-patch
    ``cross_engage._load_registry`` keep working.
    """
    return _load_registry()


def _save_registry(reg: dict[str, dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CHANNEL_IDS_PATH.write_text(json.dumps(reg, indent=2, sort_keys=True))


def save_registry(reg: dict[str, dict[str, Any]]) -> None:
    """Public alias for ``_save_registry`` — see ``load_registry`` for
    the rationale (audit S1.9)."""
    _save_registry(reg)


def resolve_channel_id(account: str, *, force: bool = False) -> dict[str, Any]:
    """Look up (and cache) the YouTube channel ID for ``account``.

    Returns ``{"channel_id": "UCxxx", "title": "...", "discovered_at": ...}``.
    Costs 1 API unit on cache miss; 0 on hit.
    """
    reg = _load_registry()
    if not force and account in reg and reg[account].get("channel_id"):
        return reg[account]

    from googleapiclient.discovery import build

    creds = authenticate(account, interactive=False)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(part="id,snippet", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(
            f"channels.list returned no items for account={account!r}; "
            f"is the OAuth account linked to a YouTube channel?"
        )
    item = items[0]
    entry = {
        "channel_id": item["id"],
        "title": (item.get("snippet") or {}).get("title", ""),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    reg[account] = entry
    _save_registry(reg)
    return entry


def refresh_all_channel_ids() -> dict[str, dict[str, Any]]:
    """Resolve channel IDs for every sibling account, force-refresh."""
    accounts = list_sibling_accounts()
    out: dict[str, dict[str, Any]] = {}
    for a in accounts:
        try:
            out[a] = resolve_channel_id(a, force=True)
            print(f"[cross_engage] {a} → {out[a]['channel_id']} ({out[a]['title']!r})")
        except Exception as e:
            print(f"[cross_engage] {a} channel-id lookup failed: {e}")
    return out


# ---- subscribe ---------------------------------------------------------


def subscribe_pair(home_account: str, target_channel_id: str) -> dict[str, Any]:
    """``home_account`` subscribes to ``target_channel_id``. Idempotent."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = authenticate(home_account, interactive=False)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "resourceId": {"kind": "youtube#channel", "channelId": target_channel_id}
        }
    }
    try:
        resp = yt.subscriptions().insert(part="snippet", body=body).execute()
        return {"status": "subscribed", "subscription_id": resp.get("id")}
    except HttpError as e:
        # Already subscribed — fine. Surface other errors.
        body_text = ""
        try:
            body_text = e.content.decode("utf-8", "ignore")
        except Exception:
            pass
        if "subscriptionDuplicate" in body_text or e.resp.status == 400:
            return {"status": "already_subscribed"}
        raise


def subscribe_all_pairs() -> list[dict[str, Any]]:
    """Cross-subscribe every sibling pair. Run once at setup time, plus
    again after adding a new channel.
    """
    accounts = list_sibling_accounts()
    if len(accounts) < 2:
        print(f"[cross_engage] only {len(accounts)} sibling(s); nothing to do")
        return []

    # Resolve channel IDs first so we don't bail mid-fan-out.
    ids: dict[str, str] = {}
    for a in accounts:
        try:
            ids[a] = resolve_channel_id(a)["channel_id"]
        except Exception as e:
            print(f"[cross_engage] skip {a}: channel-id lookup failed ({e})")
    accounts = [a for a in accounts if a in ids]

    results: list[dict[str, Any]] = []
    for home in accounts:
        for target in accounts:
            if home == target:
                continue
            try:
                r = subscribe_pair(home, ids[target])
                results.append({"home": home, "target": target, **r})
                print(f"[cross_engage] {home} → {target}: {r['status']}")
            except Exception as e:
                results.append({"home": home, "target": target, "error": str(e)})
                print(f"[cross_engage] {home} → {target}: ERROR {e}")
    return results


# ---- like --------------------------------------------------------------


def like_video(home_account: str, video_id: str) -> dict[str, Any]:
    """``home_account`` likes ``video_id``. Idempotent (re-rate is no-op)."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = authenticate(home_account, interactive=False)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    try:
        yt.videos().rate(id=video_id, rating="like").execute()
        return {"status": "liked", "account": home_account, "video_id": video_id}
    except HttpError as e:
        return {
            "status": "error",
            "account": home_account,
            "video_id": video_id,
            "error": f"{e.resp.status} {e}",
        }


def like_from_siblings(uploader_account: str, video_id: str) -> list[dict[str, Any]]:
    """Every sibling channel except the uploader likes the video."""
    siblings = [a for a in list_sibling_accounts() if a != uploader_account]
    out: list[dict[str, Any]] = []
    for s in siblings:
        r = like_video(s, video_id)
        out.append(r)
        marker = "✓" if r.get("status") == "liked" else "✗"
        print(f"[cross_engage] like {marker} {s} → {video_id} ({r.get('status')})")
    return out


# ---- backfill (existing uploads) --------------------------------------


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def discover_existing_uploads() -> list[dict[str, Any]]:
    """Walk every sibling channel's ``<slug>/uploads/*.json`` and return
    a flat list of ``{account, channel_dir, slug, video_id}`` records.

    The ``account`` field is the uploader's OAuth account name (read
    from each upload record). Falls back to the directory name if the
    record predates account tracking.
    """
    root = _project_root()
    out: list[dict[str, Any]] = []
    accounts = set(list_sibling_accounts())
    for channel_dir in sorted(p.name for p in root.iterdir() if p.is_dir()):
        ud = root / channel_dir / "uploads"
        if not ud.is_dir():
            continue
        for f in sorted(ud.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            vid = rec.get("video_id")
            if not vid:
                continue
            account = rec.get("account") or channel_dir
            if account not in accounts:
                # Account name in the upload record doesn't match any
                # cached OAuth token. Skip cleanly.
                continue
            out.append(
                {
                    "account": account,
                    "channel_dir": channel_dir,
                    "slug": rec.get("slug") or f.stem,
                    "video_id": vid,
                    "url": rec.get("url"),
                }
            )
    return out


def backfill_engagement(
    *,
    include_self: bool = True,
    include_views: bool = False,
    view_seconds: int = 35,
) -> dict[str, Any]:
    """For every existing upload, every sibling channel likes it.

    With ``include_self=True`` the uploader account also likes its own
    video — YouTube allows channel-owner self-likes and they count.
    With ``include_views=True`` each video also gets one anonymous
    Playwright view (~35s playback).

    Idempotent at YouTube — re-running is safe.
    """
    siblings = list_sibling_accounts()
    uploads = discover_existing_uploads()
    print(
        f"[cross_engage] backfill: {len(uploads)} videos × "
        f"{len(siblings) if include_self else len(siblings)-1} likers"
    )
    like_results: list[dict[str, Any]] = []
    view_results: list[dict[str, Any]] = []
    for u in uploads:
        likers = list(siblings) if include_self else [s for s in siblings if s != u["account"]]
        for liker in likers:
            r = like_video(liker, u["video_id"])
            like_results.append({**u, "liker": liker, **r})
            marker = "✓" if r.get("status") == "liked" else "✗"
            print(
                f"[cross_engage] like {marker} {liker} → "
                f"{u['channel_dir']}/{u['slug']} ({u['video_id']}) "
                f"[{r.get('status')}]"
            )
        if include_views:
            v = play_view(u["video_id"], duration_s=view_seconds)
            view_results.append({**u, **v})
            print(
                f"[cross_engage] view {v.get('status')} {u['video_id']} "
                f"playback={v.get('playback_s')}s"
            )
    return {
        "uploads_scanned": len(uploads),
        "siblings": siblings,
        "include_self": include_self,
        "include_views": include_views,
        "likes": like_results,
        "views": view_results,
    }


# ---- play view (Playwright) -------------------------------------------


def play_view(
    video_id: str,
    *,
    duration_s: int = 45,
    headless: bool = True,
) -> dict[str, Any]:
    """Open the YouTube watch URL in a muted Chromium tab and let it play.

    YouTube counts a "view" at roughly 30s of playback; we sit for 45s
    by default to give it slack. Anonymous (no login) — we don't want a
    pattern of every channel's account watching every other channel,
    and a public anonymous view is what a viewer would actually do.

    Headless YouTube has gotten harder over time:
      - The chromium-headless-shell build is detectable and blocks
        autoplay even with --autoplay-policy=no-user-gesture-required.
      - Click-based play has to dodge the ambient cookie / sign-in
        banners which differ by region.

    **Audit T1.21 — default is now headless=True.** Pre-fix the default
    was headless=False even though the docstring (mis-)claimed
    "we default to True for server use". On Cloud Run / any
    no-display environment, headless=False fails with
    ``Missing X server or $DISPLAY`` and burned ~30s before the
    daemon thread surrendered. Laptop callers that want a visible
    browser still pass ``headless=False`` explicitly.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return {
            "status": "error",
            "error": f"playwright not installed ({e}); run `pip install playwright && playwright install chromium`",
        }

    url = f"https://www.youtube.com/watch?v={video_id}"
    started = time.time()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=headless,
                args=[
                    "--mute-audio",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Dismiss EU cookie banner / sign-in interstitial if shown.
            for sel in (
                "button:has-text('Accept all')",
                "button:has-text('Reject all')",
                "button[aria-label*='Accept']",
                "tp-yt-paper-button:has-text('Accept all')",
            ):
                try:
                    page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    continue

            # Wait for the <video> element + click the page to deliver a
            # user-gesture (unblocks autoplay even when --autoplay-policy
            # didn't take). Then click the visible play button if it's
            # still showing.
            try:
                page.wait_for_selector("video", timeout=10000)
            except Exception:
                pass
            try:
                page.locator("video").first.click(timeout=3000)
            except Exception:
                pass
            for sel in (
                "button.ytp-large-play-button",
                "button.ytp-play-button",
                ".ytp-large-play-button",
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=1000):
                        btn.click(timeout=2000)
                        break
                except Exception:
                    continue

            # Force-mute + force-play defensively.
            try:
                page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "if (v) { v.muted = true; v.play().catch(()=>{}); } }"
                )
            except Exception:
                pass

            time.sleep(3)
            mid_ct: float | None = None
            try:
                mid_ct = page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "return v ? v.currentTime : null; }"
                )
            except Exception:
                pass
            time.sleep(max(duration_s - 3, 0))
            try:
                ct = page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "return v ? v.currentTime : null; }"
                )
            except Exception:
                ct = None
            context.close()
            browser.close()
        return {
            "status": "viewed",
            "video_id": video_id,
            "wall_s": round(time.time() - started, 1),
            "playback_s": ct,
            "mid_playback_s": mid_ct,
        }
    except Exception as e:
        return {
            "status": "error",
            "video_id": video_id,
            "error": str(e),
            "wall_s": round(time.time() - started, 1),
        }


# ---- the upload-time hook ---------------------------------------------


def _engage_blocking(uploader_account: str, video_id: str) -> dict[str, Any]:
    """Likes from every sibling + one anonymous Playwright view.

    All exceptions are swallowed and returned as the dict result —
    this function is the worker for a daemon thread, so an unhandled
    exception would surface as a noisy ``PytestUnhandledThreadException``
    or print a long traceback in production. The cross-engage path is
    explicitly best-effort (engagement failures must never block the
    upload), so degrading gracefully here is the correct contract.
    """
    out: dict[str, Any] = {"likes": [], "view": None, "error": None}
    try:
        out["likes"] = like_from_siblings(uploader_account, video_id)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"like_from_siblings: {e}"
    try:
        out["view"] = play_view(video_id)
    except Exception as e:  # noqa: BLE001
        # Don't overwrite an earlier error — preserve the first one.
        out["error"] = out["error"] or f"play_view: {e}"
    return out


def engage_after_upload(
    uploader_account: str,
    video_id: str,
    *,
    background: bool = True,
) -> threading.Thread | dict[str, Any]:
    """Fire likes + view for ``video_id`` from every sibling.

    By default runs in a daemon thread so the calling upload returns
    immediately. Pass ``background=False`` for synchronous behaviour
    (CLI / testing).

    Disable globally by setting env ``YTFACTORY_CROSS_ENGAGE=0``.
    """
    if os.environ.get("YTFACTORY_CROSS_ENGAGE", "1") == "0":
        print(f"[cross_engage] disabled via env; skipping for {video_id}")
        return {"status": "disabled"}

    if background:
        t = threading.Thread(
            target=_engage_blocking,
            args=(uploader_account, video_id),
            name=f"cross_engage:{video_id}",
            daemon=True,
        )
        t.start()
        print(
            f"[cross_engage] dispatched (uploader={uploader_account}, "
            f"video_id={video_id}, siblings={len(list_sibling_accounts())-1})"
        )
        return t
    return _engage_blocking(uploader_account, video_id)


# ---- CLI ---------------------------------------------------------------


def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="pipeline.cross_engage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list sibling accounts and resolved channel IDs")
    sub.add_parser(
        "reauth-all",
        help="run the browser OAuth flow for every sibling (use after expanding SCOPES)",
    )
    sub.add_parser("refresh-ids", help="re-resolve channel IDs for every sibling")
    sub.add_parser("subscribe-all", help="cross-subscribe every owned channel pair")

    sp_like = sub.add_parser("like", help="like a video from every sibling except uploader")
    sp_like.add_argument("video_id")
    sp_like.add_argument(
        "--uploader",
        default="",
        help="uploader account (excluded from likes); default: empty = like from ALL siblings",
    )

    sp_view = sub.add_parser("view", help="open a video in a muted Playwright tab")
    sp_view.add_argument("video_id")
    sp_view.add_argument("--seconds", type=int, default=45)
    sp_view.add_argument("--headed", action="store_true", help="non-headless (debug)")

    sp_engage = sub.add_parser(
        "engage",
        help="full fan-out: every sibling likes + one anonymous Playwright view",
    )
    sp_engage.add_argument("video_id")
    sp_engage.add_argument("--uploader", required=True)

    sp_bf = sub.add_parser(
        "backfill",
        help="like every existing upload from every sibling (idempotent)",
    )
    sp_bf.add_argument(
        "--no-self",
        action="store_true",
        help="exclude the uploader from its own video's likers",
    )
    sp_bf.add_argument(
        "--views",
        action="store_true",
        help="also play each video for view-count seeding (slow: ~35s/video)",
    )
    sp_bf.add_argument("--view-seconds", type=int, default=35)

    args = p.parse_args()

    if args.cmd == "list":
        accounts = list_sibling_accounts()
        reg = _load_registry()
        for a in accounts:
            entry = reg.get(a) or {}
            cid = entry.get("channel_id") or "(unresolved — run refresh-ids)"
            title = entry.get("title") or ""
            print(f"  {a:<24} {cid}  {title}")
        return 0

    if args.cmd == "reauth-all":
        for a in list_sibling_accounts():
            print(f"\n[cross_engage] reauth → {a}")
            try:
                authenticate(a, interactive=True)
                print(f"[cross_engage] {a} ✓")
            except Exception as e:
                print(f"[cross_engage] {a} FAILED: {e}")
        return 0

    if args.cmd == "refresh-ids":
        refresh_all_channel_ids()
        return 0

    if args.cmd == "subscribe-all":
        subscribe_all_pairs()
        return 0

    if args.cmd == "like":
        like_from_siblings(args.uploader, args.video_id)
        return 0

    if args.cmd == "view":
        r = play_view(args.video_id, duration_s=args.seconds, headless=not args.headed)
        print(json.dumps(r, indent=2))
        return 0 if r.get("status") == "viewed" else 1

    if args.cmd == "engage":
        r = engage_after_upload(args.uploader, args.video_id, background=False)
        print(json.dumps(r, indent=2, default=str))
        return 0

    if args.cmd == "backfill":
        r = backfill_engagement(
            include_self=not args.no_self,
            include_views=args.views,
            view_seconds=args.view_seconds,
        )
        ok = sum(1 for x in r["likes"] if x.get("status") == "liked")
        print(
            f"[cross_engage] backfill done — "
            f"{ok}/{len(r['likes'])} likes succeeded "
            f"across {r['uploads_scanned']} uploads"
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
