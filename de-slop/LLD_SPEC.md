# LLD Spec — Stage functions (authoritative, frozen)

The complete function-level design each parallel agent implements. Grounded in the
current pipeline's real responsibilities, decomposed clean (SRP, ≤~400 LoC/file).

## Core abstractions (I scaffold these first)

```python
# pipeline/core/context.py
@dataclass
class Context:
    spec: RenderSpec
    artifacts: dict[str, Any]                 # kind -> artifact
    telemetry: Telemetry                      # injected, not global
    storage: Storage                          # GCS/local abstraction
    config: Config                            # all constants/paths/URLs live here
    def get(self, kind: str) -> Any: ...
    def put(self, kind: str, artifact: Any) -> None: ...

# pipeline/core/stage.py
class Stage(Protocol):
    name: str
    def run(self, ctx: Context) -> None: ...  # read inputs from ctx, write output to ctx

# pipeline/core/orchestrator.py
class Orchestrator:
    def run(self, stages: list[Stage], ctx: Context) -> RenderResult:
        for stage in stages:
            with ctx.telemetry.stage(stage.name):   # envelope: start/end/failed + artifact dump
                stage.run(ctx)
        return RenderResult(...)
```

**Contracts (typed artifacts, `pipeline/core/contracts.py`):**
`SourceDoc · Script · CastLock · Chunk · Timeline(Segment[]) · AudioResult · VisualTrack · OverlayElement · MusicBed · ComposedVideo · UploadResult · Critique`

---

## Per-stage function spec

### 1. `sources/` — SourceStage → `SourceDoc`
```python
class SourceStage(Stage):
    def run(ctx): doc = self._route(ctx.spec).fetch(ctx.spec); ctx.put("source", doc)
    def _route(spec) -> SourceAdapter    # by niche.source.kind

class SourceAdapter(Protocol): def fetch(spec) -> SourceDoc
# impls: RedditAdapter, WikipediaAdapter, YoutubeAdapter, TwitterAdapter, LLMIdeationAdapter
#   RedditAdapter._pick_backend()  # anon(laptop) / pullpush(cloud); no oauth stub
#   *.fetch() -> SourceDoc; *_fallback(err) on 403
```
Files: `source_stage.py`, `adapters/{reddit,wikipedia,youtube,twitter,llm_ideation}.py`, `base.py`

### 2. `authoring/` — ScriptStage → `Script`, CastStage → `CastLock`
```python
class ScriptStage(Stage):
    def run(ctx): ctx.put("script", self._author(ctx.get("source"), ctx.spec, ctx.get("learnings")))
    def _build_prompt(source, spec, learnings) -> str
    def _validate(script) -> list[Failure]        # CTA/length/banned-phrase gates
    def _regen(prompt, failures) -> str           # focused-diff retry (cap N)
    def _parse(raw) -> Script

class CastStage(Stage):
    def run(ctx): ctx.put("cast", self._lock(ctx.get("script"), ctx.spec))
    def _extract_characters(script) -> list[str]
    def _build_cast_prompt(chars, spec) -> str
    def _validate / _parse -> CastLock            # char_id -> Appearance{age,hair,wardrobe,palette}
```
Files: `script_stage.py`, `cast_stage.py`, `prompts.py`, `validators.py`, `llm_client.py`

### 3. `render/chunk` — ChunkStage → `list[Chunk]`
```python
class ChunkStage(Stage):
    def run(ctx): ctx.put("chunks", self._split(ctx.get("script"), ctx.spec))
    def _split_beats(script) -> list[Chunk]       # short
    def _split_sections(script) -> list[Chunk]    # long
    def _seed_image_prompt(chunk, cast) -> str    # per-chunk prompt assembly
```

### 4. `images/` — ImageStage → `VisualTrack`
```python
class ImageStage(Stage):
    def run(ctx): ctx.put("visuals", self._produce(ctx.get("chunks"), ctx.get("cast"), ctx.spec))
    def _refine_prompts(chunks, cast) -> list[Prompt]   # was prompt_refiner; raises on whole-batch fail
    def _quality_gate(img) -> bool                       # reject black/blank/garbled(OCR)
    def _assemble_track(images, timeline) -> VisualTrack

class ImageModel(Protocol): def generate(prompt, seed, w, h) -> Path
# impls: ZImageTurbo (cloudrun); model URL from ctx.config, NOT hardcoded
```
Files: `image_stage.py`, `prompt_refiner.py`, `quality_gate.py`, `models/z_image_turbo.py`, `cache.py`

