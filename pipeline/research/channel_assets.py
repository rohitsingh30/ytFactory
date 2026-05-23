"""Mirror YouTube channel avatars + banners locally.

The studio UI shows the real channel face on every card (Channels gallery,
Create wizard, Library rows, Landing page). Hot-linking ``yt3.ggpht.com``
URLs from the Next.js origin is unreliable (CORS, occasional 403s) and
dependent on the live YT URL not changing. So we mirror once into:

    data/research/channel_assets/<account>/{avatar.jpg, banner.jpg}

served by ``/api/channels/{key}/{avatar,banner}.jpg`` (see
``control/channels_routes.py``). The downloader is idempotent — it
skips images that exist and have a non-empty body, and re-downloads
when the cached YT JSON points at a different URL.

CLI:
    python -m pipeline.research.channel_assets                # all accounts
    python -m pipeline.research.channel_assets --account ms…  # single
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Iterable

from pipeline.observability.event_helpers import inject_trace_headers, track_http_call
from pipeline.paths import RESEARCH_DIR

logger = logging.getLogger(__name__)

YOUTUBE_DIR = RESEARCH_DIR / "youtube"
ASSETS_DIR = RESEARCH_DIR / "channel_assets"


# Asset kind → (cache filename, channel-payload key)
_ASSETS: tuple[tuple[str, str], ...] = (
    ("avatar.jpg", "avatar_url"),
    ("banner.jpg", "banner_url"),
)


def _account_dir(account: str) -> Path:
    return ASSETS_DIR / account


def _source_state_path(account: str) -> Path:
    """Tracks which source URLs the cached files were last fetched from.

    Mirrors only re-download when the URL in ``data/research/youtube/<acct>.json``
    differs from the one we last cached. Keeps things idempotent without an
    HTTP HEAD round-trip.
    """
    return _account_dir(account) / ".source.json"


def _load_source_state(account: str) -> dict[str, str]:
    p = _source_state_path(account)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_source_state(account: str, state: dict[str, str]) -> None:
    p = _source_state_path(account)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def _download(url: str, dest: Path, timeout: float = 15.0) -> bool:
    """Stream ``url`` into ``dest``. Returns True iff the file was written.

    Best-effort: a single network failure logs and returns False — callers
    treat a missing local mirror as a fallback to monogram.
    """
    import requests  # local import keeps module import-time tiny

    try:
        headers: dict[str, str] = {}
        inject_trace_headers(headers)
        t0 = time.perf_counter()
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            track_http_call(
                service="channel_assets",
                method="GET",
                url=url,
                status_code=getattr(r, "status_code", 200),
                response_body=getattr(r, "headers", {}).get("content-length") or "",
                duration_ms=duration_ms,
            )
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            with tmp.open("wb") as fp:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fp.write(chunk)
            tmp.replace(dest)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort mirror
        track_http_call(
            service="channel_assets",
            method="GET",
            url=url,
            response_body=str(exc),
            success=False,
        )
        logger.warning("download failed for %s: %s", url, exc)
        # Drop a partial tmp on failure
        try:
            (dest.with_suffix(dest.suffix + ".tmp")).unlink(missing_ok=True)
        except OSError:
            pass
        return False


def download_for_account(account: str, *, quiet: bool = False) -> dict[str, bool]:
    """Mirror avatar + banner for one account.

    Reads URLs from ``data/research/youtube/<account>.json`` (so this
    runs after ``fetch_account`` has populated branding fields). Returns
    ``{"avatar": True/False, "banner": True/False}`` indicating which
    files exist on disk after the call.
    """
    cache_path = YOUTUBE_DIR / f"{account}.json"
    if not cache_path.exists():
        if not quiet:
            print(f"[assets] {account}: no cached YT JSON yet — run fetch first")
        return {"avatar": False, "banner": False}

    try:
        payload = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        if not quiet:
            print(f"[assets] {account}: cached JSON unreadable")
        return {"avatar": False, "banner": False}

    channel = payload.get("channel") or {}
    state = _load_source_state(account)
    next_state = dict(state)
    out = {"avatar": False, "banner": False}

    for fname, url_key in _ASSETS:
        kind = fname.split(".")[0]
        url = channel.get(url_key)
        dest = _account_dir(account) / fname
        if not url:
            # No URL from YT → leave any stale mirror in place (so a previous
            # successful pull stays usable) and just report whatever exists.
            out[kind] = dest.exists()
            continue
        # Skip download when the cached file already came from this URL.
        if dest.exists() and state.get(url_key) == url and dest.stat().st_size > 0:
            out[kind] = True
            continue
        ok = _download(url, dest)
        out[kind] = ok and dest.exists()
        if ok:
            next_state[url_key] = url
            if not quiet:
                size_kb = dest.stat().st_size // 1024
                print(f"[assets] {account}: {fname} ← {size_kb}KB")

    if next_state != state:
        _save_source_state(account, next_state)
    return out


def download_for_accounts(
    accounts: Iterable[str] | None = None, *, quiet: bool = False,
) -> dict[str, dict[str, bool]]:
    """Mirror assets for every account that has a cached YT JSON.

    Pass ``accounts`` to restrict; otherwise enumerates everything under
    ``data/research/youtube/*.json``.
    """
    if accounts is None:
        accounts = sorted(p.stem for p in YOUTUBE_DIR.glob("*.json"))
    summary: dict[str, dict[str, bool]] = {}
    for acct in accounts:
        summary[acct] = download_for_account(acct, quiet=quiet)
    return summary


def asset_path(account: str, kind: str) -> Path | None:
    """Return the local path for a mirrored asset, or None if missing.

    ``kind`` ∈ ``{"avatar", "banner"}``. The control plane uses this for
    the ``/api/channels/{key}/{avatar,banner}.jpg`` endpoints.
    """
    if kind not in {"avatar", "banner"}:
        return None
    p = _account_dir(account) / f"{kind}.jpg"
    return p if p.exists() and p.stat().st_size > 0 else None


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="pipeline.research.channel_assets")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--account",
        help="Mirror only this account (default: every account with cached YT JSON).",
    )
    args = ap.parse_args()

    if args.account:
        result = {args.account: download_for_account(args.account, quiet=args.quiet)}
    else:
        result = download_for_accounts(quiet=args.quiet)

    if args.quiet:
        print(json.dumps(result))
    else:
        for acct, status in result.items():
            print(f"  {acct}: avatar={status['avatar']} banner={status['banner']}")


if __name__ == "__main__":
    main()
