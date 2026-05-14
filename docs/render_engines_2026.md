# Render engine architecture (post-2026-05-14 consolidation)

This doc covers the two-engine pluggable architecture introduced by
the 4-renderer-to-2-engine consolidation. It supersedes the older
"4 renderers, one per kind" model documented in `legacy_pipeline.md`.

For why this consolidation was done, see the session plan at
`.copilot/session-state/<session-id>/plan.md`.

## TL;DR

- **Two engines:** `short` (single-pass TTS, beat-driven) and `long`
  (chunked TTS, section-driven).
- **Seven plugin slots** (`audio` / `timeline` / `visualize` /
  `overlays` / `music` / `compose`, plus a registry helper).
- **Engines have ZERO `if visual_mode == ...` branches.** Every plugin
  choice comes from `RenderSpec` field → registry → `get_plugin(slot,
  name)`.
- **Channel YAMLs use `defaults: { short: {...}, long: {...} }`** —
  every spec field can be overridden per channel + per render.
- **`pipeline.render.video.render_via_engines(spec, script, work_dir,
  out_path)`** is the single public entry point.

## File layout

```
pipeline/render/
├── contracts.py        # 7 Protocols + dataclasses + plugin registry
├── spec.py             # RenderSpec + 9 nested config dataclasses
├── short_engine.py     # render_short(spec, script, work_dir, out_path)
├── long_engine.py      # render_long(...)
├── engine.py           # pick_engine(spec) → engine fn
├── video.py            # render_via_engines(...) public entry
├── input_registry.py   # form-input → spec / cfg overlay
├── artifacts.py        # GCS + Firestore artifact emission
│
├── shared/             # pure infra — ffmpeg, env, voice fingerprint
│   ├── ffmpeg_helpers.py    # run_ffmpeg, apply_atempo, probe_duration
│   ├── trim_letterbox.py    # trim_clip_letterbox
│   ├── watermark.py         # render_watermark_png
│   ├── voice_fingerprint.py # compute_fingerprint, write_sidecar
│   ├── env_loader.py        # load_dotenv_into_environ
│   └── concat_safe.py       # escape_concat_path, concat_file_line
│
├── audio/              # AudioSynthesizer plugins
│   ├── tts_single.py        # single-pass cloud TTS
│   ├── tts_chunked.py       # chunked cloud TTS (long engine)
│   └── audio_from_fixture.py # test-only
├── timeline/           # TimelineBuilder plugins
│   ├── asr_beats.py         # cloud whisper → beats (short)
│   ├── asr_anchors.py       # cloud whisper → anchored sections (long)
│   └── timeline_from_fixture.py # test-only
├── visualize/          # VisualProducer plugins
│   ├── longform_panels.py   # Flux cloud, panels per section
│   └── visuals_from_fixture.py # test-only
├── overlays/           # OverlayProducer plugins (LIST-VALUED slot)
│   ├── word_caption_pngs.py # short-style captions
│   ├── sentence_caption_ass.py # long-style ASS subtitles
│   └── noop.py              # explicit no-op
├── music/              # MusicComposer plugins
│   ├── ducked_loop.py       # short default
│   ├── single_bed.py        # long default
│   └── silent.py            # music_policy=none
└── compose/            # FinalMux plugins
    ├── beat_slideshow_mux.py # short engine final mux
    └── section_video_mux.py  # long engine final mux
```

## The seven Protocol slots

```python
# pipeline/render/contracts.py

class AudioSynthesizer(Protocol):
    def synth(self, spec, script, work_dir) -> AudioResult: ...

class TimelineBuilder(Protocol):
    def build(self, spec, script, audio) -> Timeline: ...

class VisualProducer(Protocol):
    def produce(self, spec, timeline, work_dir) -> VisualTrack: ...

class OverlayProducer(Protocol):  # LIST-VALUED slot
    def produce(self, spec, timeline, audio) -> list[OverlayElement]: ...

class MusicComposer(Protocol):
    def compose(self, spec, narration_duration_s, sections) -> Path: ...

class FinalMux(Protocol):
    def mux(self, visuals, audio, overlays, music, spec, out_path) -> Path: ...
```

`OverlayProducer` is **list-valued** — captions, lower-thirds,
chapter cards, and anchored foreground footage are all impls of this
one Protocol. Engine collects every active producer, runs each,
concatenates the lists, hands the merged
`list[OverlayElement]` to the FinalMux. Compose stacks them by
`(layer, start_s)`.

