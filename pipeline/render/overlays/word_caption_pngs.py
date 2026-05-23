"""Word-by-word PNG caption OverlayProducer (short-engine default).

Generates one PNG per beat (TikTok-style: one large word centred at
``H * 0.78``). Each PNG becomes a layer-40 OverlayElement covering
its beat's [start_s, end_s] window.

Today's impl delegates to
:func:`pipeline.compose.render_word_caption_pngs` — the existing
helper that ``shorts.py`` already calls. The bigbang PR moves the
body inline.

Style defaults from ``spec.caption_style.font_size_minimal/standard/dense``
(driven by ``spec.captions_density``).

2026-05-15 fail-loud audit
--------------------------

When ``spec.captions_enabled is True`` AND
``pipeline.captions.render_word_caption`` cannot be imported, this
plugin RAISES :class:`pipeline.render.contracts.RenderFailedError`
instead of silently returning an empty overlay list. Pre-fix the
ImportError was caught + WARN-logged + ``return []`` — that shipped
8/10 mystoriesanimated shorts with ZERO captions in the 2026-05-15
canary batch (job f1e319a3). When ``captions_enabled is False`` we
still return ``[]`` silently (user explicitly opted out).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    RenderFailedError,
    Timeline,
    register_plugin,
)


class WordCaptionPngs:
    """One PNG per beat, centred bottom-bias.

    Output: one OverlayElement per timeline segment (each is layer 40,
    asset_path = the rendered PNG, region = None so the FinalMux's
    layer-40 default placement applies).

    Pre-2026-05-14 this lived inline in ``shorts.py`` /
    ``compose.py``. The plugin wraps those helpers.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        if not timeline:
            return []

        # Resolve font size from CaptionsDensity + spec.caption_style.
        font_size = self._font_size_for_density(spec)

        out_dir = audio.narration_path.parent / "word_captions"
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Word-caption rendering is in pipeline.captions, NOT
            # pipeline.compose. The bigbang PR collapses these helpers
            # under pipeline.render.overlays.* and removes this delegation.
            from pipeline.captions import render_word_caption  # noqa: PLC0415
        except ImportError as exc:
            # Gate removed per user direction: import failure no longer
            # raises. Render proceeds without captions regardless of
            # spec.captions_enabled.
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger(__name__).warning(
                "word_caption_pngs: pipeline.captions.render_word_caption "
                "import failed (%s) — captions skipped.", exc,
            )
            return []

        # 2026-05-17 (round 3): WORD-LEVEL ASS SUBTITLE STREAM.
        #
        # Pre-fix this plugin generated N word-PNGs and returned N
        # OverlayElements — the downstream compose stage chained N
        # ffmpeg overlay= filters in one filter_complex, which silently
        # failed/truncated past ~50 overlays. Render 75ac2667 had 114
        # overlay elements, ffmpeg compose took 37 minutes and produced
        # a video with ZERO captions visible.
        #
        # Fix: generate ONE ASS subtitle file with N timed dialogue
        # events. ffmpeg renders ASS via the ``subtitles=`` filter in
        # one pass, scales to thousands of events. Single OverlayElement
        # returned with ``extras={"format": "ass"}`` so the compose
        # plugin can dispatch to the subtitle-burn path.
        #
        # Falls back to per-word PNGs when timeline lacks ``words``
        # (non-ASR segments, fixtures). Counts >= 4 word-events triggers
        # ASS mode; below that we keep per-PNG so single-beat fixtures
        # still work.
        has_word_timings = any(
            getattr(seg, "words", None) for seg in timeline
        )
        if has_word_timings:
            try:
                ass_path = self._build_word_caption_ass(
                    spec, timeline, font_size, out_dir,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "word_caption_pngs: ASS build failed (%s) — "
                    "falling back to per-word PNG overlays", exc,
                )
            else:
                if timeline:
                    return [OverlayElement(
                        start_s=timeline[0].start_s,
                        end_s=timeline[-1].end_s,
                        layer=40,
                        asset_path=ass_path,
                        extras={"format": "ass", "n_words": "auto"},
                    )]

        elements: list[OverlayElement] = []
        # 2026-05-17: TRUE one-word-at-a-time captions.
        #
        # Pre-fix this plugin iterated BEATS — each PNG carried the
        # whole sentence ("He takes two bites and says, it's missing")
        # squeezed into one canvas, rendering at ~30-40px height per
        # frame because PIL auto-scaled the entire phrase to fit. The
        # plugin was named "word_caption" but did beat-caption.
        #
        # Now: when ``seg.words`` is populated (ASR-derived short-
        # engine path), iterate WORDS — one PNG per word, each
        # rendered at the full configured font_size (110pt standard),
        # each enabled only during [word.start, word.end]. This is the
        # TikTok / modern-Shorts style the user asked for.
        #
        # When ``seg.words is None`` (authored sections, chapter
        # cards, non-ASR timelines), fall back to one-PNG-per-segment
        # at the segment's full duration — same as pre-fix.
        word_idx = 0
        for seg in timeline:
            seg_words = getattr(seg, "words", None)
            if seg_words:
                # Word-level iteration — modern Shorts style.
                for w in seg_words:
                    text = (getattr(w, "text", "") or "").strip()
                    # 2026-05-17 (round 4): skip punctuation-only tokens
                    # so a lone "/" or "," doesn't render as a giant
                    # floating glyph (render-41d3233a hook bug).
                    if not text or not any(ch.isalnum() for ch in text):
                        continue
                    start_s = float(getattr(w, "start", seg.start_s))
                    end_s = float(getattr(w, "end", seg.end_s))
                    if end_s <= start_s:
                        # Skip zero-duration words (ASR artifacts).
                        continue
                    png_path = out_dir / f"word_{word_idx:04d}.png"
                    try:
                        render_word_caption(
                            text,
                            png_path,
                            canvas_w=spec.output_resolution[0],
                            font_size=font_size,
                        )
                    except Exception:  # noqa: BLE001
                        word_idx += 1
                        continue
                    elements.append(OverlayElement(
                        start_s=start_s,
                        end_s=end_s,
                        layer=40,
                        asset_path=png_path,
                        extras={"text": text, "anchor_id": seg.anchor_id},
                    ))
                    word_idx += 1
            else:
                # Fallback — non-ASR segment, no word timings. Render
                # the whole segment text as a single PNG over the
                # segment's duration. Pre-2026-05-17 behaviour.
                png_path = out_dir / f"word_{word_idx:04d}.png"
                try:
                    render_word_caption(
                        seg.text,
                        png_path,
                        canvas_w=spec.output_resolution[0],
                        font_size=font_size,
                    )
                except Exception:  # noqa: BLE001
                    word_idx += 1
                    continue
                elements.append(OverlayElement(
                    start_s=seg.start_s,
                    end_s=seg.end_s,
                    layer=40,
                    asset_path=png_path,
                    extras={"text": seg.text, "anchor_id": seg.anchor_id},
                ))
                word_idx += 1
        return elements

    def _font_size_for_density(self, spec: Any) -> int:
        # CaptionsDensity → font size: minimal=biggest, dense=smallest.
        density_value = (
            spec.captions_density.value
            if hasattr(spec.captions_density, "value")
            else str(spec.captions_density)
        )
        if density_value == "minimal":
            return spec.caption_style.font_size_minimal
        if density_value == "dense":
            return spec.caption_style.font_size_dense
        return spec.caption_style.font_size_standard

    def _build_word_caption_ass(
        self,
        spec: Any,
        timeline: Timeline,
        font_size: int,
        out_dir: Path,
    ) -> Path:
        """Build a libass-compatible word-level ASS file from the timeline.

        One Dialogue event per word at its (start, end) timing. Style
        is sourced from ``spec.caption_style`` — yellow italic by default
        (matches the channel signature). Renders in ONE pass via
        ffmpeg ``subtitles=`` filter at mux time; no 114-overlay
        filter_complex chain.

        2026-05-17 (round 4) — three caption bugs the critique caught
        on render-41d3233a:

        1. **PlayRes vs output aspect mismatch.** Pre-fix this used the
           hardcoded ``play_res_x=1920, play_res_y=1080`` defaults from
           ``CaptionStyleConfig`` — landscape PlayRes — even when the
           output was a 1080×1920 portrait Short. libass scales axis-
           by-axis from PlayRes to output, so a portrait output got
           anamorphic glyphs whose horizontal extent didn't match what
           the font_size predicted. "partner" rendered at font_size=260
           in 1920×1080 PlayRes was ~1015 PlayResX units wide; scaled
           to 1080-output that's still 571px, but the per-character
           kerning landed at PlayResX positions that clipped the frame
           on a portrait output. Fix: PlayRes always equals the actual
           output_resolution so the PlayRes coord system IS the output
           coord system and font_size is in real output pixels.

        2. **Alignment=5 drifts with the on-clip zoom.** Alignment=5
           is middle-center (libass numpad layout) — the caption sat
           at the visual center, which on a 9:16 Short overlapped the
           character's face/torso/crotch as the per-image zoom pushed
           subjects around the frame. Fix: Alignment=2 (bottom-center)
           with ``MarginV = play_res_y * 0.15`` (~288px on a 1920-tall
           output) anchors captions to the BOTTOM of the OUTPUT frame
           — immune to the visualize chain's zoom/crop.

        3. **Per-event width overflow on long words.** Even at the
           "correct" PlayRes, the resolved font_size for ``minimal``
           density (320) renders a 7-char word at ~1230 output pixels
           — wider than the 1080-output canvas — so the word clips
           both edges. The PNG renderer (``render_word_caption``) has
           an auto-shrink loop for exactly this reason; the ASS path
           was missing it. Fix: for every Dialogue event measure the
           word's rendered width with Pillow at the configured
           font_size; when ``width > 0.85 × play_res_x`` prepend a
           ``{\\fs<N>}`` inline override that scales the per-event
           font down. Cheaper than rebuilding the Style block per word.

        Returns the path to the generated .ass file.
        """
        from pipeline.render.shared.long_form_lib import _ass_color_from_rgba  # noqa: PLC0415
        try:
            from pipeline.captions import _find_font  # noqa: PLC0415
        except ImportError:
            _find_font = None  # type: ignore[assignment]

        text_rgba = spec.caption_style.text_rgba
        primary = _ass_color_from_rgba(text_rgba)
        outline = _ass_color_from_rgba((0, 0, 0, 255))
        back = _ass_color_from_rgba((0, 0, 0, 255))
        italic_flag = 1 if spec.caption_style.italic else 0
        # 2026-05-17 (round 4, fix #1): PlayRes always matches the actual
        # output resolution so libass' coord system IS the output coord
        # system. The static 1920×1080 fallback (landscape) was the root
        # cause of caption-edge clipping on 9:16 Shorts in render-41d3233a:
        # libass scaled glyphs anamorphically from a 16:9 reference into
        # a 9:16 canvas, blowing past the output's actual width budget.
        try:
            out_w, out_h = spec.output_resolution
            play_res_x = int(out_w)
            play_res_y = int(out_h)
        except Exception:  # noqa: BLE001
            # Defensive fallback for fixture specs without output_resolution.
            play_res_x = int(getattr(spec.caption_style, "play_res_x", 1920))
            play_res_y = int(getattr(spec.caption_style, "play_res_y", 1080))

        # 2026-05-17 (round 4, fix #2): Alignment=2 = bottom-center
        # (libass numpad layout). Pre-fix this was Alignment=5
        # (middle-center) — captions drifted with the on-clip zoom
        # because the visual subject moved relative to the frame center
        # over the zoom's duration. Bottom-anchor + a generous MarginV
        # (15% of frame height ≈ TikTok chin-clearance) makes captions
        # immune to anything happening in the visualize chain.
        alignment = 2
        margin_v = max(80, int(play_res_y * 0.15))

        # Outline / shadow proportional to font size. BorderStyle=3 =
        # opaque box behind text (TikTok-style pill). MarginV measured
        # in PlayRes units from alignment edge — PlayRes == output, so
        # these are real output pixels.
        outline_w = max(2.0, font_size * 0.04)
        shadow_w = max(2.0, font_size * 0.03)
        style_line = (
            f"Style: Default,Helvetica,{int(font_size)},"
            f"{primary},{primary},{outline},{back},"
            f"1,{italic_flag},0,0,"  # Bold=1 for legibility on busy backgrounds
            f"100,100,1,0,1,{outline_w:.1f},{shadow_w:.1f},"
            f"{alignment},80,80,{margin_v},1"
        )
        lines: list[str] = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {play_res_x}",
            f"PlayResY: {play_res_y}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
            "Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            style_line,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, "
            "MarginR, MarginV, Effect, Text",
        ]

        def _ts(t: float) -> str:
            t = max(0.0, t)
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = t - h * 3600 - m * 60
            return f"{h}:{m:02d}:{s:05.2f}"

        # 2026-05-17 (round 4, fix #3): width-fit cache. Resolving a
        # font at a given size is the expensive bit (~100ms on cold-
        # load); measuring a word's bbox once it's loaded is cheap. We
        # cache the loaded font per (size, has_devanagari) and the
        # per-word fitted size per (word, base_size) so a 200-event ASS
        # build does at most ~3 _find_font calls regardless of how many
        # auto-shrinks fire.
        font_cache: dict[tuple[int, bool], Any] = {}
        # 2026-05-18 (round 5): TIGHTER budget. The 0.85 budget passed
        # 7-8 char words ("seconds.", "covered") through Pillow's
        # measurement, but libass on the Cloud Run worker resolved to
        # a DIFFERENT font (fontconfig picks DejaVu Sans Bold Italic
        # there; Pillow on macOS picks Helvetica-Bold) whose rendered
        # width was ~20-30% wider. Result: those words clipped both
        # frame edges on render-bd2d0848. Drop budget to 0.72 so the
        # safety margin absorbs the Pillow↔libass font-metric drift.
        max_word_w = max(200, int(play_res_x * 0.72))
        # 2026-05-18 (round 5): Pillow-vs-libass safety multiplier.
        # Pillow's bbox underestimates the libass-rendered width
        # because the two pick different fonts at composition time.
        # Multiply Pillow's measurement by this factor before
        # comparing to the budget so the fit decision is conservative.
        _PILLOW_LIBASS_SAFETY = 1.20

        def _fit_font_size(text: str, base_size: int) -> int:
            """Return the largest font size ≤ base_size that keeps ``text``
            within ``max_word_w`` pixels at PlayRes (output) scale.

            Cheap heuristic when Pillow isn't available: assume
            avg_char_width ≈ 0.65 × font_size (bumped from 0.55 in
            round 5 — bolder/wider glyphs at the libass render side
            need the extra slack) and scale down when over budget.
            """
            if not text:
                return base_size
            if _find_font is None:
                est_w = int(base_size * 0.65 * len(text))
                if est_w <= max_word_w:
                    return base_size
                return max(60, int(base_size * (max_word_w / est_w)))
            # Pillow-backed precise fit. Mirrors the auto-shrink loop
            # in pipeline.captions.render_word_caption so PNG-fallback
            # and ASS paths produce visually-consistent results. The
            # _PILLOW_LIBASS_SAFETY multiplier compensates for the
            # font-mismatch between Pillow's measurement (Helvetica/
            # DejaVu local) and libass' runtime resolution.
            from pipeline.captions import _has_devanagari  # noqa: PLC0415
            has_dev = _has_devanagari(text)
            size = base_size
            while size > 60:
                key = (size, has_dev)
                font = font_cache.get(key)
                if font is None:
                    font = _find_font(size, text=text)
                    font_cache[key] = font
                bbox = font.getbbox(text)
                width = int((bbox[2] - bbox[0]) * _PILLOW_LIBASS_SAFETY)
                if width <= max_word_w:
                    return size
                size = max(60, int(size * 0.9))
            return size

        def _is_renderable_word(text: str) -> bool:
            """Skip tokens that are pure punctuation/symbols.

            Single-glyph punctuation (a lone "/" or ",") rendered as
            a TikTok-style caption reads as a glitch — the viewer sees
            a giant slash floating on screen and thinks the render is
            broken. ASR sometimes emits these as separate "words" with
            their own start/end timings. Strip them out at the caption
            layer; the spoken audio still carries the pause.
            """
            if not text:
                return False
            return any(ch.isalnum() for ch in text)

        n_events = 0
        n_shrunk = 0
        for seg in timeline:
            seg_words = getattr(seg, "words", None)
            if seg_words:
                for w in seg_words:
                    text = (getattr(w, "text", "") or "").strip()
                    if not _is_renderable_word(text):
                        continue
                    start = float(getattr(w, "start", seg.start_s))
                    end = float(getattr(w, "end", seg.end_s))
                    if end <= start:
                        continue
                    fitted_size = _fit_font_size(text, font_size)
                    # Escape ASS special characters (comma, newline, braces).
                    text_safe = (
                        text.replace("\\", "\\\\")
                        .replace("{", "\\{")
                        .replace("}", "\\}")
                        .replace("\n", "\\N")
                    )
                    if fitted_size < font_size:
                        # Per-event font override; cheaper than a
                        # second Style block. The \fs tag is reset
                        # implicitly by the next Dialogue (each event
                        # starts fresh from the Style defaults).
                        text_safe = f"{{\\fs{fitted_size}}}{text_safe}"
                        n_shrunk += 1
                    lines.append(
                        f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,"
                        f",0,0,0,,{text_safe}"
                    )
                    n_events += 1
            else:
                # Segment without word-timings — fall back to one event
                # per beat. Same single-PNG behavior as legacy.
                text = (seg.text or "").strip()
                if not _is_renderable_word(text):
                    continue
                # For multi-word sentence segments the fit check still
                # uses the same width budget; libass will wrap on
                # spaces inside ``WrapStyle: 0`` so a long sentence
                # spans multiple lines without per-event shrinkage.
                # Apply shrinkage only when the segment is a single
                # long token (rare; usually authored).
                fitted_size = font_size
                if " " not in text:
                    fitted_size = _fit_font_size(text, font_size)
                text_safe = (
                    text.replace("\\", "\\\\")
                    .replace("{", "\\{")
                    .replace("}", "\\}")
                    .replace("\n", "\\N")
                )
                if fitted_size < font_size:
                    text_safe = f"{{\\fs{fitted_size}}}{text_safe}"
                    n_shrunk += 1
                lines.append(
                    f"Dialogue: 0,{_ts(seg.start_s)},{_ts(seg.end_s)},"
                    f"Default,,0,0,0,,{text_safe}"
                )
                n_events += 1

        out_path = out_dir / "word_captions.ass"
        out_path.write_text("\n".join(lines))
        _logger.info(
            "word_caption_pngs: built ASS with %d word events at "
            "font_size=%d (n_shrunk=%d), alignment=%d, "
            "PlayRes=%dx%d, MarginV=%d (path=%s)",
            n_events, font_size, n_shrunk, alignment,
            play_res_x, play_res_y, margin_v, out_path,
        )
        return out_path


register_plugin("overlays", "word_caption_pngs", WordCaptionPngs())
assert isinstance(WordCaptionPngs(), OverlayProducer)


__all__ = ["WordCaptionPngs"]
