# ytFactory — System Requirements & Coverage

**Source of truth.** Requirements derived from the HLD specs box (USER INPUT A–G)
plus channel config. Each maps to a real contract in `pipeline.core` (all verified
importing 2026-07-12).

## User-configurable (wizard → `RenderSpec`)
| # | Requirement | Contract field | Status |
|---|---|---|---|
| A | Length | `RenderSpec.duration_target_s / duration_max_s` | ✅ |
| B | Script source (reddit / wiki / youtube / twitter / text) | `niche.source` → `SourceStage` adapters | ✅ |
| C | Audio: voice | `RenderSpec.audio_mode / voice_provider / voice_id` | ✅ |
| D,G | Background music | `RenderSpec.music_policy / music_bed` | ✅ |
| E | Caption: one-word vs sentence | `CaptionStyle.layout` | ✅ |
| E | Caption: Y-position (center / lower-third / top) | `CaptionStyle.position` | ✅ |
| E | Caption: background highlight (full / word) | `CaptionStyle.highlight` | ✅ |
| E | Caption: size | `CaptionStyle.font_size` | ✅ |
| E | Caption: font | `CaptionStyle.font_family` | ✅ |
| F | Visual: AI style (2D / real-life) | `Style.aesthetic` + `VisualKind.ANIMATION` | ✅ |
| F | Visual: real footage | `VisualKind.FOOTAGE` (per-chunk, via `RenderSpec.visual_palette`) | ✅ |

## Channel-level (`Config.channel_yaml` → `RenderSpec` / `Style`)
| Requirement | Contract | Status |
|---|---|---|
| Channel info / supported formats | `Config.channel_yaml(channel)` | ✅ |
| Watermark | `RenderSpec.watermark` (`Watermark`) | ✅ |
| Video grade / LUT | `RenderSpec.video_grade` | ✅ |
| Base image style prefix | `Style.image_style_prefix` | ✅ |
| Chapter cards / lower thirds | `RenderSpec.chapter_cards / lower_thirds` | ✅ |

**All requirements now covered** after adding `CaptionStyle` / `Watermark` +
the Camera/Mood enums on 2026-07-12 (they were the gap found in the coverage
review). `video_grade` + `watermark` live on `RenderSpec`; `image_style_prefix`
on `Style` (no `ChannelStyle`).

## Non-functional requirements
- **Fail-loud** — a stage that can't produce real output raises; never a placeholder.
- **$0 idle** — GPU services `min-instances=0`.
- **Observable** — every stage emits a telemetry envelope + dumps its artifact.
- **Consistent characters** — `DirectorLLM` locks Cast + Style once; every Scene
  references the locked cast by name.
