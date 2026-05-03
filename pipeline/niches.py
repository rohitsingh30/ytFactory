"""Canonical niche → (channel_dir, channel_yaml) routing.

Single source of truth for which channel YAML each niche renders with
and where its raw/intermediate files live on disk. Both
``pipeline.imitate`` (riff-mode profile resolution) and
``web.server`` (UI niche cards + manual job dispatch) import from
here so adding a niche YAML or moving an existing niche to its own
YAML is a one-line change.

The ``"riff"`` and ``"novel"`` flows intentionally do not appear here:
``riff`` resolves its channel per-job from the imitation profile, and
``novel`` falls back to ``NICHE_FALLBACK`` when the profile names a
niche we don't ship a YAML for.
"""

from __future__ import annotations


NICHE_CHANNEL: dict[str, tuple[str, str]] = {
    "aita":             ("reddit_amitheasshole",             "channels/aita_animated.yaml"),
    # Cliffhanger variant — Part 1 of 2. Same source subreddit as "aita"
    # but a SEPARATE channel_dir so scripts/cast/voices don't overwrite
    # vanilla-AITA outputs (the cliffhanger rewrite produces a different
    # narration ending). Defaults to the animated YAML; swap to
    # aita_cliffhanger_text.yaml or aita_cliffhanger_cooking.yaml via
    # CLI --channel for those visual styles.
    "aita_cliffhanger": ("reddit_amitheasshole_cliffhanger",  "channels/aita_cliffhanger_animated.yaml"),
    "tifu":             ("reddit_tifu",                       "channels/tifu.yaml"),
    "malicious":        ("reddit_maliciouscompliance",        "channels/aita_animated.yaml"),
    "prorevenge":       ("reddit_prorevenge",                 "channels/aita_animated.yaml"),
    "oddities":         ("wiki_oddities",                     "channels/wiki_oddities.yaml"),
    "tih":              ("today_in_history",                  "channels/today_in_history.yaml"),
    # Top-5 countdown tier-list — sports rankings with footage cut-ins.
    # The script structure (5 ranked beats + hook + closer) is authored
    # by the /make-ranking skill; pull_stories has no auto adapter for
    # this niche (the curator picks 5 from the existing sports raw pool
    # or hand-authors new raws). The website's "Generate" button still
    # routes through make_shorts.py — same renderer path as the parent
    # sports channel.
    "sports_ranked":    ("sportstoriesanimated_ranked",       "channels/sportstoriesanimated_ranked.yaml"),
}

NICHE_FALLBACK: tuple[str, str] = ("reddit_amitheasshole", "channels/aita_animated.yaml")
