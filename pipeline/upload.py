"""Stage 8 — YouTube upload (resumable, idempotent).

Sits at the end of the pipeline: the previous stages produced
``<slug>.mp4`` and a ``data/intermediate/<channel>/scripts/<slug>.json``
with title options and source metadata. This module turns those into
a YouTube video.

Design choices that match the rest of ytFactory:

- **Auth via OAuth 2.0 Installed App flow.** No service accounts (those
  can't upload to a personal channel). The browser dance happens once
  per ``account`` name; the resulting refresh token is cached at
  ``~/.config/ytfactory/youtube_token_<account>.json`` so subsequent
  uploads are non-interactive.
- **Multi-account aware.** A separate ``account`` name (default
  ``"default"``) maps to a separate token file, so different ytFactory
  channels can target different YouTube channels without re-auth churn.
- **Idempotent.** A successful upload writes
  ``data/uploads/<channel>/<slug>.json`` with the video_id; re-runs short-
  circuit and return that record instead of double-uploading.
- **Resumable.** ``MediaFileUpload(resumable=True)`` so a flaky tunnel
  doesn't waste a 50 MB upload. Chunks are 4 MiB.

Required pip deps (see requirements.txt):
    google-api-python-client
    google-auth-oauthlib
    google-auth-httplib2
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# YouTube Data API v3 — uploading + setting privacy/publish-at + setting
# a custom thumbnail all live under the upload scope (see
# https://developers.google.com/youtube/v3/docs/thumbnails/set#auth).
# `youtube.readonly` is needed by get_channel_sub_count (used by the
# Part-2 cliffhanger watcher to detect when Part 1 has crossed its
# subscriber threshold). The full `youtube` scope is needed by
# pipeline/cross_engage.py for videos.rate (likes) and
# subscriptions.insert (cross-channel subscribes). Adding scopes forces
# a one-time re-auth on accounts whose cached token predates the change
# — authenticate() detects scope-set drift and re-runs the browser flow.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
]


# YouTube's documented thumbnail constraints. We enforce them at API
# boundary so the user gets a clear error before we hit Google.
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
THUMBNAIL_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


CONFIG_DIR = Path.home() / ".config" / "ytfactory"
CLIENT_SECRET_PATH = CONFIG_DIR / "client_secret.json"


def _token_path(account: str) -> Path:
    safe = "".join(c for c in account if c.isalnum() or c in "-_") or "default"
    return CONFIG_DIR / f"youtube_token_{safe}.json"


def _record_path(project_root: Path, channel_dir: str, slug: str) -> Path:
    """Per-reorg layout: <project_root>/<channel_dir>/uploads/<slug>.json.

    ``channel_dir`` may be a compound path (e.g. ``mystoriesanimated/reddit_amitheasshole``)
    for niche-nested layouts; the result is
    ``mystoriesanimated/reddit_amitheasshole/uploads/<slug>.json``.
    """
    return project_root / channel_dir / "uploads" / f"{slug}.json"


class UploadError(RuntimeError):
    pass


# ---- auth ---------------------------------------------------------------


def inspect_token_status(account: str) -> dict:
    """Read this account's cached token and report its state — no API calls.

    ``state`` is one of:
      ok               — has refresh_token and all required scopes; ready
      missing          — file does not exist
      no_refresh_token — token exists but no refresh_token (worthless once
                         expired — re-auth required)
      missing_scopes   — token exists but lacks one or more SCOPES
      unreadable       — file exists but is malformed
    """
    tp = _token_path(account)
    out: dict[str, Any] = {"account": account, "path": str(tp)}
    if not tp.exists():
        out["state"] = "missing"
        return out
    try:
        data = json.loads(tp.read_text())
    except Exception as e:
        out.update(state="unreadable", error=str(e))
        return out
    out["expiry"] = data.get("expiry")
    cached_scopes = set(data.get("scopes") or [])
    needed = set(SCOPES)
    if cached_scopes and not cached_scopes.issuperset(needed):
        out.update(state="missing_scopes", missing=sorted(needed - cached_scopes))
        return out
    if not data.get("refresh_token"):
        out["state"] = "no_refresh_token"
        return out
    out["state"] = "ok"
    return out


def authenticate(account: str = "default", *, interactive: bool = True) -> Any:
    """Return a google-auth ``Credentials`` for the given account.

    Loads the cached refresh token if present; otherwise, if ``interactive``,
    runs the browser flow and writes the new token to disk.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET_PATH.exists():
        raise UploadError(
            f"Missing {CLIENT_SECRET_PATH}. Download an OAuth 2.0 Client ID "
            f"(type: Desktop) from Google Cloud Console and save the JSON to "
            f"that path. See PIPELINE.md §Stage 8 for the full setup."
        )

    tp = _token_path(account)
    creds = None
    # Read raw JSON first — `Credentials.scopes` reflects the SCOPES we'd
    # pass in, not what's actually in the file, so we can't detect drift
    # via the credentials object. Also preserves prior_refresh_token in
    # case the next OAuth flow omits it (Google deduplicates).
    prior_refresh_token: str | None = None
    if tp.exists():
        cached_scopes: set[str] = set()
        try:
            blob = json.loads(tp.read_text())
            cached_scopes = set(blob.get("scopes") or [])
            prior_refresh_token = blob.get("refresh_token") or None
        except Exception:
            pass
        needed = set(SCOPES)
        if cached_scopes and not cached_scopes.issuperset(needed):
            missing = needed - cached_scopes
            print(
                f"[upload] cached token for {account!r} missing scopes "
                f"{sorted(missing)}; re-auth required"
            )
        else:
            try:
                creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
            except Exception as e:
                # `from_authorized_user_file` rejects tokens missing
                # `refresh_token`. If the access_token is still valid we
                # can keep operating for ~1h — construct Credentials
                # manually so the caller gets a usable creds object,
                # warn loudly so they re-OAuth before expiry.
                try:
                    blob = json.loads(tp.read_text())
                    creds = Credentials(
                        token=blob.get("token"),
                        refresh_token=blob.get("refresh_token"),
                        token_uri=blob.get("token_uri", "https://oauth2.googleapis.com/token"),
                        client_id=blob.get("client_id"),
                        client_secret=blob.get("client_secret"),
                        scopes=blob.get("scopes") or SCOPES,
                    )
                    if not creds.refresh_token:
                        print(
                            f"[upload] WARN: token for {account!r} has no "
                            f"refresh_token — using access_token only "
                            f"(expires ~1h after issue). Fix: revoke at "
                            f"https://myaccount.google.com/connections , "
                            f"then re-run OAuth."
                        )
                except Exception as e2:
                    print(f"[upload] cached token unreadable ({e}; fallback also failed: {e2}); re-auth required")
                    creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tp.write_text(creds.to_json())
            return creds
        except Exception as e:
            print(f"[upload] token refresh failed ({e}); re-auth required")

    if not interactive:
        raise UploadError(
            f"No valid credentials for account={account!r} and "
            f"interactive=False. Run `python upload.py auth --account {account}` "
            f"first."
        )

    import sys
    # Force unbuffered stdout so the URL prints immediately when stdout
    # isn't a TTY (e.g. piped into a logfile by Claude Code's background
    # task tool). Without this, run_local_server's print() of the auth
    # URL stays trapped in the buffer until the process exits.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    # Fixed callback port (was port=0 / random) so a Web-type OAuth client
    # can register http://localhost:8089/ in its Authorized redirect URIs
    # list once. Desktop-type clients accept any loopback URI regardless,
    # so this is harmless for them. Env-overridable if 8089 is in use.
    callback_port = int(os.environ.get("YTFACTORY_OAUTH_PORT") or 8089)
    creds = flow.run_local_server(
        port=callback_port,
        # `consent select_account` forces BOTH the account picker AND the
        # consent screen, increasing the odds Google issues a fresh
        # refresh_token. `access_type=offline` is sent automatically by
        # google-auth-oauthlib's authorization_url().
        prompt="consent select_account",
        open_browser=False,
        authorization_prompt_message=(
            "\n"
            "================================================================\n"
            "  Open this URL in any browser signed into the target Google\n"
            "  account, grant access, then return here:\n"
            "----------------------------------------------------------------\n"
            "{url}\n"
            "================================================================\n"
        ),
        success_message="Authorization complete — you can close this tab.",
    )

    # Refresh-token preservation: Google may omit refresh_token in the
    # token response when the user has previously consented to this OAuth
    # client, even with prompt=consent. Without a refresh_token the next
    # interactive=False call will fail. If we have one from the prior
    # token file, splice it forward — it keeps offline access alive even
    # though it was minted under the old scope set (the access tokens
    # the refresh endpoint returns will reflect the new scopes
    # Google has on file for this user/client pair).
    if not creds.refresh_token:
        if prior_refresh_token:
            print(
                f"[upload] OAuth response for {account!r} omitted refresh_token; "
                f"reusing prior cached refresh_token"
            )
            creds.refresh_token = prior_refresh_token
        else:
            raise UploadError(
                f"OAuth flow for {account!r} returned NO refresh_token AND there "
                f"was no prior cached one to splice forward. This usually means "
                f"the Google account has the app pre-consented in a way that "
                f"deduplicates refresh-token issuance. Fix: visit "
                f"https://myaccount.google.com/connections , find the OAuth app, "
                f"REMOVE access, then re-run this auth flow."
            )

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tp.write_text(creds.to_json())
    print(f"[upload] cached refresh token → {tp}")
    return creds


