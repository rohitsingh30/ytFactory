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

# ---------------------------------------------------------------------------
# GCS state propagation (cloud cutover 2026-05-09)
# ---------------------------------------------------------------------------
# The worker runs on the laptop; the UI polls the Cloud Run control
# plane. To bridge them, the worker mirrors its state file to GCS at
# ``gs://$YTFACTORY_STATE_BUCKET/burner_engage/<slug>.json``, and the
# control plane reads from there in ``read_state``. Stop signals flow
# the other way through ``gs://.../burner_engage/<slug>.stop`` so a
# Stop click in the UI can interrupt a worker on the laptop.
# When ``YTFACTORY_STATE_BUCKET`` is unset (laptop dev), we keep the
# original on-disk semantics intact.
_STATE_BUCKET_ENV = "YTFACTORY_STATE_BUCKET"
_GCS_STATE_PREFIX = "burner_engage"
_GCS_CACHE_TTL_S = 2.0
_GCS_CLIENT = None  # lazy-init; module-global to amortise the auth
_GCS_STATE_CACHE: dict[str, tuple[float, dict | None]] = {}
_GCS_STOP_CACHE: dict[str, tuple[float, bool]] = {}


def _state_bucket() -> str | None:
    return os.environ.get(_STATE_BUCKET_ENV) or None


def _gcs_client():
    """Lazy module-global storage client; None if google.cloud.storage
    isn't installed (laptop dev without the GCP extras)."""
    global _GCS_CLIENT  # noqa: PLW0603
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    try:
        from google.cloud import storage  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        _GCS_CLIENT = storage.Client(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"),
        )
    except Exception:  # noqa: BLE001
        return None
    return _GCS_CLIENT


def _gcs_state_blob_name(slug: str) -> str:
    return f"{_GCS_STATE_PREFIX}/{slug}.json"


def _gcs_stop_blob_name(slug: str) -> str:
    return f"{_GCS_STATE_PREFIX}/{slug}.stop"


def _push_state_gcs(slug: str, payload: dict) -> None:
    """Best-effort upload of the JSON state to GCS. Never raises."""
    bucket_name = _state_bucket()
    if not bucket_name:
        return
    try:
        cli = _gcs_client()
        if cli is None:
            return
        bucket = cli.bucket(bucket_name)
        blob = bucket.blob(_gcs_state_blob_name(slug))
        blob.upload_from_string(
            json.dumps(payload, indent=2),
            content_type="application/json",
        )
        # Invalidate the cache so the next read returns the fresh data.
        _GCS_STATE_CACHE.pop(slug, None)
    except Exception as e:  # noqa: BLE001
        logger.warning("burner_engage[%s]: GCS state push failed: %s", slug, e)


def _read_state_gcs(slug: str) -> dict | None:
    """Read state JSON from GCS with a small TTL cache.

    Returns None when the bucket isn't configured, the client isn't
    available, or the blob doesn't exist."""
    bucket_name = _state_bucket()
    if not bucket_name:
        return None
    now = time.time()
    cached = _GCS_STATE_CACHE.get(slug)
    if cached and cached[0] > now:
        return cached[1]
    try:
        cli = _gcs_client()
        if cli is None:
            _GCS_STATE_CACHE[slug] = (now + _GCS_CACHE_TTL_S, None)
            return None
        bucket = cli.bucket(bucket_name)
        blob = bucket.blob(_gcs_state_blob_name(slug))
        if not blob.exists():
            data = None
        else:
            data = json.loads(blob.download_as_text())
    except Exception as e:  # noqa: BLE001
        logger.warning("burner_engage[%s]: GCS state read failed: %s", slug, e)
        data = None
    _GCS_STATE_CACHE[slug] = (now + _GCS_CACHE_TTL_S, data)
    return data


def _write_stop_sentinel_gcs(slug: str) -> bool:
    """Write the stop sentinel object to GCS. Returns True on success."""
    bucket_name = _state_bucket()
    if not bucket_name:
        return False
    try:
        cli = _gcs_client()
        if cli is None:
            return False
        bucket = cli.bucket(bucket_name)
        blob = bucket.blob(_gcs_stop_blob_name(slug))
        blob.upload_from_string(_now(), content_type="text/plain")
        _GCS_STOP_CACHE.pop(slug, None)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("burner_engage[%s]: GCS stop sentinel write failed: %s", slug, e)
        return False


