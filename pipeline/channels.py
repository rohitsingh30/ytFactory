"""Single source of truth for ytFactory production channels.

Channel data is configuration, not code. The 7 production channels
live in ``pipeline/channels.yaml`` (committed). This module loads,
validates, and exposes derived views — adding a channel is a YAML
edit, no Python change needed.

The YAML is the canonical reference for production channels (slug,
youtube_title, youtube_channel_id, niches, rotation flag, aliases).
The cloud ``youtube-channel-ids`` Secret Manager secret can drift
(stale 2026-05-04 entries persisted alongside fresh 2026-05-09
ones); treat it as a cache, not authoritative.

Renames are first-class via ``aliases``: when a channel's local
dir or slug changes but its YouTube channel_id stays the same,
record the old slug under ``aliases`` and ``resolve_channel`` will
map either form to the canonical Channel. Separate channels with
distinct channel_ids each get their own entry — never an alias
across channel_ids.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

_YAML_PATH = Path(__file__).parent / "channels.yaml"


# ---------------------------------------------------------------------------
# Where channel-specific artifacts live on disk (single source of truth).
#
# These are repo-relative directories. Per the 2026-05-10 nuclear cleanup,
# no channel-named directories live at the repo root — every per-channel
# artifact lives under one of these central trees. Callers ask the Channel
# (e.g. ``channel.channel_yaml_path()``) instead of constructing paths.
# Renaming any of these dirs is a one-line edit here.
# ---------------------------------------------------------------------------

CHANNELS_CONFIG_DIR = "pipeline/channels"
"""Per-channel render config YAMLs. ``<slug>.yaml`` per channel."""

CHANNEL_LEARNINGS_DIR = "docs/channel-learnings"
"""Per-channel authoring learnings (markdown). ``<slug>/`` subdir per channel."""

CHANNEL_VARIANTS_DIR = "pipeline/variants"
"""Per-channel variant YAMLs. ``<slug>/<variant>.yaml`` per channel."""

CHANNEL_SCRIPTS_DIR = "scripts"
"""Per-channel Python scripts. ``<slug>/`` subdir per channel."""


def _channel_yaml_path(slug: str) -> str:
    return f"{CHANNELS_CONFIG_DIR}/{slug}.yaml"


def _channel_learnings_dir(slug: str) -> str:
    return f"{CHANNEL_LEARNINGS_DIR}/{slug}"


def _channel_variants_dir(slug: str) -> str:
    return f"{CHANNEL_VARIANTS_DIR}/{slug}"


def _channel_scripts_dir(slug: str) -> str:
    return f"{CHANNEL_SCRIPTS_DIR}/{slug}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Channel(BaseModel):
    """One production ytFactory channel. Mirrors a row in channels.yaml.

    NO aliases. ``slug`` is the single canonical name; every caller
    uses exactly that string. If a channel was renamed, every
    reference in the codebase migrates in lockstep — no resolver
    layer that hides the rename.
    """

    slug: str
    platform: str = "youtube"
    youtube_title: Optional[str] = None
    youtube_channel_id: Optional[str] = None
    in_rotation: bool = True
    in_production: bool = True
    config_yaml: Optional[str] = None
    # niche_key -> [state_subdir, variant_yaml_filename]
    # YAML naturally serializes a 2-tuple as a 2-element list.
    niches: dict[str, list[str]] = Field(default_factory=dict)

    # ---- Where this channel's per-channel files live on the laptop ----
    # Single source of truth for channel artifact locations. Other
    # modules (pipeline/paths.py, pipeline/customization.py, render
    # code) read these instead of hardcoding ``pipeline/channels/<slug>``.
    # If we ever rename the central dirs, this is the only place to
    # edit; every caller automatically picks up the new path.

    def channel_yaml_path(self) -> str:
        """Repo-relative path to this channel's render config YAML."""
        return _channel_yaml_path(self.slug)

    def learnings_dir(self) -> str:
        """Repo-relative dir for this channel's authoring learnings (markdown)."""
        return _channel_learnings_dir(self.slug)

    def channel_variants_dir(self) -> str:
        """Repo-relative dir for this channel's variant YAMLs."""
        return _channel_variants_dir(self.slug)

    def channel_scripts_dir(self) -> str:
        """Repo-relative dir for this channel's per-channel Python scripts."""
        return _channel_scripts_dir(self.slug)

    def variant_yaml_path(self, niche_key: str) -> Optional[str]:
        """Repo-relative path to a specific variant YAML for ``niche_key``."""
        if niche_key not in self.niches:
            return None
        _, fname = self.niches[niche_key]
        return f"{self.channel_variants_dir()}/{fname}"

    def state_dir(self, niche_key: str) -> str:
        """GCS-relative state dir for a niche under this channel.
        Flat layout returns just the slug; niched returns slug/subdir."""
        if niche_key not in self.niches:
            return self.slug
        subdir, _ = self.niches[niche_key]
        return f"{self.slug}/{subdir}" if subdir else self.slug


