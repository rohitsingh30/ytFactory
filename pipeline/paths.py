"""Canonical channel layout — single source of truth for every disk path.

The 2026-05-05 layout audit found three generations of upload-record paths
coexisting on disk, ~20 files in pipeline/control/workers each deriving
their own subdir conventions, and 7 channels with wildly inconsistent
folder shapes. This module replaces all of it.

## Canonical layout

Every channel root MUST match this shape (use ``.gitkeep`` for empty dirs):

::

    <channel>/
      config.yaml                   # always (channel-wide defaults)
      variants/<v>.yaml             # opt — overlay YAMLs (style overlays)
      raw/[<niche>/]<slug>.json     # tracked
      narrations/[<niche>/]<slug>.json   # tracked
      cast/[<niche>/]<slug>.json    # gitignored
      shotlist/[<niche>/]<slug>.json     # tracked, footage-only
      uploads/[<niche>/]<slug>.json # tracked
      shorts/[<niche>/]<slug>.mp4   # gitignored
      long_form/<slug>.mp4          # gitignored
      cache/<slug>/...              # gitignored
      scratch/                      # gitignored
      critiques/<slug>/...          # frames gitignored, JSONs tracked
      branding/                     # *.png gitignored
      scripts/  learnings/          # tracked
      footage/{sources,long_sources,transcripts}/   # gitignored, opt
      footage_plan/  music/  songs/ emoji/          # opt

Wait — the ``[<niche>/]`` prefix is **between channel root and the
subdir name**, NOT inside the subdir. So the actual canonical paths are::

    <channel>/<niche>/narrations/<slug>.json
    <channel>/<niche>/uploads/<slug>.json
    ...

For flat (non-niched) channels::

    <channel>/narrations/<slug>.json
    <channel>/uploads/<slug>.json
    ...

This matches what ``pipeline/upload.py:_record_path`` already writes
(passes a compound ``channel_dir`` like ``"mystoriesanimated/reddit_amitheasshole"``
and joins ``"uploads"`` underneath).

## Niche rule

A channel uses niches **everywhere or nowhere** — no mixing.

* Niche channels (mystoriesanimated, sportstoriesanimated): per-slug
  subdirs (raw / narrations / cast / shotlist / uploads / shorts /
  long_form / cache / scratch / critiques) ALL nest under ``<niche>/``.
* Channel-wide subdirs (config.yaml, variants/, learnings/, scripts/,
  branding/, music/, songs/, emoji/, footage_plan/) NEVER nest under
  niche — they're shared across all niches of the channel.
* Footage (``footage/{sources,long_sources,transcripts}``) is also
  channel-wide; multiple niches can share the same footage cache.

:py:attr:`pipeline.niches.NICHE_CHANNEL` is the **source of truth** for
``variant_yaml → (channel, niche)`` mapping.

## Migration to this layout

A ``RenderPaths`` instance for any (channel, niche) pair lets every
caller compute paths from one consistent source. Goal: eliminate every
hardcoded subdir name in pipeline/, control/, workers/.

::

    from pipeline.paths import RenderPaths

    paths = RenderPaths.for_channel("mystoriesanimated", "reddit_amitheasshole")
    paths.narration_for(slug)        # mystoriesanimated/reddit_amitheasshole/narrations/<slug>.json
    paths.upload_record_for(slug)    # mystoriesanimated/reddit_amitheasshole/uploads/<slug>.json
    paths.short_for(slug)            # mystoriesanimated/reddit_amitheasshole/shorts/<slug>.mp4
    paths.cache_for(slug)            # mystoriesanimated/reddit_amitheasshole/cache/<slug>/

    flat = RenderPaths.for_channel("historyrecapped")
    flat.narration_for(slug)         # historyrecapped/narrations/<slug>.json

    # Resolve from a channel YAML path (uses NICHE_CHANNEL for niche lookup):
    paths = RenderPaths.from_channel_yaml(Path("mystoriesanimated/variants/aita_animated.yaml"))
    # → RenderPaths(channel="mystoriesanimated", niche="reddit_amitheasshole", ...)

Channel-wide accessors live on ``channel_root`` (no niche)::

    paths.config_yaml      # mystoriesanimated/config.yaml
    paths.learnings        # mystoriesanimated/learnings/
    paths.branding         # mystoriesanimated/branding/

## Cross-channel state under ``data/``

Most state moves per-channel after this refactor, but a few buckets
stay genuinely cross-channel:

* ``data/research/`` — cross-channel YouTube research (kept).
* ``data/telemetry/`` — cross-channel render telemetry (kept).
* ``data/_bench/`` — TTS A/B bench output (kept, gitignored).
* ``data/cache/`` — ML model weight cache (Kokoro, F5, Whisper).

Deprecated under data/, migrating to per-channel:

* ``data/intermediate/<channel_dir>/...`` → ``<channel>/[<niche>/]cache/<slug>/``
* ``data/critiques/<slug>/`` → ``<channel>/[<niche>/]critiques/<slug>/``
* ``data/shorts/<slug>.mp4`` → ``<channel>/[<niche>/]shorts/<slug>.mp4``
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Subdir(str, Enum):
    """Canonical subdir names. Use these instead of string literals."""

    # Per-slug subdirs (nest under <niche>/ when channel uses niches)
    RAW = "raw"
    NARRATIONS = "narrations"
    CAST = "cast"
    SHOTLIST = "shotlist"
    UPLOADS = "uploads"
    SHORTS = "shorts"
    LONG_FORM = "long_form"
    CACHE = "cache"
    SCRATCH = "scratch"
    CRITIQUES = "critiques"

    # Channel-wide subdirs (always at <channel>/, never under <niche>/)
    LEARNINGS = "learnings"
    SCRIPTS = "scripts"
    BRANDING = "branding"
    EMOJI = "emoji"
    MUSIC = "music"
    SONGS = "songs"
    FOOTAGE = "footage"
    FOOTAGE_PLAN = "footage_plan"
    VARIANTS = "variants"


# Subdirs that nest under the niche when the channel uses niches.
# Everything else lives at the channel root regardless.
_PER_SLUG_SUBDIRS: frozenset[str] = frozenset(s.value for s in (
    Subdir.RAW, Subdir.NARRATIONS, Subdir.CAST, Subdir.SHOTLIST,
    Subdir.UPLOADS, Subdir.SHORTS, Subdir.LONG_FORM, Subdir.CACHE,
    Subdir.SCRATCH, Subdir.CRITIQUES,
))


@dataclass(frozen=True)
class RenderPaths:
    """Canonical per-channel layout. Single source of truth.

    Construct via :meth:`for_channel` or :meth:`from_channel_yaml`.

    All path properties are computed lazily — no disk I/O on construction.
    Properties never call ``mkdir``; callers explicitly opt into directory
    creation via :meth:`ensure_dirs` or the slug-shaped helpers when they
    write files.
    """

    channel: str
    niche: str | None
    project_root: Path

    @classmethod
    def for_channel(
        cls,
        channel: str,
        niche: str | None = None,
        *,
        project_root: Path | None = None,
    ) -> "RenderPaths":
        """Build a :class:`RenderPaths` for ``(channel, niche)``.

        ``niche`` is None for flat channels (cosmosdecoded,
        hindutavaanimated, historyrecapped, rhymetimejunction) and a
        bare niche slug (e.g. ``"reddit_amitheasshole"``,
        ``"ranked"``) for niched ones.
        """
        return cls(
            channel=channel,
            niche=niche,
            project_root=project_root or PROJECT_ROOT,
        )

    @classmethod
    def from_channel_dir(
        cls,
        channel_dir: str,
        *,
        project_root: Path | None = None,
    ) -> "RenderPaths":
        """Parse a compound ``channel_dir`` string (``<channel>[/<niche>]``).

        Backward-compat helper for code that still passes ``channel_dir``
        as a path-style string (``pipeline.upload``,
        ``workers.heavy.render_short``). Routes through :meth:`for_channel`
        so the resulting :class:`RenderPaths` follows the canonical
        layout regardless of what the caller did historically.
        """
        if "/" in channel_dir:
            channel, niche = channel_dir.split("/", 1)
            return cls.for_channel(channel, niche, project_root=project_root)
        return cls.for_channel(channel_dir, project_root=project_root)

    @classmethod
    def from_channel_yaml(
        cls,
        channel_yaml: Path | str,
        *,
        project_root: Path | None = None,
    ) -> "RenderPaths":
        """Resolve ``(channel, niche)`` from a channel YAML path.

        Lookup precedence:

        1. :py:attr:`pipeline.niches.NICHE_CHANNEL` — authoritative for
           variant YAMLs that map to nested niche dirs (e.g.
           ``mystoriesanimated/variants/aita_animated.yaml`` →
           ``("mystoriesanimated", "reddit_amitheasshole")``).
        2. ``<channel>/config.yaml`` — flat channel, no niche.
        3. ``<channel>/variants/<variant>.yaml`` — variant YAML not yet
           registered in NICHE_CHANNEL. Defaults to flat layout (no
           niche, all writes to ``<channel>/<subdir>/``).

        Raises ``ValueError`` if the YAML path doesn't match any pattern.
        """
        from pipeline import niches  # noqa: PLC0415 — avoid cycle

        yaml_str = str(channel_yaml).replace("\\", "/")
        for chan_dir, chan_yaml_str in niches.NICHE_CHANNEL.values():
            if yaml_str.endswith(chan_yaml_str):
                # chan_dir is e.g. "mystoriesanimated/reddit_amitheasshole"
                # or "sportstoriesanimated/ranked".
                if "/" in chan_dir:
                    channel, niche = chan_dir.split("/", 1)
                    return cls.for_channel(channel, niche, project_root=project_root)
                return cls.for_channel(chan_dir, project_root=project_root)

        path = Path(yaml_str)
        if path.name == "config.yaml":
            return cls.for_channel(path.parent.name, project_root=project_root)
        # Central-config layout (post-2026-05-10 nuclear cleanup):
        # ``pipeline/channels/<slug>.yaml`` (set by
        # pipeline.channels._channel_yaml_path). The slug is the file
        # stem; the channel root is ``project_root/<slug>``.
        if (path.parent.name == "channels"
                and path.parent.parent.name == "pipeline"
                and path.suffix == ".yaml"):
            return cls.for_channel(path.stem, project_root=project_root)
        # Central-variants layout: ``pipeline/variants/<slug>/<variant>.yaml``.
        # Same intent — variant lives under a central tree, slug is the
        # parent dir name.
        if (path.parent.parent.name == "variants"
                and path.parent.parent.parent.name == "pipeline"):
            return cls.for_channel(path.parent.name, project_root=project_root)
        if path.parent.name == "variants":
            # Unregistered variant — treat as a flat channel rooted at
            # the channel folder. Loud-warn so the operator adds a
            # NICHE_CHANNEL entry. (Don't raise — the legacy fallback
            # behaviour was 'data/' which was worse.)
            print(
                f"[paths] WARN: variant YAML {channel_yaml!r} not in "
                f"NICHE_CHANNEL; defaulting to flat layout under "
                f"{path.parent.parent}. Add a NICHE_CHANNEL entry to "
                f"point this variant at its niche dir."
            )
            return cls.for_channel(path.parent.parent.name, project_root=project_root)
        raise ValueError(
            f"cannot resolve channel from yaml path {channel_yaml!r}: "
            f"expected <channel>/config.yaml, <channel>/variants/*.yaml, "
            f"pipeline/channels/<slug>.yaml, "
            f"pipeline/variants/<slug>/*.yaml, "
            f"or a path matching pipeline.niches.NICHE_CHANNEL"
        )

    # --- Roots -------------------------------------------------------------

    @property
    def channel_root(self) -> Path:
        """Plain channel folder (without niche). Channel-wide subdirs
        (config.yaml, learnings/, scripts/, branding/, music/, songs/,
        emoji/, variants/, footage/, footage_plan/) live here regardless
        of whether the channel uses niches.

        Post-2026-05-23 (task #46): channel artifact roots live under
        ``<project_root>/data/<channel>/`` instead of the legacy
        ``<project_root>/<channel>/``. Nothing is generated at the repo
        root any more. The layout below the channel root is unchanged.
        """
        return self.project_root / "data" / self.channel

    @property
    def root(self) -> Path:
        """Per-slug subdir root: ``<channel>/[<niche>/]``.

        Per-slug subdirs (raw, narrations, cast, shotlist, uploads,
        shorts, long_form, cache, scratch, critiques) are computed
        relative to this. For niched channels this includes the niche
        segment; for flat channels it's the same as :attr:`channel_root`.
        """
        if self.niche:
            return self.channel_root / self.niche
        return self.channel_root

    @property
    def channel_dir(self) -> str:
        """Compound channel-dir string (``<channel>[/<niche>]``).

        Backward-compat helper for code that still passes ``channel_dir``
        as a string (``pipeline.upload``, ``workers.heavy.render_short``).
        """
        if self.niche:
            return f"{self.channel}/{self.niche}"
        return self.channel

    # --- Per-slug subdirs (nest under niche when present) -----------------

    @property
    def raw(self) -> Path:
        return self.root / Subdir.RAW.value

    @property
    def narrations(self) -> Path:
        return self.root / Subdir.NARRATIONS.value

    @property
    def cast(self) -> Path:
        return self.root / Subdir.CAST.value

    @property
    def shotlist(self) -> Path:
        return self.root / Subdir.SHOTLIST.value

    @property
    def uploads(self) -> Path:
        return self.root / Subdir.UPLOADS.value

    @property
    def shorts(self) -> Path:
        return self.root / Subdir.SHORTS.value

    @property
    def long_form(self) -> Path:
        return self.root / Subdir.LONG_FORM.value

    @property
    def cache(self) -> Path:
        return self.root / Subdir.CACHE.value

    @property
    def scratch(self) -> Path:
        return self.root / Subdir.SCRATCH.value

    @property
    def critiques(self) -> Path:
        return self.root / Subdir.CRITIQUES.value

    # --- Channel-wide subdirs (always at channel_root, never niched) ------

    @property
    def config_yaml(self) -> Path:
        return self.channel_root / "config.yaml"

    @property
    def variants_dir(self) -> Path:
        return self.channel_root / Subdir.VARIANTS.value

    @property
    def learnings(self) -> Path:
        return self.channel_root / Subdir.LEARNINGS.value

    @property
    def scripts(self) -> Path:
        return self.channel_root / Subdir.SCRIPTS.value

    @property
    def branding(self) -> Path:
        return self.channel_root / Subdir.BRANDING.value

    @property
    def emoji(self) -> Path:
        return self.channel_root / Subdir.EMOJI.value

    @property
    def music(self) -> Path:
        return self.channel_root / Subdir.MUSIC.value

    @property
    def songs(self) -> Path:
        return self.channel_root / Subdir.SONGS.value

    @property
    def footage(self) -> Path:
        return self.channel_root / Subdir.FOOTAGE.value

    @property
    def footage_sources(self) -> Path:
        return self.footage / "sources"

    @property
    def footage_long_sources(self) -> Path:
        return self.footage / "long_sources"

    @property
    def footage_transcripts(self) -> Path:
        return self.footage / "transcripts"

    @property
    def footage_plan(self) -> Path:
        return self.channel_root / Subdir.FOOTAGE_PLAN.value

    # --- Slug-shaped helpers ---------------------------------------------

    def raw_for(self, slug: str) -> Path:
        return self.raw / f"{slug}.json"

    def narration_for(self, slug: str) -> Path:
        return self.narrations / f"{slug}.json"

    def cast_for(self, slug: str) -> Path:
        return self.cast / f"{slug}.json"

    def shotlist_for(self, slug: str) -> Path:
        return self.shotlist / f"{slug}.json"

    def upload_record_for(self, slug: str) -> Path:
        return self.uploads / f"{slug}.json"

    def short_for(self, slug: str) -> Path:
        return self.shorts / f"{slug}.mp4"

    def short_thumb_for(self, slug: str) -> Path:
        return self.shorts / f"{slug}.thumb.png"

    def long_form_for(self, slug: str) -> Path:
        return self.long_form / f"{slug}.mp4"

    def long_form_thumb_for(self, slug: str) -> Path:
        return self.long_form / f"{slug}.thumb.png"

    def cache_for(self, slug: str) -> Path:
        return self.cache / slug

    def scratch_for(self, slug: str) -> Path:
        return self.scratch / slug

    def critiques_for(self, slug: str) -> Path:
        return self.critiques / slug

    # --- Bulk helpers -----------------------------------------------------

    def ensure_dirs(self, *which: Subdir) -> None:
        """``mkdir(parents=True, exist_ok=True)`` the named subdirs.

        Call once near the start of a render before writing files. Saves
        every call site from open-coding the parent-creation dance.
        Pass nothing to skip; pass specific :class:`Subdir` members to
        create only those.
        """
        for s in which:
            target = self._dir_for(s)
            target.mkdir(parents=True, exist_ok=True)

    def _dir_for(self, subdir: Subdir) -> Path:
        """Internal: route a Subdir enum to the matching property."""
        return getattr(self, subdir.value)


# Cross-channel state under ``data/`` — kept where it makes sense.
DATA_ROOT = PROJECT_ROOT / "data"

# Cross-channel research data (NOT per-channel; aggregated YouTube etc).
RESEARCH_DIR = DATA_ROOT / "research"
# Cross-channel render telemetry (per-render JSON lines).
TELEMETRY_DIR = DATA_ROOT / "telemetry"
# Cross-channel ML model weight cache (Kokoro, F5, Whisper checkpoints).
MODEL_CACHE_DIR = DATA_ROOT / "cache"
# A/B benchmarks (TTS provider compare etc.). Gitignored.
BENCH_DIR = DATA_ROOT / "_bench"