def _read_stop_sentinel_gcs(slug: str) -> bool:
    """Cheap (cached) check for the GCS stop sentinel. Used by the
    laptop worker each tick — must not be expensive."""
    bucket_name = _state_bucket()
    if not bucket_name:
        return False
    now = time.time()
    cached = _GCS_STOP_CACHE.get(slug)
    if cached and cached[0] > now:
        return cached[1]
    try:
        cli = _gcs_client()
        if cli is None:
            _GCS_STOP_CACHE[slug] = (now + _GCS_CACHE_TTL_S, False)
            return False
        bucket = cli.bucket(bucket_name)
        blob = bucket.blob(_gcs_stop_blob_name(slug))
        present = bool(blob.exists())
    except Exception as e:  # noqa: BLE001
        logger.warning("burner_engage[%s]: GCS stop sentinel check failed: %s", slug, e)
        present = False
    _GCS_STOP_CACHE[slug] = (now + _GCS_CACHE_TTL_S, present)
    return present


def clear_stop_sentinel(slug: str) -> None:
    """Delete the GCS stop sentinel (if any). Called by the cloud
    POST endpoint right before enqueueing a fresh BURNER_ENGAGE task —
    otherwise a stale sentinel from a previous run would make the new
    worker self-terminate on its first tick."""
    bucket_name = _state_bucket()
    if not bucket_name:
        return
    try:
        cli = _gcs_client()
        if cli is None:
            return
        bucket = cli.bucket(bucket_name)
        blob = bucket.blob(_gcs_stop_blob_name(slug))
        if blob.exists():
            blob.delete()
        _GCS_STOP_CACHE.pop(slug, None)
    except Exception as e:  # noqa: BLE001
        logger.warning("burner_engage[%s]: GCS stop sentinel clear failed: %s", slug, e)


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
    """Burners loaded from the committed pipeline/burners.yaml manifest.

    burners.yaml is the single source of truth (slug, title, channel_id,
    google_email). Per-burner runtime state — token presence + Chrome
    profile signed-in — is hydrated here so the dashboard can render it.

    Older versions of this function discovered burners by intersecting
    ``~/.config/ytfactory/channel_ids.json`` × token files × not-in-
    production. That hid every burner whose token hadn't been minted
    on this laptop yet (e.g. fresh accounts created in the burner
    factory) so the /app/burner-channels page rendered empty even
    though burners.yaml had dozens of rows.

    Returns a JSON-friendly list each carrying:
      {slug, channel_id, title, email, has_token, profile_known,
       discovered_at}
    Sorted by slug for stable UI ordering.
    """
    # Lazy import — pipeline.channels itself imports from this package
    # transitively in some test paths, so keep this off module load.
    from pipeline.channels import BURNERS

    out: list[dict[str, Any]] = []
    for b in BURNERS:
        out.append({
            "slug": b.slug,
            "channel_id": b.youtube_channel_id,
            "title": b.youtube_title,
            "email": b.google_email,
            "discovered_at": "",
            "has_token": _resolve_token_path(b.slug) is not None,
            # YAML always carries google_email so the engage worker can
            # resolve the host Chrome profile. Kept as a flag for UI
            # gating + future "missing email" rows from non-YAML sources.
            "profile_known": bool(b.google_email),
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
    # Diagnostic counters for the cycle-and-retry flow (2026-05-10).
    # Each click attempt that doesn't flip the probed state increments
    # the relevant counter; once a counter passes a threshold the
    # action stops being retried for that tab.
    like_attempts: int = 0
    sub_attempts: int = 0
    # Commenting (added 2026-05-11 with the engagement-modes feature).
    # Only populated when the worker's mode is MODE_COMPLETE and this
    # video was randomly selected into the comment subset (~30% of
    # the catalog, capped at MAX_COMMENTS_PER_RUN total).
    commented: bool = False
    comment_attempts: int = 0
    comment_text: str = ""


@dataclass
class EngageState:
    slug: str
    channel_id: str
    started_at: str
    phase: str = "initializing"  # initializing | engaging | watching | stopped | failed | blocked
    last_action_at: str = ""
    last_action_msg: str = ""
    stop_requested: bool = False
    # Engagement mode (added 2026-05-11). Determines what Phase 1 does
    # per tab and whether the cycle exits-when-done or runs forever.
    # See MODE_* constants below.
    mode: str = "like_subscribe_view"
    videos: list[VideoState] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engagement modes (2026-05-11)
# ---------------------------------------------------------------------------
# The Cross-engage button in /app/burner-channels offers four intensities,
# each picking a different mix of actions per video tab:
#
#   subscribe_only       — subscribe to each unique source channel; no
#                          per-video Like, no comments, no extended watch.
#                          Exits when every channel is subscribed (or
#                          per-channel sub_attempts maxes out). Fastest
#                          mode, lowest engagement footprint.
#
#   like_subscribe       — subscribe to each channel AND like each
#                          video. No comments, no extended watch loop.
#                          Exits when all videos are liked + all
#                          channels subscribed. Useful as a "heal"
#                          run after a bad-selector incident.
#
#   like_subscribe_view  — like + subscribe + permanent watch loop. The
#                          (default) cycle keeps rotating tabs forever to
#                          accumulate watch-time. The pre-2026-05-11
#                          default behaviour.
#
#   complete             — like_subscribe_view + comments on a randomly
#                          selected ~30% subset of the catalog (capped
#                          at 15 comments per worker run). Comment text
#                          is generated per-video by the LLM dispatcher
#                          (see _generate_comment_text). Highest engage,
#                          highest shadow-ban risk.

MODE_SUBSCRIBE_ONLY = "subscribe_only"
MODE_LIKE_SUBSCRIBE = "like_subscribe"
MODE_LIKE_SUBSCRIBE_VIEW = "like_subscribe_view"
MODE_COMPLETE = "complete"
ALL_MODES = (
    MODE_SUBSCRIBE_ONLY,
    MODE_LIKE_SUBSCRIBE,
    MODE_LIKE_SUBSCRIBE_VIEW,
    MODE_COMPLETE,
)
DEFAULT_MODE = MODE_LIKE_SUBSCRIBE_VIEW

# Comment-mode tuning (only used in MODE_COMPLETE).
COMMENT_SUBSET_FRACTION = 0.30
MAX_COMMENTS_PER_RUN = 15
# Minimum spacing between two comments in a single worker run
# (seconds). 270s = 4.5 min average → ~13/hr ≤ YouTube's eyebrow-raising
# threshold for a freshly-trusted account.
MIN_SECONDS_BETWEEN_COMMENTS = 270


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
        "mode": state.mode,
        "videos": [asdict(v) for v in state.videos],
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)
    # Mirror to GCS so the cloud control plane's poll endpoint can serve
    # this state to the UI. Best-effort; failures must never block the
    # worker. No-op when YTFACTORY_STATE_BUCKET is unset (laptop dev).
    _push_state_gcs(state.slug, payload)


def read_state(slug: str) -> dict | None:
    """Public: dashboard polls this to render live status.

    On Cloud Run (``YTFACTORY_STATE_BUCKET`` set), prefers the GCS copy
    written by the laptop worker. Falls back to the local file when GCS
    has nothing yet (race: worker just enqueued, hasn't pushed first
    state) or the bucket isn't configured (laptop dev)."""
    if _state_bucket():
        gcs_state = _read_state_gcs(slug)
        if gcs_state is not None:
            return gcs_state
        # Fall through to local — the cloud control plane's local disk
        # will normally be empty, but on the laptop in mixed-mode this
        # keeps the legacy semantics.
    p = _state_path(slug)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def request_stop(slug: str) -> None:
    """Public: ask the worker (running or not) to stop on its next tick.

    Two paths: local /tmp sentinel (handles laptop-only stops where the
    worker and the requester live on the same box) AND a GCS sentinel
    (handles cloud-initiated stops where the worker is on the laptop
    and the request came from the Cloud Run control plane). Worker
    checks both each tick — see ``_maybe_stop``."""
    _stop_path(slug).write_text(_now())
    _write_stop_sentinel_gcs(slug)


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
    """Worker tick check: stop on either local /tmp sentinel OR a
    cloud-initiated GCS stop sentinel."""
    if _stop_path(state.slug).exists():
        state.stop_requested = True
        return True
    if _read_stop_sentinel_gcs(state.slug):
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


# ---------------------------------------------------------------------------
# Comment generation + posting (MODE_COMPLETE)
# ---------------------------------------------------------------------------

_COMMENT_FALLBACK_POOL = (
    "🔥", "❤️", "Loved this", "Great content", "So good",
    "Wow", "Amazing", "Subbed", "🙌", "Take my like",
    "This is everything", "Underrated", "Brilliant", "👏👏👏",
    "Absolutely",
)


def _generate_comment_text(video_title: str, channel_label: str) -> str:
    """Call the LLM dispatcher (Azure OpenAI on cloud, Claude on laptop)
    for a short, natural-sounding comment. Falls back to a generic
    pool when the LLM call fails — never blocks the worker."""
    try:
        from pipeline.llm.cli import call_llm  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.warning("burner_engage: LLM dispatcher unavailable; using fallback pool")
        return random.choice(_COMMENT_FALLBACK_POOL)

    prompt = (
        "You're a YouTube viewer leaving a short comment on this video. "
        "Be casual, natural, 5-15 words max, no hashtags, no emojis "
        "unless they fit. Don't say 'I' or 'me'. Don't quote the title. "
        "Respond with JSON: {\"comment\": \"<your text>\"}\n\n"
        f"Channel: {channel_label}\n"
        f"Video title: {video_title}"
    )
    try:
        result = call_llm(
            prompt,
            output_json=True,
            model="haiku",
            stage="burner_engage_comment",
            timeout_s=20,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("burner_engage: LLM comment-gen failed (%s); falling back", e)
        return random.choice(_COMMENT_FALLBACK_POOL)

    text = ""
    if isinstance(result, dict):
        text = str(result.get("comment", "")).strip()
    elif isinstance(result, str):
        text = result.strip().strip('"').strip("'")
    text = text.strip()
    # Strip a trailing "Comment:" prefix the model sometimes echoes.
    for prefix in ("Comment:", "comment:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if not text or len(text) > 200:
        return random.choice(_COMMENT_FALLBACK_POOL)
    return text


_COMMENT_BOX_SELECTORS = (
    # Watch page
    "ytd-comment-simplebox-renderer #placeholder-area",
    "ytd-comment-simplebox-renderer yt-formatted-string#placeholder-text",
    # Shorts page (comment overlay)
    "ytd-comments #placeholder-area",
)
_COMMENT_TEXTAREA_SELECTORS = (
    "ytd-commentbox #contenteditable-root",
    "div#contenteditable-root[contenteditable='true']",
)
_COMMENT_SUBMIT_SELECTORS = (
    "ytd-commentbox #submit-button button",
    "ytd-commentbox button[aria-label*='Comment' i]",
    "button#submit-button",
)


def _try_comment(pg, vs: VideoState, state: EngageState) -> bool:
    """Post a comment on the active page. Returns True on success,
    False on any failure (logged into vs.error / comment_attempts).

    Strategy: focus the comment placeholder → wait for the contenteditable
    to render → type the text → click submit → verify the submit button
    becomes disabled (YouTube's signal that the comment was queued).
    """
    if not vs.comment_text:
        vs.comment_text = _generate_comment_text(vs.title, vs.channel_label)

    # 1. Focus the placeholder so YouTube swaps it for the textarea.
    placeholder = None
    for sel in _COMMENT_BOX_SELECTORS:
        try:
            cand = pg.locator(sel).first
            if cand.is_visible(timeout=2000):
                placeholder = cand
                break
        except Exception:  # noqa: BLE001
            continue
    if placeholder is None:
        # Try scrolling into view — Shorts hides the comment area until
        # the user expands the comment overlay; on /watch it's below
        # the player and needs a scroll.
        try:
            pg.evaluate("() => window.scrollBy(0, 600)")
            time.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass
        for sel in _COMMENT_BOX_SELECTORS:
            try:
                cand = pg.locator(sel).first
                if cand.is_visible(timeout=2000):
                    placeholder = cand
                    break
            except Exception:  # noqa: BLE001
                continue
    if placeholder is None:
        vs.comment_attempts += 1
        vs.error = (
            (vs.error + " | " if vs.error else "")
            + "comment: no placeholder visible (Shorts overlay closed?)"
        )[:200]
        return False

    try:
        placeholder.click(timeout=3000)
        time.sleep(0.8)
    except Exception as e:  # noqa: BLE001
        vs.comment_attempts += 1
        vs.error = (
            (vs.error + " | " if vs.error else "")
            + f"comment placeholder-click {type(e).__name__}: {e}"[:100]
        )
        return False

    # 2. Type into the contenteditable.
    typed = False
    for sel in _COMMENT_TEXTAREA_SELECTORS:
        try:
            box = pg.locator(sel).first
            if not box.is_visible(timeout=2000):
                continue
            box.click(timeout=2000)
            box.type(vs.comment_text, delay=40)
            typed = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not typed:
        vs.comment_attempts += 1
        vs.error = (
            (vs.error + " | " if vs.error else "")
            + "comment: contenteditable not typeable"
        )[:200]
        return False

    time.sleep(0.6)

    # 3. Submit.
    submitted = False
    for sel in _COMMENT_SUBMIT_SELECTORS:
        try:
            btn = pg.locator(sel).first
            if not btn.is_visible(timeout=2000):
                continue
            btn.click(timeout=3000)
            submitted = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not submitted:
        vs.comment_attempts += 1
        vs.error = (
            (vs.error + " | " if vs.error else "")
            + "comment: submit button not clickable"
        )[:200]
        return False

    time.sleep(2.0)
    vs.commented = True
    _bump_action(
        state,
        f"commented on {vs.video_id} ({vs.channel}): {vs.comment_text[:60]}",
    )
    return True


def run(slug: str, *, headless: bool = False, mode: str = DEFAULT_MODE) -> int:
    """Run the burner-engage worker for a slug. Blocks until stopped.

    ``mode`` selects the engagement intensity (one of ``ALL_MODES``):

    * ``MODE_SUBSCRIBE_ONLY`` — sub each unique source channel, exit.
    * ``MODE_LIKE_SUBSCRIBE`` — like + sub each video, exit.
    * ``MODE_LIKE_SUBSCRIBE_VIEW`` — like + sub + permanent watch loop
      (default, matches pre-2026-05-11 behaviour).
    * ``MODE_COMPLETE`` — like + sub + watch loop + LLM-generated
      comments on a random 30% subset (capped at
      ``MAX_COMMENTS_PER_RUN``).
    """
    if mode not in ALL_MODES:
        print(
            f"error: unknown mode {mode!r}. Valid: {ALL_MODES}",
            file=sys.stderr,
        )
        return 2
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
    # Brand-account switcher — battle-tested helper from the sibling
    # cross-engage module. One Google account often owns several
    # burner brand accounts (e.g. rsinghtomar54@gmail.com hosts
    # afddfdf, ajfsbqe, axkxlwv, cbxqzlyivk). YouTube tracks the
    # active brand via cookies; without an explicit switch the Like /
    # Subscribe lands on whichever brand was last selected in that
    # profile (which could be the wrong burner — or even the user's
    # personal channel). This helper hits the avatar → "Switch
    # account" flow and verifies via studio.youtube.com URL probe.
    from pipeline.cross_engage.cross_engage_burner_attached import (
        switch_to_burner_brand,
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

    # Clear any stale stop signal from a previous run (local /tmp AND
    # the cloud GCS sentinel — see clear_stop_sentinel above).
    _stop_path(slug).unlink(missing_ok=True)
    clear_stop_sentinel(slug)

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
        mode=mode,
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

    # MODE_COMPLETE: pre-pick the random subset of videos that will
    # receive comments. Doing it once at run start makes the choice
    # stable across cycle re-visits — otherwise the cycle could pick a
    # different ~30% on each pass and end up commenting on more than
    # MAX_COMMENTS_PER_RUN videos. Capped at MAX_COMMENTS_PER_RUN since
    # 30% of 58 ≈ 17 > 15.
    comment_subset_ids: set[str] = set()
    if mode == MODE_COMPLETE and state.videos:
        n_comment = min(
            MAX_COMMENTS_PER_RUN,
            max(1, int(len(state.videos) * COMMENT_SUBSET_FRACTION)),
        )
        comment_subset_ids = {
            v.video_id for v in random.sample(state.videos, n_comment)
        }
        _bump_action(
            state,
            f"COMPLETE mode: will comment on {n_comment} of "
            f"{len(state.videos)} videos (random subset, "
            f"≥{MIN_SECONDS_BETWEEN_COMMENTS}s between comments)",
        )

    _bump_action(state, f"initializing (mode={mode}, catalog={len(state.videos)} videos)")

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

    _bump_action(state, f"launching Chrome (profile={profile_dir}, headless={headless})…")
    try:
        chrome_proc, cdp_port = launch_chrome_for(
            profile_dir, work_dir=work_dir, headless=headless,
        )
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

            # ── Brand-account switch ─────────────────────────────────
            # Without this step, every Like / Subscribe lands on
            # whatever brand YouTube remembers as last-active in the
            # signed-in profile — frequently the wrong burner (one
            # Google account hosts several) or the user's personal
            # channel. Hard-fail if the switch can't be verified;
            # engaging from the wrong identity is worse than failing
            # visibly.
            switch_page = ctx.new_page()
            switch_page.set_default_timeout(15000)
            try:
                _bump_action(
                    state,
                    f"switching to burner brand {burner['channel_id']} "
                    f"({burner.get('title', slug)})…",
                )
                ok = switch_to_burner_brand(
                    switch_page, work_dir, burner=burner,
                )
                if not ok:
                    state.phase = "failed"
                    _bump_action(
                        state,
                        "brand-account switch failed — refusing to "
                        "engage on the wrong channel",
                    )
                    return 1
                _bump_action(
                    state,
                    f"active brand confirmed = {burner['channel_id']}",
                )
            except Exception as e:  # noqa: BLE001
                state.phase = "failed"
                _bump_action(state, f"brand-switch crashed: {e}")
                return 1
            finally:
                try:
                    switch_page.close()
                except Exception:  # noqa: BLE001
                    pass

            # ── Phase 0: open ALL tabs at once ────────────────────────
            # Watch-time accumulates from minute one (Shorts player auto-
            # loops in every open tab) instead of waiting for the slow
            # per-video sequential engage phase to finish. Throttled to
            # 1 second between opens so we don't overwhelm Chrome with
            # 58 simultaneous network requests AND don't trigger
            # YouTube's anti-automation heuristics with rapid-fire
            # navigations from a single signed-in session.
            state.phase = "engaging"
            _bump_action(
                state,
                f"opening {len(state.videos)} tabs at once (1s/tab)…",
            )

            video_pages: dict[Any, VideoState] = {}
            for vs in state.videos:
                if _maybe_stop(state):
                    break
                # Use the Shorts player when video_id is available — the
                # Shorts UI auto-loops the clip indefinitely, multiplying
                # the watch-time signal we accumulate via the cycle.
                # The regular /watch player stops at video end and idles.
                play_url = vs.url
                if "/shorts/" not in play_url and vs.video_id:
                    play_url = f"https://www.youtube.com/shorts/{vs.video_id}"
                try:
                    page = ctx.new_page()
                    page.set_default_timeout(15000)
                    # wait_until="commit" returns as soon as the
                    # navigation commits (a few hundred ms) instead of
                    # waiting for full DOMContentLoaded (which slows
                    # to 30+ s once 10+ Shorts are auto-playing in
                    # parallel — they compete for bandwidth and CPU).
                    # Phase 1's first cycle-visit gives each page
                    # plenty of time to fully render before _probe_*
                    # runs; if the Like button isn't ready yet, the
                    # probe returns "unknown" and the next visit picks
                    # it up.
                    page.goto(play_url, wait_until="commit", timeout=30000)
                    vs.tab_open = True
                    video_pages[page] = vs
                    if "accounts.google.com" in page.url:
                        vs.error = "redirected to sign-in (cookies stale)"
                        _bump_action(
                            state,
                            f"sign-in redirect on {vs.video_id} — bridge stale",
                        )
                    else:
                        _bump_action(
                            state,
                            f"opened {vs.video_id} ({vs.channel}) [{len(video_pages)}/{len(state.videos)}]",
                        )
                except Exception as e:  # noqa: BLE001
                    vs.error = f"open: {type(e).__name__}: {e}"[:200]
                    _bump_action(
                        state,
                        f"open failed for {vs.video_id}: {vs.error}",
                    )
                # Per-tab throttle (user-set 1s — see commit 2026-05-10).
                time.sleep(1.0)

            if _maybe_stop(state):
                state.phase = "stopped"
                _bump_action(state, "stop requested during tab-open phase")
                return 0

            pages: list[Any] = list(video_pages.keys())
            if not pages:
                state.phase = "failed"
                _bump_action(state, "no tabs opened — nothing to cycle")
                return 1

            # ── Phase 1: cycle tabs (act + verify + watch + maybe-comment) ────
            # Single unified loop replaces the old Phase-1 (sequential
            # engage) + Phase-2 (watch-only). Every tick picks one tab,
            # performs missing engagement (Like / per-channel Subscribe
            # / per-video Comment if mode says so) with post-click
            # verification, dwells for watch-time, repeats. Idempotent
            # — verifies state each visit so any transient YouTube race
            # or rate-limit gets retried on the next pass.
            #
            # Exit semantics differ by mode:
            #   subscribe_only / like_subscribe → exit when all required
            #     actions are complete (or attempts maxed). Bounded run.
            #   like_subscribe_view / complete → infinite watch loop;
            #     exit only on Stop signal.
            do_like = mode in (
                MODE_LIKE_SUBSCRIBE,
                MODE_LIKE_SUBSCRIBE_VIEW,
                MODE_COMPLETE,
            )
            do_watch_loop = mode in (
                MODE_LIKE_SUBSCRIBE_VIEW,
                MODE_COMPLETE,
            )
            do_comments = mode == MODE_COMPLETE

            state.phase = "watching"
            _bump_action(
                state,
                f"cycling {len(pages)} tabs (mode={mode}, "
                f"do_like={do_like}, do_watch_loop={do_watch_loop}, "
                f"do_comments={do_comments})…",
            )

            tick = 0
            err_streaks: dict[Any, int] = {}
            comments_made = 0
            last_comment_at = 0.0
            while True:
                if _maybe_stop(state):
                    state.phase = "stopped"
                    _bump_action(state, "stop signal received — exiting")
                    break

                # Prefer tabs that still need engagement work; fall back
                # to any tab once everything's done (pure watch-time).
                # "Needs work" depends on mode — subscribe_only mode
                # never needs Like work even when vs.liked is False.
                needs_work: list[Any] = []
                for p in pages:
                    cand = video_pages[p]
                    if cand.error.startswith("redirected to sign-in"):
                        continue
                    needs_like = (
                        do_like
                        and (not cand.liked)
                        and cand.like_attempts < 3
                    )
                    needs_sub = (
                        (not cand.subscribed)
                        and cand.channel not in subscribed_channels
                        and cand.sub_attempts < 3
                    )
                    needs_comment = (
                        do_comments
                        and cand.video_id in comment_subset_ids
                        and not cand.commented
                        and cand.comment_attempts < 3
                    )
                    if needs_like or needs_sub or needs_comment:
                        needs_work.append(p)

                # Bounded modes: exit when nothing needs work AND we're
                # not in a watch-loop mode. The infinite watch-loop
                # modes fall through to "pick any tab for watch-time"
                # instead of exiting.
                if not needs_work and not do_watch_loop:
                    state.phase = "stopped"
                    _bump_action(
                        state,
                        f"all engagement done for mode={mode} — exiting clean",
                    )
                    break

                pool = needs_work if needs_work else pages
                pg = random.choice(pool)
                vs = video_pages[pg]

                try:
                    pg.bring_to_front()
                    time.sleep(0.5)

                    # Dismiss any consent / sign-in interstitial that
                    # may have appeared since the tab first loaded.
                    for sel in (
                        "button:has-text('Accept all')",
                        "button:has-text('I agree')",
                    ):
                        try:
                            pg.locator(sel).first.click(timeout=800)
                            time.sleep(0.3)
                        except Exception:  # noqa: BLE001
                            continue

                    if "accounts.google.com" in pg.url:
                        vs.error = "redirected to sign-in (cookies stale)"
                        _save_state(state)
                        continue

                    # ─ LIKE: act-or-verify (skip in subscribe_only) ───
                    if do_like and not vs.liked and vs.like_attempts < 3:
                        like_state, like_btn = _probe_like(pg)
                        if like_state == "liked":
                            vs.liked = True
                            _bump_action(state, f"verified liked {vs.video_id}")
                        elif like_state == "unliked" and like_btn is not None:
                            try:
                                like_btn.dispatch_event("click", timeout=8000)
                                time.sleep(2.5)
                                state2, _ = _probe_like(pg)
                                if state2 == "liked":
                                    vs.liked = True
                                    _bump_action(
                                        state,
                                        f"liked {vs.video_id} ({vs.channel})",
                                    )
                                else:
                                    vs.like_attempts += 1
                                    if vs.like_attempts >= 3:
                                        vs.error = "like-click ineffective after 3 tries"
                            except Exception as e:  # noqa: BLE001
                                vs.like_attempts += 1
                                vs.error = f"like-click {type(e).__name__}: {e}"[:200]

                    # ─ SUBSCRIBE: act-or-verify, per-channel ──────────
                    if (
                        not vs.subscribed
                        and vs.channel not in subscribed_channels
                        and vs.sub_attempts < 3
                    ):
                        sub_state, sub_btn = _probe_subscribe(pg)
                        if sub_state == "subscribed":
                            vs.subscribed = True
                            subscribed_channels.add(vs.channel)
                            _bump_action(
                                state,
                                f"verified subscribed to {vs.channel}",
                            )
                        elif sub_state == "unsubscribed" and sub_btn is not None:
                            try:
                                sub_btn.scroll_into_view_if_needed(timeout=3000)
                                time.sleep(0.4)
                                sub_btn.click(force=True, timeout=8000)
                                time.sleep(2.5)
                                state2, _ = _probe_subscribe(pg)
                                if state2 == "subscribed":
                                    vs.subscribed = True
                                    subscribed_channels.add(vs.channel)
                                    _bump_action(
                                        state,
                                        f"subscribed to {vs.channel}",
                                    )
                                else:
                                    vs.sub_attempts += 1
                            except Exception as e:  # noqa: BLE001
                                vs.sub_attempts += 1
                                vs.error = (
                                    (vs.error + " | " if vs.error else "")
                                    + f"sub-click {type(e).__name__}: {e}"[:100]
                                )

                    # ─ COMMENT: only in MODE_COMPLETE, only on the
                    #   pre-picked random subset, rate-limited globally. ──
                    if (
                        do_comments
                        and vs.video_id in comment_subset_ids
                        and not vs.commented
                        and vs.comment_attempts < 3
                        and comments_made < MAX_COMMENTS_PER_RUN
                        and (time.monotonic() - last_comment_at) >= MIN_SECONDS_BETWEEN_COMMENTS
                    ):
                        if _try_comment(pg, vs, state):
                            comments_made += 1
                            last_comment_at = time.monotonic()

                    # ─ Watch-time accumulation ────────────────────────
                    dwell = random.uniform(8.0, 20.0)
                    vs.last_focused_at = _now()
                    vs.watch_seconds += int(dwell)
                    if not needs_work:
                        # Pure watch-time mode — emit a quieter line so
                        # the dashboard has live activity without spam.
                        _bump_action(
                            state,
                            f"watching {vs.video_id} ({vs.channel}) {int(dwell)}s",
                        )

                    # State save cadence: every 5 ticks keeps the GCS
                    # push count manageable while keeping the UI poll
                    # responsive (~5 ticks × ~10s = 50s max stale).
                    tick += 1
                    err_streaks[pg] = 0  # success → reset error streak
                    if tick % 5 == 0:
                        _save_state(state)

                    end = time.monotonic() + dwell
                    while time.monotonic() < end:
                        if _maybe_stop(state):
                            break
                        time.sleep(2.0)

                except Exception as e:  # noqa: BLE001
                    _bump_action(state, f"tab error on {vs.video_id} (skipping): {e}")
                    # Track consecutive errors per tab. Three strikes
                    # AND it's out — covers both "page object is dead"
                    # (pg.title raises) AND "page object is alive but
                    # something keeps failing" (bring_to_front raises
                    # repeatedly, etc). Without this, a persistently-
                    # broken tab would spin the cycle forever.
                    err_streaks[pg] = err_streaks.get(pg, 0) + 1
                    page_alive = True
                    try:
                        pg.title()
                    except Exception:  # noqa: BLE001
                        page_alive = False
                    if not page_alive or err_streaks[pg] >= 3:
                        pages = [p for p in pages if p is not pg]
                        video_pages.pop(pg, None)
                        err_streaks.pop(pg, None)
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
    # Also clear the GCS sentinel so the next /engage POST doesn't
    # need to remember to do it (defensive — the POST path also clears).
    clear_stop_sentinel(slug)
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
    rp.add_argument(
        "--mode",
        choices=ALL_MODES,
        default=DEFAULT_MODE,
        help=f"Engagement intensity (default {DEFAULT_MODE}).",
    )
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
        return run(args.slug, headless=args.headless, mode=args.mode)
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
