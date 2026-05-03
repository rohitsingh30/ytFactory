"""Render a Short from a v2 video spec (see SPEC.md).

The spec is the single source of truth for what's on screen. This file
is a generic interpreter — no AITA, no cooking, no Reddit hardcoding.

Run:

    .venv/bin/python render_from_spec.py --spec data/intermediate/<channel>/specs/<slug>.yaml

The spec drives:
  - audio (TTS narration)
  - background (per-beat loops, single loop, etc.)
  - beats (auto-from-narration via Whisper or manual)
  - overlays (templates composed from primitives + built-ins
    per_beat_caption / emoji_pop)

Channel YAML (channels/<name>.yaml) layers in **before** the spec when
loaded with --channel; spec keys override channel keys (deep merge).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageDraw, ImageFont

from pipeline import align, audio as audio_mod, beats as beats_mod
from pipeline import captions as captions_mod


# ============ small helpers ============


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_w: int) -> list[str]:
    words = text.split()
    lines, cur = [], []
    for w in words:
        trial = " ".join(cur + [w])
        if font.getbbox(trial)[2] - font.getbbox(trial)[0] <= max_w:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def _parse_color(s: Any) -> tuple[int, int, int, int] | None:
    """Accept '#rrggbb', '#rrggbbaa', tuple, or None."""
    if s is None:
        return None
    if isinstance(s, (list, tuple)):
        t = tuple(int(x) for x in s)
        return t if len(t) == 4 else (*t, 255)
    if not isinstance(s, str):
        return None
    s = s.strip()
    if s.startswith("#"):
        h = s.lstrip("#")
        if len(h) == 3:
            # CSS short form: #rgb → each digit doubled
            return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16), 255)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        if len(h) == 8:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
    return None


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True,
    )
    return float(out.strip())


def _make_bg_segment(src: Path, duration: float, dest: Path, w: int = 1080, h: int = 1920) -> Path:
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", "0", "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        str(dest),
    ]
    subprocess.check_call(cmd)
    return dest


def _concat_segments(segments: list[Path], dest: Path) -> Path:
    list_file = dest.with_suffix(".list.txt")
    list_file.write_text("\n".join(f"file '{p.absolute()}'" for p in segments))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(dest),
    ]
    subprocess.check_call(cmd)
    return dest


def render_emoji_png(emoji: str, out_path: Path, size: int = 220) -> Path:
    canonical = 160
    img = Image.new("RGBA", (canonical, canonical), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", canonical)
    draw.text((0, 0), emoji, font=font, embedded_color=True)
    if size != canonical:
        img = img.resize((size, size), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


# ============ expression evaluation ============

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def _eval_ast(node, ns: dict) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        raise KeyError(f"unknown name: {node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_ast(node.left, ns), _eval_ast(node.right, ns))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_ast(node.operand, ns)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = ns[node.func.id]
        return fn(*[_eval_ast(a, ns) for a in node.args])
    if isinstance(node, ast.Attribute):
        v = _eval_ast(node.value, ns)
        return v[node.attr] if isinstance(v, dict) else getattr(v, node.attr)
    if isinstance(node, ast.Subscript):
        v = _eval_ast(node.value, ns)
        idx = _eval_ast(node.slice, ns)
        return v[idx]
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


def _lookup_path(path: str, ns: dict) -> Any:
    parts = path.split(".")
    v: Any = ns
    for p in parts:
        if isinstance(v, dict):
            v = v[p]
        elif isinstance(v, (list, tuple)) and p.lstrip("-").isdigit():
            v = v[int(p)]
        else:
            v = getattr(v, p)
    return v


def resolve_value(v: Any, ns: dict) -> Any:
    """Resolve a spec value against a namespace.

    - dict / list → recurse
    - non-string scalar → return as-is
    - string `"${path}"` (pure substitution) → return the raw looked-up value
      (preserves dicts/lists; critical for `each: of: ${buttons}`)
    - string with embedded ${name} → text-substitute, then try expression eval
    - bare expression (`pad + pill_w + pad`, `pill_x(i)`) → eval against ns
    - falls back to the literal string
    """
    if isinstance(v, dict):
        return {k: resolve_value(vv, ns) for k, vv in v.items()}
    if isinstance(v, list):
        return [resolve_value(x, ns) for x in v]
    if not isinstance(v, str):
        return v

    s = v.strip()

    # Pure substitution: the whole string is a single ${path} → return raw value.
    m = re.fullmatch(r"\$\{([\w.\-]+)\}", s)
    if m:
        return _lookup_path(m.group(1), ns)

    # Embedded substitutions: text-interpolate (always stringified).
    if "${" in s:
        s = re.sub(r"\$\{([\w.\-]+)\}", lambda m: str(_lookup_path(m.group(1), ns)), s)

    # Hex color / other #-leading literal — pass through.
    if s.startswith("#"):
        return s

    # Try expression eval (covers numbers, names like `pad`, arith, function calls).
    try:
        tree = ast.parse(s, mode="eval")
        return _eval_ast(tree.body, ns)
    except Exception:
        return s


# ============ time tokens & visibility ============


def resolve_time(v: Any, ctx: dict) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        raise ValueError(f"bad time value: {v!r}")
    s = v.strip()

    # Replace beat[<i>].start / beat[<i>].end before arith eval
    def _beat_sub(m: re.Match) -> str:
        i = int(m.group(1))
        field = m.group(2)
        b = ctx["beat"][i]
        return str(getattr(b, field))

    s = re.sub(r"beat\[(-?\d+)\]\.(start|end)", _beat_sub, s)

    ns = {
        "start": ctx.get("start", 0.0),
        "end": ctx["end"],
        "closer_start": ctx["closer_start"],
        "closer_end": ctx["closer_end"],
    }
    try:
        if s in ns:
            return float(ns[s])
        return float(_eval_ast(ast.parse(s, mode="eval").body, ns))
    except Exception as e:
        raise ValueError(f"can't resolve time {v!r}: {e}")


def resolve_visibility(vis: dict | None, ctx: dict) -> tuple[float, float]:
    if not vis:
        return 0.0, ctx["end"]
    if "at" in vis:
        a = resolve_time(vis["at"], ctx)
        d = resolve_time(vis.get("for", 1.0), ctx)
        return a, a + d
    f = resolve_time(vis.get("from", 0), ctx)
    t = resolve_time(vis.get("to", ctx["end"]), ctx)
    return f, t


# ============ primitive draw ============


def measure_primitive(p: dict, ns: dict) -> dict:
    """Return resolved bounding box {x, y, w, h} for a primitive."""
    p = resolve_value(p, ns)
    kind = p["kind"]

    if kind in ("rounded_rect", "ellipse"):
        if p.get("region") == "full":
            return {"x": 0, "y": 0, "w": ns["parent_w"], "h": ns.get("parent_h", 0)}
        return {"x": p.get("x", 0), "y": p.get("y", 0), "w": p["w"], "h": p["h"]}

    if kind == "text":
        font = _font(int(p["size"]), bool(p.get("bold", False)))
        bbox = font.getbbox(p["text"])
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        anchor = p.get("anchor", "lt")
        x, y = p.get("x", 0), p.get("y", 0)
        if anchor == "mm":
            return {"x": x - w / 2, "y": y - h / 2, "w": w, "h": h}
        return {"x": x, "y": y, "w": w, "h": h}

    if kind == "text_block":
        font = _font(int(p["size"]), bool(p.get("bold", False)))
        lines = _wrap_text(p["text"], font, int(p["w"]))
        line_h = int(p.get("line_height", int(p["size"]) + 8))
        return {"x": p.get("x", 0), "y": p.get("y", 0), "w": p["w"], "h": len(lines) * line_h}

    if kind == "emoji":
        s = int(p["size"])
        return {"x": p.get("x", 0), "y": p.get("y", 0), "w": s, "h": s}

    if kind == "image":
        return {"x": p.get("x", 0), "y": p.get("y", 0), "w": p["w"], "h": p["h"]}

    raise ValueError(f"unknown primitive kind: {kind}")


def draw_primitive(draw: ImageDraw.ImageDraw, p_resolved: dict, ns: dict) -> None:
    kind = p_resolved["kind"]

    if kind == "rounded_rect":
        if p_resolved.get("region") == "full":
            x, y = 0, 0
            w, h = ns["parent_w"], ns["parent_h"]
        else:
            x, y = p_resolved["x"], p_resolved["y"]
            w, h = p_resolved["w"], p_resolved["h"]
        fill = _parse_color(p_resolved.get("fill"))
        stroke = _parse_color(p_resolved.get("stroke"))
        sw = int(p_resolved.get("stroke_w", 0))
        radius = int(p_resolved.get("radius", 0))
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                               fill=fill, outline=stroke, width=sw)
        return

    if kind == "ellipse":
        x, y, w, h = p_resolved["x"], p_resolved["y"], p_resolved["w"], p_resolved["h"]
        fill = _parse_color(p_resolved.get("fill"))
        stroke = _parse_color(p_resolved.get("stroke"))
        draw.ellipse([x, y, x + w, y + h], fill=fill, outline=stroke)
        return

    if kind == "text":
        font = _font(int(p_resolved["size"]), bool(p_resolved.get("bold", False)))
        x, y = p_resolved["x"], p_resolved["y"]
        anchor = p_resolved.get("anchor", "lt")
        color = _parse_color(p_resolved.get("color", "#000000"))
        draw.text((x, y), p_resolved["text"], font=font, fill=color, anchor=anchor)
        return

    if kind == "text_block":
        font = _font(int(p_resolved["size"]), bool(p_resolved.get("bold", False)))
        lines = _wrap_text(p_resolved["text"], font, int(p_resolved["w"]))
        line_h = int(p_resolved.get("line_height", int(p_resolved["size"]) + 8))
        x, y = p_resolved["x"], p_resolved["y"]
        color = _parse_color(p_resolved.get("color", "#000000"))
        for i, line in enumerate(lines):
            draw.text((x, y + i * line_h), line, font=font, fill=color)
        return

    if kind == "emoji":
        # Emoji as a primitive inside a template. Bake the emoji glyph
        # to a temp PNG and paste. Used inside templates; the standalone
        # animated emoji is the built-in `emoji_pop` overlay type.
        # (Not used by current spec but here for completeness.)
        return

    raise ValueError(f"can't draw primitive: {kind}")


# ============ template expansion ============


def _template_helpers(width: int) -> dict:
    """Layout helpers a template can reference by name.

    These are intentionally a small fixed set in v1. Templates that
    need richer math should compute it inline as expressions.
    """
    pad = 36
    pill_h = 170
    pill_w = (width - 3 * pad) / 2
    return {
        "pad": pad,
        "pill_h": pill_h,
        "pill_w": pill_w,
        "pill_x": lambda i: pad + i * (pill_w + pad),
        "pill_cx": lambda i: pad + i * (pill_w + pad) + pill_w / 2,
    }


def expand_template(template_name: str, args: dict, templates: dict) -> dict:
    """Expand a template definition into:
        { width, height, primitives (resolved), anchors }
    """
    if template_name not in templates:
        raise ValueError(f"unknown template: {template_name}")
    tpl = templates[template_name]

    ns: dict = dict(args)

    size_spec = tpl.get("size", {})
    width = resolve_value(size_spec.get("w", 0), ns)
    if not isinstance(width, (int, float)):
        raise ValueError(f"template {template_name}: size.w must resolve to a number")
    width = int(width)

    helpers = _template_helpers(width)
    ns.update(helpers)
    ns["parent_w"] = width

    # Expand `each` loops; resolve every primitive's args to concrete values.
    expanded: list[dict] = []
    for item in tpl["layout"]:
        if isinstance(item, dict) and item.get("kind") == "each":
            of = resolve_value(item["of"], ns)
            as_var = item.get("as", "item")
            ix_var = item.get("index", "i")
            for i, val in enumerate(of):
                local = {**ns, as_var: val, ix_var: i}
                for p in item["do"]:
                    expanded.append(resolve_value(p, local))
        else:
            expanded.append(resolve_value(item, ns))

    # Compute parent_h: max(y + h) over primitives that DON'T need parent_h.
    h_spec = size_spec.get("h", "auto")
    if h_spec == "auto" or h_spec is None:
        max_y = 0.0
        for p in expanded:
            if p.get("region") == "full":
                continue
            box = measure_primitive(p, ns)
            max_y = max(max_y, box["y"] + box["h"])
        height = int(max_y + 30)
    else:
        height = int(resolve_value(h_spec, ns))
    ns["parent_h"] = height

    # Resolve named anchors NOW that ns is complete.
    anchors_resolved: dict[str, dict] = {}
    for name, a_spec in (tpl.get("anchors") or {}).items():
        a = resolve_value(a_spec, ns)
        ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
        anchors_resolved[name] = {
            "x": ax, "y": ay, "w": aw, "h": ah,
            "top_left": (ax, ay),
            "top_right": (ax + aw, ay),
            "top_center": (ax + aw / 2, ay),
            "left": (ax, ay + ah / 2),
            "right": (ax + aw, ay + ah / 2),
            "center": (ax + aw / 2, ay + ah / 2),
            "bottom_left": (ax, ay + ah),
            "bottom_right": (ax + aw, ay + ah),
            "bottom_center": (ax + aw / 2, ay + ah),
        }

    return {
        "width": width,
        "height": height,
        "primitives": expanded,
        "anchors": anchors_resolved,
        "ns": ns,
    }


def render_template_to_png(expanded: dict, out_path: Path) -> Path:
    img = Image.new("RGBA", (int(expanded["width"]), int(expanded["height"])), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for p in expanded["primitives"]:
        draw_primitive(draw, p, expanded["ns"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


# ============ position layout ============


def _corner_xy(target: dict, corner: str) -> tuple[float, float]:
    x, y, w, h = target["x"], target["y"], target["w"], target["h"]
    return {
        "top_left": (x, y),
        "top_right": (x + w, y),
        "top_center": (x + w / 2, y),
        "left": (x, y + h / 2),
        "right": (x + w, y + h / 2),
        "center": (x + w / 2, y + h / 2),
        "bottom_left": (x, y + h),
        "bottom_right": (x + w, y + h),
        "bottom_center": (x + w / 2, y + h),
    }[corner]


def resolve_position(
    pos: dict, elem_w: int, elem_h: int,
    canvas_w: int, canvas_h: int,
    registry: dict,
) -> tuple[int, int]:
    """Resolve top-left placement of an element on the canvas."""

    if "anchor" in pos:
        path = pos["anchor"].split(".")
        target_id = path[0]
        if target_id not in registry:
            raise ValueError(f"anchor target not yet placed: {target_id}")
        t = registry[target_id]

        if len(path) == 3:
            # overlay.named_anchor.corner
            named, corner = path[1], path[2]
            if named not in t["anchors"]:
                raise ValueError(
                    f"overlay {target_id} has no named anchor {named!r}; "
                    f"available: {list(t['anchors'])}"
                )
            local = t["anchors"][named][corner]
            cx = t["x"] + local[0]
            cy = t["y"] + local[1]
        elif len(path) == 2:
            corner = path[1]
            cx, cy = _corner_xy(t, corner)
        else:
            raise ValueError(f"bad anchor path: {pos['anchor']}")

        dx, dy = pos.get("offset", [0, 0])
        # Anchor places the element's CENTER at (cx+dx, cy+dy). This is
        # the natural reading for "peel the emoji off the button corner".
        return int(cx + dx - elem_w / 2), int(cy + dy - elem_h / 2)

    if "region" in pos:
        r = pos["region"]
        if r == "middle":
            return (canvas_w - elem_w) // 2, (canvas_h - elem_h) // 2
        if r == "top_third":
            return (canvas_w - elem_w) // 2, int(canvas_h * 0.10)
        if r == "lower_third":
            return (canvas_w - elem_w) // 2, canvas_h - elem_h - int(canvas_h * 0.18)
        raise ValueError(f"unknown region: {r}")

    x = pos.get("x", 0)
    y = pos.get("y", 0)

    if x == "center":
        x = (canvas_w - elem_w) // 2
    elif isinstance(x, dict) and "right" in x:
        x = canvas_w - elem_w - x["right"]
    elif isinstance(x, dict) and "left" in x:
        x = x["left"]

    if "y_from_bottom" in pos:
        y = canvas_h - elem_h - pos["y_from_bottom"]

    return int(x), int(y)


# ============ background ============


def build_background(
    cfg: dict, beat_list: list, total_dur: float,
    cache: Path, out_w: int, out_h: int,
) -> Path:
    bg_type = cfg.get("type", "per_beat_loops")

    if bg_type == "per_beat_loops":
        loops_dir = Path(cfg["source_dir"])
        clips = sorted([p for p in loops_dir.glob("*.mp4") if not p.name.startswith("_")])
        if not clips:
            raise RuntimeError(f"no loops in {loops_dir}; run pull_backgrounds.py first")

        seg_dir = cache / "bg_segs"
        seg_dir.mkdir(exist_ok=True)
        segments = []
        for i, b in enumerate(beat_list):
            next_start = beat_list[i + 1].start if i + 1 < len(beat_list) else total_dur
            seg_dur = next_start - b.start
            clip = clips[i % len(clips)]
            seg = seg_dir / f"seg_{i:02d}.mp4"
            if not seg.exists():
                _make_bg_segment(clip, seg_dur, seg, w=out_w, h=out_h)
            segments.append(seg)
        bg_concat = cache / "bg_concat.mp4"
        if bg_concat.exists():
            bg_concat.unlink()
        _concat_segments(segments, bg_concat)
        return bg_concat

    if bg_type == "single_loop":
        # Plumbed for completeness — not used by the example spec.
        raise NotImplementedError("single_loop bg not wired in v1")

    raise ValueError(f"unknown background.type: {bg_type}")


# ============ overlay resolution ============


def resolve_overlays(
    spec: dict, beat_list: list, ctx: dict,
    cache: Path, out_w: int, out_h: int,
) -> list[dict]:
    """Walk overlays in order, render PNG assets, resolve placement.

    Returns a list of resolved overlay dicts ready for compose.
    """
    templates = spec.get("templates", {})
    resolved: list[dict] = []
    registry: dict[str, dict] = {}

    for ov in spec["overlays"]:
        ov_id = ov["id"]
        kind: str
        png: Path | None = None
        pngs: list[Path] | None = None
        elem_w = elem_h = 0
        anchors: dict = {}

        if "template" in ov:
            expanded = expand_template(ov["template"], ov.get("args", {}), templates)
            png = cache / f"ov_{ov_id}.png"
            render_template_to_png(expanded, png)
            elem_w = int(expanded["width"])
            elem_h = int(expanded["height"])
            anchors = expanded["anchors"]
            kind = "static_png"

        elif ov.get("type") == "emoji_pop":
            elem_w = elem_h = int(ov["size"])
            png = cache / f"ov_{ov_id}.png"
            render_emoji_png(ov["emoji"], png, size=elem_w)
            kind = "emoji_pop"

        elif ov.get("type") == "per_beat_caption":
            canvas = ov.get("canvas", [1000, 240])
            elem_w, elem_h = int(canvas[0]), int(canvas[1])
            style = ov.get("style", {})
            font_size = int(style.get("font_size", 72))

            text_color = _parse_color(style.get("color", "#fff050")) or (255, 240, 80, 255)
            stroke_cfg = style.get("stroke", {})
            stroke_color = _parse_color(stroke_cfg.get("color", "#000000")) or (0, 0, 0, 255)
            stroke_w = int(stroke_cfg.get("w", 6))
            bg_cfg = style.get("bg", {})
            bg_rgb = _parse_color(bg_cfg.get("color", "#000000")) or (0, 0, 0, 255)
            bg_alpha = int(bg_cfg.get("alpha", 140))
            bg_color = (bg_rgb[0], bg_rgb[1], bg_rgb[2], bg_alpha)

            pngs = []
            for i, b in enumerate(beat_list):
                p = cache / f"ov_{ov_id}_{i:02d}.png"
                captions_mod.render_beat_caption(
                    b, out_path=p,
                    canvas_w=elem_w, canvas_h=elem_h,
                    font_size=font_size,
                    text_color=text_color, stroke_color=stroke_color,
                    stroke_width=stroke_w, bg_color=bg_color,
                )
                pngs.append(p)
            kind = "per_beat_caption"

        else:
            raise ValueError(f"overlay {ov_id}: must have `template` or `type`")

        x, y = resolve_position(ov.get("position", {}), elem_w, elem_h, out_w, out_h, registry)
        vis = resolve_visibility(ov.get("visible"), ctx)

        entry = {
            "id": ov_id, "kind": kind,
            "x": x, "y": y, "w": elem_w, "h": elem_h,
            "visible": vis, "anchors": anchors,
            "png": png, "pngs": pngs,
            "raw": ov,
        }
        registry[ov_id] = entry
        resolved.append(entry)

    return resolved


# ============ compose ============


def _emoji_pop_scale_expr(T0: float, S: float, anim: dict) -> str:
    """Return ffmpeg scale w-expression for an emoji-pop animation."""
    kind = anim.get("kind", "none")
    if kind == "scale_overshoot":
        ds = float(anim.get("duration_s", 0.25))
        ovs = float(anim.get("overshoot", 1.25))
        t_up = ds * 0.6
        return (
            f"if(lt(t,{T0:.3f}),2,"
            f"if(lt(t,{T0:.3f}+{t_up:.3f}),"
            f"  {S}*{ovs}*(t-{T0:.3f})/{t_up:.3f},"
            f"if(lt(t,{T0:.3f}+{ds:.3f}),"
            f"  {S}*({ovs}-(({ovs}-1)/{(ds-t_up):.3f})*(t-{T0:.3f}-{t_up:.3f})),"
            f"  {S})))"
        )
    return f"{S}"  # static


def compose(
    bg_concat: Path, audio_path: Path, overlays: list,
    total_dur: float, out_path: Path, out_w: int, out_h: int,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    inputs: list[str] = ["-i", str(bg_concat), "-i", str(audio_path)]
    next_idx = 2

    # Allocate input slots. For per_beat_caption, one slot per beat png.
    for ov in overlays:
        if ov["kind"] in ("static_png", "emoji_pop"):
            inputs += ["-loop", "1", "-i", str(ov["png"])]
            ov["_input_idx"] = next_idx
            next_idx += 1
        elif ov["kind"] == "per_beat_caption":
            ov["_input_indices"] = []
            for png in ov["pngs"]:
                inputs += ["-loop", "1", "-i", str(png)]
                ov["_input_indices"].append(next_idx)
                next_idx += 1

    chains: list[str] = []
    chains.append(f"[0:v]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[bg]")
    prev = "bg"
    counter = 0

    for ov in overlays:
        x, y = ov["x"], ov["y"]
        vf, vt = ov["visible"]

        if ov["kind"] == "static_png":
            counter += 1
            chains.append(f"[{ov['_input_idx']}:v]format=rgba[ov{counter}]")
            chains.append(
                f"[{prev}][ov{counter}]overlay=x={x}:y={y}:"
                f"enable='between(t,{vf:.3f},{vt:.3f})':format=auto[v{counter}]"
            )
            prev = f"v{counter}"

        elif ov["kind"] == "emoji_pop":
            counter += 1
            S = float(max(ov["w"], ov["h"]))
            anim = ov["raw"].get("animation", {"kind": "none"})
            T0 = vf
            cx = x + S / 2
            cy = y + S / 2
            w_expr = _emoji_pop_scale_expr(T0, S, anim)
            chains.append(
                f"[{ov['_input_idx']}:v]format=rgba,"
                f"scale=eval=frame:w='{w_expr}':h=-1[ov{counter}]"
            )
            chains.append(
                f"[{prev}][ov{counter}]overlay="
                f"x='{cx}-w/2':y='{cy}-h/2':"
                f"enable='between(t,{vf:.3f},{vt:.3f})':format=auto[v{counter}]"
            )
            prev = f"v{counter}"

        elif ov["kind"] == "per_beat_caption":
            # Beat list lives on the overlay (we plumbed through ctx earlier).
            beat_list = ov["raw"].get("_beat_list")
            extend = ov["raw"].get("extend_to_next_beat", True)
            for j, (idx, b) in enumerate(zip(ov["_input_indices"], beat_list)):
                cap_start = b.start
                if extend:
                    cap_end = beat_list[j + 1].start if j + 1 < len(beat_list) else total_dur
                else:
                    cap_end = b.end
                counter += 1
                chains.append(f"[{idx}:v]format=rgba[ov{counter}]")
                chains.append(
                    f"[{prev}][ov{counter}]overlay=x={x}:y={y}:"
                    f"enable='between(t,{cap_start:.3f},{cap_end:.3f})':format=auto[v{counter}]"
                )
                prev = f"v{counter}"

    filter_complex = ";".join(chains)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{prev}]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total_dur:.3f}",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.check_call(cmd)
    return out_path


# ============ orchestration ============


def _deep_merge(a: dict, b: dict) -> dict:
    """Deep-merge b into a; b wins on scalar conflicts."""
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def render_from_spec(spec_path: Path, channel_path: Path | None, out_dir: Path) -> Path:
    spec = yaml.safe_load(spec_path.read_text())
    if channel_path and channel_path.exists():
        channel = yaml.safe_load(channel_path.read_text()) or {}
        # Channel YAML is a defaults layer. Spec wins on conflicts.
        # We only merge fields the spec interpreter understands.
        for k in ("audio", "background", "beats", "templates", "output"):
            if k in channel and k not in spec:
                spec[k] = channel[k]
            elif k in channel and k in spec and isinstance(spec[k], dict):
                spec[k] = _deep_merge(channel[k], spec[k])

    slug = spec["slug"]
    out_w, out_h = spec["output"]["resolution"]
    tail_pad = float(spec["output"].get("tail_pad_s", 0.6))

    cache = out_dir / "cache" / slug
    cache.mkdir(parents=True, exist_ok=True)

    print(f"\n=== render_from_spec: {slug} ===")

    # 1. TTS
    t0 = time.time()
    audio_path = cache / "narration.wav"
    tts_cfg = spec["audio"]["narration"]["tts"]
    if not audio_path.exists():
        print(f"[1/5] TTS ({tts_cfg.get('provider','kokoro')})…")
        audio_mod.synthesize(
            spec["audio"]["narration"]["text"],
            voice=tts_cfg["voice"],
            out_path=audio_path,
            speed=float(tts_cfg.get("speed", 1.0)),
            provider=tts_cfg.get("provider", "kokoro"),
        )
    else:
        print("[1/5] TTS cached")
    print(f"     done in {time.time()-t0:.1f}s")

    # 2. Beats
    t0 = time.time()
    beats_path = cache / "beats.json"
    bcfg = spec.get("beats", {})
    if not beats_path.exists():
        print(f"[2/5] {bcfg.get('asr_provider','whisper_mlx')} word timestamps + beat split…")
        whisper_words = beats_mod.transcribe_words(audio_path, provider=bcfg.get("asr_provider", "whisper_mlx"))
        source_aligned = align.align_source_to_whisper(spec["audio"]["narration"]["text"], whisper_words)
        beat_list = beats_mod.split_into_beats(
            source_aligned,
            target_s=float(bcfg.get("target_s", 1.6)),
            max_s=float(bcfg.get("max_s", 2.6)),
        )
        beats_mod.save_beats(beat_list, beats_path)
    else:
        print("[2/5] beats cached")
        beat_list = beats_mod.load_beats(beats_path)
    print(f"     {len(beat_list)} beats, total {sum(b.duration for b in beat_list):.1f}s")

    audio_dur = _ffprobe_duration(audio_path)
    total_dur = audio_dur + tail_pad
    ctx = {
        "start": 0.0,
        "end": total_dur,
        "closer_start": beat_list[-1].start,
        "closer_end": beat_list[-1].end,
        "beat": beat_list,
    }

    # 3. Background
    t0 = time.time()
    print("[3/5] background…")
    bg_concat = build_background(spec["background"], beat_list, total_dur, cache, out_w, out_h)
    print(f"     done in {time.time()-t0:.1f}s")

    # 4. Resolve overlays
    t0 = time.time()
    print(f"[4/5] resolve overlays ({len(spec['overlays'])})…")
    overlays = resolve_overlays(spec, beat_list, ctx, cache, out_w, out_h)
    # Plumb beat_list into per_beat_caption overlays for compose.
    for ov in overlays:
        if ov["kind"] == "per_beat_caption":
            ov["raw"]["_beat_list"] = beat_list
    for ov in overlays:
        print(f"     · {ov['id']}: kind={ov['kind']}  pos=({ov['x']},{ov['y']})  size=({ov['w']}×{ov['h']})  visible={ov['visible'][0]:.2f}–{ov['visible'][1]:.2f}s")
    print(f"     done in {time.time()-t0:.1f}s")

    # 5. Compose
    t0 = time.time()
    print("[5/5] compose…")
    out_path = out_dir / "shorts" / f"{slug}.mp4"
    compose(bg_concat, audio_path, overlays, total_dur, out_path, out_w, out_h)
    print(f"     done in {time.time()-t0:.1f}s")

    print(f"\n✓ wrote {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--channel", default=None)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    render_from_spec(
        spec_path=Path(args.spec),
        channel_path=Path(args.channel) if args.channel else None,
        out_dir=Path(args.out),
    )


if __name__ == "__main__":
    main()
