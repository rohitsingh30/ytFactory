"""Shared narration-JSON schema for long-form ytFactory skills.

Skills that produce long-form (>20min) narration JSON share a common
field shape (slug / title / narration / chapters[] / sources[] /
title_options[] / thumbnail_brief / hook). Each skill ALSO carries
skill-specific fields (panels[] for sleep-history image_panels;
ranks[] for top10; mool_shloka[] for katha; support_asks[] for
cosmos-decoded). This module defines:

  * The shared TypedDicts (LongFormScript, Chapter, Source) that ALL
    long-form skills emit.
  * Per-skill TypedDict extensions that add the skill-specific fields.
  * `validate_long_form_script()` — soft validator that warns about
    missing recommended fields without rejecting (renderers stay
    permissive — older slugs that predate 2026-05-08 still work).

Design choice: TypedDict, not @dataclass. Reasons:
  1. Renderers (pipeline/render/long_engine.py) read
     narration JSON via `json.loads()` → raw dict; converting to a
     dataclass would force a rewrite of every renderer entry-point.
     TypedDict is type-hint-only — runtime is still a plain dict.
  2. Some fields are author-supplied (title, hook), others computed
     at upload time (chapters_block, sources_block, title_caps).
     TypedDict's `total=False` cleanly marks the optional fields.
  3. Backward-compat: a slug that predates this module's introduction
     is still a valid LongFormScript — the type-checker never asserts
     the absence of unexpected fields. Renderers that grew their own
     ad-hoc fields (top10 cta_inserts, katha pronunciation_dict)
     stay valid.

Used by:
  * .claude/skills/make-sleep-history/SKILL.md (Section 4a schema)
  * .claude/skills/make-top10/SKILL.md (Section 3 / "Output narration JSON")
  * .claude/skills/make-katha/SKILL.md (Section 3 / "Output narration JSON")
  * .claude/skills/make-cosmos-decoder/SKILL.md (long-form variant)

This module is the single point where the 4 skills' schemas converge.
Skill-specific TypedDicts live next to the shared base for
discoverability — when a skill author needs to know "what fields can
this slug carry?", they look here.
"""
from __future__ import annotations

from typing import TypedDict, Literal, Required, NotRequired, Any


# ---- Shared building blocks ------------------------------------------


class Chapter(TypedDict, total=False):
    """A single chapter timestamp + title for the YouTube description."""
    start_s: Required[int]
    title: Required[str]
    body_summary: NotRequired[str]            # author-only; not used by renderer


class SourceCitation(TypedDict, total=False):
    """Per-source citation; either a plain string OR this dict."""
    citation: Required[str]
    url: NotRequired[str]
    edition: NotRequired[str]
    page_or_shloka_range: NotRequired[str]


class SupportAsk(TypedDict, total=False):
    """Inline CTA for sleep-history and cosmos-decoded (NOT used by katha/top10
    which carry separate `cta_inserts[]` arrays)."""
    start_s: Required[float]
    type: Required[Literal["soft_inline", "closer_inline"]]
    text: Required[str]
    rendering_note: NotRequired[str]


# ---- Shared base — common to ALL 4 long-form skills -------------------


