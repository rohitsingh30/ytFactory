"""Frozen artifact contracts — the typed interface between pipeline stages.

Every arrow in the HLD is one of these dataclasses. A stage depends ONLY on the
contract it reads and produces; it never imports another stage. This module is the
coordination interface for the parallel rebuild: agents implement AGAINST these
shapes and MUST NOT change them without a design sign-off.

Grounded in the current pipeline's real shapes:
  SourceDoc  ← pipeline/sources/base.py::RawStory
  Script     ← pipeline/llm/script_schema.py (ShortScript / LongFormScript)
  CastLock   ← pipeline/llm/cast.py narrator+characters JSON
  RenderSpec ← pipeline/render/spec.py::RenderSpec (trimmed to the frozen subset)
  Audio/Segment/VisualAssets/OverlayElement ← pipeline/render/contracts.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Enums — the render knobs (string-valued so they serialise to JSON as-is)
# ---------------------------------------------------------------------------


class RenderKind(str, Enum):
    SHORT = "short"
    LONG_FORM = "long_form"          # sports_doc / footage_only are now visual_mode flags


class VisualKind(str, Enum):
    """Visual source for ONE chunk. A render's palette (RenderSpec.visual_palette) is the
    set of kinds allowed; the planner (SceneModel) assigns each chunk one of them."""

    ANIMATION = "animation"          # AI-generated image (diffusion)
    FOOTAGE = "footage"              # real clip fetched by search


class AudioMode(str, Enum):
    VOICE = "voice"                  # TTS narration (song mode deferred out of v1)


class MusicPolicy(str, Enum):
    DUCKED_LOOP = "ducked_loop"
    SINGLE_BED = "single_bed"
    SECTION_MOOD = "section_mood"
    NONE = "none"


class CaptionsLayout(str, Enum):
    CENTER_WORD = "center_word_by_word"
    SENTENCE = "sentence"
    NONE = "none"


class ShotSize(str, Enum):
    ESTABLISHING = "establishing"
    WIDE = "wide"
    MEDIUM = "medium"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"


class CameraAngle(str, Enum):
    EYE_LEVEL = "eye_level"
    LOW = "low"
    HIGH = "high"
    OVER_SHOULDER = "over_shoulder"
    DUTCH = "dutch"
    BIRDS_EYE = "birds_eye"


class Framing(str, Enum):
    CENTERED = "centered"
    THIRDS_LEFT = "thirds_left"
    THIRDS_RIGHT = "thirds_right"
    TWO_SHOT = "two_shot"


class Tone(str, Enum):
    TENSE = "tense"
    JOYFUL = "joyful"
    SOMBER = "somber"
    TRIUMPHANT = "triumphant"
    EERIE = "eerie"
    TENDER = "tender"
    NEUTRAL = "neutral"


class Lighting(str, Enum):
    WARM_FIRELIGHT = "warm_firelight"
    HARSH_NOON = "harsh_noon"
    NEON_NIGHT = "neon_night"
    OVERCAST = "overcast"
    GOLDEN_HOUR = "golden_hour"
    SOFT_INDOOR = "soft_indoor"


class CaptionPosition(str, Enum):
    CENTER = "center"
    LOWER_THIRD = "lower_third"
    TOP = "top"


class CaptionHighlight(str, Enum):
    NONE = "none"
    WORD = "word"           # highlight the active word only
    FULL = "full"           # solid background behind the full line


# ---------------------------------------------------------------------------
# Styling config (user caption knobs + channel visual defaults)
# ---------------------------------------------------------------------------


@dataclass
class CaptionStyle:
    """User-selectable caption styling (wizard knobs E in the HLD specs)."""

    layout: CaptionsLayout = CaptionsLayout.CENTER_WORD   # one-word vs sentence
    position: CaptionPosition = CaptionPosition.CENTER
    highlight: CaptionHighlight = CaptionHighlight.WORD
    font_family: str = "Montserrat-Bold"
    font_size: int = 96
    enabled: bool = True


@dataclass
class Watermark:
    """Channel watermark. ``enabled=False`` means no watermark (avoids Optional)."""

    enabled: bool = False
    text: str = ""
    position: str = "top_right"      # top_right | top_left | bottom_right | bottom_left
    opacity: float = 0.5


# ---------------------------------------------------------------------------
# 0. RenderSpec — the immutable request that drives one render
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderSpec:
    """One fully-typed render request. Built once by the spec builder; never mutated."""

    # identity / orchestration
    channel: str
    niche: Optional[str]
    source_ref: str                         # the specific story: a URL, or a topic/query; "" = adapter discovers
    kind: RenderKind
    visual_palette: tuple[VisualKind, ...]   # kinds this render may use; planner picks per chunk

    # output shape
    aspect_ratio: str                       # "9:16" | "16:9" | "1:1"
    output_resolution: tuple[int, int]      # (w, h) px
    output_fps: int = 30
    duration_target_s: Optional[int] = None
    duration_max_s: Optional[int] = None

    # audio
    audio_mode: AudioMode = AudioMode.VOICE
    voice_provider: Optional[str] = None    # "cloudrun_chatterbox" | "cloudrun_indicf5" | ...
    voice_id: Optional[str] = None          # bare voice id or ref-wav key (resolved via config)

    # captions / overlays / post-production (channel-resolved render knobs)
    caption_style: CaptionStyle = field(default_factory=CaptionStyle)
    lower_thirds: bool = False
    chapter_cards: bool = False
    closer_panel: bool = False
    watermark: Watermark = field(default_factory=Watermark)
    video_grade: str = "none"               # LUT / colour-grade name (post-production)

    # music
    music_policy: MusicPolicy = MusicPolicy.DUCKED_LOOP
    music_bed: Optional[str] = None         # bed key or "off"; None = channel default

    # narration cadence + QA
    tone: Optional[str] = None              # tts tone_override key
    critic_loop: bool = False
    eval_gates: bool = True                 # HLD Eval Checks (pre-compose + pre-upload); False = skip

    # free-form channel/variant knobs the wizard may add (kept typed at the edges)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. SourceDoc ← SourceStage
# ---------------------------------------------------------------------------


@dataclass
class SourceDoc:
    """One mined story before rewriting. ``body`` is the long text the ScriptStage
    condenses. ``backend`` records which adapter served it (for telemetry + fallback)."""

    slug: str
    title: str
    body: str
    source: str                             # tag e.g. "reddit:AmItheAsshole"
    url: str
    backend: str                            # "anon" | "pullpush" | "wikipedia" | "llm" | ...
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 2. Script ← ScriptStage
# ---------------------------------------------------------------------------


@dataclass
class Camera:
    """Directorial camera spec for one scene (bounded vocab so the models can't drift)."""

    shot_size: ShotSize
    angle: CameraAngle
    framing: Framing


@dataclass
class Mood:
    """Emotional + visual tone of one scene. Also feeds the music bed choice."""

    tone: Tone
    lighting: Lighting
    palette: str            # descriptive, open vocab: "muted earth tones" | ...


@dataclass
class Scene:
    """The complete visual definition of one beat — everything the image prompt needs
    beyond cast appearance: WHERE (setting), WHEN (time_of_day), WHO
    (characters_present, resolved against CastLock), WHAT (action/blocking), and HOW it
    is shot + felt (camera, mood)."""

    setting: str                    # location / environment
    time_of_day: str                # morning | midday | dusk | night | ...
    characters_present: list[str]   # character names -> resolved via CastLock
    action: str                     # what happens / blocking in this beat
    camera: Camera
    mood: Mood


@dataclass
class ShotRef:
    """Footage visual for one chunk — the search query the FootageRetriever resolves to a
    real clip (trimmed to the chunk's audio duration). Consecutive footage chunks sharing
    the same ``query`` read as one continuous shot."""

    query: str                      # what the clip should show
    source_hint: str = ""           # "stock" | "archival" | "wikimedia" | "" (any)


@dataclass
class Beat:
    """One narration segment + its visual intent (the planner's output unit). ANIMATION
    beats carry a Scene (later refined into an image); FOOTAGE beats carry a ShotRef.
    Exactly one visual per beat; ``kind`` is the discriminator."""

    index: int
    narration: str                  # spoken text for this beat
    kind: VisualKind
    visual: Scene | ShotRef


@dataclass
class Storyboard:
    """SceneModel output: the ordered visual breakdown of the whole narration."""

    beats: list[Beat] = field(default_factory=list)


@dataclass
class ScriptSection:
    """One long-form chapter."""

    id: str
    title: str
    narration: str
    target_s: Optional[float] = None        # pacing hint; audio length wins
    visual_brief: Optional[str] = None


@dataclass
class Script:
    """The authored narration — spoken text + chapter structure + metadata. The visual
    breakdown (beats) is the SceneModel's Storyboard, NOT part of the Script.
    ``sections`` is empty for Shorts, populated for long-form (drives chapter cards)."""

    slug: str
    kind: RenderKind
    hook: str                               # first-line curiosity gap
    narration: str
    characters: list[str] = field(default_factory=list)
    sections: list[ScriptSection] = field(default_factory=list)   # long-form chapters
    title_options: list[str] = field(default_factory=list)
    source_url: Optional[str] = None


# ---------------------------------------------------------------------------
# 3. CastLock ← CastStage
# ---------------------------------------------------------------------------


@dataclass
class Appearance:
    """Locked visual description of one character — injected verbatim into every
    image prompt so faces/wardrobe stay consistent across beats."""

    name: str                               # name/relation as narration refers to it
    age: str                                # concrete, e.g. "mid-30s"
    gender: str
    hair: str                               # concrete, e.g. "short cropped black"
    build: str
    wardrobe: str                           # one specific outfit
    signature_prop: Optional[str] = None


@dataclass
class CastLock:
    narrator: Appearance
    characters: dict[str, Appearance] = field(default_factory=dict)   # name -> Appearance


# ---------------------------------------------------------------------------
# 4. Timeline ← AlignStage  (ChunkStage builds a preliminary text-only one)
# ---------------------------------------------------------------------------


@dataclass
class Word:
    text: str
    start_s: float
    end_s: float


@dataclass
class Segment:
    """One time-windowed unit of narrated audio (beat / section / chapter)."""

    start_s: float
    end_s: float
    text: str
    anchor_id: str                          # "beat_000" | section id | "chapter_x"
    kind: str = "beat"                      # beat | section | chapter
    words: Optional[list[Word]] = None      # word-level timings (ASR only)


@dataclass
class Timeline:
    """The aligned beat/section timing (AlignStage output)."""

    segments: list[Segment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 5. AudioResult ← AudioStage
# ---------------------------------------------------------------------------


@dataclass
class AudioResult:
    narration_path: Path
    duration_s: float
    voice_fingerprint: dict[str, Any] = field(default_factory=dict)
    chunk_timings: Optional[list[tuple[float, float]]] = None   # per-chunk (start,end) if chunked


# ---------------------------------------------------------------------------
# 6. VisualAssets  (Compositor-internal: the raw per-chunk visuals it renders, then places)
# ---------------------------------------------------------------------------


@dataclass
class VisualAsset:
    """One rendered per-chunk visual — a still (ANIMATION) or a trimmed clip (FOOTAGE),
    NOT yet placed on the timeline. The Compositor lays it on the chunk's time window."""

    index: int              # which chunk this belongs to
    path: Path              # rendered PNG (animation) or mp4 clip (footage)
    kind: VisualKind


@dataclass
class VisualAssets:
    """Compositor-internal working set: the ordered raw per-chunk visuals the Compositor
    renders (via VisualProvider) before laying them on the timeline. NOT a bus artifact —
    there is NO pre-stitched video; ComposeStage renders + composes in a single pass."""

    assets: list[VisualAsset] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 7. OverlayElement[] ← OverlayStage
# ---------------------------------------------------------------------------


@dataclass
class OverlayElement:
    """One time-windowed element composited on top of the visual track.
    Layer convention: 10 footage · 20 lower-third · 30 chapter · 40 caption · 50 watermark."""

    start_s: float
    end_s: float
    layer: int
    asset_path: Path                        # PNG (static) or mp4 (moving overlay)
    region: Optional[tuple[int, int, int, int]] = None          # (x,y,w,h); None = mux positions
    blend: str = "over"
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 8. MusicBed ← MusicStage
# ---------------------------------------------------------------------------


@dataclass
class MusicBed:
    path: Path
    duration_s: float


# ---------------------------------------------------------------------------
# 9. ComposedVideo ← ComposeStage
# ---------------------------------------------------------------------------


@dataclass
class ComposedVideo:
    mp4_path: Path
    duration_s: float
    width: int
    height: int


# ---------------------------------------------------------------------------
# 10. UploadResult ← UploadStage
# ---------------------------------------------------------------------------


@dataclass
class Metadata:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)


@dataclass
class UploadResult:
    video_uri: str                          # gs:// artifact or youtube watch URL
    youtube_id: Optional[str]
    metadata: Metadata
    thumbnail_path: Path


# ---------------------------------------------------------------------------
# 11. Critique ← CritiqueStage  (findings feed back into config/learnings)
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    axis: str                               # "cast_consistency" | "pronunciation" | ...
    severity: str                           # "info" | "warn" | "error"
    detail: str
    fix: Optional[str] = None


@dataclass
class Critique:
    verdict: str                            # "ship" | "fix" | "block"
    findings: list[Finding] = field(default_factory=list)


# ===========================================================================
# AI MODEL I/O CONTRACTS
# Every AI piece has an exact Input (assembled + sent to the model) and Output
# (returned). All fields REQUIRED — a model call never receives a half-specified
# request. Grounded in the real payloads:
#   image → images.py POST {prompt, negative_prompt, seed, width, height, steps, cfg}
#   tts   → tts/cloudrun.py POST {text, voice, speed, ...} -> wav
#   asr   → asr_cloudrun.py POST /align {wav_url, anchors, mode} -> Segment[]
# ===========================================================================


# ---- Prompt Model output (AI-authored creative text only) ----
@dataclass
class ImagePrompt:
    """PromptModel output: the creative text for one image, authored from the Scene +
    Cast + Style. ChunkStage combines it with deterministic render numerics
    (seed/size/steps/cfg/model_id) to form the ImageModelPrompt below."""

    prompt: str
    negative_prompt: str


# ---- Image Generation Model (diffusion, Z-Image-Turbo) ----
@dataclass
class ImageModelPrompt:
    """Fully-assembled diffusion input = ImagePrompt text + render numerics, assembled
    by ChunkStage. ``prompt`` already contains the cast Appearance + scene + channel
    style prefix."""

    prompt: str
    negative_prompt: str
    seed: int
    width: int
    height: int
    steps: int
    cfg: float
    model_id: str                           # e.g. "z_image_turbo"


@dataclass
class ImageResult:
    image_path: Path
    seed: int                               # echoed for cache / reproducibility


# ---- Audio Generation Model (TTS) ----
@dataclass
class AudioModelPrompt:
    """TTS input for one chunk. ``text`` is already pronunciation-normalised
    (respellings + overrides from learnings applied upstream)."""

    text: str
    voice_provider: str                     # "cloudrun_chatterbox" | "cloudrun_indicf5"
    voice_ref: Path                         # resolved reference wav
    speed: float
    atempo: float
    sample_rate: int
    # output: AudioResult


# ---- Caption / ASR-Align Model (Whisper) ----
@dataclass
class AlignRequest:
    """Forced-alignment input. ``anchors`` are the beat texts / section titles the
    transcribed words are aligned against; ``mode`` picks the grouping."""

    wav_path: Path
    anchors: list[str]
    mode: str                               # "beats" | "anchors"
    language: str
    # output: Timeline


# ---- Chunk: one executable narration unit (diagram's "Chunk") ----
@dataclass
class Chunk:
    """ChunkStage output: one narration segment ready to render. ``audio_prompt`` always
    drives TTS; ``visual`` is an ImageModelPrompt (ANIMATION — cast+scene+style baked in)
    or a ShotRef (FOOTAGE). Exactly one visual per chunk; ``kind`` is the discriminator."""

    index: int
    narration: str
    kind: VisualKind
    audio_prompt: AudioModelPrompt
    visual: ImageModelPrompt | ShotRef


# ---- Script Model (LLM) ----
@dataclass
class ScriptRequest:
    source: SourceDoc
    kind: RenderKind
    channel_style: str                      # narration style spec from channel YAML
    language: str
    target_words: int
    learnings: str
    # output: Script


# ---- Director Model input (LLM: Cast Lock + Style Lock) ----
@dataclass
class DirectorRequest:
    """Everything the DirectorModel needs to lock Cast + Style for the whole render.
    ``channel_aesthetic`` is the channel YAML's line-style + palette spec."""

    script: Script
    channel_aesthetic: str
    learnings: str
    # output: DirectorPlan


# ---- Critique Model (LLM: visual + audio) ----
@dataclass
class CritiqueRequest:
    video: ComposedVideo
    audio: AudioResult
    script: Script
    frames: list[Path]                      # sampled frames
    # output: Critique


# ---- Director Model (LLM: Cast Lock + Style Lock) ----
@dataclass
class Style:
    """Locked visual style for the whole render (the Director's Style Lock). Injected
    into every image prompt so the aesthetic stays consistent across beats."""

    aesthetic: str          # "flat 2D crayon" | "graphic-novel" | "photoreal" | ...
    line_style: str         # "bold ink outlines" | "soft painterly" | ...
    palette: str            # base colour palette
    image_style_prefix: str # exact prefix prepended to every ImageModelPrompt.prompt
    negative_base: str      # base negative prompt applied to every image


@dataclass
class DirectorPlan:
    """DirectorModel output: the global locks every beat reuses."""

    cast: CastLock
    style: Style


# ---- Scene Model input (LLM: Scene Definition AI) ----
@dataclass
class SceneRequest:
    """SceneModel input — segments the narration into beats, assigns each a VisualKind
    from ``palette``, and directs it (Scene for ANIMATION, ShotRef for FOOTAGE), using the
    locked Cast + Style so blocking/mood stay consistent."""

    script: Script
    cast: CastLock
    style: Style
    palette: tuple[VisualKind, ...]
    learnings: str
    # output: Storyboard


# ---- Prompt Model input (Chunking AI / Prompt Refiner) ----
@dataclass
class PromptRequest:
    """PromptModel input — refines one Beat's Scene into the creative image text,
    injecting the present characters' Appearances and the locked Style."""

    beat: Beat
    cast: CastLock
    style: Style
    # output: ImagePrompt
