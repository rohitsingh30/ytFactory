"""AI model Protocols — the callable interface each model implementation satisfies.

Every AI piece in the design is here, as a typed interface. Data shapes live in
``pipeline.core.contracts``; this module is ONLY the interfaces, so a stage depends on
the abstraction (e.g. ``ImageModel``) and the concrete impl (``ZImageTurbo``,
``ChatterboxTTS``, …) is injected. None of these models is optional — the pipeline
requires every one.

Grounded in the real call sites:
  ImageModel.generate  ← pipeline/images/images.py (POST prompt/negative/seed/w/h/steps/cfg)
  TTSModel.synth       ← pipeline/tts/cloudrun.py  (POST text/voice/speed -> wav)
  ASRModel.align       ← pipeline/asr_cloudrun.py  (POST /align wav_url/anchors/mode -> Segment[])
  *Model (LLM)         ← pipeline/llm/cli.py        (build prompt -> call -> parse typed output)
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.core.contracts import (
    AlignRequest,
    AudioModelPrompt,
    AudioResult,
    Critique,
    CritiqueRequest,
    DirectorPlan,
    DirectorRequest,
    ImageModelPrompt,
    ImagePrompt,
    ImageResult,
    PromptRequest,
    Script,
    SceneRequest,
    ScriptRequest,
    Storyboard,
    Timeline,
)


# ---------------------------------------------------------------------------
# LLM models (typed request -> build prompt -> call -> parse typed output)
# ---------------------------------------------------------------------------


@runtime_checkable
class ScriptService(Protocol):
    def write(self, req: ScriptRequest) -> Script: ...


@runtime_checkable
class CritiqueSkill(Protocol):
    def critique(self, req: CritiqueRequest) -> Critique: ...


# ---------------------------------------------------------------------------
# Generative models (typed input -> media output)
# ---------------------------------------------------------------------------


@runtime_checkable
class ImageGenerationModel(Protocol):
    def generate(self, prompt: ImageModelPrompt) -> ImageResult: ...


@runtime_checkable
class AudioGenerationModel(Protocol):
    def synth(self, prompt: AudioModelPrompt) -> AudioResult: ...


@runtime_checkable
class ASRModel(Protocol):
    def align(self, req: AlignRequest) -> Timeline: ...


# ---------------------------------------------------------------------------
# Director / Scene / Prompt models (the authoring-visual AI layer)
# ---------------------------------------------------------------------------


@runtime_checkable
class DirectorLLM(Protocol):
    """Locks Cast + Style once for the whole render (the HLD 'Director LLM')."""

    def lock(self, req: DirectorRequest) -> DirectorPlan: ...


@runtime_checkable
class SceneDefinitionAI(Protocol):
    """Scene Definition AI: segments narration into beats, assigns each a VisualKind, and
    directs it — a Scene for ANIMATION, or (acting as the Footage/Clip Planner) a ShotRef
    for FOOTAGE."""

    def direct(self, req: SceneRequest) -> Storyboard: ...


@runtime_checkable
class ChunkingAI(Protocol):
    """The HLD 'Chunking AI': refines Scene + Cast + Style into the creative image text.
    Render numerics are added by ChunkStage, which emits the Chunk."""

    def refine_image(self, req: PromptRequest) -> ImagePrompt: ...