class LongFormScript(TypedDict, total=False):
    """Shared base shape for narrations/<slug>.json across long-form skills.

    Per-skill subclasses below add format-specific fields while keeping
    these common ones consistent.

    Required-ish fields (renderer crashes without them):
      - slug, narration

    Strongly recommended (skills should always emit, gates may warn):
      - title, hook, chapters[], sources[], title_options[]

    Optional (newer fields; backward-compat for older slugs):
      - title_caps, hook_question, description_hook_p1/p2/p3,
        recommended_videos[], topic_axis_tags[], sleep_ritual_close,
        thumbnail_brief
    """
    # Identity / routing
    slug: Required[str]
    format: NotRequired[
        Literal["long_form", "long_form_animated", "top10", "katha-long-form"]
    ]
    channel: NotRequired[str]               # "historyrecapped", "cosmosdecoded", ...

    # Narration body — the renderer reads this field
    narration: Required[str]                # single string with \n\n paragraph breaks

    # Authoring metadata
    title: NotRequired[str]                 # primary title for YouTube
    title_options: NotRequired[list[str]]   # alternates for A/B
    hook: NotRequired[str]                  # one-paragraph pitch (legacy field)
    thumbnail_brief: NotRequired[str]       # description for thumbnail design

    # Chapters & sources — feed upload description token resolvers
    chapters: NotRequired[list[Chapter]]
    sources: NotRequired[list[str | SourceCitation]]

    # Sleep-history-style description-template tokens (NEW 2026-05-08).
    # Other skills can opt in by populating these fields; the renderer's
    # upload-glue (`<channel>/scripts/upload_long_form.py`) builds the
    # corresponding `{script.X_block}` tokens automatically. Backward-compat:
    # a slug that omits these renders an empty section in the description.
    title_caps: NotRequired[str]                # ALL CAPS variant for desc-top header
    hook_question: NotRequired[str]             # one-line question hook
    description_hook_p1: NotRequired[str]       # body paragraph 1 — topic intro
    description_hook_p2: NotRequired[str]       # body paragraph 2 — value promise
    description_hook_p3: NotRequired[str]       # body paragraph 3 — subscribe CTA
    recommended_videos: NotRequired[list[str]]  # 2-5 URLs
    topic_axis_tags: NotRequired[list[str]]     # picked from channel YAML vocab

    # Sleep-history-only sensory close (RING composition with the hook).
    # Documented in this base TypedDict because future long-form skills
    # may adopt it (cosmos-decoded "How We Knew" closer is a candidate).
    sleep_ritual_close: NotRequired[str]

    # Author-only annotations — renderers ignore these but downstream
    # tooling (critique-audio/-video, future linters) may use them.
    duration_target_s: NotRequired[int | tuple[int, int] | list[int]]
    wpm_target: NotRequired[int]
    tone: NotRequired[str]
    _pronunciation_notes: NotRequired[str]
    _render_extension_notes: NotRequired[str]
    _comment: NotRequired[str]


# ---- /make-sleep-history extension -----------------------------------


class Panel(TypedDict, total=False):
    """Image-panels (Path B) per-beat scene description."""
    index: Required[int]
    start_s: Required[float]
    end_s: Required[float]
    scene: Required[str]                    # diffusion prompt
    mode: NotRequired[Literal["bold_ink", "soft_painted"]]


class SleepHistoryScript(LongFormScript, total=False):
    """Sleep-history extension. Adds Path-B panels[] and explicit support_asks[]."""
    panels: NotRequired[list[Panel]]                # Path B / hybrid only; cap 24
    support_asks: NotRequired[list[SupportAsk]]     # optional; closer 4-stack lives in narration


# ---- /make-top10 extension --------------------------------------------


class RankItem(TypedDict, total=False):
    """One entry in a Top-10 countdown."""
    rank: Required[int]                     # 10 → 1
    title: Required[str]
    year: NotRequired[int]
    location: NotRequired[str]
    title_card_text: NotRequired[str]
    narration_anchor: NotRequired[str]
    key_facts: NotRequired[list[str]]
    unsolved_hook: NotRequired[str]
    connective_tissue_out: NotRequired[str | None]
    footage_queries: NotRequired[list[str]]
    sources: NotRequired[list[dict]]


class CTAInsert(TypedDict, total=False):
    position: Required[str]                 # "after_intro_before_rank10", "in_closer", ...
    text: Required[str]


class Top10Script(LongFormScript, total=False):
    """Top-10 extension. Replaces chapters[] semantics with ranks[]."""
    ranks: NotRequired[list[RankItem]]
    cta_inserts: NotRequired[list[CTAInsert]]
    chapter_timestamps: NotRequired[list[dict]]   # per-skill chapter-marker shape
    topic: NotRequired[str]
    _brand_caveat: NotRequired[str]


# ---- /make-katha extension --------------------------------------------


class KathaShloka(TypedDict, total=False):
    shloka_n: Required[int]
    devanagari: Required[str]               # original verse
    anuvad: Required[str]                   # Hindi prose translation


class KathaChapter(TypedDict, total=False):
    """Katha chapter — Hindu scripture chapter with shlokas."""
    chapter_n: Required[int]
    chapter_label: Required[str]            # Devanagari
    narration_anchor: Required[str]
    mool_shloka: NotRequired[list[KathaShloka]]
    prose_summary: Required[str]
    bhavarth: NotRequired[str]              # spiritual takeaway
    characters: NotRequired[list[str]]
    locations: NotRequired[list[str]]
    sources: NotRequired[list[SourceCitation]]
    connective_tissue_out: NotRequired[str | None]


