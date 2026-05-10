---
name: History Recapped long-form visual signature — comic-illustrated panels with campfire anchor
description: Long-form sleep videos must read as hand-drawn comic-style illustrated panels (bold ink linework + watercolor wash), with a literal small yellow campfire visible in nearly every frame as the warm-light anchor. Sentence-level yellow italic captions at the bottom; channel watermark top-right. Reverse-engineered from sampled frames of Sleepy Time History "Early Humans" episode (yt AHTEnwz0bL4, 564 MB mp4 at /tmp/ref_AHTEnwz0bL4/video.mp4).
type: feedback
---

The visual signature of `@SleepyTimeHistory` (148K subs, parity target) is **not** painterly oil-on-canvas, **not** archival war footage, and **not** photo-realistic. It is hand-drawn comic-style illustrated panels — bold ink linework with watercolor or flat-color fills — with a literal small yellow campfire (or analogous warm light source) visible in **nearly every frame** as the visual anchor. That is the "small yellow thing we give it good feel" the user named. Plus sentence-level yellow italic captions at the bottom, plus a small white channel watermark in the top-right corner.

**Why:** Initial doc draft (earlier today) inferred the style from the thumbnail + dossier text alone and guessed "painterly digital illustration" + cool-blue palette + no captions. After actually downloading the video and sampling 16 frames at strategic timestamps (`/tmp/ref_AHTEnwz0bL4/frames/t*.jpg`), the real style is much closer to a graphic-novel / illustrated-children's-book aesthetic. Captions ARE present and visually load-bearing. The campfire literally appears in dozens of frames across the 2h 15m runtime — it's not subtle, it's a signature design choice.

**How to apply.** This document **supersedes** the earlier visual_signature draft. The two-path approach (Path A grade / Path B illustrated stills) still applies, but Path B is now clearly the primary — Path A on archival footage cannot replicate this look. Path A should be retained only for episodes we ship before Path B is wired and as a fallback when image-gen is GPU-blocked.

### Frame anatomy (sampled 2026-05-04)

From the 16 frames at `/tmp/ref_AHTEnwz0bL4/frames/`:

| Element | What's there |
|---|---|
| **Composition** | Wide cinematic landscape (16:9 horizontal). Often a "frame within frame" — cave mouth, rock arch, tree silhouettes — adds depth. Small human figures, environment dominates. |
| **Linework** | Bold black or sepia ink outlines. Hatching for shadow. Visible brushstroke texture, not vector-clean. |
| **Color fill** | Watercolor wash or flat color (depends on style mode — see below). Earthy palette: ochre/khaki/forest green/brown/dusk-blue. |
| **Yellow anchor** | A small bright yellow campfire (or analogous: lantern, hearth, lit torch, oil lamp) visible in nearly every frame. This is the visual ASMR equivalent of a candle in a dark room. |
| **Cool ambient** | Sky / distant mountains / water always cool blue or pale grey. The warm-cool split between fire-foreground and cool-distance is the signature. |
| **Captions** | Bottom-center, ~5-10 words per cue, **yellow italic** sans-serif, with subtle drop-shadow or stroke for legibility on busy backgrounds. Sentence-level, not word-by-word. |
| **Watermark** | "SLEEPY TIME HISTORY" in faint white serif/sans, top-right corner, ~3-5% opacity reduction so it doesn't dominate. |
| **Style modes within one video** | At least two style modes are intercut — "bold-ink" (heavy black outlines, hatching, ~early chapters) and "soft-painted" (lighter outline, more atmospheric watercolor, later chapters). Both share the campfire anchor + cool-warm split. |

### Path B (primary): Z-Image-Turbo painterly illustrated stills

Replace the shotlist + footage track entirely with ~40-60 illustrated panels per episode (1 per ~2-3 min of audio), each held with slow Ken Burns + cross-fade. Z-Image-Turbo at 1344×768 (16:9 cap), upscaled to 1920×1080 in compose.

**Image style prefix (locked, replaces earlier draft):**

```
hand-drawn illustrated panel in the style of a graphic-novel history book,
bold dark ink linework with visible hatching for shadow, soft watercolor
wash fills, earthy palette of ochre khaki forest-green dusk-blue and warm
brown, a small bright yellow campfire or hearth visible at the focal point
as the warm-light anchor, cool blue ambient backdrop with distant mountains
or water, wide cinematic 16:9 landscape framing with small human figures
in period dress, atmospheric depth via foreground silhouette frame (cave
mouth or tree silhouette or rock arch), no text, no captions on image,
no modern objects, no logo
```

