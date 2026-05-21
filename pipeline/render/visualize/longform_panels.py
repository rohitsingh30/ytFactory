"""Long-form panel slideshow VisualProducer (long-engine canonical).

Wraps :func:`pipeline.render.long_form.build_image_panels_video` so
the new long_engine can call a Protocol method instead of importing
the renderer-internal helper directly.

Today's impl is a thin delegating wrapper. The bigbang PR moves the
body fully into this module so long_form.py can be deleted.

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
    """One Flux panel per ~30-60 s of narration with Ken Burns motion.

    Today's impl wraps the existing
    :func:`pipeline.render.long_form.build_image_panels_video` helper.
    The bigbang PR moves the body inline.
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
        panels = self._panels_from_timeline(timeline)
        if not panels:
            return self._fallback_solid_color(
                spec, timeline, work_dir,
                cause=RuntimeError(
                    "longform_panels: timeline produced 0 panels"
                ),
            )

        provider = (spec.extra or {}).get("image_provider", "cloudrun_z_image_turbo")
        style_prefix = (spec.extra or {}).get("image_style_prefix", "")
        seed_base = int((spec.extra or {}).get("image_seed", 42))
        steps = int((spec.extra or {}).get("image_steps", 4))

        # 2026-05-15 (v16) — defensively rescale ``hold_s`` so the
        # sum of holds matches the timeline's narrated duration.
        # Pre-fix the asr_anchors plugin could emit overlapping
        # segments (each ``end_s = total_s``); ``_panels_from_timeline``
        # then derived ``hold_s = end_s - start_s`` which produced
        # 1415s per panel for a 1500s render — kenburns then
        # rendered 42,456 ffmpeg frames for ONE panel before the
        # next started, blowing through the cloud-run JOB wall.
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

    def _panels_from_timeline(self, timeline: Timeline) -> list[dict]:
        # 2026-05-15 — include hold_s so _assemble_panel_kenburns can
        # size each clip to the timeline segment. Pre-fix this only
        # passed scene + start_s/end_s; the kenburns helper read
        # panel.get("hold_s", 20) which defaulted to 20s per panel —
        # for a 23min render that's 1380s of visuals vs 1431s narration,
        # close enough to look fine ONLY if the helper's
        # _adjust_panel_holds_to_dur ran (it does on the main path).
        # Defensive: pass the real per-segment hold derived from
        # timeline so even if downstream skips the adjust step the
        # visuals match the narration timing.
        #
        # 2026-05-15 (P2) — derive a non-empty scene fallback when
        # ``seg.text`` is empty. ``_generate_panel_stills`` raises
        # ValueError("panel N missing 'scene' field") on the first
        # empty-scene panel, which trips the outer ``except Exception``
        # → solid color for the whole render. This bit the cosmos
        # hubble long-form (job f37bb01a) when asr_anchors emitted
        # text="" because the script's section bodies live in
        # ``sec.text`` (not ``sec.body``). asr_anchors now reads the
        # text alias too — but other future schema drift (visual_brief
        # only, summary only, etc) would re-surface this same crash;
        # this fallback makes the helper robust to any seg.text=""
        # regardless of root cause. Fallback uses anchor_id so the
        # generated panel is at least loosely thematic to that section.
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
        """Last-resort solid-color stand-in for longform_panels.

        2026-05-15 fail-loud audit
        --------------------------

        Pre-audit this silently produced a #141414 stand-in when
        ``build_image_panels_video`` failed (image-gen API down, helper
        kwarg drift, empty timeline). That was the LAST link in the
        chain ``archival_shotlist → longform_panels → solid color``
        that made every cosmosdecoded / historyrecapped long-form
        render ship as 26 min of black even after the upstream
        fallbacks dispatched here.

        Now: raises :class:`RenderFailedError` unless
        ``YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1`` is set in the env.
        See ``docs/post-audit-2026-05-15.md`` for the audit summary.
        """
        from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg  # noqa: PLC0415
        from pipeline.render.visualize._fallback import (  # noqa: PLC0415
            _solid_color_override_enabled,
        )
        from pipeline.render.contracts import RenderFailedError  # noqa: PLC0415

        if not _solid_color_override_enabled():
            raise RenderFailedError(
                f"longform_panels: refusing to return solid-color "
                f"stand-in — set YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1 "
                f"for emergency renders. "
                f"site=pipeline/render/visualize/longform_panels.py:"
                f"LongformPanels._fallback_solid_color. "
                f"Original cause: {cause!r}"
            ) from cause

        out_path = work_dir / "panels_fallback.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration_s = timeline[-1].end_s if timeline else 1.0
        w, h = spec.output_resolution
        run_ffmpeg([
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", f"color=c=0x141414:s={w}x{h}:r={spec.output_fps}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ])
        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "longform_panels_fallback"},
        )


register_plugin("visualize", "longform_panels", LongformPanels())
assert isinstance(LongformPanels(), VisualProducer)


__all__ = ["LongformPanels"]
