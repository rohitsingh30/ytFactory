"""YouTube-driven enumeration + stats fetcher for the research dashboard.

Source of truth is the live YouTube channel, not local mp4s. For each
authenticated upload account (one per ``<channel>/config.yaml``):

  - ``channels.list(mine=True)`` → channel meta + uploads playlist ID
    + subscriber/view/video count
  - paginate ``playlistItems.list(playlistId=<uploads>)`` → every
    video on that channel
  - batch ``videos.list(id=…)`` 50 at a time → per-video snippet,
    statistics, contentDetails, status

Cache shape:

    data/research/youtube/<account>.json
        {
          "fetched_at": "<iso>",
          "channel": { id, title, subscriber_count, view_count,
                       video_count, hidden_subscribers, uploads_playlist },
          "videos":  [ { video_id, title, description, published_at,
                         privacy, duration_s, thumbnail_url, url,
                         view_count, like_count, comment_count,
                         favorite_count }, ... ]
        }

CTR + retention require the youtubeAnalytics API and a separate
``yt-analytics.readonly`` scope — not wired here.

Storage backend (2026-05-10 — token-issue permanent fix, plan B1):

  When ``YTFACTORY_STATE_BUCKET`` env is set (cloud), the cache lives
  in ``gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json``
  instead of on the local filesystem. ``fetch_account`` writes there;
  ``load_account`` / ``load_all_cached`` read from there. Laptop dev
  with the env unset keeps using ``YOUTUBE_DIR`` on the local FS so
  offline workflows work unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.paths import PROJECT_ROOT, RESEARCH_DIR

# Local alias so tests + monkey-patching keep working (some test fixtures
# rebind ``pipeline.research.youtube.YOUTUBE_DIR`` directly). New code
# should prefer ``pipeline.paths.RESEARCH_DIR`` for the canonical root.
YOUTUBE_DIR = RESEARCH_DIR / "youtube"

# Relative key prefix used by both the FS and GCS storage backends. Kept
# as a module constant so the GCS layout mirrors the FS layout one-for-one.
_CACHE_KEY_PREFIX = "data/research/youtube"

logger = logging.getLogger(__name__)


# ---- storage backend (FS or GCS) ---------------------------------------
#
# A tiny indirection so the rest of this module doesn't care whether the
# cache lives on disk or in a GCS bucket. Three operations are needed:
# load JSON, save JSON, list account slugs. Read-side adds a small mtime-
# keyed in-process cache so dashboard polls don't hammer GCS.


def _state_bucket() -> str | None:
    """GCS bucket name when running in cloud, else None.

    Resolved on every call so tests can flip the env mid-session.
    """
    bucket = os.environ.get("YTFACTORY_STATE_BUCKET")
    return bucket or None


def _gcs_blob(bucket: str, key: str):
    """Lazy-imports google-cloud-storage so laptop dev doesn't need it.

    Returns a ``google.cloud.storage.Blob`` for ``gs://bucket/key``.
    """
    from google.cloud import storage  # noqa: PLC0415

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
    client = storage.Client(project=project)
    return client.bucket(bucket).blob(key)


# In-process read cache: {account: (generation_or_mtime, payload)}. Cheap
# guard against the dashboard hammering GCS on every poll. Cleared by
# every successful save so writers don't read stale.
_READ_CACHE: dict[str, tuple[Any, dict]] = {}

# Skip the GCS HEAD round-trip (`blob.reload()`) for this many seconds
# after a successful read. The dashboard polls /api/channels and
# /api/dashboard/videos every ~10–30 s — without this, EACH poll for
# EACH channel paid a sequential GCS HEAD just to confirm "yep, same
# generation, return the in-process cached payload anyway".
#
# 8 channels × 2 endpoints × ~30 ms HEAD = ~500 ms wasted per poll
# tick BEFORE any actual work. With the fast-path the warm cost
# collapses to a dict lookup.
#
# A 60 s window is comfortably shorter than any realistic publish
# cadence (the youtube research cache only refreshes once a day) AND
# the writer side (`_save_cache`) clears the entry explicitly so
# fresh writes never wait the full TTL.
_READ_CACHE_TTL_S: float = 60.0
# Parallel cache of "when did we last verify this entry" so the
# fast-path can skip without reaching for time.time() on every read.
# {account: monotonic_seconds_when_verified}
_READ_CACHE_VERIFIED_AT: dict[str, float] = {}


def _save_cache(account: str, payload: dict) -> None:
    """Write ``payload`` to ``<account>.json`` in the active backend."""
    bucket = _state_bucket()
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if bucket:
        key = f"{_CACHE_KEY_PREFIX}/{account}.json"
        try:
            blob = _gcs_blob(bucket, key)
            blob.upload_from_string(body, content_type="application/json")
            logger.info("youtube cache: wrote gs://%s/%s (%d bytes)", bucket, key, len(body))
        except Exception:
            logger.exception("youtube cache: GCS write failed for %s", account)
            raise
    else:
        YOUTUBE_DIR.mkdir(parents=True, exist_ok=True)
        (YOUTUBE_DIR / f"{account}.json").write_text(body)
    _READ_CACHE.pop(account, None)
    _READ_CACHE_VERIFIED_AT.pop(account, None)


def _load_cache(account: str) -> dict | None:
    """Read ``<account>.json`` from the active backend, or None if absent.

    GCS reads are cached in-process keyed by blob generation; subsequent
    polls within the same process hit memory until a write bumps the
    generation. FS reads are cached by mtime.

    Performance fast-path (2026-05-10):
      The dashboard polls /api/channels every ~10–30 s and that path
      calls this function once per channel. The HEAD round-trip
      (``blob.reload()``) used to fire on every call even when the
      cached payload was identical — pure overhead.

      We now skip the HEAD entirely when the in-process cache entry
      was last verified within ``_READ_CACHE_TTL_S``. Cache writers
      (`_save_cache`) clear `_READ_CACHE_VERIFIED_AT` so a fresh write
      forces a re-read on the next call instead of waiting the full
      TTL window.
    """
    import time as _time  # noqa: PLC0415 — module-level "import time" already exists below

    bucket = _state_bucket()
    if bucket:
        cached = _READ_CACHE.get(account)
        verified_at = _READ_CACHE_VERIFIED_AT.get(account)
        if cached is not None and verified_at is not None:
            if (_time.monotonic() - verified_at) < _READ_CACHE_TTL_S:
                return cached[1]
        key = f"{_CACHE_KEY_PREFIX}/{account}.json"
        try:
            blob = _gcs_blob(bucket, key)
            blob.reload()  # populates blob.generation + checks existence
        except Exception as exc:
            # 404 is a clean miss; everything else logs but degrades to None.
            from google.api_core import exceptions as gax_exc  # noqa: PLC0415

            if isinstance(exc, gax_exc.NotFound):
                return None
            logger.warning("youtube cache: GCS read failed for %s: %s", account, exc)
            return None
        if cached and cached[0] == blob.generation:
            # Generation matched — refresh "verified at" so subsequent
            # polls within the TTL hit the fast-path above.
            _READ_CACHE_VERIFIED_AT[account] = _time.monotonic()
            return cached[1]
        try:
            raw = blob.download_as_bytes()
        except Exception as exc:
            logger.warning("youtube cache: GCS download failed for %s: %s", account, exc)
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("youtube cache: GCS blob for %s is not valid JSON", account)
            return None
        _READ_CACHE[account] = (blob.generation, payload)
        _READ_CACHE_VERIFIED_AT[account] = _time.monotonic()
        return payload
    # FS path
    p = YOUTUBE_DIR / f"{account}.json"
    if not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    cached = _READ_CACHE.get(account)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    _READ_CACHE[account] = (mtime, payload)
    return payload


def _list_cached_accounts() -> list[str]:
    """Account slugs that have a cache entry in the active backend."""
    bucket = _state_bucket()
    if bucket:
        try:
            from google.cloud import storage  # noqa: PLC0415

            project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
            client = storage.Client(project=project)
            prefix = f"{_CACHE_KEY_PREFIX}/"
            out: list[str] = []
            for blob in client.list_blobs(bucket, prefix=prefix):
                name = blob.name[len(prefix):]
                if "/" in name or not name.endswith(".json"):
                    continue
                out.append(name[:-len(".json")])
            return sorted(out)
        except Exception as exc:
            logger.warning("youtube cache: GCS list failed: %s", exc)
            return []
    if not YOUTUBE_DIR.exists():
        return []
    return sorted(p.stem for p in YOUTUBE_DIR.glob("*.json"))


# ---- channel discovery --------------------------------------------------


# Top-level dirs that are not channel dirs (and so must be skipped when
# enumerating <channel>/config.yaml in the legacy laptop layout).
_NON_CHANNEL_DIRS = {
    "pipeline", "web", "web-next", "data", "docs", "tests", "scripts",
    "workers", "control", "cloud", "node_modules", "venv", ".venv", ".git",
}


# Centralised channel registry — post-2026-05-10 hygiene pass moved every
# channel YAML here. Cloud (which has no per-channel root dirs in the
# image) MUST find accounts via this path; the laptop walks both so old
# checkouts that still have per-channel dirs keep working.
#
# Resolved fresh on every iter_channel_configs() call so that tests which
# monkey-patch ``PROJECT_ROOT`` cascade transparently to the registry path
# — see tests/test_research_youtube.py::IterChannelConfigsTest.
CHANNELS_REGISTRY_DIR = PROJECT_ROOT / "pipeline" / "channels"


def iter_channel_configs() -> list[tuple[str, str]]:
    """Yield (account, channel_dir_name) for every channel config YAML.

    Discovery sources, in order:

    1. ``pipeline/channels/*.yaml`` — the canonical centralised registry
       (works on cloud where there are no per-channel root dirs).
    2. ``<PROJECT_ROOT>/<slug>/config.yaml`` — legacy laptop layout, kept
       for back-compat with checkouts that still have per-channel dirs.

    Both sources are walked and their results merged; the centralised
    entry wins on duplicate ``channel_dir_name``. ``account`` is taken
    from the YAML's ``upload.account`` (the OAuth keying), falling back
    to the channel slug. Channels with no readable YAML are skipped.
    """
    import yaml

    seen_dirs: set[str] = set()
    out: list[tuple[str, str]] = []

    # Resolved at call-time so monkey-patching ``PROJECT_ROOT`` in tests
    # cascades automatically (the legacy module-level constant
    # ``CHANNELS_REGISTRY_DIR`` is just a documentation alias).
    registry_dir = PROJECT_ROOT / "pipeline" / "channels"

    # 1) centralised registry (canonical on cloud + post-hygiene laptop)
    if registry_dir.is_dir():
        for cfg_path in sorted(registry_dir.glob("*.yaml")):
            slug = cfg_path.stem
            try:
                cfg = yaml.safe_load(cfg_path.read_text()) or {}
            except (OSError, yaml.YAMLError):
                cfg = {}
            account = ((cfg.get("upload") or {}).get("account")) or slug
            seen_dirs.add(slug)
            out.append((account, slug))

    # 2) legacy per-channel root dirs (laptop back-compat only)
    if PROJECT_ROOT.is_dir():
        for d in sorted(PROJECT_ROOT.iterdir()):
            if not d.is_dir() or d.name in _NON_CHANNEL_DIRS or d.name.startswith("."):
                continue
            if d.name in seen_dirs:
                continue
            cfg_path = d / "config.yaml"
            if not cfg_path.exists():
                continue
            try:
                cfg = yaml.safe_load(cfg_path.read_text()) or {}
            except (OSError, yaml.YAMLError):
                cfg = {}
            account = ((cfg.get("upload") or {}).get("account")) or d.name
            out.append((account, d.name))

    return out


# ---- API helpers --------------------------------------------------------


def _build_youtube(account: str):
    """Returns an authenticated youtube v3 client, or None on auth fail.

    Imports ``authenticate`` from ``pipeline.upload`` (the package).
    The package uses ``__getattr__`` to resolve attributes lazily on
    every access, so ``patch.object(pipeline.upload.upload,
    'authenticate', ...)`` and direct rebinds of either
    ``pipeline.upload.authenticate`` or
    ``pipeline.upload.upload.authenticate`` are honoured.
    """
    from googleapiclient.discovery import build

    from pipeline.upload import authenticate  # resolves via package __getattr__

    try:
        creds = authenticate(account=account, interactive=False)
    except Exception:
        return None
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _fetch_channel(youtube) -> dict | None:
    """``channels.list?mine=true`` — channel meta + uploads playlist."""
    from googleapiclient.errors import HttpError

    try:
        resp = youtube.channels().list(
            part="snippet,statistics,contentDetails", mine=True,
        ).execute()
    except HttpError:
        return None
    items = resp.get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "subscriber_count": (
            int(stats["subscriberCount"]) if "subscriberCount" in stats else None
        ),
        "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
        "video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
        "uploads_playlist": related.get("uploads"),
    }


def _fetch_uploads_playlist(youtube, playlist_id: str) -> list[str]:
    """Paginate playlistItems.list, return every video_id on the channel."""
    from googleapiclient.errors import HttpError

    if not playlist_id:
        return []
    ids: list[str] = []
    page_token: str | None = None
    while True:
        try:
            resp = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            ).execute()
        except HttpError:
            break
        for it in resp.get("items") or []:
            vid = (it.get("contentDetails") or {}).get("videoId")
            if vid:
                ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _parse_iso8601_duration(s: str | None) -> int | None:
    """Convert ISO-8601 PT#H#M#S to seconds. Returns None on bad input."""
    if not s or not s.startswith("PT"):
        return None
    rest = s[2:]
    total = 0
    num = ""
    for ch in rest:
        if ch.isdigit():
            num += ch
            continue
        if not num:
            return None
        n = int(num)
        if ch == "H":
            total += n * 3600
        elif ch == "M":
            total += n * 60
        elif ch == "S":
            total += n
        else:
            return None
        num = ""
    return total if not num else None


def _fetch_videos_batch(youtube, video_ids: list[str]) -> dict[str, dict]:
    """Returns {video_id: full row}. Batches at 50 ids per call."""
    from googleapiclient.errors import HttpError

    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        try:
            resp = youtube.videos().list(
                part="snippet,statistics,contentDetails,status",
                id=",".join(chunk),
            ).execute()
        except HttpError:
            continue
        for item in resp.get("items") or []:
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            details = item.get("contentDetails") or {}
            status = item.get("status") or {}
            thumbs = snippet.get("thumbnails") or {}
            best = (
                thumbs.get("maxres") or thumbs.get("standard")
                or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
            )
            vid = item["id"]
            out[vid] = {
                "video_id": vid,
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "published_at": snippet.get("publishedAt"),
                "channel_id": snippet.get("channelId"),
                "channel_title": snippet.get("channelTitle"),
                "tags": snippet.get("tags") or [],
                "category_id": snippet.get("categoryId"),
                "thumbnail_url": best.get("url"),
                "duration_s": _parse_iso8601_duration(details.get("duration")),
                "privacy": status.get("privacyStatus"),
                "made_for_kids": status.get("madeForKids"),
                "url": f"https://youtu.be/{vid}",
                "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
                "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
                "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
                "favorite_count": (
                    int(stats["favoriteCount"]) if "favoriteCount" in stats else None
                ),
            }
    return out


# ---- per-account fetch + cache -----------------------------------------


def _assert_safe_to_write(account: str) -> None:
    """Belt-and-braces guard: refuse to write into the real cache during tests.

    Born from the 2026-05-10 token-issue post-mortem (see
    ``tests/conftest.py`` for the test-suite-wide protection). If a test
    forgets to monkey-patch ``YOUTUBE_DIR`` AND uses fake YouTube fixtures,
    a real ``fetch_account()`` call would otherwise corrupt the laptop
    cache (and via git → the prod image). This guard turns that silent
    corruption into a loud ``RuntimeError`` instead.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _state_bucket():
        # GCS path — fake clients are responsible for not actually hitting
        # production buckets; we don't second-guess.
        return
    # Resolve the *canonical* paths fresh from pipeline.paths so a test
    # that monkey-patches our module-level ``RESEARCH_DIR`` doesn't fool
    # the guard into thinking tmp == real.
    from pipeline.paths import PROJECT_ROOT as _PR, RESEARCH_DIR as _RR  # noqa: PLC0415

    real_dir = (_RR / "youtube").resolve()
    try:
        target = YOUTUBE_DIR.resolve()
    except OSError:
        return
    if target == real_dir:
        raise RuntimeError(
            f"Refusing to write fetch_account({account!r}) into the real "
            f"data/research/youtube/ during a pytest run — your test fixture "
            f"forgot to monkey-patch pipeline.research.youtube.YOUTUBE_DIR "
            f"to a tmp path. See tests/conftest.py::isolate_research_dirs."
        )


def fetch_account(account: str, *, quiet: bool = False) -> dict | None:
    """Fetch channel meta + every uploaded video for one account.

    Storage backend (laptop FS vs. GCS) is decided by ``YTFACTORY_STATE_BUCKET``
    — see :func:`_save_cache`. Returns None on auth/API failure (caller
    treats as "channel not refreshable yet").
    """
    _assert_safe_to_write(account)
    youtube = _build_youtube(account)
    if youtube is None:
        if not quiet:
            print(f"[stats] {account}: auth failed — skipping")
        return None

    channel = _fetch_channel(youtube)
    if not channel:
        if not quiet:
            print(f"[stats] {account}: channels.list returned nothing — skipping")
        return None

    video_ids = _fetch_uploads_playlist(youtube, channel.get("uploads_playlist") or "")
    videos_by_id = _fetch_videos_batch(youtube, video_ids) if video_ids else {}

    # Preserve playlist order (most-recent first per YouTube convention).
    videos = [videos_by_id[v] for v in video_ids if v in videos_by_id]

    payload = {
        "account": account,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "channel": channel,
        "videos": videos,
    }
    _save_cache(account, payload)
    if not quiet:
        print(f"[stats] {account}: {len(videos)} videos · {channel.get('subscriber_count')} subs")
    return payload


def fetch_all(*, quiet: bool = False) -> dict[str, Any]:
    """Refresh every channel discoverable via <channel>/config.yaml.

    Returns a small summary dict for CLI/log output.
    """
    accounts = iter_channel_configs()
    if not accounts:
        if not quiet:
            print("[stats] no channels found — nothing to refresh")
        return {"channels": 0, "fetched": 0, "missing_auth": 0, "videos": 0}

    fetched = 0
    missing_auth = 0
    total_videos = 0
    # One fetch per distinct account (a few channel dirs may share an account).
    seen: set[str] = set()
    for account, _chan_dir in accounts:
        if account in seen:
            continue
        seen.add(account)
        result = fetch_account(account, quiet=quiet)
        if result is None:
            missing_auth += 1
            continue
        fetched += 1
        total_videos += len(result.get("videos") or [])

    return {
        "channels": len(seen),
        "fetched": fetched,
        "missing_auth": missing_auth,
        "videos": total_videos,
    }


# ---- offline readers (cheap; no network) -------------------------------


def load_account(account: str) -> dict | None:
    """Read the cached YT JSON for ``account`` (FS or GCS), or None."""
    return _load_cache(account)


def load_all_cached() -> list[dict]:
    """Every cached account payload, in account-name order."""
    out: list[dict] = []
    for acct in _list_cached_accounts():
        payload = _load_cache(acct)
        if payload is not None:
            out.append(payload)
    return out


# ---- CLI ----------------------------------------------------------------


def _cmd_token_status(quiet: bool = False) -> int:
    """``--token-status`` subcommand: snapshot OAuth health for every channel.

    Wraps :func:`pipeline.upload.upload.inspect_token_status` for each
    channel discovered via :func:`iter_channel_configs`. Same data shape
    the future ``/api/admin/token-health`` endpoint returns, so this CLI
    is the laptop-side debugging counterpart of the cloud probe.

    Pretty-prints by default; ``--quiet`` switches to NDJSON for piping.
    Exit code 0 if every account is ``ok``, 1 otherwise.
    """
    from pipeline.upload.upload import inspect_token_status  # noqa: PLC0415

    accounts = sorted({a for a, _ in iter_channel_configs()})
    if not accounts:
        if not quiet:
            print("No channels discovered (pipeline/channels/*.yaml empty).")
        return 1

    rows = [inspect_token_status(a) for a in accounts]
    bad = [r for r in rows if r.get("state") != "ok"]

    if quiet:
        for r in rows:
            print(json.dumps(r))
    else:
        name_w = max((len(r["account"]) for r in rows), default=10)
        print(f"{'account':<{name_w}}  {'state':<18}  detail")
        print("-" * (name_w + 2 + 18 + 2 + 40))
        for r in rows:
            state = r.get("state") or "?"
            detail = ""
            if state == "missing_scopes":
                detail = "missing: " + ",".join(s.split("/")[-1] for s in r.get("missing") or [])
            elif state == "no_refresh_token":
                detail = f"expiry={r.get('expiry') or '?'} — re-auth required"
            elif state == "ok":
                detail = f"expiry={r.get('expiry') or '?'}"
            elif state == "unreadable":
                detail = (r.get("error") or "")[:60]
            elif state == "missing":
                detail = "no token file — run `python -m pipeline.upload.upload auth --account <a>`"
            print(f"{r['account']:<{name_w}}  {state:<18}  {detail}")
        if bad:
            print(f"\n{len(bad)}/{len(rows)} account(s) need attention.")
        else:
            print(f"\nAll {len(rows)} account(s) ok.")

    return 0 if not bad else 1


def main() -> None:
    import argparse
    import sys as _sys

    ap = argparse.ArgumentParser(prog="pipeline.youtube_stats")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--account",
        help="Refresh only this account (default: every <channel>/config.yaml).",
    )
    ap.add_argument(
        "--token-status",
        action="store_true",
        help="Print OAuth-token state for every discovered channel and exit.",
    )
    args = ap.parse_args()

    if args.token_status:
        _sys.exit(_cmd_token_status(quiet=args.quiet))

    if args.account:
        result = fetch_account(args.account, quiet=args.quiet)
        summary = {
            "account": args.account,
            "fetched": 1 if result else 0,
            "videos": len(result.get("videos") or []) if result else 0,
        }
    else:
        summary = fetch_all(quiet=args.quiet)

    if args.quiet:
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
