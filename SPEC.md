# Video Spec — declarative format for a single Short

A Short is fully described by a single YAML file ("video spec"). The
renderer is a generic interpreter — no hardcoded "AITA cooking" logic,
just primitives + templates + a layout pass. New visual concepts (a
score chip, a progress bar, a niche-specific badge) are added by
**writing more spec**, not by editing renderer code.

The spec lives at `data/intermediate/<channel>/specs/<slug>.yaml`.
Channel-level defaults live in `channels/<name>.yaml`; the spec
**overrides** the channel defaults per-Short.

---

## Top-level shape

```yaml
version: "2"
slug: amitheasshole-aita-...

output:
  resolution: [1080, 1920]
  tail_pad_s: 0.6

audio:
  narration:
    text: "..."
    tts: { provider: kokoro, voice: af_heart, speed: 1.1 }

background:
  type: per_beat_loops
  source_dir: assets/cooking_loops
  fit: scale_crop
  cycle: round_robin

beats:
  source: auto_from_narration
  asr_provider: whisper_mlx
  target_s: 1.6
  max_s: 2.6

# Overlay templates: recipes built from primitives. The renderer ships
# only primitives + a few built-ins (emoji_pop, per_beat_caption); every
# composite visual element (reddit card, vote panel, score chip…) is a
# template. Templates can be declared here, in the channel YAML, or
# imported from a shared library.
templates:
  reddit_card: { ... }       # shown in full below
  two_button_panel: { ... }

overlays:
  - { id: reddit_card,  template: reddit_card,       ... }
  - { id: closer_panel, template: two_button_panel,  ... }
  - { id: thumb_emoji,  type: emoji_pop,             ... }
  - { id: captions,     type: per_beat_caption,      ... }
```

---

## Time tokens

Anywhere the spec asks for a time, the value is one of:

| Form | Meaning |
|---|---|
| `<number>` | absolute seconds from the start of the Short |
| `start` | `0.0` |
| `end` | total duration (= narration_duration + `tail_pad_s`) |
| `closer_start` | `beat[-1].start` (when the closing AITA-question begins) |
| `closer_end` | `beat[-1].end` |
| `beat[<i>].start` / `beat[<i>].end` | per-beat references; `i` can be negative |
| `<expr> + <number>` / `<expr> - <number>` | arithmetic on any of the above |

`<number>` and `<expr>` may be parenthesised. Examples:

```yaml
visible: { from: 0,                  to: closer_start }
visible: { from: closer_start - 0.3, to: end }
visible: { from: beat[2].start,      to: beat[5].end }
visible: { at: closer_start, for: 0.3 }   # sugar for { from: closer_start, to: closer_start + 0.3 }
```

---

## Position model — auto-layout

Positions resolve in two passes:

1. **Measure pass** — every overlay reports its bounding box. Templates
   with `size.h: auto` compute height from their laid-out content.
2. **Place pass** — positions are resolved against the now-known boxes.

A `position:` block accepts:

```yaml
position: { x: <x_token>, y: <y_token> }                 # explicit
position: { region: <region_token> }                     # named region
position: { anchor: <overlay_id>.<corner>, offset: [dx, dy] }  # relative
```

### x tokens
| Token | Meaning |
|---|---|
| `<number>` | absolute pixel from left |
| `center` | `(W − element_w) / 2` |
| `left: <number>` | absolute pixel from left (alias of number) |
| `right: <number>` | `W − element_w − <number>` |

### y tokens
| Token | Meaning |
|---|---|
| `<number>` | absolute pixel from top |
| `top: <number>` | alias of number |
| `y_from_bottom: <number>` | `H − element_h − <number>` |

### regions (sugar)
| Token | Meaning |
|---|---|
| `top_third` | centred horizontally, `y` ≈ `H * 0.10` |
| `middle` | centred both axes |
| `lower_third` | centred horizontally, `y_from_bottom` ≈ `H * 0.18` |

### anchors
Refer to another overlay's resolved box by `<overlay_id>.<corner>`. Corners:

```
top_left   top_center   top_right
left       center       right
bottom_left bottom_center bottom_right
```

