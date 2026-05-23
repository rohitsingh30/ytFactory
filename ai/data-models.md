# Data Models

> Last updated 2026-05-23 (post-Q&A refactor).

Every typed shape a stage emits or consumes. Field-level documentation lives in the source — this file is the map. Citations are file:line. The dataclasses below are mostly `@dataclass` Python — JSON-friendly via `asdict` and a `to_dict` for the ones written into Firestore.

---

## 1. `RenderSpec` — the universal render description

`pipeline/render/spec.py:393`. Fully describes one render. Built once at job start by `build_spec(proposal, channel_yaml_path, variant_yaml_path)` (`pipeline/render/spec.py:794`) merging four sources (precedence highest-last):

1. Inferred defaults
2. Base channel YAML (`pipeline/channels/<channel>.yaml`)
3. Variant overlay YAML (`pipeline/variants/<channel>/<niche>.yaml`)
4. Form overrides (`proposal.channel_overrides`)

`build_spec` NEVER raises — ambiguity goes into `spec.notes` (a list of strings the dashboard surfaces). Phantom niches (variant YAML missing) proceed from base + form with a note explaining the experimental path.

### Top-level fields

| Field                  | Type                                | Notes                                                                 |
|------------------------|-------------------------------------|----------------------------------------------------------------------|
| `channel`              | `str`                               | Slug — e.g. `mystoriesanimated`                                       |
| `niche`                | `str \| None`                       | Variant slug; None = base channel                                     |
| `kind`                 | `RenderKind`                        | Top-level orchestration                                               |
| `visual_mode`          | `VisualMode`                        | Per-beat visualize strategy                                           |
| `aspect_ratio`         | `str`                               | `9:16` / `16:9` / `1:1` / `4:5`                                       |
| `output_resolution`    | `tuple[int, int]`                   | (w, h) px — inferred from aspect if not set                           |
| `output_fps`           | `int = 30`                          |                                                                       |
| `duration_target_s`    | `int \| None`                       | Target final duration                                                 |
| `duration_max_s`       | `int \| None`                       | Hard cap                                                              |
| `audio_mode`           | `AudioMode = VOICE`                 |                                                                       |
| `voice_provider`       | `str \| None`                       | `cloudrun_chatterbox`, `cloudrun_indicparler`, `f5_tts`, `kokoro`, … |
| `voice_id`             | `str \| None`                       | Ref-wav path OR bare voice id                                         |
| `music_bed`            | `str \| None`                       | Bed key (no `.mp3`) or `off`                                          |
| `song_style` / `song_vocal_gender` / `song_model` | `str?`         | Suno-only fields when `audio_mode=SONG`                               |
| `captions_density`     | `CaptionsDensity = STANDARD`        | back-compat with the older user-facing knob                           |
| `captions_enabled`     | `bool = True`                       |                                                                       |
| `captions_layout`      | `CaptionsLayout = CENTER_WORD_BY_WORD` | 2026-05-14 wizard knob; engine dispatches on this                  |
| `music_policy`         | `MusicPolicy = DUCKED_LOOP`         |                                                                       |
| `lower_thirds`         | `bool = False`                      | Toggles the `overlays.lower_third` producer                           |
| `chapter_cards`        | `bool = False`                      | Toggles the `overlays.chapter_card` producer                          |
| `closer_panel`         | `bool = False`                      | (NEW, P5.1, R4) — toggles the `overlays.closer_panel` producer (LIKE+SUBSCRIBE end-card). Wired in both engines' `_collect_overlays`. |
| `overlay_timeline`     | `bool = False`                      | Activates `overlays.anchored_footage` (sports-doc shape)              |
| `critic_loop`          | `bool \| None`                      | None = respect channel default; explicit True/False overrides         |
| `tone`                 | `str \| None`                       | Cadence override; looks up `TtsConfig.tone_overrides[tone]`           |
| `narrator_visual_mode` | `str = "on_screen"`                 | `on_screen` (default) or `voice_only` (sportsrecapped + cosmosdecoded)|
| `visual_source`        | `str \| None`                       | Form override: `ai` / `footage` / `both`                              |
| `visibility`           | `str \| None`                       | `public` / `unlisted` / `private`                                     |
| `schedule_at`          | `str \| None`                       | ISO-8601 UTC                                                          |
| `extra`                | `dict[str, Any]`                    | Form-override keys with no typed home; reachable from the spec. Notable post-2026-05-23 keys: `character_description` (singular, legacy, used by every downstream consumer) AND `character_descriptions` (plural list, P4.3) — `spec_enrich._populate_character_descriptions` populates the plural list and back-fills the singular from `descriptions[0]`. |
| `notes`                | `list[str]`                         | Builder-resolution notes surfaced to the dashboard                    |
| `source_channel_yaml`  | `str \| None`                       | Absolute path the spec was built from                                 |
| `source_variant_yaml`  | `str \| None`                       | Absolute path; empty for phantom niches                               |