class KathaScript(LongFormScript, total=False):
    """Katha extension. Hindi scripture devotional schema."""
    text: NotRequired[str]                  # "bhagavad-gita" | "mahabharat" | ...
    section: NotRequired[str]               # chapter/adhyay identifier
    tradition: NotRequired[Literal["prose", "sung"]]   # "sung" blocks emit
    blessing_close: NotRequired[str]        # closing blessing in Hindi
    pronunciation_dict: NotRequired[dict[str, str]]    # Devanagari → phonetic
    cta_inserts: NotRequired[list[CTAInsert]]
    chapter_timestamps: NotRequired[list[dict]]
    description_template: NotRequired[str]
    tags: NotRequired[list[str]]
    source: NotRequired[str]                # e.g. "manual:make-katha"
    source_urls: NotRequired[list[str]]
    chapters: NotRequired[list[KathaChapter]]   # type: ignore[misc]   # narrows base


# ---- /make-cosmos-decoder extension ----------------------------------


class CosmosChapter(TypedDict, total=False):
    """Three-act fixed structure: Prediction / Experiment / Consequence."""
    start_s: Required[int]
    title: Required[str]                    # "Prediction (...): ...", etc.
    body_summary: NotRequired[str]


class CosmosScript(LongFormScript, total=False):
    """Cosmos-decoded extension. 3-act explainer schema."""
    topic: NotRequired[str]                 # physics concept / mission / person
    support_asks: NotRequired[list[SupportAsk]]   # exactly 2 per learnings
    chapters: NotRequired[list[CosmosChapter]]   # type: ignore[misc]


# ---- Soft validator (warn, don't reject) -----------------------------


def validate_long_form_script(
    script: dict, *, skill: str, strict: bool = False
) -> list[str]:
    """Return a list of warning strings; empty if all good.

    `skill`: 'sleep-history' | 'top10' | 'katha' | 'cosmos-decoded'.
    `strict`: if True, raise ValueError on any issue. Default False
    (renderers stay permissive — older slugs that predate this module
    still work).

    Usage in skill quality gates:
        warnings = validate_long_form_script(script, skill='sleep-history')
        if warnings:
            print('\\n'.join(f'[warn] {w}' for w in warnings))
    """
    warnings: list[str] = []

    # Universal checks
    if not script.get("slug"):
        warnings.append("missing required 'slug' field")
    if not script.get("narration"):
        warnings.append("missing required 'narration' field")
    elif not isinstance(script["narration"], str):
        warnings.append(f"'narration' must be a string, got {type(script['narration']).__name__}")

    # Strongly recommended (warn for sleep-history; tolerate for others)
    if skill == "sleep-history":
        for field in (
            "title", "chapters", "sources", "title_options",
            "title_caps", "hook_question",
            "description_hook_p1", "description_hook_p2", "description_hook_p3",
            "recommended_videos", "topic_axis_tags", "sleep_ritual_close",
        ):
            if not script.get(field):
                warnings.append(
                    f"sleep-history: missing recommended '{field}' field "
                    f"(description / closer will lose Sleepy Time History parity)"
                )

    if skill == "top10":
        ranks = script.get("ranks") or []
        if len(ranks) != 10:
            warnings.append(f"top10: expected 10 ranks, got {len(ranks)}")
        if not script.get("cta_inserts"):
            warnings.append("top10: missing 'cta_inserts' (need exactly 2 per spec)")

    if skill == "katha":
        if script.get("tradition") == "sung":
            warnings.append("katha: tradition='sung' is BLOCKED — kill-on-sight per make-katha SKILL.md")
        for field in ("text", "section", "blessing_close", "pronunciation_dict"):
            if not script.get(field):
                warnings.append(f"katha: missing recommended '{field}' field")

    if skill == "cosmos-decoded":
        chapters = script.get("chapters") or []
        if len(chapters) != 3:
            warnings.append(f"cosmos-decoded: expected exactly 3 chapters, got {len(chapters)}")

    if strict and warnings:
        raise ValueError(
            f"long-form script validation failed ({skill}):\n  - "
            + "\n  - ".join(warnings)
        )
    return warnings


__all__ = [
    "Chapter", "SourceCitation", "SupportAsk",
    "LongFormScript",
    "Panel", "SleepHistoryScript",
    "RankItem", "CTAInsert", "Top10Script",
    "KathaShloka", "KathaChapter", "KathaScript",
    "CosmosChapter", "CosmosScript",
    "validate_long_form_script",
]
