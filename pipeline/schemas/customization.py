"""Per-channel customization schema — derived from each channel's
``config.yaml``, surfaced via ``/api/channels/{ch}/customization_schema``.

Adding a new knob is a YAML edit (or, for cross-channel knobs, a tweak
to ``DEFAULT_FIELDS`` below). The frontend never hard-codes a knob.

The schema is intentionally a thin Pydantic model — JSON-friendly,
versionable, and trivial to mirror in TypeScript (`web-next/lib/types.ts`).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Per-channel UI/render presentation metadata. Channel artifact LOCATIONS
# (yaml + variants_dir) are NOT hardcoded here — they derive from
# ``pipeline.channels`` (the single source of truth for where channel
# files live). This module owns only what's UI-specific: label,
# tagline, language hint, default render format.
_CHANNEL_PRESENTATION: list[dict[str, Any]] = [
    {"key": "mystoriesanimated", "label": "MyStoriesAnimated",
     "tagline": "Reddit AITA & TIFU drama, animated 2D crayon",
     "language": "en", "default_format": "animated", "has_variants": True},
    {"key": "sportsrecapped", "label": "SportsRecapped",
     "tagline": "Tifo line-art with real broadcast cut-ins",
     "language": "en", "default_format": "animated", "has_variants": True},
    {"key": "hindutavaanimated", "label": "HindutavaAnimated",
     "tagline": "Mahabharat & Ramayan, Amar Chitra Katha style",
     "language": "hi", "default_format": "animated", "has_variants": False},
    {"key": "historyrecapped", "label": "History Recapped",
     "tagline": "Archival footage documentaries · Shorts & sleep long-form",
     "language": "en", "default_format": "footage_only", "has_variants": False},
    {"key": "rhymetimejunction", "label": "Rhyme Time Junction",
     "tagline": "Bilingual nursery rhymes with mascots",
     "language": "hi-en", "default_format": "rhyme", "has_variants": False},
    {"key": "scrollpulse", "label": "ScrollPulse",
     "tagline": "Reddit/X brain-rot split-screen + daily AI/tech news recaps",
     "language": "en", "default_format": "split_screen", "has_variants": False},
    {"key": "cosmosdecoded", "label": "Cosmos Decoded",
     "tagline": "Astronomy explainers, image-driven",
     "language": "en", "default_format": "animated", "has_variants": False},
]


def _build_channel_registry() -> list[dict[str, Any]]:
    """Compose CHANNEL_REGISTRY from presentation metadata + the
    channel-locations SoT in ``pipeline.channels``. Paths are derived,
    never hardcoded — renaming ``pipeline/channels`` is one edit there.
    """
    from pipeline.channels import (  # noqa: PLC0415 — avoid circular at import
        _channel_yaml_path, _channel_variants_dir,
    )
    out: list[dict[str, Any]] = []
    for p in _CHANNEL_PRESENTATION:
        slug = p["key"]
        out.append({
            "key": slug,
            "label": p["label"],
            "tagline": p["tagline"],
            "yaml": _channel_yaml_path(slug),
            "variants_dir": (_channel_variants_dir(slug)
                             if p["has_variants"] else None),
            "language": p["language"],
            "default_format": p["default_format"],
        })
    return out


CHANNEL_REGISTRY: list[dict[str, Any]] = _build_channel_registry()


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class FieldOption(BaseModel):
    value: str
    label: str
    description: str | None = None


class CustomizationField(BaseModel):
    key: str
    label: str
    kind: str  # select | text | textarea | number | slider | switch | url | datetime | hidden
    help: str | None = None
    default: Any = None
    required: bool = False
    options: list[FieldOption] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    placeholder: str | None = None
    max_length: int | None = Field(default=None, alias="maxLength")

    class Config:
        populate_by_name = True


class CustomizationSchema(BaseModel):
    channel: str
    label: str
    tagline: str
    language: str
    variants: list[FieldOption]
    fields: list[CustomizationField]
    # Sidecar-saved default variant (the "niche") — round-trips through
    # the channel /defaults editor's Niche tab so the create wizard can
    # pre-pick it before falling back to the YAML's default_format.
    default_variant: str | None = None


class RecentVideo(BaseModel):
    """A tiny per-video tile for the channel hero card preview reel."""

    video_id: str
    title: str
    thumbnail: str | None = None
    views: int | None = None
    watch_url: str


class ChannelSummary(BaseModel):
    """Lightweight payload for the /app/channels gallery."""

    key: str
    label: str
    tagline: str
    language: str
    default_format: str
    default_voice: str | None = None
    default_length_s: int | None = None
    image_provider: str | None = None
    tts_provider: str | None = None
    # Audio backend declared by the channel YAML — drives the Customize
    # form's Voice/Song flip default. Values: "tts" (default) | "sunoapi"
    # | "external_song". The frontend reads this and pre-selects the
    # Song tab for sunoapi/external_song channels (rhymetimejunction
    # today). Always-present so the user can flip both ways per render.
    audio_provider: str = "tts"
    has_overrides: bool = False
    variants_count: int = 0

    # Personality fields — populated from data/research/youtube/<account>.json
    # when the account has been auth'd + refreshed (see
    # pipeline.research.youtube + pipeline.research.channel_assets).
    # Channels without YT auth get ``None`` and the UI falls back to a
    # monogram / niche-themed gradient.
    avatar_url: str | None = None
    banner_url: str | None = None
    youtube_url: str | None = None
    custom_url: str | None = None
    subscribers: int | None = None
    youtube_video_count: int | None = None
    total_views: int | None = None
    recent_videos: list[RecentVideo] = []


# ---------------------------------------------------------------------------
# YAML reading + sidecar overrides
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("channel YAML missing: %s", path)
        return {}
    try:
        with path.open() as fp:
            return yaml.safe_load(fp) or {}
    except Exception:  # noqa: BLE001
        logger.warning("failed to parse %s", path, exc_info=True)
        return {}


def _sidecar_path(channel_key: str) -> Path:
    """User overrides live in <channel>/.user_defaults.json — never touch the YAML."""
    entry = _registry_entry(channel_key)
    if not entry:
        return PROJECT_ROOT / channel_key / ".user_defaults.json"
    return PROJECT_ROOT / Path(entry["yaml"]).parent / ".user_defaults.json"


def load_user_defaults(channel_key: str) -> dict[str, Any]:
    p = _sidecar_path(channel_key)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        logger.warning("failed to parse %s", p, exc_info=True)
        return {}


def save_user_defaults(channel_key: str, defaults: dict[str, Any]) -> None:
    p = _sidecar_path(channel_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(defaults, indent=2, sort_keys=True))


def _registry_entry(channel_key: str) -> dict[str, Any] | None:
    for entry in CHANNEL_REGISTRY:
        if entry["key"] == channel_key:
            return entry
    return None


def _list_variants(entry: dict[str, Any]) -> list[FieldOption]:
    """Either: variant YAMLs in <variants_dir>, OR the channel's
    ``default_format`` as a single option.

    For variant YAMLs we honour two top-level fields when present:
      - ``name``        → human label (e.g. "AITA Animated", "TIFU")
      - ``description`` → one-line blurb shown under the variant card

    Both fall back to slug-derived defaults if absent — but the slug
    fallback is acronym-aware so e.g. ``aita_animated`` becomes
    "AITA Animated", not "Aita Animated".
    """
    out: list[FieldOption] = []
    vd = entry.get("variants_dir")
    if vd:
        path = PROJECT_ROOT / vd
        if path.exists():
            for v in sorted(path.glob("*.yaml")):
                stem = v.stem
                vdoc = _load_yaml(v)
                label = (
                    str(vdoc.get("name") or "").strip()
                    or _humanize_slug(stem)
                )
                description = str(vdoc.get("description") or "").strip() or None
                out.append(FieldOption(value=stem, label=label, description=description))
    if not out:
        fmt = entry.get("default_format", "animated")
        out.append(FieldOption(value=fmt, label=_humanize_slug(fmt)))
    return out


# Acronyms / brand tokens that should stay uppercase when we have to
# fall back to slug-derived labels. Keep small and targeted — it's only
# used when a variant YAML has no ``name:`` field.
_ACRONYMS: set[str] = {
    "AITA", "TIFU", "AI", "AIO", "NPC", "NSFW", "NTA", "YTA", "ESH", "NAH",
    "MMA", "NBA", "NFL", "MLB", "NHL", "UFC", "FAQ", "DIY", "POV", "ASMR",
    "TIL", "ELI5", "IMHO", "BRB", "FYI", "MVP", "DNF",
}


def _humanize_slug(slug: str) -> str:
    """Slug → human label, with known acronyms preserved.

    >>> _humanize_slug("aita_animated")
    'AITA Animated'
    >>> _humanize_slug("tifu")
    'TIFU'
    >>> _humanize_slug("today_in_history")
    'Today in History'
    """
    smalls = {"in", "of", "and", "or", "the", "a", "an", "to", "for", "on"}
    words = slug.replace("-", "_").split("_")
    out: list[str] = []
    for i, w in enumerate(words):
        if not w:
            continue
        upper = w.upper()
        if upper in _ACRONYMS:
            out.append(upper)
        elif i > 0 and w.lower() in smalls:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


# ---------------------------------------------------------------------------
# Default cross-channel knobs — every channel exposes these
# ---------------------------------------------------------------------------


def _length_field(default_s: int) -> CustomizationField:
    return CustomizationField(
        key="length_s",
        label="Length",
        kind="slider",
        help="Target length in seconds",
        default=default_s,
        min=15,
        max=90,
        step=5,
    )


def _topic_field() -> CustomizationField:
    return CustomizationField(
        key="topic",
        label="Topic",
        kind="textarea",
        help="One-line description of what the Short is about",
        required=True,
        placeholder="The 1962 Cuban Missile Crisis — thirteen days that nearly ended the world.",
        max_length=500,
    )


def _source_kind_field(allowed: Iterable[str]) -> CustomizationField:
    opts = [FieldOption(value=k, label=_pretty(k)) for k in allowed]
    return CustomizationField(
        key="source_kind",
        label="Source",
        kind="select",
        default=opts[0].value if opts else "auto",
        options=opts,
        help="Where to pull the story from",
    )


def _source_ref_field() -> CustomizationField:
    return CustomizationField(
        key="source_ref",
        label="Source link or topic",
        kind="text",
        help="Reddit URL, Wikipedia topic, or freeform text — leave blank for auto-pick",
        placeholder="https://reddit.com/r/AmItheAsshole/...  or  Cuban Missile Crisis",
    )


def _visibility_field() -> CustomizationField:
    return CustomizationField(
        key="visibility",
        label="Visibility on publish",
        kind="select",
        default="unlisted",
        options=[
            FieldOption(value="public", label="Public"),
            FieldOption(value="unlisted", label="Unlisted (default)"),
            FieldOption(value="private", label="Private"),
        ],
    )


def _schedule_field() -> CustomizationField:
    return CustomizationField(
        key="schedule_at",
        label="Schedule (optional)",
        kind="datetime",
        help="Leave blank to publish immediately on approve",
    )


def _notes_field() -> CustomizationField:
    return CustomizationField(
        key="notes",
        label="Notes",
        kind="textarea",
        help="Anything extra — visual cues, character names, language preference",
        placeholder="",
        max_length=1000,
    )


def _pretty(s: str) -> str:
    return s.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Per-channel knob extras (channel-specific knobs on top of defaults)
# ---------------------------------------------------------------------------


def _voice_options_for(language: str) -> list[FieldOption]:
    if language.startswith("hi"):
        return [
            FieldOption(value="hf_alpha", label="Hindi female · Alpha"),
            FieldOption(value="hf_beta", label="Hindi female · Beta"),
            FieldOption(value="hm_omega", label="Hindi male · Omega"),
        ]
    if language == "hi-en":
        return [FieldOption(value="external_song", label="Sung (external)")]
    # English defaults
    return [
        FieldOption(value="sarah", label="Sarah · documentary"),
        FieldOption(value="am_michael", label="Michael · documentary"),
        FieldOption(value="bf_isabella", label="Isabella · narrator"),
    ]


def _voice_field(default_voice: str, language: str) -> CustomizationField:
    return CustomizationField(
        key="voice",
        label="Voice",
        kind="select",
        default=default_voice,
        options=_voice_options_for(language),
        help="Narrator voice. Cloud Run TTS handles synthesis.",
    )


def _captions_density_field() -> CustomizationField:
    return CustomizationField(
        key="captions_density",
        label="Captions density",
        kind="select",
        default="standard",
        options=[
            FieldOption(value="minimal", label="Minimal · 1 line"),
            FieldOption(value="standard", label="Standard · 2 lines"),
            FieldOption(value="dense", label="Dense · 3 lines"),
        ],
    )


def _music_field(default: str = "ambient_low") -> CustomizationField:
    return CustomizationField(
        key="music_bed",
        label="Music bed",
        kind="select",
        default=default,
        options=[
            FieldOption(value="off", label="Off"),
            FieldOption(value="ambient_low", label="Ambient · low"),
            FieldOption(value="ambient_med", label="Ambient · medium"),
            FieldOption(value="cinematic", label="Cinematic"),
            FieldOption(value="upbeat", label="Upbeat"),
        ],
    )


# ---------------------------------------------------------------------------
# Voice / Song flip — Customize step renders these as a 2-way segmented
# control inside the Audio section. The Song tab exposes the actual
# Suno API knobs (verified against pipeline/tts/song.py::synth_via_sunoapi):
# the wrapper takes only style (free-text), vocal_gender (f/m), and
# model (V4_5/V5). instrumental and title are intentionally not exposed
# (rhymes always need lyrics; title is auto-derived from slug).
# ---------------------------------------------------------------------------


def _audio_mode_field(audio_provider: str) -> CustomizationField:
    """Voice vs Song flip. Default tab = Song iff the channel YAML's
    audio_provider is sunoapi or external_song (rhymetimejunction).
    Both tabs are always present so any channel can flip per render —
    on a TTS channel that picks Song we route through synth_via_sunoapi
    (requires SUNOAPI_API_KEY + a lyrics script), and on a song channel
    that picks Voice we route through the channel's tts_provider.
    """
    is_song_default = audio_provider in ("sunoapi", "external_song")
    return CustomizationField(
        key="audio_mode",
        label="Audio mode",
        kind="select",
        default="song" if is_song_default else "voice",
        options=[
            FieldOption(value="voice", label="Voice", description="Spoken narration via TTS"),
            FieldOption(value="song", label="Song", description="Sung audio via Suno"),
        ],
        help="Voice = spoken narration · Song = full sung audio (Suno).",
    )


def _song_style_field(ydoc: dict[str, Any], language: str) -> CustomizationField:
    """Free-text style description passed to Suno's `style` parameter.

    Default seeded from a channel-flavour hint — Hinglish/kid for rhyme,
    generic upbeat for everything else. Users edit before render.
    """
    seeded = ydoc.get("default_song_style") or ""
    if not seeded:
        if language.startswith("hi"):
            seeded = (
                "cheerful upbeat children's nursery rhyme, female lead with "
                "kids choir, gentle acoustic guitar + tabla, 120 BPM"
            )
        else:
            seeded = "uplifting cinematic pop, female lead, warm strings, 110 BPM"
    return CustomizationField(
        key="song_style",
        label="Song style",
        kind="textarea",
        default=seeded,
        help="Genre + instruments + tempo — passed verbatim to Suno's `style` parameter.",
        placeholder=(
            "cheerful upbeat children's nursery rhyme, female lead with "
            "kids choir, gentle acoustic guitar + tabla, 120 BPM"
        ),
        max_length=500,
    )


def _song_vocal_gender_field(ydoc: dict[str, Any]) -> CustomizationField:
    return CustomizationField(
        key="song_vocal_gender",
        label="Vocal gender",
        kind="select",
        default=str(ydoc.get("sunoapi_vocal_gender") or "f"),
        options=[
            FieldOption(value="f", label="Female"),
            FieldOption(value="m", label="Male"),
        ],
    )


def _song_model_field(ydoc: dict[str, Any]) -> CustomizationField:
    return CustomizationField(
        key="song_model",
        label="Suno model",
        kind="select",
        default=str(ydoc.get("sunoapi_model") or "V4_5"),
        options=[
            FieldOption(value="V4_5", label="V4_5", description="Best for kids' content + bilingual"),
            FieldOption(value="V5", label="V5", description="Newer · richer production"),
        ],
    )


# ---------------------------------------------------------------------------
# Background visuals — 3-way picker rendered as its own card in Customize
# ---------------------------------------------------------------------------


def _visual_source_default_for(default_format: str) -> str:
    """Map the channel YAML's default_format to one of ai/footage/both.

    Keeps the picker pre-selected at whatever the channel ships as today
    so changing the picker is opt-in rather than silently flipping the
    visual style on the user.
    """
    if default_format in ("footage_only", "footage"):
        return "footage"
    if default_format == "split_screen":
        return "both"
    # animated, rhyme, sports_doc, long_form — all generative-image paths
    return "ai"


def _visual_source_field(default_format: str) -> CustomizationField:
    """3-way segmented: AI generations / Real footage / Both.

    Honored by pipeline/render/shorts.py via cfg["visual_source"]:
      - ai      → channel's existing image_provider runs (current default)
      - footage → routes to the dedicated footage_only render path; warns
                  + falls back to AI if the channel has no footage data
      - both    → respects per-beat `kind: footage` tags via the existing
                  compose_hybrid path (sportsrecapped uses this today)
    """
    return CustomizationField(
        key="visual_source",
        label="Background visuals",
        kind="select",
        default=_visual_source_default_for(default_format),
        options=[
            FieldOption(
                value="ai",
                label="AI generations",
                description="Image-gen per beat (animated / illustrated)",
            ),
            FieldOption(
                value="footage",
                label="Real footage",
                description="Stock + archival video clips per beat",
            ),
            FieldOption(
                value="both",
                label="Both",
                description="Mix per beat — AI for character moments, footage for context",
            ),
        ],
        help="What plays behind the audio. Default is the channel's house style.",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _account_for(entry: dict[str, Any], ydoc: dict[str, Any]) -> str:
    """The OAuth/upload account used for YouTube — used to look up the
    cached YT brand assets in ``data/research/youtube/<account>.json``.

    Falls back to the channel directory name (registry key) if
    ``upload.account`` isn't set in the YAML — matches the same
    fallback pipeline.research.youtube.iter_channel_configs uses.
    """
    upload = ydoc.get("upload") or {}
    return upload.get("account") or entry["key"]


def _live_channel_via_api_key(account: str) -> dict[str, Any] | None:
    """Fetch live channel rollup via YouTube Data API v3 + ``YOUTUBE_API_KEY``.

    Cold-start fallback for ``_personality_for`` when the GCS YT cache
    is empty (typical right after a cloud deploy, before the hourly
    stats-refresh JOB has run).

    Channel-id discovery in priority order:

      1. ``/secrets/youtube-channel-ids/value`` (Secret Manager mount;
         the canonical account → channel_id registry, populated by the
         laptop pipeline.research.cross_engage discovery).
      2. ``~/.config/ytfactory/channel_ids.json`` (laptop fallback).
      3. Probe via any cached upload-record's video_id (``videos.list``
         → ``snippet.channelId``).

    Then ``channels.list`` for the per-channel rollup. In-process cached
    for 10 minutes per account so we don't burn quota on every poll.

    Returns ``None`` (and is silent) on any failure — caller falls back
    to ``None``-stats. The dashboard cards endpoint always works
    independently because it has its own per-video stats cache.
    """
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    cached = _LIVE_CHANNEL_CACHE.get(account)
    if cached and (time.time() - cached[0]) < _LIVE_CHANNEL_TTL_S:
        return cached[1]

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return None

    cid = _channel_id_for_account(account)
    if not cid:
        return None

    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    # channels.list → subscriber_count + view_count + video_count
    try:
        params = urllib.parse.urlencode({
            "part": "snippet,statistics,brandingSettings",
            "id": cid,
            "key": api_key,
        })
        with urllib.request.urlopen(
            f"https://www.googleapis.com/youtube/v3/channels?{params}",
            timeout=8,
        ) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    items = data.get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    branding = (item.get("brandingSettings") or {}).get("image") or {}
    thumbs = snippet.get("thumbnails") or {}
    avatar_url = (
        (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {})
        .get("url")
    )

    out = {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "subscriber_count": (
            int(stats["subscriberCount"]) if "subscriberCount" in stats else None
        ),
        "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
        "video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
        "custom_url": snippet.get("customUrl"),
        "avatar_url": avatar_url,
        "banner_url": branding.get("bannerExternalUrl"),
        # The fallback doesn't fetch the full uploads playlist (would
        # double our API quota cost). Recent-videos tiles stay empty
        # until the hourly stats-refresh JOB populates GCS.
        "videos": [],
    }
    _LIVE_CHANNEL_CACHE[account] = (time.time(), out)
    return out


def _channel_id_for_account(account: str) -> str | None:
    """Resolve a YouTube channel id from an OAuth-account slug.

    Priority order:
      1. ``/secrets/youtube-channel-ids/value`` (Secret Manager mount,
         canonical on cloud).
      2. ``~/.config/ytfactory/channel_ids.json`` (laptop dev fallback).
      3. Probe any cached upload-record's video_id via ``videos.list``
         → ``snippet.channelId`` (covers a freshly-onboarded channel
         that has uploads but isn't in the static registry yet).
    """
    import json as _json  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415

    # 1. Secret Manager mount
    sec = _P("/secrets/youtube-channel-ids/value")
    if sec.exists():
        try:
            registry = _json.loads(sec.read_text())
            entry = registry.get(account)
            if entry:
                cid = entry.get("channel_id")
                if cid:
                    return cid
        except (OSError, _json.JSONDecodeError):
            pass

    # 2. Laptop fallback
    laptop = _P.home() / ".config" / "ytfactory" / "channel_ids.json"
    if laptop.exists():
        try:
            registry = _json.loads(laptop.read_text())
            entry = registry.get(account)
            if entry:
                cid = entry.get("channel_id")
                if cid:
                    return cid
        except (OSError, _json.JSONDecodeError):
            pass

    # 3. Probe via uploads
    try:
        from control.routes import dashboard_routes as _dr  # noqa: PLC0415
    except Exception:
        return None
    target_video_id: str | None = None
    for _ch_slug, _slug, vid, rec in _dr._enumerate_uploads():
        if (rec.get("account") or _ch_slug) == account and vid:
            target_video_id = vid
            break
    if not target_video_id:
        return None

    import os as _os  # noqa: PLC0415
    api_key = _os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return None
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    try:
        params = urllib.parse.urlencode({
            "part": "snippet",
            "id": target_video_id,
            "key": api_key,
        })
        with urllib.request.urlopen(
            f"https://www.googleapis.com/youtube/v3/videos?{params}",
            timeout=8,
        ) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    items = data.get("items") or []
    if not items:
        return None
    return (items[0].get("snippet") or {}).get("channelId")


# Per-account in-process cache for the API-key fallback. 10-min TTL —
# subscriber counts barely move and we want to keep request-time API
# spend predictable. Each entry is (epoch_seconds, channel_dict).
_LIVE_CHANNEL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LIVE_CHANNEL_TTL_S = 600


def _personality_for(account: str) -> dict[str, Any]:
    """Read the cached YT brand fields, recent shorts, and stats.

    Returns a dict with ``avatar_url``, ``banner_url``, ``youtube_url``,
    ``custom_url``, ``subscribers``, ``youtube_video_count``,
    ``total_views``, ``recent_videos``. Missing pieces stay ``None``.
    The avatar/banner URLs prefer our locally-mirrored
    ``/api/channels/{key}/{avatar,banner}.jpg`` so the UI never
    hot-links yt3.ggpht.com (CORS / link-rot).
    """
    # Resolve via ``sys.modules`` so test mocks of
    # ``pipeline.research.youtube`` / ``channel_assets`` (via
    # ``patch.dict("sys.modules", ...)``) are honoured even after
    # the package's __dict__ has cached the real submodules. A bare
    # ``from pipeline.research import youtube as _yt`` would read
    # the package attr and skip sys.modules.
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415
    _yt = sys.modules.get("pipeline.research.youtube") or importlib.import_module(
        "pipeline.research.youtube"
    )
    channel_assets = sys.modules.get(
        "pipeline.research.channel_assets"
    ) or importlib.import_module("pipeline.research.channel_assets")

    payload = _yt.load_account(account) or {}
    channel = payload.get("channel") or {}
    videos = payload.get("videos") or []

    # Fallback: when the GCS YT cache is empty (cloud cold-start before
    # the stats-refresh JOB has run), use the YouTube Data API v3 key
    # to fetch subscriber + uploads count directly. We need the
    # channel id (UCxxxx) to query — pull it from any cached upload-
    # record's video_id (videos.list returns snippet.channelId), then
    # channels.list returns the per-channel rollup.
    if not channel.get("subscriber_count") and not videos:
        try:
            ch_live = _live_channel_via_api_key(account)
        except Exception:
            ch_live = None
        if ch_live:
            channel = {
                "id": ch_live.get("id"),
                "title": ch_live.get("title"),
                "subscriber_count": ch_live.get("subscriber_count"),
                "view_count": ch_live.get("view_count"),
                "video_count": ch_live.get("video_count"),
                "hidden_subscribers": ch_live.get("hidden_subscribers", False),
                "custom_url": ch_live.get("custom_url") or channel.get("custom_url"),
                "avatar_url": ch_live.get("avatar_url") or channel.get("avatar_url"),
                "banner_url": ch_live.get("banner_url") or channel.get("banner_url"),
            }
            videos = ch_live.get("videos") or videos

    avatar_local = channel_assets.asset_path(account, "avatar")
    banner_local = channel_assets.asset_path(account, "banner")

    # The frontend resolves these via /api/channels/{key}/{avatar,banner}.jpg
    # which serves the mirrored file when present (404 otherwise → UI
    # fallback). The full external URL is also kept around for debugging.
    avatar_url = channel.get("avatar_url")
    banner_url = channel.get("banner_url")

    # Hand-crafted recent-video tiles for the channel hero card.
    recent: list[RecentVideo] = []
    for v in videos[:3]:
        vid = v.get("video_id")
        if not vid:
            continue
        # Prefer the YT-hosted hqdefault.jpg (small, cached on Google's
        # CDN, never link-rots within YT's lifetime). The full mp4 thumb
        # ladder lives elsewhere — this is just for inline tiles.
        thumb = (
            v.get("thumbnail_url")
            or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        )
        recent.append(
            RecentVideo(
                video_id=vid,
                title=v.get("title") or vid,
                thumbnail=thumb,
                views=v.get("view_count"),
                watch_url=(
                    v.get("url")
                    or f"https://youtu.be/{vid}"
                ),
            )
        )

    youtube_url: str | None = None
    if channel.get("custom_url"):
        # customUrl from the API is the @handle form (no slash prefix in
        # newer responses); normalize to a full URL.
        handle = channel["custom_url"].lstrip("@")
        youtube_url = f"https://youtube.com/@{handle}"
    elif channel.get("id"):
        youtube_url = f"https://youtube.com/channel/{channel['id']}"

    return {
        "avatar_url": avatar_url,
        "banner_url": banner_url,
        "avatar_mirrored": avatar_local is not None,
        "banner_mirrored": banner_local is not None,
        "youtube_url": youtube_url,
        "custom_url": channel.get("custom_url"),
        "subscribers": channel.get("subscriber_count"),
        "youtube_video_count": channel.get("video_count"),
        "total_views": channel.get("view_count"),
        "recent_videos": recent,
    }


def list_channels() -> list[ChannelSummary]:
    """Build a ChannelSummary for every registered channel.

    Performance: each entry's `_personality_for(account)` makes a
    GCS read (HEAD or full download) on cloud. Sequential the loop
    paid 8 × ~30–100 ms = ~240–800 ms before any FastAPI overhead.
    Fan out across a small thread pool so the network latency is
    paid once instead of per channel.
    """
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    def _build(entry: dict) -> ChannelSummary:
        ydoc = _load_yaml(PROJECT_ROOT / entry["yaml"])
        overrides = load_user_defaults(entry["key"])
        account = _account_for(entry, ydoc)
        personality = _personality_for(account)
        return ChannelSummary(
            key=entry["key"],
            label=entry["label"],
            tagline=entry["tagline"],
            language=entry["language"],
            default_format=entry["default_format"],
            default_voice=overrides.get("voice") or _default_voice_for(entry, ydoc),
            default_length_s=overrides.get("length_s")
            or _default_length_for(entry, ydoc),
            image_provider=ydoc.get("image_provider"),
            tts_provider=ydoc.get("tts_provider"),
            audio_provider=str(ydoc.get("audio_provider") or "tts"),
            has_overrides=bool(overrides),
            variants_count=len(_list_variants(entry)),
            avatar_url=personality["avatar_url"],
            banner_url=personality["banner_url"],
            youtube_url=personality["youtube_url"],
            custom_url=personality["custom_url"],
            subscribers=personality["subscribers"],
            youtube_video_count=personality["youtube_video_count"],
            total_views=personality["total_views"],
            recent_videos=personality["recent_videos"],
        )

    if not CHANNEL_REGISTRY:
        return []
    # 8 workers covers our current registry size; scales naturally if
    # we ever add channels. I/O-bound, so a thread pool is fine.
    with ThreadPoolExecutor(
        max_workers=min(16, len(CHANNEL_REGISTRY)),
        thread_name_prefix="list-channels",
    ) as pool:
        return list(pool.map(_build, CHANNEL_REGISTRY))


def get_channel(channel_key: str) -> ChannelSummary | None:
    """Build a ChannelSummary for a single channel.

    Was: ``return next((c for c in list_channels() if c.key == channel_key), None)``
    which made all 8 channels' YAML reads + GCS HEADs to satisfy a
    single-channel lookup. Now resolves the registry entry first and
    only does the work for that one channel.
    """
    entry = _registry_entry(channel_key)
    if entry is None:
        return None
    ydoc = _load_yaml(PROJECT_ROOT / entry["yaml"])
    overrides = load_user_defaults(entry["key"])
    account = _account_for(entry, ydoc)
    personality = _personality_for(account)
    return ChannelSummary(
        key=entry["key"],
        label=entry["label"],
        tagline=entry["tagline"],
        language=entry["language"],
        default_format=entry["default_format"],
        default_voice=overrides.get("voice") or _default_voice_for(entry, ydoc),
        default_length_s=overrides.get("length_s")
        or _default_length_for(entry, ydoc),
        image_provider=ydoc.get("image_provider"),
        tts_provider=ydoc.get("tts_provider"),
        audio_provider=str(ydoc.get("audio_provider") or "tts"),
        has_overrides=bool(overrides),
        variants_count=len(_list_variants(entry)),
        avatar_url=personality["avatar_url"],
        banner_url=personality["banner_url"],
        youtube_url=personality["youtube_url"],
        custom_url=personality["custom_url"],
        subscribers=personality["subscribers"],
        youtube_video_count=personality["youtube_video_count"],
        total_views=personality["total_views"],
        recent_videos=personality["recent_videos"],
    )


def get_customization_schema(channel_key: str) -> CustomizationSchema | None:
    entry = _registry_entry(channel_key)
    if not entry:
        return None
    ydoc = _load_yaml(PROJECT_ROOT / entry["yaml"])
    overrides = load_user_defaults(channel_key)

    default_voice = overrides.get("voice") or _default_voice_for(entry, ydoc)
    default_length = overrides.get("length_s") or _default_length_for(entry, ydoc)
    audio_provider = str(ydoc.get("audio_provider") or "tts")

    fields: list[CustomizationField] = [
        _topic_field(),
        _source_kind_field(_source_kinds_for(entry)),
        _source_ref_field(),
        _audio_mode_field(audio_provider),
        _voice_field(default_voice, entry["language"]),
        _song_style_field(ydoc, entry["language"]),
        _song_vocal_gender_field(ydoc),
        _song_model_field(ydoc),
        _visual_source_field(entry["default_format"]),
        _length_field(default_length),
        _captions_density_field(),
        _music_field(overrides.get("music_bed", "ambient_low")),
        _visibility_field(),
        _schedule_field(),
        _notes_field(),
    ]

    variants = _list_variants(entry)
    saved_variant = overrides.get("default_variant")
    if saved_variant is not None and not any(v.value == saved_variant for v in variants):
        # Stale sidecar pointing at a deleted/renamed variant — drop
        # silently rather than surface a 4xx; the create wizard will
        # fall back to the YAML default_format.
        saved_variant = None

    return CustomizationSchema(
        channel=channel_key,
        label=entry["label"],
        tagline=entry["tagline"],
        language=entry["language"],
        variants=variants,
        fields=fields,
        default_variant=saved_variant,
    )


def _default_voice_for(entry: dict[str, Any], ydoc: dict[str, Any]) -> str:
    # YAML voice values can be paths (sarah.wav) or kokoro keys (am_michael).
    raw = ydoc.get("tts_voice") or ""
    if isinstance(raw, str):
        if raw.endswith(".wav"):
            return Path(raw).stem  # sarah.wav → sarah
        if raw:
            return raw
    if entry["language"].startswith("hi"):
        return "hf_alpha"
    return "sarah"


def _default_length_for(entry: dict[str, Any], ydoc: dict[str, Any]) -> int:
    rng = ydoc.get("duration_target_s")
    if isinstance(rng, list) and rng:
        try:
            return int(rng[-1])  # take the upper bound — better-default UX
        except Exception:  # noqa: BLE001
            pass
    return 55


def _source_kinds_for(entry: dict[str, Any]) -> list[str]:
    base = ["auto", "user_text"]
    key = entry["key"]
    if key in {"mystoriesanimated", "scrollpulse"}:
        base += ["reddit_url"]
    if key in {"historyrecapped", "cosmosdecoded", "hindutavaanimated"}:
        base += ["wikipedia_topic"]
    if key in {"sportsrecapped", "historyrecapped"}:
        base += ["youtube_video"]
    return base