# ---- description templating --------------------------------------------


def render_description(template: str, *, script: dict, raw: dict | None) -> str:
    """Substitute ``{script.X}``, ``{raw.X}``, ``{raw.metadata.X}`` tokens.

    Missing keys render as empty strings (so a description template that
    references ``{raw.url}`` still works for stories without a raw file).
    """
    import re

    def lookup(path: str) -> str:
        parts = path.split(".")
        root = parts[0]
        rest = parts[1:]
        if root == "script":
            cur: Any = script
        elif root == "raw":
            cur = raw or {}
        else:
            return ""
        for p in rest:
            if isinstance(cur, dict):
                cur = cur.get(p, "")
            elif isinstance(cur, list):
                try:
                    cur = cur[int(p)]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""
        if cur is None:
            return ""
        return str(cur)

    return re.sub(r"\{([a-zA-Z0-9_.\-]+)\}", lambda m: lookup(m.group(1)), template)


# ---- metadata derivation ------------------------------------------------


def derive_metadata(
    *,
    script: dict,
    raw: dict | None,
    upload_cfg: dict,
    title_override: str | None = None,
    description_override: str | None = None,
) -> dict:
    """Build the ``snippet``/``status`` payload from script + channel YAML.

    The channel YAML's ``upload:`` block looks like:

        upload:
          account: default
          privacy: private              # private | unlisted | public
          made_for_kids: false
          category_id: "24"             # Entertainment (default)
          tags: [aita, reddit, drama]
          description_template: |
            From r/{raw.metadata.subreddit}.
            Source: {raw.url}
    """
    title = title_override
    if not title:
        opts = script.get("title_options") or []
        if opts:
            title = str(opts[0])
        else:
            title = script.get("hook") or script.get("slug") or "Untitled"
    # YouTube hard-caps title at 100 chars.
    title = title[:100]

    if description_override is not None:
        description = description_override
    else:
        tmpl = upload_cfg.get("description_template") or (
            "From {raw.metadata.subreddit}.\nSource: {raw.url}"
        )
        description = render_description(tmpl, script=script, raw=raw)
    description = description[:5000]

    tags = list(upload_cfg.get("tags") or [])
    privacy = (upload_cfg.get("privacy") or "private").lower()
    if privacy not in ("private", "unlisted", "public"):
        raise UploadError(f"upload.privacy must be one of private|unlisted|public; got {privacy!r}")
    category_id = str(upload_cfg.get("category_id") or "24")
    made_for_kids = bool(upload_cfg.get("made_for_kids", False))

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "privacy": privacy,
        "category_id": category_id,
        "made_for_kids": made_for_kids,
    }


