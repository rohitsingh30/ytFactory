"""Universal niche video schema.

Every video produced by ytFactory — across 7 channels and ~19 niches —
serializes to the same JSON shape. Niches differ in *values* (beat
role labels, closer text, render pipeline), not in *structure*.

This is the contract every producer (Claude Code skill, future cloud
agent, web form, batch importer) writes to GCS, and every consumer
(state API, renderer, validator) reads back. The state API
(``control/state_routes.py``) validates every PUT against this model
before write.

Why one schema, not 19:
    A schema-per-niche meant maintaining 19 places to keep in sync
    with each rule change. Forcing different niches into the same
    shape might feel artificial, but in practice every video has the
    same parts:

      title  +  hook  +  narration  +  ordered beats  +  closer

    The beats are flexible — a history Short's beats are
    place-date / stakes / subject / ... ; a Reddit Short's beats are
    post / comment / comment / ... ; a kathaa's beats are
    chapter_1 / chapter_2 / ... — but they're all "ordered narrative
    units with text + optional visual cue + optional source asset".
    The schema captures that without forcing a specific role
    vocabulary.

The skill body is the AUTHORING GUIDE for a specific niche; this
schema is the OUTPUT CONTRACT every authoring guide ships to.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Title(BaseModel):
    """One or more YouTube title candidates. Curator/A-B test picks one."""

    options: list[str] = Field(..., min_length=1, max_length=10)
    selected_index: int = 0

    @model_validator(mode="after")
    def _selected_in_range(self):
        if not (0 <= self.selected_index < len(self.options)):
            raise ValueError(
                f"selected_index={self.selected_index} out of range "
                f"for {len(self.options)} options"
            )
        return self


class Narration(BaseModel):
    """The full spoken text + cadence targets."""

    text: str = Field(..., min_length=1)
    word_count: Optional[int] = None
    duration_target_s: Optional[float] = Field(None, gt=0)


class FootageWindow(BaseModel):
    """Archival-channel beat: a clip from a YouTube source."""

    source_url: str
    in_s: float = Field(..., ge=0)
    out_s: float = Field(..., gt=0)
    match_text: Optional[str] = None
    black_intro: Optional[bool] = None

    @model_validator(mode="after")
    def _out_after_in(self):
        if self.out_s <= self.in_s:
            raise ValueError(f"out_s ({self.out_s}) must exceed in_s ({self.in_s})")
        return self


class Beat(BaseModel):
    """One ordered narrative unit. Flexible enough to cover every niche.

    For history Shorts, role ∈ {place_date, stakes, subject, plan, ...}.
    For AITA, role ∈ {hook, context, incident, conflict, twist, ...}.
    For Reddit threads, role ∈ {post_title, post_body, comment, ...}.
    For kathaa, role ∈ {chapter_<n>, ...}.
    For last5/ranking countdowns, role ∈ {rank_5, rank_4, ...}.

    Text is what gets spoken. key_visual + scene drive image gen for
    animated paths. footage_window pins archival paths to a YouTube
    clip. extras carries niche-specific data that doesn't fit anywhere
    else (e.g. {author: "u/foo", upvotes: 1234} for Reddit).
    """

    index: int = Field(..., ge=0)
    role: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    key_visual: Optional[str] = None
    scene: Optional[str] = None
    footage_window: Optional[FootageWindow] = None
    extras: dict[str, Any] = Field(default_factory=dict)


class Closer(BaseModel):
    """The end-of-video CTA. ``spoken`` is the literal narration tail.
    ``style`` selects which visual closer panel the renderer overlays
    (military / general / mythological / aita-vote / brain-rot / ...).
    """

    spoken: str = Field(..., min_length=1)
    style: str = Field(..., min_length=1)


class RenderConfig(BaseModel):
    """How this video gets rendered. Renderer dispatches on ``pipeline``."""

    pipeline: Literal[
        "footage_only",
        "shorts",
        "long_form",
        "long_form_doc",
        "split_screen",
        "tweet_reaction",
        "rivalry_recap",
    ]
    aspect: Literal["9:16", "16:9"] = "9:16"
    tts_provider: Optional[str] = None
    tts_voice: Optional[str] = None
    image_provider: Optional[str] = None


class Source(BaseModel):
    """A canonical reference. Goes into the YouTube description."""

    url: str
    type: str = "wikipedia"
    title: Optional[str] = None


class Metadata(BaseModel):
    """Everything that's about-the-video but isn't content or render."""

    sources: list[Source] = Field(default_factory=list)
    pronunciation_notes: Optional[str] = None
    research_notes: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class ValidationGates(BaseModel):
    """Niche-specific quality gates encoded as data, not prose.

    The renderer + state API check these against the content before
    accepting / rendering. Today most of the gates live in skill body
    prose; once niches populate this, gates become enforceable at the
    boundary instead of relying on Claude Code reading prose.
    """

    word_count_band: Optional[tuple[int, int]] = None
    min_numbers_in_narration: Optional[int] = None
    max_duration_s: Optional[float] = None
    closer_literal_options: Optional[list[str]] = None
    banned_phrases: list[str] = Field(default_factory=list)
    required_beat_roles: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Top-level envelope
# ---------------------------------------------------------------------------


class NicheVideo(BaseModel):
    """Universal video envelope — every niche, every channel.

    Skills produce one of these. State API validates and stores it.
    Renderer reads it back and dispatches.
    """

    schema_version: Literal["1"] = "1"
    channel: str = Field(..., min_length=1)
    niche: str = Field(..., min_length=1)
    slug: str = Field(..., min_length=1, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

    title: Title
    hook: str = Field(..., min_length=1)
    narration: Narration
    beats: list[Beat] = Field(..., min_length=1)
    closer: Closer

    render: RenderConfig
    metadata: Metadata = Field(default_factory=Metadata)
    validation: ValidationGates = Field(default_factory=ValidationGates)
    extras: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _beats_indexed_in_order(self):
        for i, b in enumerate(self.beats):
            if b.index != i:
                raise ValueError(
                    f"beats must be indexed 0..{len(self.beats) - 1} in order; "
                    f"got beat[{i}].index={b.index}"
                )
        return self

    @model_validator(mode="after")
    def _gates_consistent_with_content(self):
        """Run the data-driven gates from ``validation`` against ``narration``
        + ``closer``. These are advisory at the schema layer (we do not
        compute word count, run banned-phrase scan, etc. heuristically) —
        but the gates that ARE checkable here we check.
        """
        v = self.validation

        if v.word_count_band is not None and self.narration.word_count is not None:
            lo, hi = v.word_count_band
            if not (lo <= self.narration.word_count <= hi):
                raise ValueError(
                    f"narration.word_count={self.narration.word_count} "
                    f"outside niche band [{lo}, {hi}]"
                )

        if v.closer_literal_options:
            if self.closer.spoken not in v.closer_literal_options:
                raise ValueError(
                    f"closer.spoken does not match any niche-allowed literal; "
                    f"allowed: {v.closer_literal_options}"
                )

        if v.banned_phrases:
            blob = (self.narration.text + " " + self.closer.spoken).lower()
            hit = [p for p in v.banned_phrases if p.lower() in blob]
            if hit:
                raise ValueError(f"banned phrases present: {hit}")

        if v.required_beat_roles:
            roles = {b.role for b in self.beats}
            missing = [r for r in v.required_beat_roles if r not in roles]
            if missing:
                raise ValueError(
                    f"required beat roles missing: {missing} "
                    f"(present: {sorted(roles)})"
                )

        return self


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_payload(payload: dict[str, Any]) -> NicheVideo:
    """Parse + validate a niche video payload. Raises ValidationError on
    schema violation. Returns the parsed model on success."""
    return NicheVideo.model_validate(payload)


def schema_dict() -> dict[str, Any]:
    """JSON Schema export — for tooling / web-form generators."""
    return NicheVideo.model_json_schema()


# ---------------------------------------------------------------------------
# Niche registry — known niches + their default render config
# ---------------------------------------------------------------------------
#
# Producers (skills) consult this to pre-fill render config + the
# default closer/banned-phrase gates. The registry is data, not code —
# adding a niche is one entry here, no schema change needed.
#

NICHE_REGISTRY: dict[str, dict[str, Any]] = {
    "history-short": {
        "channel": "historyrecapped",
        "default_render": {
            "pipeline": "footage_only",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
        },
        "default_validation": {
            "word_count_band": [130, 138],
            "min_numbers_in_narration": 3,
            "closer_literal_options": [
                "LIKE to honor those who served. SUBSCRIBE for more such stories.",
                "LIKE if you learned something. SUBSCRIBE for more such stories.",
            ],
            "banned_phrases": [
                "smash that subscribe button",
                "vote in comments",
                "hit the bell icon",
                "AITA", "WIBTA", "YTA", "NTA", "NAH", "ESH",
            ],
            "required_beat_roles": [
                "place_date", "stakes", "subject", "plan", "complication",
                "action", "twist", "resolution", "cost", "meaning", "closer",
            ],
        },
    },
    "history-sleep": {
        "channel": "historyrecapped",
        "default_render": {
            "pipeline": "long_form",
            "aspect": "16:9",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "english-male-history-sleep-narrator",
        },
        "default_validation": {
            "min_numbers_in_narration": 0,
            "banned_phrases": ["smash that subscribe button", "vote in comments"],
        },
    },
    "cosmos-short": {
        "channel": "cosmosdecoded",
        "default_render": {
            "pipeline": "footage_only",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "michael.wav",
        },
        "default_validation": {
            "word_count_band": [145, 165],
            "banned_phrases": ["smash that subscribe button"],
        },
    },
    "cosmos-long": {
        "channel": "cosmosdecoded",
        "default_render": {
            "pipeline": "long_form",
            "aspect": "16:9",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "michael.wav",
        },
        "default_validation": {},
    },
    "hindutava-short": {
        "channel": "hindutavaanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
            "tts_provider": "cloudrun_indicf5",
            "tts_voice": "hindutava",
            "image_provider": "cloudrun_z_image_turbo",
        },
        "default_validation": {
            "banned_phrases": ["AITA", "WIBTA"],
        },
    },
    "hindutava-long": {
        "channel": "hindutavaanimated",
        "default_render": {
            "pipeline": "long_form",
            "aspect": "16:9",
            "tts_provider": "cloudrun_indicf5",
            "tts_voice": "hindi-female-iitm-anchor",
            "image_provider": "cloudrun_z_image_turbo",
        },
        "default_validation": {},
    },
    "hindutava-katha": {
        "channel": "hindutavaanimated",
        "default_render": {
            "pipeline": "long_form",
            "aspect": "16:9",
            "tts_provider": "cloudrun_indicf5",
            "tts_voice": "hindutava",
        },
        "default_validation": {},
    },
    "aita-animated": {
        "channel": "mystoriesanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
            "image_provider": "cloudrun_z_image_turbo",
        },
        "default_validation": {
            "closer_literal_options": [
                "LIKE if YTA. COMMENT if NTA.",
                "LIKE if you'd do the same. COMMENT what you'd do.",
            ],
            "banned_phrases": [
                "AITA", "WIBTA", "YTA", "NTA", "NAH", "ESH",
                "smash that subscribe button", "vote in comments",
            ],
        },
    },
    "aita-cooking": {
        "channel": "mystoriesanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
        },
        "default_validation": {
            "closer_literal_options": [
                "LIKE if YTA. COMMENT if NTA.",
            ],
            "banned_phrases": ["AITA", "WIBTA", "YTA", "NTA"],
        },
    },
    "aita-cliffhanger": {
        "channel": "mystoriesanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
        },
        "default_validation": {},
    },
    "tifu-animated": {
        "channel": "mystoriesanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
            "image_provider": "cloudrun_z_image_turbo",
        },
        "default_validation": {
            "banned_phrases": ["AITA", "YTA", "NTA"],
        },
    },
    "wiki-oddities": {
        "channel": "mystoriesanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
        },
        "default_validation": {},
    },
    "today-in-history": {
        "channel": "mystoriesanimated",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
        },
        "default_validation": {},
    },
    "football-explainer": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "long_form_doc",
            "aspect": "16:9",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "michael.wav",
        },
        "default_validation": {},
    },
    "sports-doc": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "long_form_doc",
            "aspect": "16:9",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "michael.wav",
        },
        "default_validation": {},
    },
    "sports-last5": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "footage_only",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "michael.wav",
        },
        "default_validation": {
            "required_beat_roles": ["rank_5", "rank_4", "rank_3", "rank_2", "rank_1", "closer"],
        },
    },
    "sports-ranking": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "footage_only",
            "aspect": "9:16",
        },
        "default_validation": {
            "required_beat_roles": ["rank_5", "rank_4", "rank_3", "rank_2", "rank_1", "closer"],
        },
    },
    "sports-rivalry": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "rivalry_recap",
            "aspect": "9:16",
        },
        "default_validation": {},
    },
    "sports-tweet-reaction": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "tweet_reaction",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "michael.wav",
        },
        "default_validation": {},
    },
    "reddit-thread": {
        "channel": "sportsrecapped",
        "default_render": {
            "pipeline": "split_screen",
            "aspect": "9:16",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
        },
        "default_validation": {},
    },
    "rhyme-bilingual": {
        "channel": "rhymetimejunction",
        "default_render": {
            "pipeline": "shorts",
            "aspect": "9:16",
        },
        "default_validation": {},
    },
    "top10-long": {
        "channel": "historyrecapped",
        "default_render": {
            "pipeline": "long_form",
            "aspect": "16:9",
            "tts_provider": "cloudrun_chatterbox",
            "tts_voice": "sarah.wav",
        },
        "default_validation": {
            "required_beat_roles": [
                "rank_10", "rank_9", "rank_8", "rank_7", "rank_6",
                "rank_5", "rank_4", "rank_3", "rank_2", "rank_1", "closer",
            ],
        },
    },
}


def list_niches() -> list[str]:
    """Stable list of registered niche keys."""
    return sorted(NICHE_REGISTRY.keys())


def niche_defaults(niche: str) -> dict[str, Any]:
    """Defaults producers can pre-fill into a NicheVideo. Empty dict if
    the niche is not registered (forward-compat for new niches)."""
    return NICHE_REGISTRY.get(niche, {})
