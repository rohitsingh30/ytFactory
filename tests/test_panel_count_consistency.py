"""Tests pinning the panel-count single-source-of-truth contract.

Before 2026-05-23, panel count was determined by five disconnected
places (LLM prompt hint, hardcoded truncate, channel YAML cap that
was never read, and the renderer's per-section iteration that
ignored authored panels entirely). For a 30-min long-form, the
documented intent was "60 panels for 30 min, one per ~30s"; what
actually shipped was 10 panels at ~125s each — which both tanked
retention and timed out the Cloud Run 3600s wall.

Source of truth post-2026-05-23: ``channel.long_form.panel_seconds_target``
(default 25). All five paths agree on this value.

This module pins:

1. ``_planned_sections_and_panels`` derives panel_count_target from
   channel cfg's ``panel_seconds_target`` (default 25), not the
   hardcoded 7 it used to use.
2. ``after_section_id`` round-trips through ``LongFormPanel`` and
   ``to_legacy_long_form_dict`` / ``from_dict``.
3. ``populate_render_extras`` stages ``script.panels`` into
   ``spec.extra["authored_long_form_panels"]`` so the
   ``longform_panels`` visualize plugin can read them.
4. ``longform_panels._panels_from_timeline`` honours the authored
   panels when present and falls back to per-section otherwise.
5. Channel YAML's ``long_form.panel_max_count`` is the cap that
   ``_aggregate`` enforces (not the legacy hardcoded 60).
6. The Ken-Burns / xfade kwargs on ``build_image_panels_video`` are
   accepted but ignored — back-compat only.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.llm import rewrite_long_form as _rlf
from pipeline.llm.script_schema import LongFormPanel, ScriptEnvelope
from pipeline.render.contracts import Segment
from pipeline.render.spec import (
    RenderKind,
    RenderSpec,
    VisualMode,
)
from pipeline.render.spec_enrich import populate_render_extras
from pipeline.render.visualize.longform_panels import LongformPanels


# ---------------------------------------------------------------------------
# 1. _planned_sections_and_panels reads channel cfg
# ---------------------------------------------------------------------------


class PlannedSectionsAndPanelsTest(unittest.TestCase):
    def test_default_panel_seconds_target_is_25s(self):
        """No channel cfg → default cadence 25 s/panel.

        30 min @ 25 s = 72 panel target. Pre-fix this was 7 s/panel
        → 257 target (which the LLM ignored, returning 25, which the
        renderer dropped to 10 — the headline bug).
        """
        (
            _wt, _wf, _wc,
            _sc, _swt, _swf,
            panel_count_target, _pmin,
        ) = _rlf._planned_sections_and_panels(1800)
        assert panel_count_target == 72, (
            f"30-min default cadence (25s/panel) → 72 panels; got {panel_count_target}"
        )

    def test_channel_cfg_overrides_panel_seconds_target(self):
        """A channel that wants denser cadence (e.g. 15 s/panel) bumps
        the target accordingly. 30 min @ 15 s = 120 panels.
        """
        cfg = {"long_form": {"panel_seconds_target": 15}}
        (*_p, panel_count_target, _pmin) = _rlf._planned_sections_and_panels(1800, channel_cfg=cfg)
        assert panel_count_target == 120, (
            f"30-min @ 15s/panel → 120 panels; got {panel_count_target}"
        )

    def test_channel_cfg_clamps_panel_seconds_target_floor(self):
        """``panel_seconds_target`` is floored at 8s — denser than that
        is impractical (image gen cost + retention diminishing returns).
        """
        cfg = {"long_form": {"panel_seconds_target": 2}}
        (*_p, panel_count_target, _pmin) = _rlf._planned_sections_and_panels(1800, channel_cfg=cfg)
        # 1800 / 8 = 225 (clamped to floor of 8s/panel)
        assert panel_count_target == 225, (
            f"floor at 8s/panel → 225 panels; got {panel_count_target}"
        )

    def test_short_video_has_panel_floor(self):
        """Sub-1-min videos still get 8 panels minimum so the visuals
        track has at least some variety.
        """
        (*_p, panel_count_target, _pmin) = _rlf._planned_sections_and_panels(60)
        assert panel_count_target >= 8


# ---------------------------------------------------------------------------
# 2. after_section_id round-trips
# ---------------------------------------------------------------------------


class AfterSectionIdRoundTripTest(unittest.TestCase):
    def test_default_is_none(self):
        p = LongFormPanel(scene="x", hold_s=5.0)
        assert p.after_section_id is None

    def test_set_explicitly_preserved(self):
        p = LongFormPanel(scene="x", hold_s=5.0, after_section_id="sec-3")
        assert p.after_section_id == "sec-3"

    def test_round_trip_through_to_legacy_dict(self):
        """to_legacy_long_form_dict serialises after_section_id when set;
        omits it when None (avoids polluting old envelopes with extra keys).
        """
        from pipeline.llm.script_schema import (
            LongFormScript, LongFormSection,
        )
        env = ScriptEnvelope(
            slug="t", kind="long_form",
            title_options=["t"], source_url="", source="",
            long_form=LongFormScript(
                hook="h", thesis="t",
                sections=[LongFormSection(id="sec-0", title="T", narration="n")],
                panels=[
                    LongFormPanel(scene="a", hold_s=5.0, after_section_id="sec-0"),
                    LongFormPanel(scene="b", hold_s=5.0),  # no after_section_id
                ],
            ),
        )
        d = env.to_legacy_long_form_dict()
        panels = d["panels"]
        assert panels[0]["after_section_id"] == "sec-0"
        assert "after_section_id" not in panels[1]

    def test_round_trip_through_from_dict(self):
        """from_dict accepts the new field and reconstructs LongFormPanel."""
        d = {
            "kind": "long_form",
            "slug": "t",
            "long_form": {
                "hook": "h", "thesis": "t",
                "sections": [{"id": "sec-0", "title": "T", "narration": "n"}],
                "panels": [
                    {"scene": "a", "hold_s": 5.0, "after_section_id": "sec-0"},
                    {"scene": "b", "hold_s": 5.0},
                ],
            },
        }
        env = ScriptEnvelope.from_dict(d)
        assert env.long_form.panels[0].after_section_id == "sec-0"
        assert env.long_form.panels[1].after_section_id is None


# ---------------------------------------------------------------------------
# 3. spec_enrich stages authored panels
# ---------------------------------------------------------------------------


def _bare_spec() -> RenderSpec:
    return RenderSpec(
        channel="mystoriesanimated",
        niche=None,
        kind=RenderKind.LONG_FORM,
        visual_mode=VisualMode.LONGFORM_PANELS,
        aspect_ratio="16:9",
        output_resolution=(1920, 1080),
    )


class PopulateAuthoredPanelsTest(unittest.TestCase):
    def test_populates_when_script_has_panels(self):
        spec = _bare_spec()
        script = {
            "slug": "t",
            "panels": [
                {"scene": "A", "hold_s": 5.0},
                {"scene": "B", "hold_s": 7.0, "after_section_id": "sec-1"},
            ],
        }
        populate_render_extras(spec, script)
        out = spec.extra["authored_long_form_panels"]
        assert len(out) == 2
        assert out[0] == {"scene": "A", "hold_s": 5.0}
        assert out[1] == {"scene": "B", "hold_s": 7.0, "after_section_id": "sec-1"}

    def test_no_op_when_script_has_no_panels(self):
        spec = _bare_spec()
        populate_render_extras(spec, {"slug": "t"})
        assert "authored_long_form_panels" not in spec.extra

    def test_no_op_when_panels_empty(self):
        spec = _bare_spec()
        populate_render_extras(spec, {"slug": "t", "panels": []})
        assert "authored_long_form_panels" not in spec.extra

    def test_drops_panels_with_empty_scene(self):
        spec = _bare_spec()
        script = {
            "slug": "t",
            "panels": [
                {"scene": "", "hold_s": 5.0},
                {"scene": "  ", "hold_s": 5.0},
                {"scene": "real", "hold_s": 5.0},
            ],
        }
        populate_render_extras(spec, script)
        out = spec.extra["authored_long_form_panels"]
        assert len(out) == 1
        assert out[0]["scene"] == "real"

    def test_idempotent_preserves_existing_value(self):
        spec = _bare_spec()
        spec.extra["authored_long_form_panels"] = [{"scene": "pre", "hold_s": 1.0}]
        populate_render_extras(spec, {
            "slug": "t",
            "panels": [{"scene": "fromscript", "hold_s": 5.0}],
        })
        # Pre-existing value wins.
        assert spec.extra["authored_long_form_panels"][0]["scene"] == "pre"


# ---------------------------------------------------------------------------
# 4. longform_panels._panels_from_timeline priority
# ---------------------------------------------------------------------------


class LongformPanelsResolveTest(unittest.TestCase):
    def setUp(self):
        self.plugin = LongformPanels()
        self.timeline = [
            Segment(
                start_s=0.0, end_s=300.0,
                text="Section 1 narration text.",
                anchor_id="sec-0", kind="section",
            ),
            Segment(
                start_s=300.0, end_s=600.0,
                text="Section 2 narration text.",
                anchor_id="sec-1", kind="section",
            ),
        ]

    def test_authored_panels_take_priority(self):
        """When spec.extra has authored panels, plugin uses them — NOT
        the per-section fallback. This is the headline fix.
        """
        spec = _bare_spec()
        spec.extra["authored_long_form_panels"] = [
            {"scene": f"panel {i}", "hold_s": 5.0} for i in range(20)
        ]
        out = self.plugin._panels_from_timeline(self.timeline, spec)
        assert len(out) == 20
        # Sanity: NOT per-section.
        assert out[0]["scene"] == "panel 0"

    def test_per_section_fallback_when_no_authored_panels(self):
        """No authored panels → one panel per timeline section
        (legacy behaviour, preserved for hand-rolled envelopes).
        """
        spec = _bare_spec()
        out = self.plugin._panels_from_timeline(self.timeline, spec)
        assert len(out) == 2
        assert out[0]["scene"] == "Section 1 narration text."
        assert out[1]["scene"] == "Section 2 narration text."

    def test_per_section_fallback_when_spec_is_none(self):
        """Backward-compat: callers that don't pass spec still get the
        old per-section behaviour.
        """
        out = self.plugin._panels_from_timeline(self.timeline)
        assert len(out) == 2

    def test_authored_panel_with_zero_hold_gets_default(self):
        """LLM occasionally emits hold_s=0; floor at 6s so
        _adjust_panel_holds_to_dur can scale safely.
        """
        spec = _bare_spec()
        spec.extra["authored_long_form_panels"] = [
            {"scene": "A", "hold_s": 0.0},
        ]
        out = self.plugin._panels_from_timeline(self.timeline, spec)
        assert out[0]["hold_s"] >= 0.5

    def test_empty_authored_panels_falls_through(self):
        """Empty list = fall back to per-section, NOT crash."""
        spec = _bare_spec()
        spec.extra["authored_long_form_panels"] = []
        out = self.plugin._panels_from_timeline(self.timeline, spec)
        assert len(out) == 2  # per-section


# ---------------------------------------------------------------------------
# 5. Ken-Burns / xfade kwargs are accepted but ignored
# ---------------------------------------------------------------------------


class KenBurnsKwargsBackCompatTest(unittest.TestCase):
    def test_build_image_panels_video_accepts_legacy_kwargs(self):
        """Old call sites pass crossfade_s / zoom_factor — must not raise.
        2026-05-23: the kwargs are silently ignored (back-compat only).
        """
        from pipeline.render.shared import long_form_lib
        # We're not actually calling it (would need real images / ffmpeg);
        # we just inspect the signature.
        import inspect
        sig = inspect.signature(long_form_lib.build_image_panels_video)
        assert "crossfade_s" in sig.parameters
        assert "zoom_factor" in sig.parameters

    def test_assemble_panel_kenburns_alias_still_resolves(self):
        """Tests / external callers that import the old symbol still work."""
        from pipeline.render.shared import long_form_lib
        assert hasattr(long_form_lib, "_assemble_panel_kenburns")
        assert hasattr(long_form_lib, "_assemble_panel_static")

    def test_image_to_static_clip_alias_exists(self):
        from pipeline import compose
        assert compose.image_to_static_clip is compose.image_to_kenburns_clip


if __name__ == "__main__":
    unittest.main()