# ---- upload core --------------------------------------------------------


def set_thumbnail(video_id: str, thumbnail_path: Path, *, account: str = "default") -> dict:
    """Upload a custom thumbnail for an existing video.

    YouTube limits: ≤ 2 MB, JPG/PNG, ideally 1280×720 (16:9). For Shorts,
    a 9:16 image still works — YouTube center-crops for the inline thumb.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    if not thumbnail_path.exists():
        raise UploadError(f"thumbnail not found: {thumbnail_path}")
    size = thumbnail_path.stat().st_size
    if size > THUMBNAIL_MAX_BYTES:
        raise UploadError(
            f"thumbnail {thumbnail_path} is {size} bytes; YouTube max is "
            f"{THUMBNAIL_MAX_BYTES} (2 MB). Resize/recompress before upload."
        )
    suffix = thumbnail_path.suffix.lower()
    if suffix not in THUMBNAIL_MIME_TYPES:
        raise UploadError(
            f"thumbnail {thumbnail_path} has unsupported extension {suffix!r}; "
            f"YouTube accepts {sorted(THUMBNAIL_MIME_TYPES)}."
        )
    mime = THUMBNAIL_MIME_TYPES[suffix]

    creds = authenticate(account, interactive=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(str(thumbnail_path), mimetype=mime, resumable=False)
    try:
        resp = youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
    except HttpError as e:
        # Common cause: account isn't verified for custom thumbnails. Surface
        # the message so the user knows to verify their YouTube account.
        raise UploadError(
            f"thumbnail upload failed ({e.resp.status}): {e}. "
            f"Note: custom thumbnails require a verified YouTube account "
            f"(youtube.com/verify)."
        ) from e
    return {"video_id": video_id, "thumbnail_response": resp}


def youtube_upload(
    mp4_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    category_id: str = "24",
    privacy: str = "private",
    publish_at: str | None = None,
    made_for_kids: bool = False,
    account: str = "default",
    thumbnail_path: Path | None = None,
    progress_cb: Any = None,
) -> dict:
    """Resumable upload of one mp4. Returns ``{video_id, url, uploaded_at, ...}``.

    ``publish_at`` is an RFC 3339 timestamp (e.g. ``2026-05-02T13:00:00Z``).
    YouTube requires ``privacyStatus=private`` when ``publishAt`` is set;
    we adjust automatically.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    if not mp4_path.exists():
        raise UploadError(f"mp4 not found: {mp4_path}")

    creds = authenticate(account, interactive=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    status: dict[str, Any] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if publish_at:
        # YouTube only honours publishAt when video is private.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": status,
    }

    media = MediaFileUpload(
        str(mp4_path),
        chunksize=4 * 1024 * 1024,  # 4 MiB resumable chunks
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    last_progress = -1.0
    backoff = 1.0
    while response is None:
        try:
            chunk_status, response = request.next_chunk()
        except HttpError as e:
            # 5xx — transient, retry with exponential backoff up to 60s.
            if e.resp.status in (500, 502, 503, 504) and backoff <= 64:
                print(f"[upload] transient {e.resp.status}, retrying in {backoff}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            raise UploadError(f"YouTube API error {e.resp.status}: {e}") from e
        if chunk_status:
            pct = chunk_status.progress() * 100.0
            if pct - last_progress >= 5.0:
                print(f"[upload] {pct:.1f}%")
                last_progress = pct
                if progress_cb is not None:
                    try:
                        progress_cb(pct)
                    except Exception:
                        pass
        backoff = 1.0  # reset on success

    video_id = response.get("id")
    if not video_id:
        raise UploadError(f"upload returned no video id: {response!r}")

    thumbnail_set = False
    thumbnail_error: str | None = None
    if thumbnail_path is not None:
        try:
            set_thumbnail(video_id, thumbnail_path, account=account)
            thumbnail_set = True
            print(f"[upload] custom thumbnail set ← {thumbnail_path.name}")
        except Exception as e:
            # Don't fail the whole upload over a thumbnail glitch — the
            # video is up; the user can re-thumbnail in Studio.
            thumbnail_error = str(e)
            print(f"[upload] thumbnail set failed (non-fatal): {e}")

    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "privacy": status["privacyStatus"],
        "publish_at": publish_at,
        "account": account,
        "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
        "thumbnail_set": thumbnail_set,
        "thumbnail_error": thumbnail_error,
        "raw_response": {k: response.get(k) for k in ("id", "kind", "etag")},
    }


# ---- record-keeping (idempotency) --------------------------------------


# ---- pre-upload critic gate -------------------------------------------
#
# Every Short going public passes through a vision critique first. The
# critic samples the rendered mp4 at 1 fps and Claude returns a 1..10
# score plus per-beat / class-of-bug findings. We cache the critique on
# disk (the same place make_shorts.py writes it) so we don't pay the
# critic twice for the same render.
#
# A score below ``upload.min_score`` blocks the upload outright — the
# operator gets a one-line summary explaining why so they can either
# re-render or override with ``--skip-critic`` (the explicit "I know,
# ship it anyway" knob).


def _ensure_critique(
    *,
    slug: str,
    mp4_path: Path,
    project_root: Path,
    force: bool = False,
) -> dict | None:
    """Return the latest critique for this slug; run the critic if needed.

    Returns ``None`` if the critic can't run (missing beats.json — usually
    a legacy mp4 rendered before beat caching was added). Caller decides
    whether the gate fails-open or fails-closed for that case.
    """
    cache_dir = project_root / "data" / "cache" / slug
    out_dir = project_root / "data" / "critiques" / slug
    score_path = out_dir / f"{slug}.score.json"

    # Reuse cached critique iff the mp4 hasn't changed since.
    if not force and score_path.exists() and mp4_path.exists():
        try:
            if score_path.stat().st_mtime >= mp4_path.stat().st_mtime:
                cached = json.loads(score_path.read_text())
                if isinstance(cached, dict) and "score" in cached:
                    return cached
        except (OSError, json.JSONDecodeError):
            pass

    beats_path = cache_dir / "beats.json"
    if not beats_path.exists():
        # Legacy render — beat cache was wiped or never produced.
        # Without beats.json the critic can't sample meaningfully.
        return None

    # Lazy import: keeps `pipeline.upload` importable in environments
    # that don't have the LLM toolchain installed (the upload-only path).
    try:
        from pipeline import critic as critic_mod
    except ImportError as e:
        print(f"[upload] critic import failed; cannot pre-check ({e})")
        return None

    print(f"[upload] running pre-upload critic on {slug}…")
    try:
        return critic_mod.critique_short(
            slug=slug,
            mp4_path=mp4_path,
            cache_dir=cache_dir,
            out_dir=out_dir,
        )
    except Exception as e:
        # Don't blow up the whole upload over a critic flake (network,
        # claude CLI auth, etc.). Fail open with a warning.
        print(f"[upload] critic failed (non-fatal — proceeding): {e}")
        return None


def existing_upload(project_root: Path, channel_dir: str, slug: str) -> dict | None:
    p = _record_path(project_root, channel_dir, slug)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
        if rec.get("video_id"):
            return rec
    except (OSError, json.JSONDecodeError):
        return None
    return None


def get_channel_sub_count(account: str = "default") -> int:
    """Return the current subscriber count for the YouTube channel that
    ``account`` is authenticated against.

    Used by the Part-2 cliffhanger flow to (a) snapshot a baseline at
    Part-1 upload time, and (b) check current count from the watcher.
    Costs 1 unit of YouTube Data API quota per call.
    """
    from googleapiclient.discovery import build

    creds = authenticate(account, interactive=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = youtube.channels().list(part="statistics", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise UploadError(
            f"channels.list returned no items for account={account!r}; "
            f"is the OAuth account linked to a YouTube channel?"
        )
    stats = items[0].get("statistics") or {}
    raw = stats.get("subscriberCount")
    if raw is None:
        # Some channels hide subscriber count; in that case `hiddenSubscriberCount`
        # is true and we can't gate Part 2 on it. Surface clearly.
        raise UploadError(
            f"channel for account={account!r} has subscriberCount hidden; "
            f"unhide it in YouTube Studio or pick a different gate metric."
        )
    return int(raw)


def _part2_pending_path(project_root: Path, channel_dir: str, slug: str) -> Path:
    return (
        project_root / "data" / "intermediate" / channel_dir
        / "part2_pending" / f"{slug}.json"
    )


def _find_cast_path_for_sidecar(project_root: Path, channel_dir: str, slug: str) -> str | None:
    """Path to the per-story cast.json, if one exists. Returned as a
    project-root-relative string so the sidecar stays portable."""
    cast = project_root / "data" / "intermediate" / channel_dir / "cast" / f"{slug}.json"
    if cast.exists():
        try:
            return str(cast.relative_to(project_root))
        except ValueError:
            return str(cast)
    return None


def write_part2_pending(
    *,
    project_root: Path,
    channel_yaml: dict,
    channel_dir: str,
    slug: str,
    upload_record: dict,
    script: dict,
    raw: dict | None,
    account: str,
) -> Path | None:
    """If this Part-1 upload opts into the cliffhanger Part-2 flow, drop
    a pending sidecar so pipeline/part2_watcher.py can fire Part 2 once
    the subscriber threshold is met.

    Returns the sidecar path, or None if the channel doesn't opt in.
    Failures fetching the sub baseline are non-fatal: they log and skip
    the sidecar (Part 2 won't auto-render, but Part 1 still ships).
    """
    if not channel_yaml.get("cliffhanger"):
        return None
    part2_channel = channel_yaml.get("part2_channel")
    if not part2_channel:
        return None  # cliffhanger but no Part-2 channel wired — silent skip

    trigger = channel_yaml.get("part2_trigger") or {}
    subs_delta = int(trigger.get("subs_delta", 100))
    window_days = int(trigger.get("window_days", 7))

    try:
        baseline_subs = get_channel_sub_count(account)
    except Exception as e:
        # Non-fatal: log and bail. The Part-1 upload itself is fine.
        print(
            f"[upload] part2_pending skipped for {slug!r}: failed to read "
            f"baseline sub count ({e}). Re-auth with the youtube.readonly "
            f"scope or run pipeline/part2_watcher.py --rebaseline to fix."
        )
        return None

    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + window_days * 86400
    sidecar = {
        "slug": slug,
        "video_id": upload_record.get("video_id"),
        "video_url": upload_record.get("url"),
        "uploaded_at": now.isoformat(),
        "window_expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "account": account,
        "baseline_subs": baseline_subs,
        "threshold_subs_delta": subs_delta,
        "part1_channel_dir": channel_dir,
        "part2_channel": part2_channel,
        "part1_narration": (script or {}).get("narration") or "",
        "raw_story": raw,  # full raw_story dict; Part-2 rewrite reads from this
        "cast_path": _find_cast_path_for_sidecar(project_root, channel_dir, slug),
    }
    out = _part2_pending_path(project_root, channel_dir, slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sidecar, indent=2))
    print(
        f"[upload] part2_pending: {out.relative_to(project_root)} "
        f"(baseline={baseline_subs} subs, fires at +{subs_delta}, "
        f"window={window_days}d)"
    )
    return out


def write_upload_record(
    project_root: Path, channel_dir: str, slug: str, record: dict
) -> Path:
    p = _record_path(project_root, channel_dir, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2))
    return p


# ---- the orchestrator entry point --------------------------------------


def upload_short(
    *,
    project_root: Path,
    channel_yaml: dict,
    channel_dir: str,
    slug: str,
    mp4_path: Path,
    script: dict,
    raw: dict | None = None,
    title_override: str | None = None,
    description_override: str | None = None,
    privacy_override: str | None = None,
    publish_at: str | None = None,
    tags_override: list[str] | None = None,
    thumbnail_path: Path | None = None,
    auto_thumbnail: bool | None = None,
    headline_override: str | None = None,
    force: bool = False,
    skip_critic: bool = False,
    force_critic: bool = False,
    min_score_override: int | None = None,
    progress_cb: Any = None,
) -> dict:
    """High-level: dedupe → derive metadata → upload → record.

    ``channel_yaml`` is the parsed channel YAML dict; we read its ``upload:``
    block. ``channel_dir`` is the on-disk directory name under
    ``data/intermediate/`` (needed only for the upload-record path).
    """
    if not force:
        existing = existing_upload(project_root, channel_dir, slug)
        if existing:
            print(
                f"[upload] {slug} already on YouTube as {existing['video_id']} "
                f"({existing['url']}) — skipping (pass --force to re-upload)"
            )
            return existing

    upload_cfg = dict(channel_yaml.get("upload") or {})
    if privacy_override:
        upload_cfg["privacy"] = privacy_override
    if tags_override is not None:
        upload_cfg["tags"] = list(tags_override)

    meta = derive_metadata(
        script=script,
        raw=raw,
        upload_cfg=upload_cfg,
        title_override=title_override,
        description_override=description_override,
    )

    account = upload_cfg.get("account") or "default"

    # ---- pre-upload critic gate ----
    # Always run (or reuse cached) the critic before pushing public.
    # Block uploads with score < min_score unless caller passed
    # ``skip_critic`` (the explicit "ship it anyway" override).
    min_score = int(
        min_score_override
        if min_score_override is not None
        else upload_cfg.get("min_score", 6)
    )
    if not skip_critic:
        critique = _ensure_critique(
            slug=slug,
            mp4_path=mp4_path,
            project_root=project_root,
            force=force_critic,
        )
        if critique is None:
            print(
                f"[upload] critic skipped — beats.json missing for {slug!r}; "
                f"proceeding (legacy render). Pass --skip-critic to silence."
            )
        else:
            score = int(critique.get("score") or 0)
            take = (critique.get("one_line_take") or "").strip()
            print(f"[upload] critic score: {score}/10 — {take[:120]}")
            if score < min_score:
                raise UploadError(
                    f"critic score {score}/10 < min_score {min_score} for "
                    f"{slug!r}. Re-render to fix, or pass --skip-critic to "
                    f"override. Critique: {take[:200]}"
                )
    else:
        print(f"[upload] critic gate SKIPPED for {slug!r} (explicit override)")

    # ---- auto-thumbnail (Stage 8.5) ----
    # If the caller didn't supply a thumbnail and the channel YAML
    # opts in (or doesn't opt out), compose a catchy thumbnail from
    # the rendered scene + a curiosity-gap headline. This runs after
    # the critic gate so we don't waste cycles on shorts that won't
    # ship anyway.
    auto_cfg = upload_cfg.get("thumbnail") or {}
    do_auto_thumb = auto_thumbnail
    if do_auto_thumb is None:
        do_auto_thumb = bool(auto_cfg.get("auto", True))
    if thumbnail_path is None and do_auto_thumb:
        try:
            from pipeline import thumbnails as thumb_mod

            cache_dir = project_root / "data" / "cache" / slug
            out_thumb = cache_dir / "auto_thumb.jpg"
            generated = thumb_mod.auto_thumbnail(
                slug=slug,
                cache_dir=cache_dir,
                script=script,
                channel_yaml=channel_yaml,
                channel_dir=channel_dir,
                out_path=out_thumb,
                headline_override=headline_override or auto_cfg.get("headline"),
                style_override=auto_cfg.get("style"),
            )
            if generated:
                thumbnail_path = generated
                print(f"[upload] auto-thumbnail composed → {generated.name}")
        except Exception as e:
            # Don't block upload on thumbnail glitches; YouTube will
            # auto-pick a frame in that case.
            print(f"[upload] auto-thumbnail skipped (non-fatal): {e}")

    print(
        f"[upload] {slug} → YouTube (account={account}, "
        f"privacy={meta['privacy']}{', publish_at='+publish_at if publish_at else ''})"
    )
    print(f"[upload]   title: {meta['title']}")

    record = youtube_upload(
        mp4_path,
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
        category_id=meta["category_id"],
        privacy=meta["privacy"],
        publish_at=publish_at,
        made_for_kids=meta["made_for_kids"],
        account=account,
        thumbnail_path=thumbnail_path,
        progress_cb=progress_cb,
    )
    record["slug"] = slug
    record["channel_dir"] = channel_dir
    record["mp4_path"] = str(mp4_path)
    record["description"] = meta["description"]
    record["tags"] = meta["tags"]

    write_upload_record(project_root, channel_dir, slug, record)
    print(f"[upload] ✓ {record['url']}  (record: {channel_dir}/uploads/{slug}.json)")

    # Cliffhanger Part-1 → drop a pending sidecar so the Part-2 watcher
    # can auto-render the finale once subscribers cross the threshold.
    # Non-fatal on errors; Part-1 upload above already succeeded.
    try:
        write_part2_pending(
            project_root=project_root,
            channel_yaml=channel_yaml,
            channel_dir=channel_dir,
            slug=slug,
            upload_record=record,
            script=script,
            raw=raw,
            account=account,
        )
    except Exception as e:
        print(f"[upload] part2_pending write failed (non-fatal): {e}")

    # Cross-channel engagement: every sibling channel likes the new
    # video + one anonymous Playwright tab plays it muted to register a
    # natural view. Runs in a background daemon thread so this call
    # returns fast. Disable globally via YTFACTORY_CROSS_ENGAGE=0.
    try:
        from pipeline import cross_engage

        cross_engage.engage_after_upload(account, record["video_id"])
    except Exception as e:
        print(f"[upload] cross_engage dispatch failed (non-fatal): {e}")

    return record


# ---- CLI ----------------------------------------------------------------


def _discover_accounts() -> list[str]:
    """Distinct OAuth-account names across every <channel>/config.yaml.

    Falls back to listing existing token files if no channel YAMLs are
    discoverable (e.g. running outside the repo).
    """
    try:
        from pipeline import youtube_stats

        accts = sorted({a for a, _ in youtube_stats.iter_channel_configs()})
        if accts:
            return accts
    except Exception:
        pass
    if CONFIG_DIR.exists():
        return sorted({
            p.stem.replace("youtube_token_", "")
            for p in CONFIG_DIR.glob("youtube_token_*.json")
        })
    return []


def _print_status_table(rows: list[dict]) -> None:
    state_color = {
        "ok": "\033[32m",  # green
        "missing": "\033[31m",  # red
        "no_refresh_token": "\033[33m",  # yellow
        "missing_scopes": "\033[33m",
        "unreadable": "\033[31m",
    }
    reset = "\033[0m"
    name_w = max((len(r["account"]) for r in rows), default=10)
    print(f"{'account':<{name_w}}  {'state':<18}  detail")
    print("-" * (name_w + 2 + 18 + 2 + 40))
    for r in rows:
        col = state_color.get(r["state"], "")
        detail = ""
        if r["state"] == "missing_scopes":
            detail = "missing: " + ",".join(s.split("/")[-1] for s in r.get("missing") or [])
        elif r["state"] == "no_refresh_token":
            detail = f"expiry={r.get('expiry') or '?'} — re-auth required"
        elif r["state"] == "ok":
            detail = f"expiry={r.get('expiry') or '?'}"
        elif r["state"] == "unreadable":
            detail = (r.get("error") or "")[:60]
        print(f"{r['account']:<{name_w}}  {col}{r['state']:<18}{reset}  {detail}")


def _cmd_auth_status(_args) -> int:
    accounts = _discover_accounts()
    if not accounts:
        print("No accounts discovered (no <channel>/config.yaml files, no token files).")
        return 1
    rows = [inspect_token_status(a) for a in accounts]
    _print_status_table(rows)
    bad = [r for r in rows if r["state"] != "ok"]
    if bad:
        print(
            f"\n{len(bad)} account(s) need attention. Re-auth with:\n"
            f"  python -m pipeline.upload auth refresh"
        )
    return 0


def _cmd_auth_refresh(args) -> int:
    """Interactively (re-)auth one account or every broken account."""
    excludes = set(args.exclude or [])
    if args.account:
        targets = [args.account]
    else:
        accounts = _discover_accounts()
        if not accounts:
            print("No accounts discovered. Pass --account <name> to auth a single channel.")
            return 1
        rows = [inspect_token_status(a) for a in accounts]
        targets = [
            r["account"] for r in rows
            if r["state"] != "ok" and r["account"] not in excludes
        ]
        if not targets:
            print("All accounts already authed (state=ok). Nothing to do.")
            return 0
        skipped = [r["account"] for r in rows if r["account"] in excludes]
        print(f"Will (re-)auth {len(targets)} account(s):")
        for r in rows:
            if r["state"] != "ok" and r["account"] not in excludes:
                print(f"  - {r['account']}  (state={r['state']})")
        if skipped:
            print(f"Skipping (--exclude): {', '.join(skipped)}")
        if not args.yes:
            try:
                answer = input("\nProceed? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                print("aborted.")
                return 1

    import errno
    import socket
    import time

    callback_port = int(os.environ.get("YTFACTORY_OAUTH_PORT") or 8089)

    def _wait_for_port_free(port: int, max_wait_s: int = 90) -> bool:
        """Poll until ``port`` accepts a fresh bind. The OAuth callback
        server binds without SO_REUSEADDR so the kernel holds the port
        in TIME_WAIT (~30s on macOS) after the previous auth completes.
        Probing via SO_REUSEADDR=1 lets the kernel tell us the port is
        actually claimable by the next ``run_local_server`` call.
        """
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("localhost", port))
                s.close()
                return True
            except OSError:
                s.close()
                time.sleep(2)
        return False

    failed: list[tuple[str, str]] = []
    for i, account in enumerate(targets, 1):
        if i > 1:
            print(f"[auth] waiting for port {callback_port} to clear (TIME_WAIT)…")
            if not _wait_for_port_free(callback_port):
                print(
                    f"[auth] port {callback_port} still busy after 90s; aborting"
                )
                failed.append((account, "port still busy"))
                break
        print(
            f"\n================ [{i}/{len(targets)}] re-authing "
            f"{account!r} ================"
        )
        # If the cached token is in a state where authenticate() would
        # use it instead of running the browser flow (e.g. no_refresh_token
        # but access_token still valid for ~1h), move it aside first so
        # the interactive path actually fires. The .pre-reauth-* sidecar
        # is keepable in case anything goes wrong.
        pre_status = inspect_token_status(account)
        if pre_status["state"] in ("no_refresh_token", "missing_scopes", "unreadable"):
            tp = _token_path(account)
            backup = tp.with_suffix(
                tp.suffix + f".pre-reauth-{int(time.time())}"
            )
            try:
                tp.rename(backup)
                print(
                    f"[auth] moved stale token aside → {backup.name} "
                    f"(state was {pre_status['state']})"
                )
            except OSError:
                pass
        try:
            authenticate(account=account, interactive=True)
            print(f"[auth] {account}: OK")
        except KeyboardInterrupt:
            print("\n[auth] interrupted by user")
            failed.append((account, "interrupted"))
            break
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                # Port wasn't released between auths — most likely the
                # last grant timed out before the WSGI server shut down.
                # Surface a clear hint instead of the cryptic Errno 48.
                print(
                    f"[auth] {account}: FAILED — port {callback_port} busy "
                    f"(retry: rerun this command, or wait 60s)"
                )
                failed.append((account, "port busy"))
            else:
                print(f"[auth] {account}: FAILED — {e}")
                failed.append((account, str(e)))
        except Exception as e:
            print(f"[auth] {account}: FAILED — {e}")
            failed.append((account, str(e)))

    print("\n--- summary ---")
    rows = [inspect_token_status(a) for a in targets]
    _print_status_table(rows)
    if failed:
        print(f"\n{len(failed)} account(s) failed: {[a for a,_ in failed]}")
        return 1
    return 0


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m pipeline.upload")
    sub = ap.add_subparsers(dest="cmd", required=True)

    auth = sub.add_parser("auth", help="Inspect or refresh OAuth tokens")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)

    s = auth_sub.add_parser("status", help="Show auth state of every channel")
    s.set_defaults(func=_cmd_auth_status)

    r = auth_sub.add_parser(
        "refresh",
        help="Run the OAuth flow for a single account or every broken one",
    )
    r.add_argument("--account", help="Re-auth only this account")
    r.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Skip this account (repeatable). Useful for X-only channels.",
    )
    r.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    r.set_defaults(func=_cmd_auth_refresh)

    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
