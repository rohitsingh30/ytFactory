"""pipeline.core — the frozen spine every stage builds against.

Contracts (the typed artifacts on the HLD arrows) + the Stage protocol + Context
(the bus) + Orchestrator (the sequencer). Nothing here imports a stage; stages import
from here. This is the coordination interface for the parallel rebuild — do not change
a contract shape without a design sign-off.
"""
from __future__ import annotations

from pipeline.core.contracts import (
    # enums
    RenderKind, VisualKind, AudioMode, MusicPolicy, CaptionsLayout,
    ShotSize, CameraAngle, Framing, Tone, Lighting, CaptionPosition, CaptionHighlight,
    # input + styling config
    RenderSpec, CaptionStyle, Watermark,
    # artifacts
    SourceDoc, Camera, Mood, Scene, ShotRef, Beat, Storyboard, ScriptSection, Script, Appearance, CastLock,
    Word, Segment, Timeline, AudioResult, VisualAsset, VisualAssets, OverlayElement,
    MusicBed, ComposedVideo, Metadata, UploadResult, Finding, Critique,
    # AI model I/O
    ImageModelPrompt, ImagePrompt, ImageResult, AudioModelPrompt, AlignRequest, Chunk,
    ScriptRequest, CritiqueRequest,
    # director / scene / prompt
    Style, DirectorPlan, DirectorRequest, SceneRequest, PromptRequest,
)
from pipeline.core.models import (
    ScriptService,
    ImageGenerationModel, AudioGenerationModel, ASRModel, CritiqueSkill,
    DirectorLLM, SceneDefinitionAI, ChunkingAI,
)
from pipeline.core.stage import Stage, Artifact
from pipeline.core.context import Context, Telemetry, Storage, Config
from pipeline.core.orchestrator import Orchestrator, RenderResult

__all__ = [
    "RenderKind", "VisualKind", "AudioMode", "MusicPolicy", "CaptionsLayout",
    "ShotSize", "CameraAngle", "Framing", "Tone", "Lighting", "CaptionPosition", "CaptionHighlight",
    "RenderSpec", "CaptionStyle", "Watermark",
    "SourceDoc", "Camera", "Mood", "Scene", "ShotRef", "Beat", "Storyboard", "ScriptSection", "Script", "Appearance", "CastLock",
    "Word", "Segment", "Timeline", "AudioResult", "VisualAsset", "VisualAssets", "OverlayElement",
    "MusicBed", "ComposedVideo", "Metadata", "UploadResult", "Finding", "Critique",
    "ImageModelPrompt", "ImagePrompt", "ImageResult", "AudioModelPrompt", "AlignRequest", "Chunk",
    "ScriptRequest", "CritiqueRequest",
    "Style", "DirectorPlan", "DirectorRequest", "SceneRequest", "PromptRequest",
    "ScriptService",
    "ImageGenerationModel", "AudioGenerationModel", "ASRModel", "CritiqueSkill",
    "DirectorLLM", "SceneDefinitionAI", "ChunkingAI",
    "Stage", "Artifact", "Context", "Telemetry", "Storage", "Config",
    "Orchestrator", "RenderResult",
]