### 5. `audio/` — AudioStage → `AudioResult`
```python
class AudioStage(Stage):
    def run(ctx): ctx.put("audio", self._synth(ctx.get("script"), ctx.spec))
    def _normalize_text(text, pronunciation_dict) -> str   # respellings/overrides (learnings feed here)
    def _chunk_text(text) -> list[str]
    def _stitch(wavs) -> AudioResult

class TTSVoice(Protocol): def synth(text, ref) -> Path
# impls: Chatterbox(en), IndicF5(hi); service URL from ctx.config
```
Files: `audio_stage.py`, `text_normalize.py`, `voices/{chatterbox,indicf5}.py`, `voice_catalog.py`

### 6. `audio/align` — AlignStage → `Timeline`
```python
class AlignStage(Stage):
    def run(ctx): ctx.put("timeline", self._align(ctx.get("audio"), ctx.get("script")))
    def _transcribe(wav) -> list[Word]            # whisper (cloud, laptop fallback)
    def _align_to_script(words, script) -> list[Segment]
    def _group_into_beats(segments) -> Timeline
```

### 7. `render/overlays` — OverlayStage → `list[OverlayElement]`
```python
class OverlayStage(Stage):
    def run(ctx): ctx.put("overlays", self._collect(ctx.get("timeline"), ctx.get("audio"), ctx.spec))
class OverlayProducer(Protocol): def produce(timeline, audio, spec) -> list[OverlayElement]
# impls: WordCaptions, SentenceCaptions, LowerThird, ChapterCard, CloserPanel  (layer convention)
```

### 8. `render/music` — MusicStage → `MusicBed`
```python
class MusicStage(Stage):
    def run(ctx): ctx.put("music", self._compose(ctx.spec, ctx.get("audio").duration_s))
class MusicComposer(Protocol): def compose(spec, duration_s, sections) -> Path
# impls: DuckedLoop, SingleBed, SectionMood, Silent
```

### 9. `render/compose` — ComposeStage → `ComposedVideo`
```python
class ComposeStage(Stage):
    def run(ctx): ctx.put("video", self._mux(ctx.get("visuals"), ctx.get("audio"),
                                             ctx.get("overlays"), ctx.get("music"), ctx.spec))
    def _build_filtergraph(...) -> list[str]      # split from the 1.6k god-module
    def _loudnorm(audio) / _letterbox(...) / _stack_overlays(...)
    def _run_ffmpeg(args) -> Path                 # single ffmpeg call site + telemetry
```
Files: `compose_stage.py`, `filtergraph.py`, `ffmpeg.py`, `mux/{beat_slideshow,section_video}.py`

### 10. `upload/` — UploadStage → `UploadResult`
```python
class UploadStage(Stage):
    def run(ctx): ctx.put("upload", self._publish(ctx.get("video"), ctx.get("script"), ctx.spec))
    def _generate_thumbnail(mp4) -> Path
    def _derive_metadata(script, spec) -> Metadata    # title/desc/tags/hashtags
    def _upload(mp4, meta) -> UploadResult            # API path + playwright fallback
```
Files: `upload_stage.py`, `thumbnail.py`, `metadata.py`, `youtube/{api,playwright}.py`

### 11. `critique/` — CritiqueStage → `Critique`
```python
class CritiqueStage(Stage):
    def run(ctx): ctx.put("critique", self._review(ctx.get("video"), ctx.artifacts))
    def _sample_frames(mp4) -> list[Path]
    def _critique_visual(frames, script) -> list[Finding]
    def _critique_audio(wav, script) -> list[Finding]     # mispronunciations -> learnings
    def _write_learnings(findings) -> None                # feeds SourceStage/ScriptStage/AudioStage next run
```

---

## Cross-cutting (`infra/`, `config/`) — injected via Context, never import-time globals
- `infra/telemetry` — the `ctx.telemetry.stage()` envelope + event emit.
- `infra/storage` — GCS/local read/write (the only place paths are built).
- `config/` — `Config` dataclass: all service URLs, voice_refs root, model params,
  channel/variant YAML loaders. **Zero hardcoded paths/URLs anywhere else.**

## File-size rule
No module > ~400 LoC. If porting a stage exceeds it, split by the functions above
(e.g. compose → stage + filtergraph + ffmpeg + mux/*).
