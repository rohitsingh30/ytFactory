"""The Stage protocol + the artifact-kind keys stages use to talk through Context.

A Stage is the ONE unit of the pipeline. It reads its input artifact(s) from the
Context, does exactly one job, and writes its output artifact back. Stages never
call each other — the Orchestrator sequences them and the Context is the only bus.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pipeline.core.context import Context


# Canonical artifact keys (the strings used with ctx.get / ctx.put). Kept here so
# every stage references the same constants instead of stringly-typed literals.
class Artifact:
    SOURCE = "source"              # SourceDoc
    SCRIPT = "script"              # Script
    DIRECTOR = "director"          # DirectorPlan   (cast + style lock; animation only)
    STORYBOARD = "storyboard"      # Storyboard     (beats: Scene | ShotRef per kind)
    CHUNKS = "chunks"              # list[Chunk]    (audio_prompt + one visual per chunk)
    AUDIO = "audio"                # AudioResult
    TIMELINE = "timeline"          # Timeline
    OVERLAYS = "overlays"          # list[OverlayElement]
    MUSIC = "music"                # MusicBed
    VIDEO = "video"                # ComposedVideo  (Compose renders chunk visuals + composes all)
    UPLOAD = "upload"              # UploadResult
    CRITIQUE = "critique"          # Critique


@runtime_checkable
class Stage(Protocol):
    """One pipeline step. Implementations live in their concern package
    (e.g. ``pipeline.sources.source_stage.SourceStage``)."""

    #: stable stage name used for telemetry envelopes + the orchestrator's stage list
    name: str

    def run(self, ctx: "Context") -> None:
        """Read inputs via ``ctx.get(...)``, do one job, write output via ``ctx.put(...)``.
        Raise on failure — never write a placeholder artifact."""
        ...
