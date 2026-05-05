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
    "aita":             ("mystoriesanimated/reddit_amitheasshole",             "mystoriesanimated/variants/aita_animated.yaml"),
    # Cliffhanger variant — Part 1 of 2. Same source subreddit as "aita"
    # but a SEPARATE channel_dir so scripts/cast/voices don't overwrite
    # vanilla-AITA outputs (the cliffhanger rewrite produces a different
    # narration ending). Defaults to the animated YAML; swap to
    # aita_cliffhanger_text.yaml or aita_cliffhanger_cooking.yaml via
    # CLI --channel for those visual styles.
    "aita_cliffhanger": ("mystoriesanimated/reddit_amitheasshole_cliffhanger",  "mystoriesanimated/variants/aita_cliffhanger_animated.yaml"),
    "tifu":             ("mystoriesanimated/reddit_tifu",                       "mystoriesanimated/variants/tifu.yaml"),
    "malicious":        ("mystoriesanimated/reddit_maliciouscompliance",        "mystoriesanimated/variants/aita_animated.yaml"),
    "prorevenge":       ("mystoriesanimated/reddit_prorevenge",                 "mystoriesanimated/variants/aita_animated.yaml"),
    "oddities":         ("mystoriesanimated/wiki_oddities",                     "mystoriesanimated/variants/wiki_oddities.yaml"),
    "tih":              ("mystoriesanimated/today_in_history",                  "mystoriesanimated/variants/today_in_history.yaml"),
    # /aita_cooking is a SEPARATE niche from "aita" because the cooking
    # rewrite prompt produces materially different narration text (silent
    # cooking video bg + Reddit-text overlay format). Its narration JSON
    # therefore lives in its own dir alongside the vanilla AITA pool —
    # multiple variants writing to the same niche dir would clobber each
    # other.
    "aita_cooking":     ("mystoriesanimated/aita_cooking",                      "mystoriesanimated/variants/aita_cooking.yaml"),
    # Wikipedia "list of common misconceptions" — a separate source with
    # its own raw shape (per-misconception story rather than per-Reddit-
    # post) and its own narration cadence. Doesn't share with wiki_oddities
    # because the source pull + rewrite are distinct. No bespoke variant
    # YAML yet — defaults to the vanilla animated pipeline.
    "wiki_misconceptions": ("mystoriesanimated/wiki_misconceptions",            "mystoriesanimated/variants/aita_animated.yaml"),
    # Top-5 countdown tier-list — sports rankings with footage cut-ins.
    # The script structure (5 ranked beats + hook + closer) is authored
    # by the /make-ranking skill; pull_stories has no auto adapter for
    # this niche (the curator picks 5 from the existing sports raw pool
    # or hand-authors new raws). The website's "Generate" button still
    # routes through make_shorts.py — same renderer path as the parent
    # sports channel.
    "sports_ranked":    ("sportstoriesanimated/ranked",       "sportstoriesanimated/variants/ranked.yaml"),
}

NICHE_FALLBACK: tuple[str, str] = ("mystoriesanimated/reddit_amitheasshole", "mystoriesanimated/variants/aita_animated.yaml")