## Engine dispatch flow

```
RenderSpec (channel + niche + form overrides)
  ↓
pipeline.render.engine.pick_engine(spec) → render_short | render_long
  ↓
For each plugin slot:
  ↓
  spec field name (e.g. spec.visual_mode.value = "ai_beat_slideshow")
  ↓
  pipeline.render.contracts.get_plugin("visualize", "ai_beat_slideshow")
  ↓
  plugin.produce(spec, timeline, work_dir) → VisualTrack
```

Engines have **zero** `if visual_mode == ...` branches. Adding a new
visual_mode = add a module under `pipeline/render/visualize/`,
register it via `register_plugin("visualize", "<name>", impl)`, and
the engine picks it up automatically.

## Cloud Run services the engines depend on

| service | purpose | env var |
|---|---|---|
| `ytfactory-tts-chatterbox` | English short + long TTS | `CLOUDRUN_TTS_CHATTERBOX_URL` |
| `ytfactory-tts-indicparler` | Hindi descriptive TTS | `CLOUDRUN_TTS_INDICPARLER_URL` |
| `ytfactory-tts-indicf5` | Hindi voice-clone TTS | `CLOUDRUN_TTS_INDICF5_URL` |
| `ytfactory-asr-whisper` | Cloud whisper (timeline plugins) | `CLOUDRUN_ASR_URL` |
| `ytfactory-image-flux2-klein` | AI image gen (visualize plugins) | `CLOUDRUN_IMAGE_*_URL` |

All five follow the same Cloud Run + L4 + OTel + ADC bypass pattern
(see `docs/cloud_service_dep_playbook.md`).

Each plugin that calls a cloud service ships a **laptop fallback**
(local F5/kokoro for TTS; whisper-mlx for ASR; mflux for image) so
a Cloud Run outage never blocks a render. Set
`CLOUDRUN_*_DISABLE_FALLBACK=1` in tests to hard-error instead.

## Cutover

The cloud worker (`cloud/render-worker-v2/entrypoint.py`) ships with
the new engine path env-gated:

- `YTFACTORY_USE_ENGINES=1` → uses `_run_renderer_via_engines` →
  `pipeline.render.video.render_via_engines` → `pick_engine(spec)` →
  engine + plugins.
- Default (unset) → uses the legacy `_run_renderer_subprocess` path
  that shells out to `pipeline.render.shorts` / `long_form`.

Channel YAML migration is staged:

- `scripts/migrate_channel_yamls.py` rewrites every
  `pipeline/channels/*.yaml`, `pipeline/variants/*/*.yaml`, and
  `<channel>/config.yaml` from the legacy shape to
  `defaults: { short: {...}, long: {...} }`.
- Comment preservation is partial (ruamel keeps comments attached to
  unmoved keys; reorganisation drops comments on moved keys). The
  bigbang PR will need per-channel manual review.

## Testing

- **Per-plugin unit tests** under `tests/render/<slot>/` — each plugin
  is tested in isolation against its Protocol contract.
- **Engine integration goldens** under `tests/render/test_*_engine_golden.py`:
  - Fixture mode (CI default) — fixture audio + visuals + timeline,
    real overlays + music + compose. Runs in <10s.
  - Cloud mode (`YTFACTORY_GOLDEN_CLOUD=1`) — real cloud TTS + cloud
    whisper + Flux visualize. ~6-15min per golden, ~$1-2 per PR.
- **Tolerance assertions** under `tests/render/golden_assertions.py`:
  structural (exact match), duration (±1%), LUFS (±0.5 dB), overlay
  presence (variance > threshold). Stable across cloud model updates.

## Backlog (post-this-doc, not yet shipped)

- Real plugin impls for the remaining visual_modes:
  `ai_beat_slideshow`, `motion_clips`, `hybrid_beat_footage`,
  `footage_windows`, `archival_shotlist`, `footage_filler`.
- Real overlay impls: `lower_third`, `chapter_card`, `anchored_footage`.
- Real music impls: `section_mood`.
- Per-channel YAML migration with manual comment review.
- Bigbang PR: delete `shorts.py`, `long_form.py`, `sports_doc.py`,
  `footage_only.py` after all callers + tests + skills + docs are
  updated.
- Wizard UI surfaces the new knobs (`music_policy`, `lower_thirds`,
  `chapter_cards`, `overlay_timeline`, `critic_loop`, `tone`).
