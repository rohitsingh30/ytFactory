#!/usr/bin/env python3
"""Seed canonical ``NicheDoc``s into the GCS niche store.

The /create wizard's "Niche" bubble row reads from
``GET /api/channels/<channel>/niches`` (backed by
``pipeline.niche_specs``). NicheDocs are the **single source of truth
for niches** — channel routing (state subdir, variant YAML), source
pool, writing templates, and aesthetic defaults all live in the doc.
Channel-level metadata (slug, youtube_channel_id, in_rotation) lives
in ``pipeline/channels.yaml``; the two stores are now disjoint.

This script is the source of truth for the **default** niche pool.
Edit ``CHANNEL_NICHE_SEEDS`` to add / rename / remove a default niche
and re-run. User-authored niches (created via the AI-draft endpoint)
land alongside seeded ones in the same JSON store and are not
overwritten by re-seeding (the writer skips keys created_by != backfill).

Storage. ``pipeline.niche_specs.save_niche()`` writes to GCS when
``YTFACTORY_STATE_BUCKET`` is set (canonical). The local-disk mirror
is **opt-in** (set ``YTFACTORY_NICHE_DISK_MIRROR=1`` to also persist
under ``<channel>/niches/<key>.json``); without it the laptop's
channel folders stay clean.

Hard rule. Every (channel, length_kind) cell MUST have ≥5 niches so
the bubble row always offers a meaningful pick. The
``_assert_min_niches_per_cell()`` gate at the bottom enforces this at
script-run time. AI is always invoked downstream regardless of which
bubble the user picks; the niche only declares **which kind of video**
to author (source pool + format + routing).

Voice is **deliberately absent**. Voice picks (gender / language /
persona / energy) live in the standalone voice catalog
(``web/server.py:VOICES`` + ``pipeline/voice_refs/``) and are
selected per-render. A given niche can be narrated by any voice.

Usage:
    # Against prod GCS (use the same bucket Cloud Run serves from)
    YTFACTORY_STATE_BUCKET=ytfactory-prod-v3-state \\
        .venv/bin/python scripts/seed_channel_niches.py

    # Local disk only (no GCS write — useful for dry-running)
    .venv/bin/python scripts/seed_channel_niches.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo importable when run as `python scripts/seed_channel_niches.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.niche_specs import (  # noqa: E402
    NicheDoc, save_niche, _state_bucket, get_niche,
)


def _seed(
    *,
    channel: str,
    key: str,
    label: str,
    length_kind: str,
    fmt: str,
    source_kind: str,
    source_ref: str | None,
    state_subdir: str,
    variant_yaml: str | None,
    description: str = "",
    thumbnail_style_key: str | None = None,
    prompt_style_guide: str = "",
    hook: str = "",
    closer: str = "",
    image_style: str = "",
    music_bed: str | None = None,
) -> dict:
    """Build a single NicheDoc-shaped dict in the new nested layout.

    Helper exists so the bulk seed list reads as data, not boilerplate.
    """
    return {
        "key": key,
        "label": label,
        "description": description,
        "length_kind": length_kind,
        "format": fmt,
        "source": {"kind": source_kind, "ref": source_ref},
        "routing": {
            "channel": channel,
            "state_subdir": state_subdir,
            "variant_yaml": variant_yaml,
        },
        "templates": {
            "prompt_style_guide": prompt_style_guide,
            "hook": hook,
            "closer": closer,
        },
        "aesthetic": {
            "image_style": image_style,
            "music_bed": music_bed,
            "thumbnail_style_key": thumbnail_style_key,
        },
        "created_by": "backfill",
    }


# ---------------------------------------------------------------------------
# CHANNEL_NICHE_SEEDS — one entry per default niche.
#
# **Niche key reconciliation (2026-05-11)**: keys here are the
# canonical names. The pre-2026-05-11 channels.yaml ``niches:`` block
# used a few different keys for the same niches (``oddities`` vs
# ``wiki_oddities``, ``tih`` vs ``today_in_history``, ``sports_ranked``
# vs ``top5_countdown``). The canonical keys win; legacy aliases were
# dropped along with the channels.yaml block.
# ---------------------------------------------------------------------------
CHANNEL_NICHE_SEEDS: dict[str, list[dict]] = {
    "mystoriesanimated": [
        # ---- short (Reddit-driven 2D-crayon Shorts) ----
        _seed(channel="mystoriesanimated", key="aita", label="r/AmItheAsshole",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="AmItheAsshole",
              state_subdir="reddit_amitheasshole", variant_yaml="aita_animated.yaml",
              thumbnail_style_key="aita",
              description="Reddit r/AmItheAsshole top-of-day. Conflict, drama, vote-bait closer."),
        _seed(channel="mystoriesanimated", key="aita_cliffhanger", label="AITA — Cliffhanger (Part 1)",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="AmItheAsshole",
              state_subdir="reddit_amitheasshole_cliffhanger",
              variant_yaml="aita_cliffhanger_animated.yaml",
              thumbnail_style_key="aita",
              description="r/AmItheAsshole, but cut at peak tension with a SUBSCRIBE-for-Part-2 closer."),
        _seed(channel="mystoriesanimated", key="aita_cooking", label="AITA — Cooking",
              length_kind="short", fmt="cooking",
              source_kind="reddit", source_ref="AmItheAsshole",
              state_subdir="aita_cooking", variant_yaml="aita_cooking.yaml",
              thumbnail_style_key="aita",
              description="Silent cooking video bg + Reddit-text overlay format. Materially different rewrite."),
        _seed(channel="mystoriesanimated", key="tifu", label="r/tifu",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="tifu",
              state_subdir="reddit_tifu", variant_yaml="tifu.yaml",
              thumbnail_style_key="tifu",
              description="r/tifu top-of-day. Embarrassment, regret, oh-no story arcs."),
        _seed(channel="mystoriesanimated", key="malicious", label="r/MaliciousCompliance",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="MaliciousCompliance",
              state_subdir="reddit_maliciouscompliance", variant_yaml="aita_animated.yaml",
              thumbnail_style_key="aita",
              description="Following the rules to spite the rule-maker."),
        _seed(channel="mystoriesanimated", key="prorevenge", label="r/ProRevenge",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="ProRevenge",
              state_subdir="reddit_prorevenge", variant_yaml="aita_animated.yaml",
              thumbnail_style_key="aita",
              description="Calculated, satisfying, slow-burn payback."),
        _seed(channel="mystoriesanimated", key="pettyrevenge", label="r/pettyrevenge",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="pettyrevenge",
              state_subdir="reddit_pettyrevenge", variant_yaml="aita_animated.yaml",
              thumbnail_style_key="aita",
              description="Small, satisfying revenge stories."),
        _seed(channel="mystoriesanimated", key="relationship_advice", label="r/relationship_advice",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="relationship_advice",
              state_subdir="reddit_relationship_advice", variant_yaml="aita_animated.yaml",
              thumbnail_style_key="aita",
              description="Reddit relationship dilemmas, shipping-bait closer."),
        _seed(channel="mystoriesanimated", key="wiki_oddities", label="Wiki Oddities",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref="List_of_unusual_deaths",
              state_subdir="wiki_oddities", variant_yaml="wiki_oddities.yaml",
              thumbnail_style_key="oddities",
              description="Wikipedia 'List of unusual deaths (21st century)'. Bizarre history."),
        _seed(channel="mystoriesanimated", key="wiki_misconceptions", label="Wiki Misconceptions",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref="List_of_common_misconceptions",
              state_subdir="wiki_misconceptions", variant_yaml="aita_animated.yaml",
              thumbnail_style_key="oddities",
              description="Wikipedia 'List of common misconceptions'. Per-misconception story."),
        _seed(channel="mystoriesanimated", key="today_in_history", label="Today in History",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref="On_this_day",
              state_subdir="today_in_history", variant_yaml="today_in_history.yaml",
              thumbnail_style_key="tih",
              description="Wikipedia 'On this day' for today's date. One historical event per Short."),
        # ---- long (sleep-paced narratives) ----
        _seed(channel="mystoriesanimated", key="truecrime", label="r/TrueCrime",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="TrueCrime",
              state_subdir="reddit_truecrime", variant_yaml=None,
              description="Long-form true crime narratives. Slow-burn investigation arcs."),
        _seed(channel="mystoriesanimated", key="letsnotmeet", label="r/LetsNotMeet",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="LetsNotMeet",
              state_subdir="reddit_letsnotmeet", variant_yaml=None,
              description="True scary stories. Atmospheric, paced for ambient listening."),
        _seed(channel="mystoriesanimated", key="nosleep", label="r/nosleep",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="nosleep",
              state_subdir="reddit_nosleep", variant_yaml=None,
              description="Horror fiction told as first-person true story."),
        _seed(channel="mystoriesanimated", key="unresolved_mysteries", label="r/UnresolvedMysteries",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="UnresolvedMysteries",
              state_subdir="reddit_unresolvedmysteries", variant_yaml=None,
              description="Real unsolved mysteries. Cold cases, disappearances, paranormal."),
        _seed(channel="mystoriesanimated", key="glitch_in_the_matrix", label="r/Glitch_in_the_Matrix",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="Glitch_in_the_Matrix",
              state_subdir="reddit_glitch_in_the_matrix", variant_yaml=None,
              description="Reality-bending personal accounts."),
    ],

    "scrollpulse": [
        # ---- short ----
        _seed(channel="scrollpulse", key="askreddit", label="r/AskReddit",
              length_kind="short", fmt="split_screen",
              source_kind="reddit", source_ref="AskReddit",
              state_subdir="askreddit", variant_yaml=None,
              description="AskReddit thread + top comments + gameplay loop."),
        _seed(channel="scrollpulse", key="showerthoughts", label="r/Showerthoughts",
              length_kind="short", fmt="split_screen",
              source_kind="reddit", source_ref="Showerthoughts",
              state_subdir="showerthoughts", variant_yaml=None,
              description="Single-line musings rendered as Reddit cards."),
        _seed(channel="scrollpulse", key="unpopularopinion", label="r/UnpopularOpinion",
              length_kind="short", fmt="split_screen",
              source_kind="reddit", source_ref="UnpopularOpinion",
              state_subdir="unpopularopinion", variant_yaml=None,
              description="Spicy takes + comment dunks."),
        _seed(channel="scrollpulse", key="confession", label="r/Confession",
              length_kind="short", fmt="split_screen",
              source_kind="reddit", source_ref="Confession",
              state_subdir="confession", variant_yaml=None,
              description="Anonymous confessions + reactions."),
        _seed(channel="scrollpulse", key="trueoffmychest", label="r/TrueOffMyChest",
              length_kind="short", fmt="split_screen",
              source_kind="reddit", source_ref="TrueOffMyChest",
              state_subdir="trueoffmychest", variant_yaml=None,
              description="Confessional posts + supportive / cutting comments."),
        _seed(channel="scrollpulse", key="tweet_xfeed", label="X / Twitter feed",
              length_kind="short", fmt="split_screen",
              source_kind="x_twitter", source_ref=None,
              state_subdir="tweet_xfeed", variant_yaml=None,
              description="Trending tweet + reactions, split-screen with gameplay."),
        # ---- long ----
        _seed(channel="scrollpulse", key="ai_tech_daily", label="AI / tech daily recap",
              length_kind="long", fmt="long_form",
              source_kind="rss", source_ref="https://news.ycombinator.com/rss",
              state_subdir="ai_tech_daily", variant_yaml=None,
              description="Daily 10-20 min recap of the top AI / tech news beats."),
        _seed(channel="scrollpulse", key="reddit_drama_recap", label="Reddit drama deep-dive",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="SubredditDrama",
              state_subdir="reddit_drama_recap", variant_yaml=None,
              description="Long-form recap of viral Reddit dramas, with timeline + receipts."),
        _seed(channel="scrollpulse", key="askreddit_megathread", label="AskReddit megathread",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="AskReddit",
              state_subdir="askreddit_megathread", variant_yaml=None,
              description="Hour-long compilation of top-voted answers from a single AskReddit thread."),
        _seed(channel="scrollpulse", key="twitter_feud_breakdown", label="X / Twitter feud breakdown",
              length_kind="long", fmt="long_form",
              source_kind="x_twitter", source_ref=None,
              state_subdir="twitter_feud_breakdown", variant_yaml=None,
              description="Long-form analysis of a public X feud — players, beats, fallout."),
        _seed(channel="scrollpulse", key="aita_compilation", label="AITA compilation",
              length_kind="long", fmt="long_form",
              source_kind="reddit", source_ref="AmItheAsshole",
              state_subdir="aita_compilation", variant_yaml=None,
              description="Hour-long compilation of the week's spiciest AITA threads."),
    ],

    "historyrecapped": [
        # ---- short ----
        _seed(channel="historyrecapped", key="history_today", label="Today in History",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref="On_this_day",
              state_subdir="history_today", variant_yaml=None,
              thumbnail_style_key="tih",
              description="Wikipedia 'On this day' — one event per Short."),
        _seed(channel="historyrecapped", key="askhistorians", label="r/AskHistorians",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="AskHistorians",
              state_subdir="askhistorians", variant_yaml=None,
              description="Curated AskHistorians answers, condensed to ~60s."),
        _seed(channel="historyrecapped", key="history_did_you_know", label="Did you know? (history)",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref=None,
              state_subdir="history_did_you_know", variant_yaml=None,
              description="Surprising one-fact history Shorts. ~60s, hook-heavy."),
        _seed(channel="historyrecapped", key="history_inventions", label="Inventions & firsts",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref="List_of_inventions",
              state_subdir="history_inventions", variant_yaml=None,
              description="Origin stories of everyday inventions, one per Short."),
        _seed(channel="historyrecapped", key="history_quotes", label="Famous historical quote",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref=None,
              state_subdir="history_quotes", variant_yaml=None,
              description="One quote, the moment behind it, why it still matters."),
        # ---- long ----
        _seed(channel="historyrecapped", key="wars_battles", label="Wars & battles",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="List_of_wars",
              state_subdir="wars_battles", variant_yaml=None,
              description="Multi-min long-form deep-dives on wars / battles."),
        _seed(channel="historyrecapped", key="empires_dynasties", label="Empires & dynasties",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="List_of_empires",
              state_subdir="empires_dynasties", variant_yaml=None,
              description="Empire-rise-and-fall arcs in 15-30 min."),
        _seed(channel="historyrecapped", key="historical_figures", label="Historical figures",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref=None,
              state_subdir="historical_figures", variant_yaml=None,
              description="Biographical long-form on a single historical figure."),
        _seed(channel="historyrecapped", key="ancient_civilizations", label="Ancient civilizations",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="List_of_ancient_civilizations",
              state_subdir="ancient_civilizations", variant_yaml=None,
              description="Long-form arcs on ancient civilizations — rise, peak, collapse."),
        _seed(channel="historyrecapped", key="historical_disasters", label="Historical disasters",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="Lists_of_disasters",
              state_subdir="historical_disasters", variant_yaml=None,
              description="Long-form retellings of pandemics, fires, floods, and the world they reshaped."),
    ],

    "cosmosdecoded": [
        # ---- short ----
        _seed(channel="cosmosdecoded", key="r_space", label="r/space — news",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="space",
              state_subdir="r_space", variant_yaml=None,
              description="Latest space-news headlines, condensed."),
        _seed(channel="cosmosdecoded", key="r_physics", label="r/Physics",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="Physics",
              state_subdir="r_physics", variant_yaml=None,
              description="Physics community discussion, distilled into Shorts."),
        _seed(channel="cosmosdecoded", key="r_astronomy", label="r/Astronomy",
              length_kind="short", fmt="animated",
              source_kind="reddit", source_ref="Astronomy",
              state_subdir="r_astronomy", variant_yaml=None,
              description="Astronomy news + APOD-style images."),
        _seed(channel="cosmosdecoded", key="wiki_physics", label="Wikipedia · physics",
              length_kind="short", fmt="animated",
              source_kind="wikipedia", source_ref=None,
              state_subdir="wiki_physics", variant_yaml=None,
              description="Wikipedia physics articles for explainer Shorts."),
        _seed(channel="cosmosdecoded", key="jwst_image_short", label="JWST image-of-the-day",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref=None,
              state_subdir="jwst_image", variant_yaml=None,
              description="One JWST image per Short, with the science it unlocked."),
        # ---- long ----
        _seed(channel="cosmosdecoded", key="how_we_knew", label="How We Knew (decoder)",
              length_kind="long", fmt="long_form",
              source_kind="manual", source_ref=None,
              state_subdir="how_we_knew", variant_yaml=None,
              description="Three-act decoder: prediction → experiment → consequence."),
        _seed(channel="cosmosdecoded", key="space_missions", label="Space missions",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="List_of_space_missions",
              state_subdir="space_missions", variant_yaml=None,
              description="Long-form mission histories with archival footage."),
        _seed(channel="cosmosdecoded", key="cosmology_lecture", label="Cosmology lecture",
              length_kind="long", fmt="long_form",
              source_kind="manual", source_ref=None,
              state_subdir="cosmology_lecture", variant_yaml=None,
              description="Lecture-paced long-form on a cosmology topic — Big Bang, dark matter, CMB."),
        _seed(channel="cosmosdecoded", key="telescope_history", label="Telescope history",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="List_of_largest_optical_reflecting_telescopes",
              state_subdir="telescope_history", variant_yaml=None,
              description="Long-form history of telescopes — from Galileo to JWST."),
        _seed(channel="cosmosdecoded", key="astronaut_bios", label="Astronaut biographies",
              length_kind="long", fmt="footage_only",
              source_kind="wikipedia", source_ref="List_of_astronauts_by_name",
              state_subdir="astronaut_bios", variant_yaml=None,
              description="Biographical long-form on a single astronaut, mission-centric."),
    ],

    "hindutavaanimated": [
        # ---- short ----
        _seed(channel="hindutavaanimated", key="mahabharat", label="Mahabharat katha",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref="mahabharat",
              state_subdir="mahabharat", variant_yaml=None,
              description="Mahabharat episode rendered in Amar Chitra Katha style."),
        _seed(channel="hindutavaanimated", key="ramayan", label="Ramayan katha",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref="ramayan",
              state_subdir="ramayan", variant_yaml=None,
              description="Ramayan episode in Hindi, ACK visual style."),
        _seed(channel="hindutavaanimated", key="krishna_leela", label="Krishna leela",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref="krishna_leela",
              state_subdir="krishna_leela", variant_yaml=None,
              description="Krishna leela vignettes."),
        _seed(channel="hindutavaanimated", key="puraan", label="Puraan katha",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref="puraan",
              state_subdir="puraan", variant_yaml=None,
              description="Lesser-known Puraan kathas."),
        _seed(channel="hindutavaanimated", key="bhagavad_gita_verse", label="Bhagavad Gita verse",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref="bhagavad_gita",
              state_subdir="bhagavad_gita_verse", variant_yaml=None,
              description="Single Gita verse + commentary in 50-60s."),
        # ---- long ----
        _seed(channel="hindutavaanimated", key="mahabharat_kathaa", label="Mahabharat — full kathaa",
              length_kind="long", fmt="footage_only",
              source_kind="manual", source_ref="mahabharat",
              state_subdir="mahabharat_kathaa", variant_yaml=None,
              description="50-70 min calming Mahabharat narration."),
        _seed(channel="hindutavaanimated", key="ramayan_kathaa", label="Ramayan — full kathaa",
              length_kind="long", fmt="footage_only",
              source_kind="manual", source_ref="ramayan",
              state_subdir="ramayan_kathaa", variant_yaml=None,
              description="50-70 min calming Ramayan narration."),
        _seed(channel="hindutavaanimated", key="gita_adhyay", label="Bhagavad Gita adhyay",
              length_kind="long", fmt="footage_only",
              source_kind="manual", source_ref="bhagavad_gita",
              state_subdir="gita_adhyay", variant_yaml=None,
              description="Full adhyay of the Bhagavad Gita, devotional pacing."),
        _seed(channel="hindutavaanimated", key="ramcharitmanas_kand", label="Ramcharitmanas — kand",
              length_kind="long", fmt="footage_only",
              source_kind="manual", source_ref="ramcharitmanas",
              state_subdir="ramcharitmanas_kand", variant_yaml=None,
              description="Tulsidas Ramcharitmanas, kand-by-kand long-form devotional."),
        _seed(channel="hindutavaanimated", key="devi_mahatmya", label="Devi Mahatmya",
              length_kind="long", fmt="footage_only",
              source_kind="manual", source_ref="devi_mahatmya",
              state_subdir="devi_mahatmya", variant_yaml=None,
              description="Devi Mahatmya chapters, Navratri-friendly devotional pacing."),
    ],

    "sportsrecapped": [
        # ---- short ----
        _seed(channel="sportsrecapped", key="top5_countdown", label="Top-5 Countdown",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref=None,
              state_subdir="ranked", variant_yaml="ranked.yaml",
              description="Tier-list ranking format. 5 ranked moments × ~10s, footage cut-in per rank."),
        _seed(channel="sportsrecapped", key="head_to_head", label="Last-N Head-to-Head",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref=None,
              state_subdir="head_to_head", variant_yaml="ranked.yaml",
              description="Last N fixtures between two players / teams."),
        _seed(channel="sportsrecapped", key="football_explainer", label="Football Explainer",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref=None,
              state_subdir="football_explainer", variant_yaml=None,
              description="Tactical / rule explainer in ~60s."),
        _seed(channel="sportsrecapped", key="quick_stat", label="Eye-popping stat",
              length_kind="short", fmt="animated",
              source_kind="manual", source_ref=None,
              state_subdir="quick_stat", variant_yaml=None,
              description="One stat, the moment behind it, why it broke records."),
        _seed(channel="sportsrecapped", key="breaking_sports_news", label="Breaking sports news",
              length_kind="short", fmt="animated",
              source_kind="rss", source_ref=None,
              state_subdir="breaking_sports_news", variant_yaml=None,
              description="Day-of breaking sports news — transfer, scandal, upset."),
        # ---- long ----
        _seed(channel="sportsrecapped", key="rivalry_recap", label="Rivalry recap",
              length_kind="long", fmt="sports_doc",
              source_kind="manual", source_ref=None,
              state_subdir="rivalry_recap", variant_yaml=None,
              description="Long-form rivalry doc — multi-decade arcs."),
        _seed(channel="sportsrecapped", key="career_arc", label="Player career arc",
              length_kind="long", fmt="sports_doc",
              source_kind="manual", source_ref=None,
              state_subdir="career_arc", variant_yaml=None,
              description="Single player's career, rise → peak → twilight."),
        _seed(channel="sportsrecapped", key="tournament_retro", label="Tournament retrospective",
              length_kind="long", fmt="sports_doc",
              source_kind="manual", source_ref=None,
              state_subdir="tournament_retro", variant_yaml=None,
              description="Multi-act recap of a tournament / season."),
        _seed(channel="sportsrecapped", key="season_recap", label="Season recap",
              length_kind="long", fmt="sports_doc",
              source_kind="manual", source_ref=None,
              state_subdir="season_recap", variant_yaml=None,
              description="End-of-season long-form: ten beats that defined the campaign."),
        _seed(channel="sportsrecapped", key="tactical_evolution", label="Tactical evolution",
              length_kind="long", fmt="sports_doc",
              source_kind="manual", source_ref=None,
              state_subdir="tactical_evolution", variant_yaml=None,
              description="How a team or coach's tactics evolved over an era — Gegenpress, tiki-taka, total football."),
    ],

    "rhymetimejunction": [
        # ---- short ----
        _seed(channel="rhymetimejunction", key="classic_english", label="Classic English rhyme",
              length_kind="short", fmt="rhyme",
              source_kind="manual", source_ref="classic_english",
              state_subdir="classic_english", variant_yaml=None,
              description="Public-domain English nursery rhyme reimagined."),
        _seed(channel="rhymetimejunction", key="classic_hindi", label="Hindi rhyme",
              length_kind="short", fmt="rhyme",
              source_kind="manual", source_ref="classic_hindi",
              state_subdir="classic_hindi", variant_yaml=None,
              description="Classic Hindi balgeet."),
        _seed(channel="rhymetimejunction", key="hinglish_original", label="Hinglish original",
              length_kind="short", fmt="rhyme",
              source_kind="manual", source_ref="hinglish_original",
              state_subdir="hinglish_original", variant_yaml=None,
              description="Original Hinglish nursery rhyme with recurring mascots."),
        _seed(channel="rhymetimejunction", key="numbers_song", label="Numbers / counting song",
              length_kind="short", fmt="rhyme",
              source_kind="manual", source_ref="numbers",
              state_subdir="numbers_song", variant_yaml=None,
              description="Counting / numbers Hinglish song with visual cues 1-10."),
        _seed(channel="rhymetimejunction", key="mascot_song", label="Mascot adventure song",
              length_kind="short", fmt="rhyme",
              source_kind="manual", source_ref="mascots",
              state_subdir="mascot_song", variant_yaml=None,
              description="Recurring mascots Laddu / Jalebi / Tuk-Tuk in a sung mini-adventure."),
        # ---- long ----
        _seed(channel="rhymetimejunction", key="bedtime_story", label="Bedtime story narration",
              length_kind="long", fmt="long_form",
              source_kind="manual", source_ref="bedtime",
              state_subdir="bedtime_story", variant_yaml=None,
              description="Calm 20-30 min Hinglish bedtime story for kids."),
        _seed(channel="rhymetimejunction", key="rhyme_medley", label="Rhyme medley (30 min)",
              length_kind="long", fmt="rhyme",
              source_kind="manual", source_ref="medley",
              state_subdir="rhyme_medley", variant_yaml=None,
              description="Compilation of short rhymes back-to-back, 30 min sit-along."),
        _seed(channel="rhymetimejunction", key="mascot_adventure", label="Mascot episode",
              length_kind="long", fmt="long_form",
              source_kind="manual", source_ref="mascots",
              state_subdir="mascot_adventure", variant_yaml=None,
              description="Episodic Laddu / Jalebi / Tuk-Tuk story arc, 15-20 min."),
        _seed(channel="rhymetimejunction", key="lullaby_compilation", label="Lullaby compilation",
              length_kind="long", fmt="long_form",
              source_kind="manual", source_ref="lullaby",
              state_subdir="lullaby_compilation", variant_yaml=None,
              description="Calming Hinglish lullaby reel — for sleep, hour-long."),
        _seed(channel="rhymetimejunction", key="counting_lesson", label="Numbers & colors lesson",
              length_kind="long", fmt="long_form",
              source_kind="manual", source_ref="lesson",
              state_subdir="counting_lesson", variant_yaml=None,
              description="Educational long-form on numbers, colors, shapes — sung throughout."),
    ],
}


# ---------------------------------------------------------------------------
# Hard rule: every (channel, length_kind) cell needs ≥5 niches.
# Without this gate the wizard's bubble row falls back to its empty-state
# copy and the user has nothing to pick — see the sportsrecapped 0-niche
# regression that caused this rule to exist (2026-05-11).
# ---------------------------------------------------------------------------

MIN_NICHES_PER_CELL = 5


def _assert_min_niches_per_cell() -> None:
    bad: list[str] = []
    for channel, seeds in CHANNEL_NICHE_SEEDS.items():
        for kind in ("short", "long"):
            n = sum(1 for s in seeds if s["length_kind"] == kind)
            if n < MIN_NICHES_PER_CELL:
                bad.append(f"  {channel} / {kind}: {n} (need ≥{MIN_NICHES_PER_CELL})")
    if bad:
        raise SystemExit(
            "CHANNEL_NICHE_SEEDS violates the per-cell minimum:\n"
            + "\n".join(bad)
            + "\n\nAdd more entries to satisfy the rule and re-run."
        )


_assert_min_niches_per_cell()


# ---------------------------------------------------------------------------
# Schema sanity gate — every seed must validate against the NicheDoc
# schema BEFORE we hit the network. Catches typos in the in-memory
# seed list (wrong format string, missing required field, etc.) up
# front instead of partway through a slow GCS run.
# ---------------------------------------------------------------------------


def _assert_seeds_valid() -> None:
    seen_keys: dict[str, str] = {}
    for channel, seeds in CHANNEL_NICHE_SEEDS.items():
        for seed in seeds:
            key = seed.get("key", "???")
            # Niche keys are GLOBALLY unique — niche_channel_map() returns
            # dict[niche_key, ...] across all channels, so a collision
            # silently drops one of them. Catch at seed time.
            if key in seen_keys:
                raise SystemExit(
                    f"duplicate niche key {key!r}: declared in both "
                    f"{seen_keys[key]!r} and {channel!r}. Niche keys must be "
                    f"globally unique. Pick distinct keys (e.g. prefix the "
                    f"second with the channel name)."
                )
            seen_keys[key] = channel
            try:
                NicheDoc.model_validate(seed)
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(
                    f"seed {channel}/{key!r} fails schema:\n  {exc}"
                ) from exc


_assert_seeds_valid()


def main() -> int:
    bucket = _state_bucket()
    if bucket:
        print(f"==> Writing to GCS bucket gs://{bucket}/<channel>/niches/")
        from pipeline.niche_specs import _should_disk_mirror  # noqa: PLC0415
        if _should_disk_mirror():
            print("    YTFACTORY_NICHE_DISK_MIRROR is on — also mirroring to laptop disk.")
        else:
            print("    Laptop disk mirror is OFF (set YTFACTORY_NICHE_DISK_MIRROR=1 to enable).")
    else:
        print("==> YTFACTORY_STATE_BUCKET unset — writing to local disk only.")
    print()

    total = 0
    skipped_user = 0
    for channel_key, seeds in CHANNEL_NICHE_SEEDS.items():
        print(f"  channel: {channel_key} ({len(seeds)} niches)")
        for seed in seeds:
            # Don't clobber user-authored niches that share a key with a seed.
            existing = get_niche(channel_key, seed["key"])
            if existing is not None and existing.created_by != "backfill":
                print(
                    f"    · {seed['key']:28s} ({seed['length_kind']:5s})  "
                    f"SKIP — user-authored doc exists"
                )
                skipped_user += 1
                continue
            doc = NicheDoc.model_validate(seed)
            try:
                save_niche(channel_key, doc)
                print(f"    ✓ {seed['key']:28s} ({seed['length_kind']:5s})  {seed['label']}")
                total += 1
            except Exception as e:  # pragma: no cover — surface any save failure
                print(f"    ✗ {seed['key']:28s} FAILED: {type(e).__name__}: {e}")
                return 1
        print()

    print(f"==> Wrote {total} NicheDocs across {len(CHANNEL_NICHE_SEEDS)} channels.")
    if skipped_user:
        print(f"    Skipped {skipped_user} user-authored docs (created_by != backfill).")
    if bucket:
        print(f"    Verify: gsutil ls gs://{bucket}/mystoriesanimated/niches/")
    print(f"    Frontend: hard-reload /app/create — Niche bubbles will populate from /api/channels/<ch>/niches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
