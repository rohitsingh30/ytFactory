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


def _personality_for(account: str) -> dict[str, Any]:
    """Read the cached YT brand fields, recent shorts, and stats.

    Returns a dict with ``avatar_url``, ``banner_url``, ``youtube_url``,
    ``custom_url``, ``subscribers``, ``youtube_video_count``,
    ``total_views``, ``recent_videos``. Missing pieces stay ``None``.
    The avatar/banner URLs prefer our locally-mirrored
    ``/api/channels/{key}/{avatar,banner}.jpg`` so the UI never
    hot-links yt3.ggpht.com (CORS / link-rot).
    """
    from pipeline.research import channel_assets, youtube as _yt

    payload = _yt.load_account(account) or {}
    channel = payload.get("channel") or {}
    videos = payload.get("videos") or []

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
    out: list[ChannelSummary] = []
    for entry in CHANNEL_REGISTRY:
        ydoc = _load_yaml(PROJECT_ROOT / entry["yaml"])
        overrides = load_user_defaults(entry["key"])
        account = _account_for(entry, ydoc)
        personality = _personality_for(account)
        out.append(
            ChannelSummary(
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
        )
    return out


def get_channel(channel_key: str) -> ChannelSummary | None:
    for c in list_channels():
        if c.key == channel_key:
            return c
    return None


def get_customization_schema(channel_key: str) -> CustomizationSchema | None:
    entry = _registry_entry(channel_key)
    if not entry:
        return None
    ydoc = _load_yaml(PROJECT_ROOT / entry["yaml"])
    overrides = load_user_defaults(channel_key)

    default_voice = overrides.get("voice") or _default_voice_for(entry, ydoc)
    default_length = overrides.get("length_s") or _default_length_for(entry, ydoc)

    fields: list[CustomizationField] = [
        _topic_field(),
        _source_kind_field(_source_kinds_for(entry)),
        _source_ref_field(),
        _voice_field(default_voice, entry["language"]),
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