For composite templates (e.g. `two_button_panel`), the template can
expose **named anchors** beyond the corners:

```yaml
position:
  anchor: closer_panel.button_0.top_right
  offset: [-30, -30]
```

The renderer resolves the chain (`closer_panel` is the overlay,
`button_0` is a named anchor inside its template, `top_right` is the
corner of that anchor's box). If the chain is unresolvable, the
renderer errors with the unresolved name.

---

## Primitives

The renderer ships these (and only these) drawing primitives. Every
template composes from them.

| Kind | Args |
|---|---|
| `rounded_rect` | `x, y, w, h, fill, stroke, stroke_w, radius` |
| `ellipse` | `x, y, w, h, fill, stroke, stroke_w` |
| `text` | `x, y, text, size, color, anchor (lt/mm/…), bold, font_family` |
| `text_block` | `x, y, w, text, size, color, line_height, wrap` (auto-height) |
| `image` | `x, y, w, h, src` (file path or url) |
| `emoji` | `x, y, size, char, embedded_color` |

Coordinates inside a template are local to the template's box. The
template's outer position (set by the overlay that instantiates it)
maps the template's `(0, 0)` to its placement on the canvas.

### Template structure

```yaml
templates:
  <name>:
    params: [<param-list>]
    size: { w: <expr>, h: auto | <expr> }
    anchors:               # optional named anchors exposed to overlays
      button_0: { x: pad,            y: 36, w: pill_w,    h: pill_h }
      button_1: { x: pad+pill_w+pad, y: 36, w: pill_w,    h: pill_h }
    layout:
      - { kind: <primitive>, ... }
      - kind: each            # loop primitive
        of: ${<param>}
        as: <var>
        index: <var>
        do: [ <primitive>, ... ]
```

Inside a template:

- `${param}` — substitute a parameter passed via `args`.
- Simple expressions in args/coords: `+ - * /`, parens, params, and a
  small set of helpers: `pad`, `pill_w`, `pill_h`, `pill_x(i)`,
  `pill_cx(i)` (resolved from the template's `size` and a fixed
  layout grammar). Anything else is a literal.

The "expression" sublanguage is intentionally tiny — enough to lay
out grids, not a full DSL.

---

## Built-in overlay types

A few overlays have intrinsic logic the primitive system can't express
cleanly. These are built-in.

### `per_beat_caption`

Renders narration text per-beat, swapping at beat boundaries.

```yaml
- id: captions
  type: per_beat_caption
  visible: { from: start, to: end }
  position: { region: lower_third }
  canvas: [1000, 240]
  style:
    font_size: 72
    color: "#fff050"
    stroke: { color: "#000", w: 6 }
    bg: { color: "#000", alpha: 140 }
  extend_to_next_beat: true
```

### `emoji_pop`

A single emoji rendered with Apple Color Emoji and animated in.

```yaml
- id: thumb_emoji
  type: emoji_pop
  visible: { from: closer_start, to: end }
  emoji: "👍"
  size: 220
  position: { anchor: closer_panel.button_0.top_right, offset: [-30, -30] }
  animation: { kind: scale_overshoot, duration_s: 0.25, overshoot: 1.25 }
```

Supported `animation.kind`: `scale_overshoot`, `fade_in`, `none`.

---

## Background types

| Type | Behaviour |
|---|---|
| `per_beat_loops` | one mp4 per beat from `source_dir`, scaled+cropped per `fit`, concat'd to the narration's length. |
| `single_loop` | one mp4 looped to fill the narration. |
| `per_beat_image` | hand-off to the existing `make_shorts.py` slideshow path (one image per beat). |

`fit`: `scale_crop` (centre-crop to fill) | `pan_scan` (slow pan
across original frame for content preservation).

---

## Channel YAML vs spec

`channels/<name>.yaml` holds **defaults** for that channel: voice, beat
shape, default loops dir, default templates. The spec **overrides** any
of those per-Short. Templates declared in the channel apply unless the
spec redeclares them.

Resolution order: spec > channel > built-ins.
