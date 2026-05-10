# Shorts cadence calibration — premium-quality 2.66s/cut sweet spot

**Established 2026-05-08** during the cosmosdecoded 5-Shorts iteration
(pound-rebka / michelson-morley / hubble / penzias-wilson / super-K).
Reverse-engineered the right pace from 4 visible iterations:

| Version | Windows / Short | Avg cut | User feedback |
|---|---|---|---|
| v1 | 6-8 | ~7.0s | "very very less images, use more" — too sparse, viewer stares |
| v2 | 13-14 | ~3.7s | better but still felt sparse |
| v3 | 26-28 | ~1.8s | "transition is too fast" — brain can't process per-shot |
| **v4 (sweet spot)** | **18-21** | **~2.66s** | "looks good" |

## The rule

For 50-60s science-explainer Shorts on Cosmos Decoded (and any channel
with caption-heavy narration), target:

- **18-21 unique-image windows** for a 50-60s narration
- **Per-shot pacing structure:**
  - Hook (W0): **3.0s** — let viewer absorb the first image
  - Establishing shots (per beat opener): **2.7s**
  - Mid-beat support shots: **2.5s**
  - Closer (last shot): **3.5s** — final visual breathes
- **Average across the Short: ~2.66s/cut**

## Why ~2.66s

- **Caption-readability floor:** sentence-level captions need ~2s minimum
  to read; viewer's brain needs another ~0.5-0.7s to map the image to
  the spoken phrase. Anything under 2s means the image flashes by
  before recognition.
- **Engagement ceiling:** anything over ~4s on a single image with
  steady narration causes attention drop on autoplay. Especially if
  the narration is dense with new vocabulary (acronyms, names,
  numbers).
- **Quotient of caption-words / shot-duration ≈ 4-5 words per shot**
  is the comfort zone. At 148 wpm = 2.46s per 6-word phrase. So 2.5-3s
  per shot lines up with one "thought unit" of speech.

## Other channel calibrations (for reference)

This is the Cosmos-Decoded calibration. Other channels' Shorts use
similar but slightly different settings:

| Channel | Avg cut | Reason |
|---|---|---|
| Cosmos Decoded (science explainer, dense vocab) | **2.66s** | this doc |
| HistoryRecapped (war/event archival) | 3.0-3.5s | longer hold on archival footage; viewer absorbs era |
| MyStoriesAnimated (Reddit AITA) | 2.0-2.4s | beat-driven story has natural cuts; animated character pace is faster |
| SportsRecapped (broadcast-cut-ins) | 2.0-2.5s during narration, then a single 8-12s broadcast clip at the climax | the long broadcast cut is the payoff |
| HindutavaAnimated (Hindi mythology) | 3.0-3.5s | sentence-level Devanagari captions read slower than Latin |
| RhymeTimeJunction | continuous animation, no cuts | Suno-rendered sung music with continuous backgrounds |

## How to apply

1. Count narration words; estimate duration at the channel's wpm
   target.
2. `windows = round(narration_duration_s / target_avg_cut)`
3. For dense-vocab science explainer: `windows = duration / 2.66`
4. Allocate per-beat: 4-5 windows for hook (10-12s), 3-4 windows per
   middle beat, 1-2 windows for closer.
5. Hook window gets 3.0s; closer gets 3.5s; rest are 2.5-2.7s.

## When to deviate

- **Slower (3.0-3.5s/cut)** if narration is reading historical
  archival text, or showing a single iconic photo where viewer
  recognition matters (e.g. astronaut on the Moon, Einstein with
  chalkboard).
- **Faster (1.8-2.2s/cut)** at a climactic build-up beat — quick cuts
  signal urgency. But never sustain for more than 4-5 windows in a
  row; the brain needs to come up for air.

## Companion rules

- [Zero image reuse per Short](shorts_zero_image_reuse.md) — no window
  shares a `source_url` with any other window in the same Short.
- [Image relevance gate](../cosmosdecoded/learnings/image_relevance_gate.md)
  — every window's image must literally depict what the narration
  names at that beat.
- [AI-gen fallback rule](../cosmosdecoded/learnings/ai_gen_fallback_rule.md)
  — when no Wikimedia photo exists, AI-gen photoreal cosmos-style.

## Memory pointer

`feedback_shorts_cadence_calibration.md`.