### Nested config dataclasses (one of each on the spec)

All defined in `pipeline/render/spec.py`. Defaults reproduce the historical `long_form.py` behaviour so a channel that overrides nothing renders the same way.

| Nested config         | Defined at                       | Key knobs                                                               |
|-----------------------|----------------------------------|--------------------------------------------------------------------------|
| `TtsConfig`           | `pipeline/render/spec.py:276`    | `speed_default=0.98`, `post_atempo_default=1.0`, `chunk_target_chars=380`, `chunk_join_silence_s=0.4`, `tone_overrides` (tifo-academic / intense-podcast / playful-spicy / serious-doc) |
| `CaptionStyleConfig`  | `pipeline/render/spec.py:297`    | `font_size_minimal=320 / standard=260 / dense=200`, `text_rgba=(255,217,61,255)` warm yellow, `italic=True`, `play_res_x=1920`, `play_res_y=1080`, `black_intro_buffer_s=0.30` |
| `MusicPolicyConfig`   | `pipeline/render/spec.py:324`    | `default_bed="ambient_low"`, `music_bed_db=-28.0`, `filler_level_db=-22.0`, `mix_default=0.35` |
| `VideoGradeConfig`    | `pipeline/render/spec.py:338`    | `blur_sigma=22.0`, `brightness=0.0`                                     |
| `WatermarkConfig`     | `pipeline/render/spec.py:345`    | `font_size=28`, `text_rgba=(255,255,255,140)`, `margin=32`, `position=TOP_RIGHT` |
| `ChapterCardConfig`   | `pipeline/render/spec.py:354`    | deep-teal slab (`bg_rgba=(10,22,38,240)`) + orange number (`(255,168,0,255)`), `duration_s=3.0` |
| `LowerThirdConfig`    | `pipeline/render/spec.py:370`    | `bg_rgba=(10,22,38,215)`, `accent_rgba=(255,168,0,255)`, `hold_min_s=2.5` |
| `FillerConfig`        | `pipeline/render/spec.py:381`    | `bg_color="0x0a1626"` for no-narration windows in overlay_timeline      |
| `NetworkConfig`       | `pipeline/render/spec.py:387`    | `timeout_s=30` for ffprobe                                              |

### Discriminators (enums)

All defined under `pipeline/render/spec.py:94-238`:

- `RenderKind` (`:94`) — `short` / `long_form` / `sports_doc` / `footage_only`
- `VisualMode` (`:116`) — `ai_beat_slideshow` / `motion_clips` / `hybrid_beat_footage` / `footage_windows` / `longform_panels` / `archival_shotlist` / `sports_overlay_timeline`
- `AudioMode` (`:158`) — `voice` / `song`
- `CaptionsDensity` (`:168`) — `minimal` / `standard` / `dense`
- `CaptionsLayout` (`:215`) — `center_word_by_word` / `bottom_one_line` / `bottom_two_line`
- `MusicPolicy` (`:174`) — `ducked_loop` / `single_bed` / `section_mood` / `none`
- `WatermarkPosition` (`:200`) — `tr` / `tl` / `br` / `bl` / `none`

