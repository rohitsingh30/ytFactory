"""Stage 8b — X (Twitter) cross-post.

Companion to ``pipeline/upload.py`` (YouTube). Sits at the end of the
pipeline alongside YouTube: takes the same ``<channel>/uploads/<slug>.mp4``
and posts it as a native video tweet on the X handle bound to the channel.

Design parity with pipeline/upload.py:

- **Per-account credentials.** Each channel's ``x:`` config block names
  an ``account``. Credentials live at
  ``~/.config/ytfactory/x_credentials_<account>.json``. Different
  ytFactory channels target different X handles without auth churn.
- **Idempotent.** A successful post writes
  ``<channel>/uploads/<slug>.x.json`` with the tweet id; re-runs short-
  circuit. Sidecar lives next to the YouTube ``<slug>.json`` so both
  platforms can coexist on one render.
- **Resumable / chunked.** X's media upload uses the v1.1 chunked
  endpoint (still the only video path). tweepy handles INIT/APPEND/
  FINALIZE/STATUS for us; we expose a progress callback.
- **No critic gate.** The YouTube upload already gated on the critic;
  if it shipped to YouTube, it ships to X.

Required pip deps:
    tweepy>=4.14
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import observability as _obs


CONFIG_DIR = Path.home() / ".config" / "ytfactory"


class XUploadError(RuntimeError):
    pass


# ---- credentials --------------------------------------------------------


def _credentials_path(account: str) -> Path:
    safe = "".join(c for c in account if c.isalnum() or c in "-_") or "default"
    return CONFIG_DIR / f"x_credentials_{safe}.json"


def load_credentials(account: str = "default") -> dict:
    """Load OAuth 1.0a creds for ``account``.

    Expects a JSON file written per ``docs/X_SETUP.md``. We surface a
    friendly error if it's missing — same shape as the YouTube uploader.
    """
    p = _credentials_path(account)
    if not p.exists():
        raise XUploadError(
            f"Missing {p}. Follow docs/X_SETUP.md to create an X app "
            f"and drop credentials there. Required keys: consumer_key, "
            f"consumer_secret, access_token, access_token_secret."
        )
    try:
        creds = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise XUploadError(f"{p} is not valid JSON: {e}") from e
    required = {"consumer_key", "consumer_secret", "access_token", "access_token_secret"}
    missing = required - set(creds)
    if missing:
        raise XUploadError(
            f"{p} missing keys: {sorted(missing)}. See docs/X_SETUP.md."
        )
    return creds


# ---- record-keeping (idempotency) --------------------------------------


def _record_path(project_root: Path, channel_dir: str, slug: str) -> Path:
    """Per-render X sidecar — sits next to the YouTube record.

    YouTube writes ``<channel>/[<niche>/]/uploads/<slug>.json``; X writes
    ``<channel>/[<niche>/]/uploads/<slug>.x.json``. Two platforms, one
    render dir. Routes through :class:`pipeline.paths.RenderPaths`
    (canonical layout module since 2026-05-05).
    """
    from pipeline.paths import RenderPaths  # noqa: PLC0415

    return RenderPaths.from_channel_dir(channel_dir, project_root=project_root).x_upload_record_for(slug)


def existing_post(project_root: Path, channel_dir: str, slug: str) -> dict | None:
    p = _record_path(project_root, channel_dir, slug)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
        if rec.get("tweet_id"):
            return rec
    except (OSError, json.JSONDecodeError):
        return None
    return None


def write_post_record(
    project_root: Path, channel_dir: str, slug: str, record: dict
) -> Path:
    p = _record_path(project_root, channel_dir, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2))
    return p


# ---- tweet text templating ---------------------------------------------


def render_tweet_text(template: str, *, script: dict, raw: dict | None) -> str:
    """Substitute ``{script.X}``, ``{raw.X}``, ``{raw.metadata.X}`` tokens.

    Same templating as pipeline.upload.render_description — kept inline
    so this module stays importable without the YouTube deps.
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


