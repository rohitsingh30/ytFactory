"""Sentence-level ASS captions OverlayProducer.

Selected by ``spec.captions_layout`` when the user picks
``bottom_one_line`` or ``bottom_two_line`` from the UI. Used by both
the shorts engine and the long-form engine.

2026-05-24 rewrite — was previously a 5-line wrapper that called
``build_captions_ass(cues=..., out_path=..., total_duration_s=...,
play_res_x=..., play_res_y=...)``. NONE of those keyword arguments
existed on the actual ``build_captions_ass`` signature
(``out_ass, *, narration_text=..., chunk_wavs=..., max_lines=2,
...``) — so the call would TypeError at runtime, meaning *every*
render that selected ``bottom_one_line`` or ``bottom_two_line``
crashed at the caption-overlay stage. The bug went unobserved
because the most-used caption layout is ``center_word_by_word``
(handled by ``word_caption_pngs.py``), which doesn't hit this code
path.

The rewrite below writes an ASS file directly from the Timeline's
segments — no call into ``build_captions_ass``. The producer now:

- honours ``spec.captions_layout``:
  * ``BOTTOM_ONE_LINE``  → ``max_lines=1`` (truncate per cue to 1 line)
  * ``BOTTOM_TWO_LINE``  → ``max_lines=2`` (wrap up to 2 lines per cue)
  * (``CENTER_WORD_BY_WORD`` is routed elsewhere — never reaches here)
- honours ``spec.caption_style``: text_rgba, italic, font_name,
  font_size, margin_v, play_res_x/y.
- scales the base ``font_size`` for the actual canvas aspect — the
  ``CaptionStyleConfig.font_size = 38`` default is calibrated for a
  1920×1080 long-form frame (38 px ≈ 3.5% of frame height); on a
  9:16 1080×1920 shorts frame the same 38 px is only 2% of frame
  height (essentially unreadable). The producer multiplies by
  ``play_res_y / 1080.0`` so 1080-tall renders use 38 px and
  1920-tall renders use ~67 px (still ~3.5% of frame height).
  Channels that want a fixed absolute size can pin
  ``spec.caption_style.font_size`` AND set ``caption_style.
  auto_scale_to_aspect = False`` once that knob lands; for now,
  auto-scaling is always on for this producer.

Output contract (unchanged): one ``OverlayElement`` of layer 40
spanning ``[timeline[0].start_s, timeline[-1].end_s]``, asset_path
points at the generated ``.ass`` file. Compose burns it with
ffmpeg's ``ass=`` filter.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)
from pipeline.render.telemetry_helpers import track_event


# ---- ASS file helpers ----------------------------------------------------


def _ass_time(t: float) -> str:
    """ASS time stamp ``H:MM:SS.cc`` (centiseconds)."""
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    r"""Escape ``{``, ``}``, ``\`` and fold newlines to ``\N``."""
    if not text:
        return ""
    return (
        text
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
        .replace("\r", r"\N")
    )


def _ass_color(rgba: tuple | list, *, alpha_override: int | None = None) -> str:
    """``(R, G, B[, A])`` on 0-255 → ASS ``&HAABBGGRR`` (alpha inverted)."""
    if len(rgba) == 4:
        r, g, b, a = rgba
    else:
        r, g, b = rgba
        a = 255
    if alpha_override is not None:
        a = alpha_override
    # ASS alpha is inverted: 0 = opaque, 255 = transparent.
    ass_alpha = 255 - max(0, min(255, int(a)))
    return f"&H{ass_alpha:02X}{int(b):02X}{int(g):02X}{int(r):02X}"


def _soft_wrap(text: str, *, max_chars: int, max_lines: int) -> str:
    """Soft-wrap ``text`` to at most ``max_lines`` lines of
    ``max_chars`` each, joined with ``\\N`` (the ASS line break).

    If the wrapped output exceeds ``max_lines``, the trailing lines
    are dropped and ``…`` appended to the last surviving line. This
    is the right behaviour for ``BOTTOM_ONE_LINE`` — a sentence too
    long to fit shows the first line plus an ellipsis rather than
    being silently chopped or spilling onto a hidden line.
    """
    tokens = (text or "").split()
    if not tokens:
        return ""
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        if not cur:
            cur = tok
        elif len(cur) + 1 + len(tok) <= max_chars:
            cur = f"{cur} {tok}"
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        # Truncate. Keep the first max_lines-1 lines verbatim; on the
        # last surviving line, append "…" to signal truncation.
        kept = lines[:max_lines]
        kept[-1] = kept[-1].rstrip(".,;:") + "…"
        lines = kept
    return r"\N".join(lines)


# ---- producer ------------------------------------------------------------


class SentenceCaptionAss:
    """Writes one ``.ass`` file from the Timeline's segments.

    Honors ``spec.captions_layout`` (``BOTTOM_ONE_LINE`` /
    ``BOTTOM_TWO_LINE``) and ``spec.caption_style`` (colors, font,
    margins). Aspect-aware font sizing — see module docstring.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        if not timeline:
            return []
        cues = [
            (float(s.start_s), float(s.end_s), s.text)
            for s in timeline if s.text
        ]
        if not cues:
            return []

        # ---- resolve user-selected sizing knobs ---------------------------
        # Layout → max_lines per cue (the UI selection lands here).
        layout = getattr(spec, "captions_layout", None)
        layout_value = getattr(layout, "value", layout)
        if layout_value == "bottom_one_line":
            max_lines = 1
            max_chars = 56  # tighter wrap for a 1-liner
        else:
            # Default and bottom_two_line both use 2 lines + the
            # historic ~70-char wrap from build_captions_ass.
            max_lines = 2
            max_chars = 70

        style = getattr(spec, "caption_style", None)
        text_rgba = getattr(style, "text_rgba", (255, 217, 61, 255))
        italic = bool(getattr(style, "italic", True))
        font_name = getattr(style, "font_name", "Helvetica")
        base_font_size = int(getattr(style, "font_size", 38))
        margin_v = int(getattr(style, "margin_v", 80))
        play_res_x = int(getattr(style, "play_res_x", 1920))
        play_res_y = int(getattr(style, "play_res_y", 1080))

        # Aspect-aware base font size. CaptionStyleConfig.font_size = 38
        # is calibrated for 1080-tall long-form; scale proportionally
        # for taller canvases (shorts at 1920-tall → ~67 px). The same
        # ~3.5% of frame height regardless of aspect.
        scaled_font_size = max(
            16, int(round(base_font_size * (play_res_y / 1080.0)))
        )

        # ---- write the ASS file --------------------------------------------
        out_path = audio.narration_path.parent / "captions.ass"

        primary = _ass_color(text_rgba)
        outline = _ass_color((0, 0, 0, 255))
        back = _ass_color((0, 0, 0, 255))
        italic_flag = 1 if italic else 0
        alignment = 2  # libass numpad: 2 = bottom-center

        style_line = (
            f"Style: Default,{font_name},{scaled_font_size},"
            f"{primary},{primary},{outline},{back},"
            f"0,{italic_flag},0,0,"
            f"100,100,1,0,1,1.5,2,"
            f"{alignment},80,80,{margin_v},1"
        )
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {play_res_x}\n"
            f"PlayResY: {play_res_y}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"{style_line}\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
        )

        events: list[str] = []
        t0 = time.perf_counter()
        for start, end, text in cues:
            wrapped = _soft_wrap(
                text, max_chars=max_chars, max_lines=max_lines,
            )
            if not wrapped:
                continue
            wrapped = _ass_escape(wrapped)
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},"
                f"Default,,0,0,0,,{wrapped}"
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")

        duration_ms = int((time.perf_counter() - t0) * 1000)
        track_event(
            "overlay.render",
            category="pipeline",
            duration_ms=duration_ms,
            metadata={
                "kind": "sentence",
                "layout": str(layout_value),
                "max_lines": max_lines,
                "font_size_scaled": scaled_font_size,
                "play_res_y": play_res_y,
                "count": len(events),
                "total_chars": sum(len(c[2] or "") for c in cues),
                "duration_ms": duration_ms,
            },
        )

        return [OverlayElement(
            start_s=cues[0][0],
            end_s=cues[-1][1],
            layer=40,
            asset_path=out_path,
            extras={
                "format": "ass",
                "n_cues": len(events),
                "max_lines": max_lines,
                "font_size": scaled_font_size,
            },
        )]


register_plugin("overlays", "sentence_caption_ass", SentenceCaptionAss())
assert isinstance(SentenceCaptionAss(), OverlayProducer)


__all__ = ["SentenceCaptionAss"]