### Firestore serialization

`RenderSpec.to_dict()` — `pipeline/render/spec.py:534`. Flattens nested configs, unwraps Enum → str, tuple → list. Cloud worker writes this into `jobs/<id>.render_spec` so the dashboard renders "the system interpreted your inputs as kind=short, visual_mode=ai_beat_slideshow, …".

---

## 2. Plugin data shapes (Protocol I/O)

All `@dataclass`, defined in `pipeline/render/contracts.py`.

### `AudioResult` — `pipeline/render/contracts.py:162`
Output of every `AudioSynthesizer`.
- `narration_path: Path` — the narrated wav
- `duration_s: float` — measured, matches `ffprobe`
- `voice_fingerprint: dict[str, Any]` — hashable summary of cfg fields (provider, voice_id, speed, post_atempo, chunk_target_chars, …) — used to invalidate cached narration when voice changes
- `chunk_timings: list[tuple[float, float]] | None` — per-chunk (start, end) for chunked TTS; `None` for single-pass

### `Segment` — `pipeline/render/contracts.py:198`
One time-windowed unit of narrated audio. Engines emit different `kind` values.
- `start_s: float`, `end_s: float`
- `text: str` — narrated text in this window
- `anchor_id: str` — stable opaque key. Convention: `beat_<idx>` (zero-padded 3 digits) for ASR beats, section id for authored sections, `chapter_<id>` for overlay-timeline chapters
- `kind: str = "beat"` — `beat` / `section` / `chapter` / `custom`
- `words: list[Any] | None` — word-level timings (`pipeline.beats.Word` duck-typed). Set by `asr_beats` for word-PNG caption producer; None for non-ASR timelines (sentence-level captions then fall back to one PNG per beat)

`Timeline` is a type alias for `list[Segment]` (`pipeline/render/contracts.py:258`).

### `VisualTrack` — `pipeline/render/contracts.py:261`
The always-on background video.
- `video_path: Path` — one continuous file covering `[0, audio.duration_s]`
- `duration_s: float`
- `extras: dict[str, Any]` — producer-specific (e.g. `{"images": [Path, ...]}` for the slideshow producer)

### `OverlayElement` — `pipeline/render/contracts.py:289`
One time-windowed compositing element. Captions, lower-thirds, chapter cards, anchored foreground footage are ALL OverlayElements.
- `start_s: float`, `end_s: float`
- `layer: int` — convention 10 / 20 / 30 / 40 / 50 (see API contracts doc)
- `asset_path: Path` — PNG (static) or mp4 (moving)
- `region: tuple[int,int,int,int] | None` — (x, y, w, h). `None` = FinalMux positions it
- `blend: str = "over"` — ffmpeg blend mode
- `extras: dict[str, Any]` — `{"text": "...", "lang": "en"}` etc. for sidecar SRT

### `Section` — `pipeline/render/contracts.py:336`
One authored section of a long-form script. Used by `long_engine` when the TimelineBuilder is `authored_sections` and consumed by `music.section_mood` for per-section mood crossfades.
- `id: str`, `title: str`, `start_s: float`, `end_s: float`
- `body: str` — full narrated text
- `extras: dict[str, Any]`

---

## 3. `ScriptEnvelope` — the LLM authoring output

`pipeline/llm/script_schema.py:164`. Outer envelope with shared metadata + ONE typed payload picked by `kind`.

```
ScriptEnvelope
  slug: str
  kind: str  # "short" | "long_form" | "sports_doc"
  title_options: list[str]
  source_url: str
  source: str  # "user_text" / "reddit_api" / "wikipedia_topic" / ...
  short:      ShortScript      | None
  long_form:  LongFormScript   | None
  sports_doc: SportsDocScript  | None
```

