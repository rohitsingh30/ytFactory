"""Populate ``spec.extra`` with script-derived render-time inputs.

The :class:`pipeline.render.spec.RenderSpec` is built ONCE at job-creation
time (before the script has been authored) — see
:func:`pipeline.render.spec.build_spec`. The visualize plugins, however,
need two pieces of information that only exist AFTER the script + cast
have been authored:

* ``era_anchor_prefix`` — the ``[ERA — <costume tokens>]`` directorial
  prefix that suppresses modernist defaults in the diffusion model.
  Pre-fix: ``ai_beat_slideshow.py`` read it from ``spec.extra``, but
  NOTHING in :func:`build_spec` wrote it there — so 17+ renders shipped
  with WW1 trenches instead of the era the script's
  ``metadata.era_anchor`` declared (the audit's "Mongols 1258 rendered
  as WW1" bug). See ``docs/post-audit-2026-05-14.md``.

* ``character_description`` — the per-render narrator/protagonist
  description authored by ``pipeline.llm.cast`` into
  ``<channel>/cast/<slug>.json``. Same broken-pipe bug class: read from
  ``spec.extra`` by the visualize plugins, never written there by
  ``build_spec``. Result: near-universal cast drift (5 different
  anonymous footballers as "Ronaldinho" across one Short).

This module bridges the two phases. :func:`populate_render_extras` is
called at the TOP of every engine (``render_short`` / ``render_long``)
AFTER the script has been loaded but BEFORE the visualize plugin is
fetched. The function is idempotent — if a caller has already populated
``spec.extra`` (tests, fixtures, or future code that pre-stages
prompts), it leaves those keys untouched.

Why this isn't in ``build_spec`` itself
---------------------------------------

``build_spec`` runs at the orchestrator level (browser → /api/chat/confirm
→ Firestore job doc) where the script JSON hasn't been authored yet.
That stage owns ONLY the proposal + YAML chain; threading the unauthored
script into it would conflate concerns. The engines run on the cloud
worker AFTER ``pipeline.llm.rewrite`` produces the script — that's the
natural injection point.

Hard rules
----------

* Don't swallow errors silently. If ``script.metadata.era_anchor`` is
  set but :func:`pipeline.era_anchor.era_prefix_for` returns None,
  log a WARNING that names the unknown era key + the closest matches.
  Same goes for malformed cast.json — log + skip, don't poison the
  render.
* Catch only the specific exceptions that can fire (FileNotFoundError,
  JSONDecodeError, KeyError). No BLE001 catch-alls.
* Idempotent: if ``spec.extra`` already has a key, don't overwrite.
  Lets tests + future pre-staging code take priority.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pipeline import era_anchor
from pipeline.paths import RenderPaths
from pipeline.render.spec import RenderSpec


_logger = logging.getLogger(__name__)


def populate_render_extras(spec: RenderSpec, script: dict[str, Any]) -> None:
    """Mutate ``spec.extra`` in place with script-derived render inputs.

    Reads:

    * ``script["metadata"]["era_anchor"]`` (preferred) or
      ``script["metadata"]["era_lock"]`` (legacy alias from earlier
      audit drafts) → :func:`era_anchor.era_prefix_for` →
      ``spec.extra["era_anchor_prefix"]``.
    * ``<channel>/cast/<slug>.json``:
        - ``cast["narrator"]["description"]`` (preferred — set by the
          cast LLM stage)
        - ``cast["character_description"]`` (fallback — top-level shape
          used by some older channels)
      → ``spec.extra["character_description"]``.

    Idempotent: never overwrites a pre-existing ``spec.extra`` key.

    Never raises. Specific failure modes (unknown era key, missing
    cast.json, malformed cast.json) log a WARNING and skip that key —
    the render continues with whatever the visualize plugin's
    fallback behaviour produces.
    """
    if spec.extra is None:  # defensive — dataclass default is {}, but a caller could None it
        spec.extra = {}

    _populate_era_anchor_prefix(spec, script)
    _populate_character_description(spec, script)
    _populate_character_descriptions(spec, script)
    _populate_authored_long_form_panels(spec, script)


def _populate_authored_long_form_panels(
    spec: RenderSpec, script: dict[str, Any]
) -> None:
    """Stage the script's authored ``panels[]`` into
    ``spec.extra["authored_long_form_panels"]`` so the
    ``longform_panels`` visualize plugin can pick them up.

    Why this exists: pre-2026-05-23 the long-form ``longform_panels``
    plugin iterated the per-section ``timeline`` and emitted ONE panel
    per timeline segment, ignoring the LLM-authored ``script.panels[]``
    entirely. For a 30-min long-form that meant 10 panels @ ~125s
    each — the headline panel-count bug. The plugin now
    reads this key when present and falls back to per-section
    behavior only when it's empty/missing.

    The visualize-plugin protocol (``produce(spec, timeline, work_dir)``)
    doesn't pass ``script``; this enricher bridges the gap. Same
    pattern used by ``_populate_character_description`` etc.

    Idempotent: never overwrites a pre-existing key.
    """
    if "authored_long_form_panels" in spec.extra:
        return
    panels = script.get("panels")
    if not isinstance(panels, list) or not panels:
        return
    cleaned: list[dict[str, Any]] = []
    for p in panels:
        if not isinstance(p, dict):
            continue
        scene = (p.get("scene") or "").strip()
        if not scene:
            continue
        try:
            hold_s = float(p.get("hold_s") or 0.0)
        except (TypeError, ValueError):
            hold_s = 0.0
        entry: dict[str, Any] = {"scene": scene, "hold_s": hold_s}
        after = p.get("after_section_id")
        if after:
            entry["after_section_id"] = str(after)
        cleaned.append(entry)
    if cleaned:
        spec.extra["authored_long_form_panels"] = cleaned
        _logger.info(
            "spec_enrich: staged %d authored long-form panels into spec.extra",
            len(cleaned),
        )


def _populate_era_anchor_prefix(spec: RenderSpec, script: dict[str, Any]) -> None:
    """Resolve ``script.metadata.era_anchor`` → era prefix string.

    Accepts the legacy ``era_lock`` key as a fallback so older scripts
    (pre-2026-05-14 audit) still resolve.
    """
    if "era_anchor_prefix" in spec.extra:
        # Pre-populated by the caller (test fixture, future pre-stage
        # step, …). Honour idempotence — don't clobber.
        return

    metadata = script.get("metadata") or {}
    era_key = metadata.get("era_anchor") or metadata.get("era_lock")
    if not era_key:
        return

    try:
        prefix = era_anchor.era_prefix_for(era_key)
    except RuntimeError as exc:
        # The taxonomy file is unreadable / malformed — load_taxonomy
        # raises RuntimeError in that case. Loud-warn but proceed
        # with the render (the diffusion model will fall back to its
        # default; the operator will see this WARNING in the worker
        # log and fix the taxonomy).
        _logger.warning(
            "spec_enrich: era_anchor taxonomy lookup raised RuntimeError "
            "for era_key=%r — proceeding without era prefix: %s",
            era_key, exc,
        )
        return

    if prefix is None:
        # Unknown era key — surface the typo + suggestions to the
        # operator. This is exactly the "Mongols 1258 → WW1" failure
        # mode the audit caught; don't silently drop it.
        try:
            suggestions = era_anchor.closest_match(era_key)
        except RuntimeError:
            suggestions = []
        _logger.warning(
            "spec_enrich: unknown era_anchor key %r — image-gen will "
            "render WITHOUT a costume/period prefix (likely modernist "
            "defaults). Did you mean one of: %s?",
            era_key, suggestions or "(no close matches)",
        )
        return

    spec.extra["era_anchor_prefix"] = prefix
    _logger.info(
        "spec_enrich: resolved era_anchor=%r → prefix=%r",
        era_key, prefix,
    )


def _populate_character_description(spec: RenderSpec, script: dict[str, Any]) -> None:
    """Read ``<channel>/cast/<slug>.json`` → narrator description.

    Falls through silently when the slug is missing (some test
    fixtures) or the cast file doesn't exist (channel doesn't run the
    cast stage, e.g. footage-only channels).
    """
    if "character_description" in spec.extra:
        # Idempotent — don't overwrite a pre-populated value.
        return

    slug = (script.get("slug") or "").strip()
    if not slug:
        return

    try:
        paths = RenderPaths.from_channel_dir(spec.channel)
    except ValueError as exc:
        # Unparseable channel_dir — shouldn't happen for a real render
        # but tests sometimes pass synthetic spec.channel values. Log
        # and skip rather than crash the engine.
        _logger.warning(
            "spec_enrich: cannot resolve RenderPaths for channel=%r: %s",
            spec.channel, exc,
        )
        return

    cast_path = paths.cast_for(slug)
    try:
        cast = json.loads(cast_path.read_text())
    except FileNotFoundError:
        # No cast.json — common for channels that skip the cast stage
        # (footage-only, archival, song-mode). Silent because it's
        # the documented default behaviour, not an error.
        return
    except (json.JSONDecodeError, OSError) as exc:
        _logger.warning(
            "spec_enrich: cast.json at %s unreadable/malformed (%s) — "
            "rendering without character_description",
            cast_path, exc,
        )
        return

    if not isinstance(cast, dict):
        _logger.warning(
            "spec_enrich: cast.json at %s is %s (expected dict) — skipping",
            cast_path, type(cast).__name__,
        )
        return

    description = _extract_description(cast)
    if not description:
        # File exists but neither field is populated. Quiet info — the
        # cast LLM may have produced a partial doc; nothing actionable
        # for the operator.
        _logger.info(
            "spec_enrich: cast.json at %s has no narrator.description "
            "nor character_description — leaving spec.extra unchanged",
            cast_path,
        )
        return

    spec.extra["character_description"] = description
    _logger.info(
        "spec_enrich: resolved character_description (%d chars) from %s",
        len(description), cast_path,
    )


def _populate_character_descriptions(spec: RenderSpec, script: dict[str, Any]) -> None:
    """P4.3 (Q70): pluralised structured-cast reader.

    Reads ``<channel>/cast/<slug>.json`` and stashes a list of structured
    per-character spec strings at ``spec.extra["character_descriptions"]``.
    Each entry pairs a stable ``role`` token (``narrator`` /
    ``<secondary_name>``) with a single dense sentence assembled from the
    structured fields W2's cast LLM authors:

      "{age} {hair}, {build}, wearing {clothing}{ , holding signature_prop}."

    Beat-prompt assembly (``pipeline.images.images.build_full_prompt`` /
    ``pipeline.images.prompt_refiner._build_user_prompt``) consumes
    ``character_descriptions[0]`` for the canonical narrator
    description (back-compat with ``character_description``) and the
    rest for in-scene secondary characters.

    Idempotent: skips when ``character_descriptions`` is already
    populated. Falls through silently when there's no cast.json /
    structured fields aren't present (older channels without W2's
    P4.3 cast schema).
    """
    if "character_descriptions" in spec.extra:
        return

    slug = (script.get("slug") or "").strip()
    if not slug:
        return

    try:
        paths = RenderPaths.from_channel_dir(spec.channel)
    except ValueError:
        return

    cast_path = paths.cast_for(slug)
    try:
        cast = json.loads(cast_path.read_text())
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, OSError):
        return

    if not isinstance(cast, dict):
        return

    descriptions: list[dict[str, str]] = []

    narrator = cast.get("narrator")
    if isinstance(narrator, dict):
        spec_str = _assemble_structured_spec(narrator)
        if spec_str:
            descriptions.append({"role": "narrator", "spec": spec_str})

    secondaries = cast.get("secondary") or cast.get("supporting") or []
    if isinstance(secondaries, list):
        for entry in secondaries:
            if not isinstance(entry, dict):
                continue
            spec_str = _assemble_structured_spec(entry)
            if spec_str:
                role = (entry.get("name") or entry.get("role") or "supporting").strip()
                descriptions.append({"role": role, "spec": spec_str})

    if descriptions:
        spec.extra["character_descriptions"] = descriptions
        # R3 (2026-05-23): the plural list is the structured-fields output
        # but every downstream consumer today (``ai_beat_slideshow``,
        # ``images.build_full_prompt``) reads the singular
        # ``spec.extra["character_description"]``. Without this back-fill
        # the structured cast work (P4.3) would be dead data. Singular
        # populator runs FIRST and is idempotent; this only fires if it
        # didn't set anything (no narrator description in the older
        # ``cast["narrator"]["description"]`` slot).
        if "character_description" not in spec.extra:
            narrator_spec = descriptions[0].get("spec", "")
            if narrator_spec:
                spec.extra["character_description"] = narrator_spec
        _logger.info(
            "spec_enrich: resolved %d character_descriptions from %s",
            len(descriptions), cast_path,
        )


def _assemble_structured_spec(entry: dict[str, Any]) -> str:
    """Build one dense sentence from W2's structured cast fields.

    Honors only the fields that are non-empty strings; falls back to
    a stripped freeform ``description`` when none of the structured
    fields are present (channel YAMLs that pre-date P4.3).
    """
    fields = {
        k: (entry.get(k) or "").strip()
        for k in ("age", "hair", "build", "clothing", "signature_prop")
    }
    if not any(fields.values()):
        # Older shape — fall back to the freeform description verbatim.
        desc = (entry.get("description") or "").strip()
        return desc
    parts: list[str] = []
    if fields["age"]:
        parts.append(fields["age"])
    if fields["hair"]:
        parts.append(fields["hair"])
    if fields["build"]:
        parts.append(fields["build"])
    head = ", ".join(parts)
    if fields["clothing"]:
        head += f", wearing {fields['clothing']}"
    if fields["signature_prop"]:
        head += f", carrying {fields['signature_prop']}"
    return head + "."


def _extract_description(cast: dict[str, Any]) -> str | None:
    """Pluck the description out of cast.json in priority order.

    Priority:
      1. ``cast["narrator"]["description"]`` — the canonical shape that
         ``pipeline.llm.cast`` writes today.
      2. ``cast["character_description"]`` — top-level fallback for
         older channels (sportsrecapped legacy, the audit's broken
         shape).

    Returns ``None`` when neither field is a non-empty string.
    """
    narrator = cast.get("narrator")
    if isinstance(narrator, dict):
        desc = narrator.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()

    top = cast.get("character_description")
    if isinstance(top, str) and top.strip():
        return top.strip()

    return None


__all__ = ["populate_render_extras"]