# X enforces 280 chars (free) / 25k (Premium). We clamp to 280 by
# default; channels on Premium can override via ``x.max_chars``.
TWEET_DEFAULT_MAX_CHARS = 280


def derive_tweet(
    *,
    script: dict,
    raw: dict | None,
    x_cfg: dict,
    text_override: str | None = None,
) -> dict:
    """Build the tweet payload from script + channel YAML's ``x:`` block.

    Channel YAML shape::

        x:
          account: historyrecapped
          enabled: true
          tweet_template: |
            {script.hook}

            📖 {raw.url}

            #history #shorts
          max_chars: 280       # optional; bump on Premium accounts
    """
    if text_override is not None:
        text = text_override
    else:
        tmpl = x_cfg.get("tweet_template")
        if not tmpl:
            # Fall back to the script's first title option, or the hook.
            opts = script.get("title_options") or []
            base = opts[0] if opts else (script.get("hook") or script.get("slug") or "")
            tmpl = str(base)
        text = render_tweet_text(tmpl, script=script, raw=raw)

    # Strip leading/trailing whitespace; collapse 3+ blank lines to 2
    # (X renders >2 newlines as 2 anyway, and the trim avoids surprise
    # truncation when the hash-tags get clipped).
    import re as _re

    text = text.strip()
    text = _re.sub(r"\n{3,}", "\n\n", text)

    max_chars = int(x_cfg.get("max_chars") or TWEET_DEFAULT_MAX_CHARS)
    if len(text) > max_chars:
        # Truncate at a word boundary to keep hash-tags intact when
        # possible. If the trailing tag block is dropped entirely
        # that's fine — the algo still indexes by media + handle.
        text = text[: max_chars - 1].rstrip() + "…"

    return {"text": text, "max_chars": max_chars}


# ---- upload core --------------------------------------------------------


def x_post(
    mp4_path: Path,
    *,
    text: str,
    account: str = "default",
    progress_cb: Any = None,
) -> dict:
    """Chunked-upload an mp4 to X and post it as a tweet.

    Returns ``{tweet_id, url, uploaded_at, media_id, ...}``.
    Wrapped in an ``x_post`` span so the dashboard's per-channel X
    upload health view can chart latency / error rate.
    """
    metadata: dict = {
        "account": account,
        "text_chars": len(text or ""),
    }
    try:
        metadata["mp4_bytes"] = mp4_path.stat().st_size
    except Exception:  # noqa: BLE001
        pass
    with _obs.timed("x_post", category="upload", metadata=metadata) as t:
        result = _x_post_impl(
            mp4_path, text=text, account=account, progress_cb=progress_cb,
        )
        if isinstance(result, dict):
            t.add(metadata={
                "tweet_id": result.get("tweet_id"),
                "media_id": result.get("media_id"),
            })
        return result


