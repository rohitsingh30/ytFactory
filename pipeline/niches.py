"""Canonical niche → (channel_dir, channel_yaml) routing.

Single source of truth for which channel YAML each niche renders with
and where its raw/intermediate files live on disk. Both
``pipeline.llm.imitate`` (riff-mode profile resolution) and
``web.server`` (UI niche cards + manual job dispatch) import from
here so adding a niche YAML or moving an existing niche to its own
YAML is a one-line change.

The ``"riff"`` and ``"novel"`` flows intentionally do not appear here:
``riff`` resolves its channel per-job from the imitation profile, and
``novel`` falls back to ``NICHE_FALLBACK`` when the profile names a
niche we don't ship a YAML for.

CANONICAL VARIANT YAML LOCATION (2026-05-14 audit fix, batch D):
``pipeline/variants/<channel>/<variant>.yaml``. Pre-fix the entries
below pointed at ``<channel>/variants/<variant>.yaml`` (legacy layout,
removed during the 2026-05-10 nuclear cleanup), which made the
endswith() check in pipeline/paths.py:RenderPaths.from_yaml_path()
silently miss every NICHE_CHANNEL entry and degrade to flat layout.
That's catalogue YAML-04 through YAML-14 (and partially NCH-09/10).
"""

from __future__ import annotations


NICHE_CHANNEL: dict[str, tuple[str, str]] = {
    "aita":             ("mystoriesanimated/reddit_amitheasshole",             "pipeline/variants/mystoriesanimated/aita_animated.yaml"),
    # Cliffhanger variant — Part 1 of 2. Same source subreddit as "aita"
    # but a SEPARATE channel_dir so scripts/cast/voices don't overwrite
    # vanilla-AITA outputs (the cliffhanger rewrite produces a different
    # narration ending). Defaults to the animated YAML; swap to
    # aita_cliffhanger_text.yaml or aita_cliffhanger_cooking.yaml via
    # CLI --channel for those visual styles.
    "aita_cliffhanger": ("mystoriesanimated/reddit_amitheasshole_cliffhanger",  "pipeline/variants/mystoriesanimated/aita_cliffhanger_animated.yaml"),
    "tifu":             ("mystoriesanimated/reddit_tifu",                       "pipeline/variants/mystoriesanimated/tifu.yaml"),
    "malicious":        ("mystoriesanimated/reddit_maliciouscompliance",        "pipeline/variants/mystoriesanimated/aita_animated.yaml"),
    "prorevenge":       ("mystoriesanimated/reddit_prorevenge",                 "pipeline/variants/mystoriesanimated/aita_animated.yaml"),
    "oddities":         ("mystoriesanimated/wiki_oddities",                     "pipeline/variants/mystoriesanimated/wiki_oddities.yaml"),
    "tih":              ("mystoriesanimated/today_in_history",                  "pipeline/variants/mystoriesanimated/today_in_history.yaml"),
    # /aita_cooking is a SEPARATE niche from "aita" because the cooking
    # rewrite prompt produces materially different narration text (silent
    # cooking video bg + Reddit-text overlay format). Its narration JSON
    # therefore lives in its own dir alongside the vanilla AITA pool —
    # multiple variants writing to the same niche dir would clobber each
    # other.
    "aita_cooking":     ("mystoriesanimated/aita_cooking",                      "pipeline/variants/mystoriesanimated/aita_cooking.yaml"),
    # Wikipedia "list of common misconceptions" — a separate source with
    # its own raw shape (per-misconception story rather than per-Reddit-
    # post) and its own narration cadence. Doesn't share with wiki_oddities
    # because the source pull + rewrite are distinct. No bespoke variant
    # YAML yet — defaults to the vanilla animated pipeline.
    "wiki_misconceptions": ("mystoriesanimated/wiki_misconceptions",            "pipeline/variants/mystoriesanimated/aita_animated.yaml"),
    # Top-5 countdown tier-list — sports rankings with footage cut-ins.
    # The script structure (5 ranked beats + hook + closer) is authored
    # by the /make-ranking skill; pull_stories has no auto adapter for
    # this niche (the curator picks 5 from the existing sports raw pool
    # or hand-authors new raws). The website's "Generate" button still
    # routes through the same renderer path (pipeline.render) as the
    # parent sports channel.
    #
    # NOTE: channel slug is "sportsrecapped" — pre-fix this entry pointed
    # at "sportstoriesanimated" which has never existed (audit YAML-14).
    "sports_ranked":    ("sportsrecapped/ranked",                               "pipeline/variants/sportsrecapped/ranked.yaml"),
}

NICHE_FALLBACK: tuple[str, str] = ("mystoriesanimated/reddit_amitheasshole", "pipeline/variants/mystoriesanimated/aita_animated.yaml")


def _validate_niche_paths_exist() -> None:
    """Boot-time check: every variant YAML in NICHE_CHANNEL must exist
    on disk. Catalogue YAML-04..14 + NCH-09/10: pre-fix the paths were
    all stale, falling through to flat-layout fallback in paths.py and
    silently mis-routing niche renders. This validator surfaces drift
    immediately on import instead of at render time.

    Skipped when YTFACTORY_NICHE_PATHS_NO_VALIDATE=1 (tests + tooling
    that builds artifacts before the variant YAMLs exist).
    """
    import os
    if os.environ.get("YTFACTORY_NICHE_PATHS_NO_VALIDATE", "").strip() in {"1", "true", "yes"}:
        return
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    missing: list[str] = []
    for niche_key, (chan_dir, chan_yaml) in NICHE_CHANNEL.items():
        if not (repo_root / chan_yaml).exists():
            missing.append(f"  {niche_key!r}: {chan_yaml}")
    if missing:
        msg = (
            "pipeline.niches.NICHE_CHANNEL has stale variant YAML paths:\n"
            + "\n".join(missing)
            + f"\n\nRepo root checked: {repo_root}\n"
            + "Either fix the path or set YTFACTORY_NICHE_PATHS_NO_VALIDATE=1 "
            + "(tooling/build context)."
        )
        raise FileNotFoundError(msg)


# Validate on import — boot-time fast-fail.
_validate_niche_paths_exist()
