"""Long-form panel slideshow VisualProducer (long-engine canonical).

Wraps :func:`pipeline.render.shared.long_form_lib.build_image_panels_video`
so the long_engine can call a Protocol method instead of importing
the renderer-internal helper directly.

Plugin selection: ``spec.visual_mode = LONGFORM_PANELS``. Default for
mystoriesanimated / hindutavaanimated / rhymetimejunction long-form
renders. Channels using archival footage instead set
``visual_mode=archival_shotlist``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    Timeline,
    VisualProducer,
    VisualTrack,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration


class LongformPanels:
    """One Flux panel per ~25 s of narration, static held + hard cuts.

    Static stills only; the panel-count cadence in
    ``_planned_sections_and_panels`` is 25s/panel (target ~72 for a
    30-min long-form) so each still doesn't dwell long enough to
    register as a frozen frame.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        try:
            from pipeline.render.shared.long_form_lib import (  # noqa: PLC0415
                build_image_panels_video,
            )
        except ImportError as _imp_exc:
            # Helper not available — defer to _fallback_solid_color which
            # raises RenderFailedError by default (2026-05-15 fail-loud).
            # Set YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1 to opt back into
            # the legacy stand-in.
            return self._fallback_solid_color(
                spec, timeline, work_dir, cause=_imp_exc,
            )

        out_path = work_dir / "panels_video.mp4"

        # 2026-05-15 — pin the real kwargs for build_image_panels_video.
        # Pre-fix this caller passed ``narration_dur_s=`` /
        # ``out_path=`` / ``image_w=`` / ``image_h=`` — NONE of which
        # exist on the helper's signature. The ``except (TypeError,
        # Exception)`` block below swallowed the kwarg-mismatch and
        # fell through to solid color, which is why every cloud
        # long-form render of historyrecapped / cosmosdecoded landed
        # at panels_fallback.mp4 (still solid color) even after the
        # archival_shotlist / footage_windows fallbacks dispatched to
        # longform_panels. Surfaced by job d8a0a076 ("The fall of the
        # Roman Empire") on 2026-05-15 — the v13 chain "archival/footage
        # → longform_panels → solid color" was broken at the last
        # step.
        #
        # The real signature is:
        #   build_image_panels_video(
        #       panels, style_prefix, image_provider, image_seed,
        #       image_steps, image_width, image_height, cache_dir,
        #       out_w=1920, out_h=1080, fps=30, crossfade_s=1.5,
        #       zoom_factor=1.08,
        #   )
        # — note it RETURNS the out path (writes to cache_dir/video.mp4)
        # and doesn't accept ``out_path`` as a kwarg. We post-move the
        # produced video to our requested out_path below.
        panels = self._panels_from_timeline(timeline, spec)
        if not panels:
            return self._fallback_solid_color(
                spec, timeline, work_dir,
                cause=RuntimeError(
                    "longform_panels: timeline produced 0 panels"
                ),
            )

        from pipeline.images.images import CANONICAL_IMAGE_PROVIDER  # noqa: PLC0415
        provider = (spec.extra or {}).get("image_provider") or CANONICAL_IMAGE_PROVIDER
        style_prefix = (spec.extra or {}).get("image_style_prefix", "")
        seed_base = int((spec.extra or {}).get("image_seed", 42))
        steps = int((spec.extra or {}).get("image_steps", 4))

        # O37 / Fix #2 (2026-05-24) — refiner context for the long-form
        # panel path. Mirrors the keys ai_beat_slideshow reads off
        # spec.extra so the two paths agree on which auxiliary inputs
        # the prompt refiner sees. ``default_scene_anchor`` is the new
        # channel-level setting string (O40) — see
        # pipeline.images.prompt_refiner for how it weaves into
        # refined_scene when the beat's authored scene lacks a setting.
        era_anchor_prefix = (spec.extra or {}).get("era_anchor_prefix")
        character_description = (spec.extra or {}).get("character_description")
        mood = (spec.extra or {}).get("mood")
        scene_anchor = (spec.extra or {}).get("default_scene_anchor")
        channel_key = getattr(spec, "channel", None)

        # 2026-05-15 (v16) — defensively rescale ``hold_s`` so the
        # sum of holds matches the timeline's narrated duration.
        # Pre-fix the asr_anchors plugin could emit overlapping
        # segments (each ``end_s = total_s``); ``_panels_from_timeline``
        # then derived ``hold_s = end_s - start_s`` which produced
        # 1415s per panel for a 1500s render — ffmpeg then rendered
        # 42,456 frames for ONE panel before the next started, blowing
        # through the cloud-run JOB wall.
        # asr_anchors's 2-pass refactor (same v16) is the root-cause
        # fix; this call is the belt-and-braces net so any future
        # TimelineBuilder regression cannot reproduce the disaster.
        # See tests/render/visualize/test_long_form_fallback.py
        # ::LongformPanelsAdjustHoldsToNarrationTest.
        narration_dur_s = (
            timeline[-1].end_s if timeline else 0.0
        )
        if narration_dur_s > 0.0:
            try:
                from pipeline.render.shared.long_form_lib import (  # noqa: PLC0415
                    _adjust_panel_holds_to_dur,
                )
                _adjust_panel_holds_to_dur(panels, narration_dur_s=narration_dur_s)
            except Exception as exc:  # noqa: BLE001 — defensive only
                import logging as _logging  # noqa: PLC0415
                _logging.getLogger(__name__).warning(
                    "longform_panels: _adjust_panel_holds_to_dur "
                    "unavailable (%s) — proceeding with raw holds",
                    exc,
                )

        try:
            produced = build_image_panels_video(
                panels=panels,
                style_prefix=style_prefix,
                image_provider=provider,
                image_seed=seed_base,
                image_steps=steps,
                image_width=spec.output_resolution[0],
                image_height=spec.output_resolution[1],
                cache_dir=work_dir,
                out_w=spec.output_resolution[0],
                out_h=spec.output_resolution[1],
                fps=spec.output_fps,
                era_anchor_prefix=era_anchor_prefix,
                character_description=character_description,
                mood=mood,
                scene_anchor=scene_anchor,
                channel_key=channel_key,
            )
        except Exception as exc:  # noqa: BLE001 — wrap and re-raise via _fallback_solid_color
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger(__name__).warning(
                "longform_panels: build_image_panels_video failed (%s) — "
                "deferring to solid color (will raise unless "
                "YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1). Helper signature "
                "may have drifted again; see tests/render/visualize/"
                "test_long_form_fallback.py::LongformPanelsBuildKwargContractTest "
                "for the contract pin.",
                exc,
            )
            return self._fallback_solid_color(spec, timeline, work_dir, cause=exc)

        # Move the helper's output to our requested out_path so the
        # engine's mux step finds it where it expects.
        if produced != out_path:
            import shutil as _shutil  # noqa: PLC0415
            _shutil.move(str(produced), str(out_path))

        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "longform_panels", "n_panels": len(panels)},
        )

    def _panels_from_timeline(self, timeline: Timeline, spec: Any = None) -> list[dict]:
        """Resolve the panel list to render.

        Priority order (2026-05-23 fix for the headline panel-count bug):

        1. ``spec.extra["authored_long_form_panels"]`` — the LLM-authored
           ``panel_briefs[]`` staged by ``populate_render_extras``. This
           is the canonical path: 30-min long-form → ~72 panels at ~25s
           each, instead of the pre-fix 10 panels at ~125s each (which
           tanked retention AND timed out the Cloud Run task).
           ``hold_s`` here is the LLM's authored guess; downstream
           ``_adjust_panel_holds_to_dur`` scales the holds uniformly
           to match the narrated duration.

        2. Per-section fallback — one panel per timeline segment, scene
           derived from ``seg.text`` (or ``"Establishing scene for
           <anchor_id>"`` if text is empty). Activated only when
           ``authored_long_form_panels`` is absent — preserves the
           pre-fix behavior for legacy envelopes that ship without
           authored panels (e.g. hand-rolled narrations).

        Notes on the older multi-line backstory below (kept for grep
        when the next regression hits):

        2026-05-15 — include hold_s so _assemble_panel_static can
        size each clip to the timeline segment. Pre-fix this only
        passed scene + start_s/end_s; the assembly helper read
        panel.get("hold_s", 20) which defaulted to 20s per panel.

        2026-05-15 (P2) — derive a non-empty scene fallback when
        ``seg.text`` is empty. ``_generate_panel_stills`` raises
        ValueError("panel N missing 'scene' field") on the first
        empty-scene panel, which trips the outer ``except Exception``
        → solid color for the whole render. asr_anchors now reads
        the text alias too — this fallback is belt-and-braces.
        """
        authored = None
        if spec is not None and getattr(spec, "extra", None):
            authored = spec.extra.get("authored_long_form_panels")
        if authored:
            out: list[dict] = []
            for p in authored:
                scene = (p.get("scene") or "").strip()
                if not scene:
                    continue
                try:
                    hold_s = float(p.get("hold_s") or 0.0)
                except (TypeError, ValueError):
                    hold_s = 0.0
                # Floor at 0.5s — _adjust_panel_holds_to_dur expects
                # positive holds; some LLM outputs emit hold_s=0.
                if hold_s < 0.5:
                    hold_s = 6.0
                entry: dict = {"scene": scene, "hold_s": hold_s}
                after = p.get("after_section_id")
                if after:
                    entry["after_section_id"] = after
                out.append(entry)
            if out:
                return out

        # Per-section fallback.
        out = []
        for i, seg in enumerate(timeline):
            hold_s = max(0.5, seg.end_s - seg.start_s)
            scene = (seg.text or "").strip()
            if not scene:
                anchor = seg.anchor_id or f"section {i + 1}"
                scene = f"Establishing scene for {anchor}"
            out.append({
                "id": seg.anchor_id,
                "scene": scene,
                "start_s": seg.start_s,
                "end_s": seg.end_s,
                "hold_s": hold_s,
            })
        return out

    def _fallback_solid_color(
        self, spec: Any, timeline: Timeline, work_dir: Path,
        cause: BaseException | None = None,
    ) -> VisualTrack:
        """Solid-color fallback REMOVED per user direction. Raises."""
        from pipeline.render.contracts import RenderFailedError  # noqa: PLC0415
        raise RenderFailedError(
            f"longform_panels: image-gen failed and no solid-color "
            f"fallback path remains. "
            f"site=pipeline/render/visualize/longform_panels.py:"
            f"_fallback_solid_color. Cause: {cause!r}"
        ) from cause


register_plugin("visualize", "longform_panels", LongformPanels())
assert isinstance(LongformPanels(), VisualProducer)


__all__ = ["LongformPanels"]