`payload()` returns the active payload or raises `ValueError`. `to_dict()` drops the inactive payloads; persisted to `scripts/<slug>.json` (Shorts) or `narrations/<slug>.json` (long-form). Inverse: `from_dict`. Legacy adapters: `to_legacy_short_dict`, `to_legacy_long_form_dict` (matches what the legacy long-form renderer reads from disk), `from_short_script` (adapts existing `pipeline.llm.rewrite.Script`).

### Payload shapes

- `ShortScript` — `pipeline/llm/script_schema.py:46`
  - `hook: str` (first ~1.5s line, curiosity gap; thumbnail/title cue)
  - `narration: str` (FULL narration including the hook; ASR + beats split it for per-beat cuts)

- `LongFormSection` — `pipeline/llm/script_schema.py:60`
  - `id: str`, `title: str`, `narration: str`
  - `target_s: float | None` (pacing hint only; audio length wins)
  - `target_words: int | None` (NEW, P3.1, `script_schema.py:82`) — allocated by the outline LLM; the per-section ±10% gate validates each section against this number.
  - `visual_brief: str | None`

- `LongFormPanel` — `pipeline/llm/script_schema.py:80`
  - `scene: str` (image-gen prompt; channel `image_style_prefix` is prepended at render time)
  - `hold_s: float = 30.0`

- `LongFormScript` — `pipeline/llm/script_schema.py:96`
  - `hook: str` (opening 30-60s)
  - `thesis: str` (one-sentence thesis; surfaced in the description, NOT narrated)
  - `sections: list[LongFormSection]`
  - `narration_flat: str | None` (optional joined narration)
  - `panels: list[LongFormPanel]` (required when `visual_mode == longform_panels`)
  - `sources: list[str]` (URLs for the "Sources" block in the description)
  - `shotlist_hints: list[dict]` (`{section_id, query, source_kind}` for `archival_shotlist` mode)

- `SportsDocScript` — `pipeline/llm/script_schema.py:141`
  - `hook: str`
  - `chapters: list[dict]` (`{id, title, narration, footage_plan, talking_head?, chapter_card?}` — free-form for now)
  - `footage_plan: list[dict]`

Authored by `pipeline.llm.rewrite.rewrite()` (Shorts) and `pipeline.llm.rewrite_long_form.rewrite_long_form()` (long-form). The long-form rewriter is a fan-out: one outline LLM call allocates per-section `target_words`, then N parallel section bodies fire (`_generate_all_section_bodies` at `pipeline/llm/rewrite_long_form.py:1017`, `_SECTION_BODY_MAX_WORKERS=5`), then aggregate → `pipeline/critic_long_form.py::validate_long_form_envelope`.

### Section-body JSON schema (post-P3.1)

The section-body LLM payload now requires a `word_count` integer field (`pipeline/llm/rewrite_long_form.py:382, :396`) — the model self-reports its word count. Per ADR-005 the validator uses the **actual** word count (`len(text.split())`) for the gate decision; the emitted `word_count` is logged as an emitted-vs-actual delta for drift telemetry, never used as the gate signal. The schema's `required` array is `["narration", "sentences", "word_count"]`.

### Cast schema (post-P4.3)

`pipeline/llm/cast.py::_CAST_SCHEMA` (ADR-009). Output shape:

```
{
  "narrator": {
    "age": "<age band — e.g. '50s' / 'late teens' / 'mid-30s'>",
    "default_emotion": "<dominant emotional tone>",
    "hair": "<length + color>",
    "build": "<short structural cue>",
    "clothing": "<palette + key pieces>",
    "signature_prop": "<optional recurring object>",
    "description": "<assembled long-form spec string, used by downstream prompts>"
  },
  "supporting_characters": [
    {
      "name": "...",
      "description": "...",
      "age": "...",
      "hair": "...",
      "build": "...",
      "clothing": "...",
      "signature_prop": "..."
    }
  ]
}
```