def _x_post_impl(
    mp4_path: Path,
    *,
    text: str,
    account: str = "default",
    progress_cb: Any = None,
) -> dict:
    """Chunked-upload an mp4 to X and post it as a tweet.

    Returns ``{tweet_id, url, uploaded_at, media_id, ...}``.
    """
    try:
        import tweepy  # noqa: PLC0415
    except ImportError as e:
        raise XUploadError(
            "tweepy is not installed. Add `tweepy>=4.14` to requirements.txt "
            "and `pip install tweepy` in your venv."
        ) from e

    if not mp4_path.exists():
        raise XUploadError(f"mp4 not found: {mp4_path}")

    creds = load_credentials(account)

    # OAuth 1.0a is mandatory for media upload (the v2 endpoints are
    # OAuth-2-only but media upload still routes through v1.1).
    auth = tweepy.OAuth1UserHandler(
        creds["consumer_key"],
        creds["consumer_secret"],
        creds["access_token"],
        creds["access_token_secret"],
    )
    api_v1 = tweepy.API(auth)

    # Chunked upload — required for video. tweepy hides INIT/APPEND/
    # FINALIZE; we still poll STATUS to wait for X's async transcode.
    print(f"[x_upload] uploading {mp4_path.name} ({mp4_path.stat().st_size // 1024 // 1024} MB)…")
    media = api_v1.chunked_upload(
        filename=str(mp4_path),
        media_category="tweet_video",
    )
    media_id = getattr(media, "media_id", None) or getattr(media, "media_id_string", None)
    if not media_id:
        raise XUploadError(f"chunked_upload returned no media_id: {media!r}")

    # Wait for X's transcode (chunked_upload returns immediately after
    # FINALIZE; the video isn't postable until processing_info.state is
    # 'succeeded'). tweepy >=4.14 handles this in chunked_upload itself
    # via wait_for_async_finalize=True (default), but older versions
    # don't — guard explicitly.
    proc_info = getattr(media, "processing_info", None)
    if proc_info and proc_info.get("state") not in (None, "succeeded"):
        import time as _time

        deadline = _time.time() + 180  # 3 min hard cap
        while _time.time() < deadline:
            check_after = int(proc_info.get("check_after_secs") or 5)
            _time.sleep(check_after)
            status = api_v1.get_media_upload_status(media_id)
            proc_info = getattr(status, "processing_info", None) or {}
            state = proc_info.get("state")
            if progress_cb is not None:
                try:
                    progress_cb(proc_info.get("progress_percent", 0))
                except Exception:
                    pass
            if state == "succeeded":
                break
            if state == "failed":
                err = proc_info.get("error") or {}
                raise XUploadError(f"X transcode failed: {err}")
        else:
            raise XUploadError("X transcode timed out after 3 minutes")

    # v2 tweet endpoint — needed for the modern algo signal.
    client = tweepy.Client(
        consumer_key=creds["consumer_key"],
        consumer_secret=creds["consumer_secret"],
        access_token=creds["access_token"],
        access_token_secret=creds["access_token_secret"],
    )
    resp = client.create_tweet(text=text, media_ids=[str(media_id)])
    data = getattr(resp, "data", None) or {}
    tweet_id = data.get("id")
    if not tweet_id:
        raise XUploadError(f"create_tweet returned no id: {resp!r}")

    handle = creds.get("handle") or "i"  # X auto-redirects /i/status/<id>
    url = f"https://x.com/{handle}/status/{tweet_id}"

    return {
        "tweet_id": str(tweet_id),
        "url": url,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "media_id": str(media_id),
        "account": account,
        "handle": creds.get("handle"),
    }


# ---- the orchestrator entry point --------------------------------------


def post_short(
    *,
    project_root: Path,
    channel_yaml: dict,
    channel_dir: str,
    slug: str,
    mp4_path: Path,
    script: dict,
    raw: dict | None = None,
    text_override: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress_cb: Any = None,
) -> dict:
    """High-level X post entry. Wraps the implementation in an
    ``x_post_short`` span — the dashboard groups per-channel X health
    on this name. The inner ``x_post`` span (one per actual API call)
    nests under it.
    """
    metadata = {
        "channel_dir": channel_dir,
        "slug": slug,
        "force": force,
        "dry_run": dry_run,
    }
    with _obs.timed("x_post_short", category="upload",
                    metadata=metadata) as t:
        result = _post_short_impl(
            project_root=project_root,
            channel_yaml=channel_yaml,
            channel_dir=channel_dir,
            slug=slug,
            mp4_path=mp4_path,
            script=script,
            raw=raw,
            text_override=text_override,
            force=force,
            dry_run=dry_run,
            progress_cb=progress_cb,
        )
        if isinstance(result, dict):
            t.add(metadata={
                "tweet_id": result.get("tweet_id"),
                "skipped": result.get("skipped", False),
            })
        return result