# ---------------------------------------------------------------------------
# Load + validate
# ---------------------------------------------------------------------------


def _load_channels(yaml_path: Path = _YAML_PATH) -> tuple[Channel, ...]:
    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict) or "channels" not in raw:
        raise ValueError(
            f"{yaml_path} must be a mapping with a top-level 'channels' list"
        )
    parsed = [Channel.model_validate(c) for c in raw["channels"]]

    # Sanity gates: catch the failure modes we've actually hit.
    seen_slugs: set[str] = set()
    seen_channel_ids: dict[str, str] = {}
    repo_root = yaml_path.parent.parent
    for c in parsed:
        if c.slug in seen_slugs:
            raise ValueError(f"duplicate channel slug: {c.slug!r}")
        seen_slugs.add(c.slug)

        if c.youtube_channel_id:
            if c.youtube_channel_id in seen_channel_ids:
                # Two slugs claiming the same YouTube channel_id =
                # the SoT is lying. Block at load time.
                other = seen_channel_ids[c.youtube_channel_id]
                raise ValueError(
                    f"duplicate youtube_channel_id {c.youtube_channel_id!r} "
                    f"on slugs {c.slug!r} and {other!r} — pick one "
                    f"canonical slug and migrate every caller to it"
                )
            seen_channel_ids[c.youtube_channel_id] = c.slug

        # Audit T1.1: an in_rotation channel whose config_yaml doesn't
        # exist on disk crashes any caller that loads the render config
        # (and the round-robin scheduler will pick it). Block at load
        # time so the failure surfaces at startup, not mid-render.
        if c.in_rotation and c.config_yaml:
            cfg_path = repo_root / c.config_yaml
            if not cfg_path.exists():
                raise FileNotFoundError(
                    f"channel {c.slug!r} has in_rotation=true but its "
                    f"config_yaml {c.config_yaml!r} does not exist at "
                    f"{cfg_path}. Either create the YAML or set "
                    f"in_rotation: false in {yaml_path.name}."
                )

    return tuple(parsed)


CHANNELS: tuple[Channel, ...] = _load_channels()

_BY_SLUG: dict[str, Channel] = {c.slug: c for c in CHANNELS}


# ---------------------------------------------------------------------------
# Public API — derived views over CHANNELS. Other modules read through
# these instead of touching CHANNELS directly.
# ---------------------------------------------------------------------------


def all_channels() -> tuple[Channel, ...]:
    """Every production channel registered in channels.yaml."""
    return CHANNELS


def channel_rotation() -> list[str]:
    """Slugs the round-robin scheduler picks from. Replaces the
    hardcoded list at control/scheduler.py:CHANNEL_ROTATION."""
    return [c.slug for c in CHANNELS if c.in_rotation]


def get_channel(slug: str) -> Optional[Channel]:
    """Look up a channel by its canonical slug. Returns None if unknown.
    There is no alias fallback — callers MUST use the canonical slug."""
    return _BY_SLUG.get(slug)


def is_known_channel(slug: str) -> bool:
    return slug in _BY_SLUG


def niche_channel_map() -> dict[str, tuple[str, str]]:
    """niche_key -> (state_dir, variant_yaml_path).

    Drop-in replacement for pipeline/niches.py:NICHE_CHANNEL.
    """
    out: dict[str, tuple[str, str]] = {}
    for c in CHANNELS:
        for niche_key in c.niches:
            out[niche_key] = (
                c.state_dir(niche_key),
                c.variant_yaml_path(niche_key),  # type: ignore[arg-type]
            )
    return out


def channel_for_niche(niche_key: str) -> Optional[Channel]:
    for c in CHANNELS:
        if niche_key in c.niches:
            return c
    return None


def all_niches() -> list[str]:
    out: list[str] = []
    for c in CHANNELS:
        out.extend(c.niches.keys())
    return sorted(out)


# ---------------------------------------------------------------------------
# Burners — DEPRECATED 2026-05-13.
#
# The burner-channel cross-engagement system was retired (YouTube filtered
# subs from new burner accounts out of public sub counts; engineering effort
# better spent elsewhere). The browser-automation patterns it pioneered are
# captured project-agnostic at docs/chrome_signed_in_automation.md.
#
# Removed: pipeline/burners.yaml, pipeline/cross_engage/, pipeline/laptop_agent.py,
# control/routes/burner_routes.py, web-next/app/app/burner-channels/.
# ---------------------------------------------------------------------------