**Per-beat scene authoring rules:**

- The LLM author writes a `scene` string per panel describing subjects + setting + the small warm-light source. Always include the warm-light anchor explicitly — don't trust the prefix alone, the diffusion model regularly drops "campfire" if not in the per-beat scene text.
- Example: `"a small group of WW1 British soldiers gathered around a brazier in a chalk dugout at dawn, steam rising from a tin cup, the brazier's small bright fire glowing yellow at the center of the composition, faces lit warm, distant cool-blue trench parapet silhouette beyond"`
- For era-without-fire scenes (interior portrait, daylight), substitute another small yellow source: oil lamp, lit candle, lantern, hearth, sun catching on a lit window, glowing forge.
- Pin the era + period dress in the scene string — diffusion drifts to anachronistic kit otherwise (same lock pattern as the Shorts era-pinning rule).

**Ken Burns motion (per panel):**
- 18-22 s hold per painting (jarring cuts wake the viewer per `long_form_sources.md`)
- Slow zoom 100% → 108% over the hold, panning toward the warm-light anchor
- 1.5 s cross-fade between panels

**Style mode intercut.** Author the LLM to alternate between two style modes per chapter to avoid same-style fatigue across 2 hours:
- `mode: bold_ink` — heavier outlines, more hatching, higher contrast (use for active scenes — hunts, combat, work)
- `mode: soft_painted` — softer outlines, more atmospheric wash, lower contrast (use for reflective scenes — dawn, dusk, rest, conversation)

The renderer can tweak the prefix per panel to nudge Z-Image-Turbo into either mode.

### Path A (fallback): warm-cool grade on archival footage

The previously-shipped `visual_grade.filter` in `historyrecapped/config.yaml long_form:` still applies on episodes that use archival footage (see `long_form_sources.md`). It cannot replicate the comic-illustrated look — but it warms the midtones and keeps shadows cool, which is at least directionally correct. Use this for any in-flight render that can't wait for Path B image-gen time.

### Captions (REVISED — captions ARE on)

Earlier `long_form_channel.md` rule "NO captions (eyes closed)" is **partially retired**. Sleep listeners with eyes closed don't read captions, but the segment of the audience with eyes open (especially during the awake-engagement first 20-30 min) reads them — and the yellow italic style is part of the channel signature. Already enabled in `config.yaml long_form.captions_enabled: true` as of 2026-05-04.

Spec for the caption renderer (`build_caption_pngs` in `render_long_form.py`):
- Sentence-level cues (5-10 words), not word-by-word
- Yellow text (`#FFD93D` or similar warm yellow — match by sampling reference PNGs at `/tmp/ref_AHTEnwz0bL4/frames/`)
- Italic sans-serif (system font fallback chain)
- Subtle black drop-shadow + thin black stroke for legibility on busy/light backgrounds
- Bottom-center, margin ~80 px from bottom edge

### Channel watermark (NEW)

Add a faint "HISTORY RECAPPED" watermark in the top-right corner of every long-form frame. White or off-white, ~50% opacity, ~22 px sans-serif, 30-40 px margin from top + right edges. Renderer wires it via a final `overlay` filter step. Asset goes at `historyrecapped/branding/watermark_topright.png` (or generated on the fly via ffmpeg `drawtext`).

### Reference cache

All findings derive from frame samples of `https://youtu.be/AHTEnwz0bL4` cached at:
- Video: `/tmp/ref_AHTEnwz0bL4/video.mp4` (564 MB, 720p, 8135 s)
- Frames: `/tmp/ref_AHTEnwz0bL4/frames/t*.jpg` at t=5,30,90,200,400,600,1100,1800,2700,3600,4500,5400,6500,7500,8000,8100 s
- Dossier + transcript: `/tmp/ref_AHTEnwz0bL4/dossier.json`, `/tmp/ref_AHTEnwz0bL4/transcript.json`

Recreate via `/tmp/yt_dossier.py` + `yt-dlp` with `--cookies-from-browser "chrome:/tmp/yt_pw_ref_p4" --js-runtimes "node:/opt/homebrew/bin/node"` if cache is wiped.