def _post_short_impl(
    *,
    project_root: Path,
    channel_yaml: dict,
    channel_dir: str,
    slug: str,
    mp4_path: Path,
    script: dict,
    raw: dict | None = None,
    text_override: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress_cb: Any = None,
) -> dict:
    """High-level: dedupe → derive tweet text → upload → record.

    ``channel_yaml`` is the parsed channel YAML dict; reads the ``x:``
    block. Returns the upload record (or a dry-run preview if
    ``dry_run``).
    """
    x_cfg = dict(channel_yaml.get("x") or {})
    if not x_cfg:
        raise XUploadError(
            f"channel YAML for {channel_dir!r} has no `x:` block. "
            f"See docs/X_SETUP.md §5."
        )
    if not x_cfg.get("enabled", True) is True:
        raise XUploadError(f"x.enabled is false for {channel_dir!r}")

    if not force:
        existing = existing_post(project_root, channel_dir, slug)
        if existing:
            print(
                f"[x_upload] {slug} already on X as {existing['tweet_id']} "
                f"({existing['url']}) — skipping (pass --force to re-post)"
            )
            return existing

    tweet = derive_tweet(
        script=script, raw=raw, x_cfg=x_cfg, text_override=text_override
    )
    account = x_cfg.get("account") or "default"

    print(f"[x_upload] {slug} → X (account={account})")
    print(f"[x_upload]   {len(tweet['text'])}/{tweet['max_chars']} chars")
    print(f"[x_upload]   text: {tweet['text']!r}")

    if dry_run:
        return {
            "dry_run": True,
            "slug": slug,
            "channel_dir": channel_dir,
            "account": account,
            "text": tweet["text"],
            "mp4_path": str(mp4_path),
        }

    record = x_post(
        mp4_path,
        text=tweet["text"],
        account=account,
        progress_cb=progress_cb,
    )
    record["slug"] = slug
    record["channel_dir"] = channel_dir
    record["mp4_path"] = str(mp4_path)

    write_post_record(project_root, channel_dir, slug, record)
    print(f"[x_upload] ✓ {record['url']}  (record: {channel_dir}/uploads/{slug}.x.json)")
    return record


# ---- CLI ----------------------------------------------------------------


def _cli() -> int:
    import argparse

    import yaml  # noqa: PLC0415 — only when CLI runs

    p = argparse.ArgumentParser(description="Cross-post a rendered Short to X.")
    p.add_argument("--channel", required=True, help="Channel slug (top-level dir)")
    p.add_argument("--slug", required=True, help="Story slug")
    p.add_argument("--mp4", help="Override mp4 path (defaults to <channel>/uploads/<slug>.mp4)")
    p.add_argument("--text", help="Override tweet text entirely (skips template)")
    p.add_argument("--force", action="store_true", help="Re-post even if record exists")
    p.add_argument("--dry-run", action="store_true", help="Print what would be posted")
    args = p.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    channel_dir = args.channel
    from pipeline.paths import RenderPaths  # noqa: PLC0415
    paths = RenderPaths.from_channel_dir(channel_dir, project_root=project_root)
    yaml_path = paths.config_yaml
    if not yaml_path.exists():
        print(f"error: {yaml_path} not found", flush=True)
        return 2
    channel_yaml = yaml.safe_load(yaml_path.read_text()) or {}

    # Default mp4 location: <channel>/[<niche>/]/shorts/<slug>.mp4
    # (legacy default was uploads/, but renders never landed there —
    # 2026-05-05 fix). Caller can still pass --mp4 to override.
    mp4_path = Path(args.mp4) if args.mp4 else paths.short_for(args.slug)

    # Load script + raw the same way pipeline/upload.py does.
    script_path = paths.narration_for(args.slug)
    raw_path = paths.raw_for(args.slug)
    if not script_path.exists():
        print(f"error: script not found at {script_path}", flush=True)
        return 2
    script = json.loads(script_path.read_text())
    raw = json.loads(raw_path.read_text()) if raw_path.exists() else None

    try:
        rec = post_short(
            project_root=project_root,
            channel_yaml=channel_yaml,
            channel_dir=channel_dir,
            slug=args.slug,
            mp4_path=mp4_path,
            script=script,
            raw=raw,
            text_override=args.text,
            force=args.force,
            dry_run=args.dry_run,
        )
    except XUploadError as e:
        print(f"error: {e}", flush=True)
        return 1
    print(json.dumps(rec, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
