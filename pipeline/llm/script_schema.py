"""Shared script schema for every render kind.

Pre-2026-05-12 each renderer module owned its own narration shape:

- ``pipeline.llm.rewrite.Script`` — Shorts shape (slug, hook, narration,
  title_options, source_url, source). Authored by ``rewrite()``.
- Long-form narration JSON loaded by ``pipeline.render.long_form`` —
  ``{narration | sections[].text, panels[]?, sources[]?}``. Authored
  by hand or by the ``/make-katha`` skill.
- Sports-doc narration — its own chaptered shape.

The schemas drifted: a renderer that wanted to consume "any" narration
had to know which mode produced it. The unified renderer
(``pipeline.render.video``) dispatches by ``ScriptEnvelope.kind``
instead.

Why an envelope, not one mega-class
------------------------------------

Per the rubber-duck critique on Slice 1: long-form is a structurally
different artifact (sectioned narration, panel/shotlist hints, source
plan) — squeezing it into the Shorts ``Script`` as optional fields
would be a leaky abstraction.

Instead: an outer ``ScriptEnvelope`` carries shared metadata
(slug, kind, title_options, source_url, source) and ONE of three
typed payloads — ``short``, ``long_form``, ``sports_doc`` — picked by
``kind``. Each payload is the canonical shape for its mode.

Backward compat: existing ``rewrite.Script`` is structurally a
``ShortScript`` plus the envelope-level fields. ``ScriptEnvelope.from_short_script``
adapts existing call sites without breaking them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Mode-specific payloads
# ---------------------------------------------------------------------------


@dataclass
class ShortScript:
    """The current Shorts narration shape — what
    ``pipeline.llm.rewrite.rewrite()`` produces today.

    Single tight 22-32 s arc, hook-first, 110-160 words. The hook
    field carries the first 1.5 s line (the curiosity gap) and is
    used for thumbnail/title cues.
    """
    hook: str
    narration: str
    """Full narration including the hook. ASR + beats split this into
    sentences for per-beat image cuts."""


@dataclass
class LongFormSection:
    """One chapter / segment of a long-form narration.

    ``target_s`` is the rewriter's intended duration — the chunked-TTS
    + ASR pipeline does NOT enforce it (audio length wins). Useful as a
    pacing hint for the LLM.

    ``visual_brief`` is an optional one-line hint the visualize stage
    can use when authoring panel scenes / matching footage. Empty for
    pure-archival shotlists.
    """
    id: str
    title: str
    narration: str
    target_s: float | None = None
    visual_brief: str | None = None


@dataclass
class LongFormPanel:
    """One image-panel cue — used when ``visual_mode == longform_panels``.

    Mirrors the ``panels[]`` shape ``pipeline.render.long_form`` already
    expects on disk (see render_long_form's ``script.get("panels")``).
    ``hold_s`` may be auto-padded by the renderer if the total panel
    duration is shorter than the synthesised narration.
    """
    scene: str
    """The image-gen prompt for this panel. Channel YAML's
    ``image_style_prefix`` is prepended at render time."""

    hold_s: float = 30.0


@dataclass
class LongFormScript:
    """Long-form sectioned narration with optional panel/shotlist hints.

    Authored by ``pipeline.llm.rewrite_long_form.rewrite_long_form()``
    or hand-written. Consumed by ``pipeline.render.long_form`` (the
    unified ``pipeline.render.video.render(spec)`` orchestrator
    dispatches to long_form when ``spec.kind == long_form``).

    Shape lines up with what long_form.py loads on disk today
    (``narration`` OR ``sections[].text``, plus optional ``panels[]``)
    so a render that previously hand-built a ``narrations/<slug>.json``
    by hand still works after dropping it through ``to_legacy_dict()``.
    """
    hook: str
    """Opening 30-60 s line — sets the topic hook + emotional frame."""

    thesis: str
    """One-sentence thesis the whole long-form video proves / explores.
    Surfaced in the description; not narrated verbatim."""

    sections: list[LongFormSection]
    """Ordered list of chapters / segments. Concatenated for chunked TTS."""

    narration_flat: str | None = None
    """Optional pre-flattened narration (joined sections). Useful when a
    single rewrite pass produced one paragraph stream instead of
    sectioning it. The renderer falls back to ``"\\n\\n".join(s.narration
    for s in sections)`` if this is empty."""

    panels: list[LongFormPanel] = field(default_factory=list)
    """Per-panel scene + hold cues. Required when the spec's
    ``visual_mode == longform_panels``. Empty list = renderer expects
    a shotlist (archival_shotlist mode) or a hybrid plan."""

    sources: list[str] = field(default_factory=list)
    """Source URLs / refs cited in the description. Long-form uses these
    for the YouTube description's "Footage credits" / "Sources" block."""

    shotlist_hints: list[dict] = field(default_factory=list)
    """Optional per-section footage suggestions when
    ``visual_mode == archival_shotlist``. Each entry: ``{section_id,
    query, source_kind}`` — passed to the footage-match stage."""


@dataclass
class SportsDocScript:
    """Chaptered narration with a separate footage_plan.

    Owned by ``pipeline.render.sports_doc``. NOT consumed by the long_form
    renderer — sports docs have their own overlay-timeline composer
    (talking-head / match-footage / chapter-card lanes). Slice 5 lands
    the rewriter for this; today it's hand-authored via ``/make-sports-doc``.
    """
    hook: str
    chapters: list[dict]
    """List of ``{id, title, narration, footage_plan, talking_head?,
    chapter_card?}``. Free-form for now — formalise once the rewriter
    starts producing them."""

    footage_plan: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass
class ScriptEnvelope:
    """Outer envelope carrying shared metadata + ONE typed payload.

    The payload picked by ``kind``:

    - ``kind == "short"`` → ``short`` populated, others None
    - ``kind == "long_form"`` → ``long_form`` populated, others None
    - ``kind == "sports_doc"`` → ``sports_doc`` populated, others None

    Renderer dispatch:
    >>> env = rewrite_long_form(...)
    >>> assert env.kind == "long_form"
    >>> long_form_renderer.render(env.long_form, env.slug, env.title_options)
    """

    slug: str
    kind: str  # "short" | "long_form" | "sports_doc"

    title_options: list[str] = field(default_factory=list)
    """Candidate titles. Short uses the strongest; long-form uses the
    first as the YouTube title, the rest as A/B alternates."""

    source_url: str = ""
    source: str = ""
    """Original-content provenance. ``source`` is the rough kind
    (``user_text`` / ``reddit_api`` / ``wikipedia_topic`` / …)."""

    short: ShortScript | None = None
    long_form: LongFormScript | None = None
    sports_doc: SportsDocScript | None = None

    def payload(self) -> Any:
        """The active payload, or raise if none is set for this kind."""
        if self.kind == "short" and self.short:
            return self.short
        if self.kind == "long_form" and self.long_form:
            return self.long_form
        if self.kind == "sports_doc" and self.sports_doc:
            return self.sports_doc
        raise ValueError(
            f"ScriptEnvelope.kind={self.kind!r} but no matching payload set"
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict that drops the inactive payloads.

        Persisted to disk as the canonical ``scripts/<slug>.json`` (Shorts)
        or ``narrations/<slug>.json`` (long-form). The mode-specific
        renderers read it back via ``from_dict``.
        """
        d: dict[str, Any] = {
            "slug": self.slug,
            "kind": self.kind,
            "title_options": list(self.title_options),
            "source_url": self.source_url,
            "source": self.source,
        }
        if self.short:
            d["short"] = asdict(self.short)
        if self.long_form:
            d["long_form"] = asdict(self.long_form)
        if self.sports_doc:
            d["sports_doc"] = asdict(self.sports_doc)
        return d

    def to_legacy_short_dict(self) -> dict[str, Any]:
        """Flatten to the shape ``pipeline.llm.rewrite.Script.asdict()``
        already produces. For backward compat with downstream code that
        still imports the old ``Script`` dataclass directly."""
        if not self.short:
            raise ValueError("envelope has no short payload to flatten")
        return {
            "slug": self.slug,
            "hook": self.short.hook,
            "narration": self.short.narration,
            "title_options": list(self.title_options),
            "source_url": self.source_url,
            "source": self.source,
        }

    def to_legacy_long_form_dict(self) -> dict[str, Any]:
        """Flatten to the shape ``pipeline.render.long_form`` reads from
        ``narrations/<slug>.json`` today: ``{narration?, sections?,
        panels?, sources?}``. Used by the orchestrator to write the
        narration file the long-form renderer expects without changing
        long_form.py's loader."""
        if not self.long_form:
            raise ValueError("envelope has no long_form payload to flatten")
        lf = self.long_form
        flat = lf.narration_flat or "\n\n".join(s.narration for s in lf.sections)
        d: dict[str, Any] = {
            "slug": self.slug,
            "title_options": list(self.title_options),
            "source_url": self.source_url,
            "source": self.source,
            "narration": flat,
            "sections": [
                {
                    "id": s.id,
                    "title": s.title,
                    "text": s.narration,  # long_form.py reads s["text"]
                    "target_s": s.target_s,
                    "visual_brief": s.visual_brief,
                }
                for s in lf.sections
            ],
        }
        if lf.panels:
            d["panels"] = [{"scene": p.scene, "hold_s": p.hold_s} for p in lf.panels]
        if lf.sources:
            d["sources"] = list(lf.sources)
        if lf.shotlist_hints:
            d["shotlist_hints"] = list(lf.shotlist_hints)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScriptEnvelope":
        """Inverse of ``to_dict``. Tolerates missing optional fields."""
        kind = d.get("kind") or "short"
        env = cls(
            slug=d.get("slug", ""),
            kind=kind,
            title_options=list(d.get("title_options") or []),
            source_url=d.get("source_url", ""),
            source=d.get("source", ""),
        )
        if "short" in d and d["short"]:
            env.short = ShortScript(**d["short"])
        if "long_form" in d and d["long_form"]:
            lf = dict(d["long_form"])
            env.long_form = LongFormScript(
                hook=lf.get("hook", ""),
                thesis=lf.get("thesis", ""),
                sections=[LongFormSection(**s) for s in lf.get("sections") or []],
                narration_flat=lf.get("narration_flat"),
                panels=[LongFormPanel(**p) for p in lf.get("panels") or []],
                sources=list(lf.get("sources") or []),
                shotlist_hints=list(lf.get("shotlist_hints") or []),
            )
        if "sports_doc" in d and d["sports_doc"]:
            env.sports_doc = SportsDocScript(**d["sports_doc"])
        return env

    @classmethod
    def from_short_script(
        cls, script_obj: Any, *, source_url: str = "", source: str = ""
    ) -> "ScriptEnvelope":
        """Adapt the existing ``pipeline.llm.rewrite.Script`` into an
        envelope. Lets call sites that produce the old shape feed into
        the new orchestrator without changing rewrite.py."""
        return cls(
            slug=getattr(script_obj, "slug", ""),
            kind="short",
            title_options=list(getattr(script_obj, "title_options", []) or []),
            source_url=getattr(script_obj, "source_url", source_url),
            source=getattr(script_obj, "source", source),
            short=ShortScript(
                hook=getattr(script_obj, "hook", ""),
                narration=getattr(script_obj, "narration", ""),
            ),
        )


__all__ = [
    "ScriptEnvelope",
    "ShortScript",
    "LongFormScript",
    "LongFormSection",
    "LongFormPanel",
    "SportsDocScript",
]