Beat prompts literally prepend the structured fields verbatim — no
paraphrase — so the same character renders consistently across all
panels of a render.

---

## 4. Channel YAML shape

Source of truth for per-channel rules. Located at `pipeline/channels/<channel>.yaml`. The top-level keys observed across the 6 channels (the YAMLs themselves are the source of truth — this is just the inventory):

| Key                            | Type           | Example / meaning                                           |
|--------------------------------|----------------|-------------------------------------------------------------|
| `name`                         | str            | Human-readable channel name                                 |
| `source_adapter`               | str            | `reddit_video`, `manual`, `<channel>_manual`, `sports_moments_manual` |
| `render_style`                 | str            | `footage_only` for footage-driven channels (cosmosdecoded, historyrecapped) |
| `narrator_visual_mode`         | str            | `on_screen` (default) or `voice_only`                       |
| `tts_provider`                 | str            | `cloudrun_chatterbox`, `cloudrun_indicf5`, `kokoro`, …      |
| `tts_voice`                    | str            | Ref wav path OR voice id                                    |
| `tts_speed`                    | float          | 0.90 / 1.0 / 1.02                                            |
| `tts_ref_text`                 | str            | Reference text the voice-clone TTS uses for prosody         |
| `tts_language`                 | str            | e.g. `hi` (HindutavaAnimated)                               |
| `audio_provider`               | str            | `sunoapi` (Rhyme Time Junction only)                        |
| `audio_trim_start_s` / `audio_fade_out_s` / `audio_drop_vocal_pickup` | float/bool | Song trim knobs (Rhyme Time Junction only) |
| `image_provider`               | str            | **`cloudrun_z_image_turbo`** on every channel today         |
| `image_style_prefix`           | str (multiline)| Z-Image-Turbo style block (warm hand-drawn / Amar Chitra Katha / etc.) |
| `character_description`        | str (multiline)| Fallback character spec for stories without per-story cast  |
| `opening_image_directives`     | dict           | `{required_concrete_tokens, example_tokens}` for first beat |
| `image_seed`                   | int            | Locked seed for character consistency                       |
| `image_steps`                  | int            | 9 = Z-Image-Turbo sweet spot                                |
| `image_width` / `image_height` | int            | 768 / 1344 (9:16) typical                                   |
| `image_min_mean_luminance`     | float          | Post-gen luminance gate                                     |
| `beat_target_s` / `beat_max_s` | float          | Beat-pacing window                                          |
| `duration_target_s`            | int OR [min,max] | `[50, 60]` or `[10, 20]` (mystoriesanimated)              |
| `output_resolution`            | [int, int]     | `[1080, 1920]` for Shorts                                   |
| `closer_format`                | str            | CTA text per channel                                        |
| `closer_hold_s`                | float          |                                                              |
| `script_check_strict`          | bool           | Tighten script validator on channels with structured arcs   |
| `motion_provider`              | str            | When set, the spec builder picks `VisualMode.MOTION_CLIPS` (none in production today) |
| `long_form`                    | dict           | Nested block: own `tts_provider`, `tts_voice`, `image_provider`, `aspect_ratio`, `output_resolution`, `duration_target_s`, `music_bed_default`, `captions_layout` |
| `sports_doc` / `kathaa` / `long_form_doc` / `footage_only` | dict | Kind-specific blocks; spec builder reads from the right block by `RenderKind` (`pipeline/render/spec.py:905-922`) |
| `upload`                       | dict           | `{account, title_template, description_template, tags, category_id, default_privacy, made_for_kids}` — passes through to Stage 8 |

Per-channel rules (e.g. "Cosmos Decoded is footage-only, voice-only, Chatterbox TTS, Z-Image-Turbo unused") live in the YAML — don't restate. The spec builder applies them through the precedence chain in `build_spec` (`pipeline/render/spec.py:794`). Variant overlays live at `pipeline/variants/<channel>/<variant>.yaml` and override matching keys; the form's `proposal.channel_overrides` overrides both.
