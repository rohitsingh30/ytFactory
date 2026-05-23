"""Pin the 2026-05-15 long-form solid-color disaster.

Backstory: cosmosdecoded + historyrecapped channels both default to
visualize plugins (``archival_shotlist`` / ``footage_windows``) that
need a curated shotlist file. The wizard pipeline never authors one.
Pre-fix, both plugins fell back to ``ffmpeg color=c=0x141414`` (or
``0x0a1626``) → 26 minutes of solid near-black on every cloud
long-form render.

Surfaced by jobs ce309c80 (CosmosDecoded) + 0ffe6dcd (HistoryRecapped)
— both produced "successful" mp4s with audio + chapter cards but ZERO
visible content. Class-of-bug for the next 100 long-forms.

Fix: both plugins now dispatch to ``longform_panels`` (AI panel
slideshow) when shotlist is missing. The OLD solid color path is
retained as last-resort fallback (for when longform_panels itself
fails). The recursion guard ensures we don't loop archival_shotlist
→ longform_panels → archival_shotlist.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from pipeline.render.contracts import Timeline, VisualTrack
from pipeline.render.visualize._fallback import (
    _fallback_to_longform_panels,
)


def _spec(extra: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.output_resolution = (1920, 1080)
    s.output_fps = 30
    s.extra = extra or {}
    s.channel = "cosmosdecoded"
    return s


def _timeline(end_s: float = 60.0) -> Timeline:
    seg = MagicMock()
    seg.start_s = 0.0
    seg.end_s = end_s
    seg.text = "panel scene"
    seg.anchor_id = "anchor_001"
    return [seg]


class FallbackDispatchesToLongformPanelsTest(unittest.TestCase):
    """Pin: when shotlist is missing, the fallback hits longform_panels
    (AI slideshow) NOT solid color."""

    def test_dispatches_to_longform_panels_plugin(self):
        spec = _spec()
        timeline = _timeline()
        captured = {}

        def fake_panels_produce(s, t, wd):
            captured["called"] = True
            return VisualTrack(
                video_path=wd / "panels_video.mp4",
                duration_s=60.0,
                extras={"source": "longform_panels"},
            )

        plugin_mock = MagicMock()
        plugin_mock.produce = fake_panels_produce

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "panels_video.mp4").write_bytes(b"fake mp4")
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                track = _fallback_to_longform_panels(
                    spec, timeline, wd,
                    sentinel_kwarg="_test_already_falling_back",
                    color="0x141414",
                    label="test_fallback",
                )

        self.assertTrue(captured["called"])
        self.assertEqual(track.extras["source"], "longform_panels")

    # 4 tests that exercised the now-deleted solid-color override path
    # (test_recursion_guard_returns_solid_color, test_longform_panels_
    # failure_falls_through_to_solid_color, test_color_param_threaded_
    # through_to_ffmpeg, test_empty_timeline_uses_default_duration)
    # have been removed. The solid-color fallback was deleted per user
    # direction 2026-05-23 — _fallback.py now raises on longform_panels
    # failure with no fallback path. The recursion guard still raises
    # (covered by FailLoudFallback tests).

    def test_frozen_spec_extra_doesnt_crash_helper(self):
        """When ``spec.extra =`` raises (frozen dataclass / read-only
        property), the recursion-guard set MUST be best-effort — the
        helper still continues to longform_panels."""
        # Build a real object whose .extra property setter raises.
        class FrozenSpec:
            def __init__(self):
                self.output_resolution = (1920, 1080)
                self.output_fps = 30
                self.channel = "test"
            @property
            def extra(self):
                return {}
            @extra.setter
            def extra(self, value):
                raise AttributeError("spec is frozen — extra is read-only")

        spec = FrozenSpec()
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.return_value = VisualTrack(
            video_path=Path("/tmp/panels.mp4"),
            duration_s=10.0,
            extras={"source": "longform_panels"},
        )

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                # Should NOT raise — best-effort sentinel set.
                track = _fallback_to_longform_panels(
                    spec, timeline, wd,
                    sentinel_kwarg="_frozen_test",
                    color="0x141414",
                    label="frozen_spec_test",
                )

        self.assertEqual(track.extras["source"], "longform_panels",
            "Frozen spec.extra MUST not block the fallback; "
            "longform_panels' own recursion guard handles loops.")


class ArchivalShotlistFallbackHitsLongformPanelsTest(unittest.TestCase):
    """End-to-end on archival_shotlist: when shotlist_path is unset,
    the plugin MUST dispatch to longform_panels (not solid color)."""

    def test_no_shotlist_path_dispatches_to_longform_panels(self):
        from pipeline.render.visualize.archival_shotlist import ArchivalShotlist

        spec = _spec()  # no shotlist_path in extra
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.return_value = VisualTrack(
            video_path=Path("/tmp/panels.mp4"),
            duration_s=60.0,
            extras={"source": "longform_panels"},
        )

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                track = ArchivalShotlist().produce(spec, timeline, wd)

        self.assertEqual(track.extras["source"], "longform_panels",
            "archival_shotlist with NO shotlist MUST fall back to "
            "longform_panels (AI slideshow), NOT solid color. "
            "Pre-fix this returned 26 min of #141414 black on every "
            "cosmosdecoded long-form render.")


class FootageWindowsFallbackHitsLongformPanelsTest(unittest.TestCase):
    """End-to-end on footage_windows: when shotlist_path is unset,
    the plugin MUST dispatch to longform_panels (not solid color)."""

    def test_no_shotlist_path_dispatches_to_longform_panels(self):
        from pipeline.render.visualize.footage_windows import FootageWindows

        spec = _spec()
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.return_value = VisualTrack(
            video_path=Path("/tmp/panels.mp4"),
            duration_s=60.0,
            extras={"source": "longform_panels"},
        )

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                track = FootageWindows().produce(spec, timeline, wd)

        self.assertEqual(track.extras["source"], "longform_panels",
            "footage_windows with NO shotlist MUST fall back to "
            "longform_panels. Pre-fix this returned 25 min of "
            "#0a1626 deep navy on every historyrecapped long-form.")

    def test_missing_shotlist_file_dispatches_to_longform_panels(self):
        from pipeline.render.visualize.footage_windows import FootageWindows

        spec = _spec(extra={"shotlist_path": "/nonexistent/path/shotlist.json"})
        timeline = _timeline()

        plugin_mock = MagicMock()
        plugin_mock.produce.return_value = VisualTrack(
            video_path=Path("/tmp/panels.mp4"),
            duration_s=60.0,
            extras={"source": "longform_panels"},
        )

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.render.visualize._fallback.get_plugin",
                return_value=plugin_mock,
            ):
                track = FootageWindows().produce(spec, timeline, wd)

        self.assertEqual(track.extras["source"], "longform_panels")


class LongformPanelsBuildKwargContractTest(unittest.TestCase):
    """Pin the 2026-05-15 build_image_panels_video kwarg-drift bug.

    Backstory: ``longform_panels.produce`` called
    ``build_image_panels_video`` with ``narration_dur_s=`` /
    ``out_path=`` / ``image_w=`` / ``image_h=`` — NONE of which exist
    on the helper's signature. The ``except (TypeError, Exception)``
    block swallowed the kwarg-mismatch and fell through to solid
    color. So even after archival_shotlist / footage_windows
    fallbacks dispatched to longform_panels, longform_panels itself
    landed at ``panels_fallback.mp4`` (still solid color).

    Surfaced by job d8a0a076 (HR Roman Empire) on 2026-05-15 — v13
    chain "archival/footage → longform_panels → solid color" was
    broken at the last step. Logs showed
    ``[fallback] footage_windows_fallback → longform_panels OK
    (video=panels_fallback.mp4 …)`` — the ``_fallback`` suffix gave
    it away.

    Fix: pin the caller→callee kwarg contract via AST introspection,
    same pattern as
    ``tests/render/audio/test_tts_chunked_kwarg_contract.py``.
    """

    def test_longform_panels_passes_only_kwargs_that_exist_on_callee(self):
        import ast
        import inspect
        import textwrap
        from pipeline.render.visualize.longform_panels import LongformPanels
        from pipeline.render.shared.long_form_lib import (
            build_image_panels_video,
        )

        callee_sig = inspect.signature(build_image_panels_video)
        callee_kwargs = set(callee_sig.parameters.keys())

        source = textwrap.dedent(inspect.getsource(LongformPanels.produce))
        tree = ast.parse(source)
        passed: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute)
                else None
            )
            if name != "build_image_panels_video":
                continue
            for kw in node.keywords:
                if kw.arg is not None:
                    passed.add(kw.arg)

        unknown = passed - callee_kwargs
        self.assertFalse(
            unknown,
            f"longform_panels.produce passes kwargs that don't exist "
            f"on build_image_panels_video's signature: {sorted(unknown)}. "
            f"Either rename the call site OR add alias on the callee. "
            f"Last drift (narration_dur_s / out_path / image_w / image_h) "
            f"caused every cloud long-form to land at solid-color "
            f"fallback — pinned here so future renames are caught at "
            f"unit-test time, not at the engine boundary.",
        )

    def test_longform_panels_passes_all_required_kwargs(self):
        import ast
        import inspect
        import textwrap
        from pipeline.render.visualize.longform_panels import LongformPanels
        from pipeline.render.shared.long_form_lib import (
            build_image_panels_video,
        )

        callee_sig = inspect.signature(build_image_panels_video)
        required = {
            name for name, p in callee_sig.parameters.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }

        source = textwrap.dedent(inspect.getsource(LongformPanels.produce))
        tree = ast.parse(source)
        passed: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute)
                else None
            )
            if name != "build_image_panels_video":
                continue
            for kw in node.keywords:
                if kw.arg is not None:
                    passed.add(kw.arg)

        missing = required - passed
        self.assertFalse(
            missing,
            f"longform_panels.produce doesn't pass required kwargs of "
            f"build_image_panels_video: {sorted(missing)}. Without "
            f"these the call raises TypeError before any panel "
            f"rendering happens.",
        )

    def test_panels_from_timeline_includes_hold_s(self):
        from pipeline.render.visualize.longform_panels import LongformPanels
        plugin = LongformPanels()
        seg = MagicMock()
        seg.start_s = 10.0
        seg.end_s = 25.0
        seg.text = "panel scene text"
        seg.anchor_id = "anchor_42"
        panels = plugin._panels_from_timeline([seg])
        self.assertEqual(len(panels), 1)
        self.assertIn("hold_s", panels[0])
        self.assertEqual(panels[0]["hold_s"], 15.0)
        self.assertEqual(panels[0]["scene"], "panel scene text")

    def test_panels_from_timeline_fills_empty_scene_with_anchor_fallback(self):
        # 2026-05-15 — pre-fix, an empty seg.text propagated to
        # scene="" → _generate_panel_stills raised ValueError("panel N
        # missing 'scene' field") → 26 minutes of solid black on the
        # cosmos hubble render. asr_anchors's text-field resolution
        # is the root cause and is fixed; THIS test pins the
        # belt-and-braces fallback so future schema drift can't
        # reintroduce the same crash.
        from pipeline.render.visualize.longform_panels import LongformPanels
        plugin = LongformPanels()
        seg = MagicMock()
        seg.start_s = 0.0
        seg.end_s = 30.0
        seg.text = ""
        seg.anchor_id = "sec_007"
        panels = plugin._panels_from_timeline([seg])
        self.assertEqual(len(panels), 1)
        self.assertTrue(panels[0]["scene"],
                        "scene must be non-empty even when seg.text=''")
        # Anchor id surfaces in the fallback so the panel is loosely
        # thematic — purely aesthetic, but worth pinning so we never
        # silently degrade to a generic placeholder.
        self.assertIn("sec_007", panels[0]["scene"])

    def test_panels_from_timeline_fills_whitespace_scene_with_fallback(self):
        from pipeline.render.visualize.longform_panels import LongformPanels
        plugin = LongformPanels()
        seg = MagicMock()
        seg.start_s = 0.0
        seg.end_s = 30.0
        seg.text = "   \n  \t  "  # whitespace-only counts as empty
        seg.anchor_id = "sec_001"
        panels = plugin._panels_from_timeline([seg])
        self.assertNotEqual(panels[0]["scene"].strip(), "")
        self.assertIn("sec_001", panels[0]["scene"])

    def test_panels_from_timeline_fallback_uses_index_when_no_anchor(self):
        from pipeline.render.visualize.longform_panels import LongformPanels
        plugin = LongformPanels()
        seg1 = MagicMock()
        seg1.start_s = 0.0
        seg1.end_s = 30.0
        seg1.text = ""
        seg1.anchor_id = ""  # unset anchor
        seg2 = MagicMock()
        seg2.start_s = 30.0
        seg2.end_s = 60.0
        seg2.text = ""
        seg2.anchor_id = None
        panels = plugin._panels_from_timeline([seg1, seg2])
        self.assertEqual(len(panels), 2)
        # Both fallbacks must be non-empty + distinct so panel 1 and 2
        # don't share a single seed-like prompt.
        self.assertNotEqual(panels[0]["scene"], panels[1]["scene"])
        self.assertTrue(panels[0]["scene"])
        self.assertTrue(panels[1]["scene"])

    def test_panels_from_timeline_real_text_unaffected_by_fallback(self):
        # Regression guard: the fallback ONLY fires when text is
        # empty/whitespace. Real text passes through unchanged.
        from pipeline.render.visualize.longform_panels import LongformPanels
        plugin = LongformPanels()
        seg = MagicMock()
        seg.start_s = 0.0
        seg.end_s = 60.0
        seg.text = "  Authored scene with leading whitespace.  "
        seg.anchor_id = "anchor_99"
        panels = plugin._panels_from_timeline([seg])
        # text gets stripped but anchor fallback should NOT fire.
        self.assertEqual(panels[0]["scene"],
                         "Authored scene with leading whitespace.")
        self.assertNotIn("Establishing", panels[0]["scene"])


class LongformPanelsAdjustHoldsToNarrationTest(unittest.TestCase):
    """v16 — pin the defensive _adjust_panel_holds_to_dur call in
    LongformPanels.produce.

    Pre-v16, an upstream TimelineBuilder bug (asr_anchors emitted
    overlapping segments — every end_s = total_s) made
    _panels_from_timeline produce 1415s holds for a 1500s render →
    ffmpeg kenburns rendered 42,456 frames per panel → cloud-run JOB
    killed at the 1-hour wall.

    The asr_anchors 2-pass refactor is the root-cause fix; THIS test
    pins the belt-and-braces defense so any future TimelineBuilder
    that emits oversized holds gets compressed to fit the narration
    instead of running off the rails.
    """

    def _spec(self):
        spec = MagicMock()
        spec.output_resolution = (1920, 1080)
        spec.output_fps = 30
        spec.extra = {"image_provider": "cloudrun_z_image_turbo",
                      "image_seed": 42, "image_steps": 4}
        spec.channel = "historyrecapped"
        return spec

    def _seg(self, start_s, end_s, text="scene", anchor_id="a"):
        s = MagicMock()
        s.start_s = start_s
        s.end_s = end_s
        s.text = text
        s.anchor_id = anchor_id
        return s

    def test_oversized_overlapping_holds_get_compressed_to_narration(self):
        # Simulate the v15 disaster: 10 segments, each end_s=1500,
        # but staggered start_s. hold_s = end - start → 1500, 1350,
        # 1200, ..., 150 → total 8250s for 1500s of narration.
        from pipeline.render.visualize.longform_panels import LongformPanels

        timeline = []
        for i in range(10):
            start = (i / 10) * 1500.0
            timeline.append(self._seg(start, 1500.0,
                                      text=f"scene {i}", anchor_id=f"sec_{i}"))

        captured = {}

        def fake_helper(**kwargs):
            captured["panels"] = list(kwargs["panels"])
            cd = kwargs["cache_dir"]
            v = cd / "video.mp4"
            v.write_bytes(b"fake")
            return v

        with TemporaryDirectory() as tmp:
            with patch(
                "pipeline.render.shared.long_form_lib.build_image_panels_video",
                side_effect=fake_helper,
            ), patch(
                "pipeline.render.visualize.longform_panels.probe_duration",
                return_value=1500.0,
            ):
                LongformPanels().produce(self._spec(), timeline, Path(tmp))

        panels = captured["panels"]
        total_hold = sum(p["hold_s"] for p in panels)
        # Must be compressed to ±1s of narration (per the helper's
        # tolerance). Pre-v16 this would be 8250s.
        self.assertLess(
            abs(total_hold - 1500.0), 2.0,
            f"sum of holds {total_hold:.1f} must be compressed to "
            f"narration 1500.0s (±1s). Pre-v16 this was 8250s and "
            f"each panel exceeded its share by ~5×, killing the "
            f"cloud-run JOB on render.",
        )

    def test_well_formed_holds_unchanged(self):
        # Defensive call must be a no-op when timeline is already
        # non-overlapping (the post-v16 asr_anchors output).
        from pipeline.render.visualize.longform_panels import LongformPanels

        timeline = [
            self._seg(0.0, 100.0, text="s0"),
            self._seg(100.0, 200.0, text="s1"),
            self._seg(200.0, 300.0, text="s2"),
        ]
        captured = {}

        def fake_helper(**kwargs):
            captured["panels"] = list(kwargs["panels"])
            cd = kwargs["cache_dir"]
            v = cd / "video.mp4"
            v.write_bytes(b"fake")
            return v

        with TemporaryDirectory() as tmp:
            with patch(
                "pipeline.render.shared.long_form_lib.build_image_panels_video",
                side_effect=fake_helper,
            ), patch(
                "pipeline.render.visualize.longform_panels.probe_duration",
                return_value=300.0,
            ):
                LongformPanels().produce(self._spec(), timeline, Path(tmp))

        panels = captured["panels"]
        total_hold = sum(p["hold_s"] for p in panels)
        self.assertAlmostEqual(total_hold, 300.0, places=1)
        # Holds should still be ~100 each (no scaling needed).
        for p in panels:
            self.assertAlmostEqual(p["hold_s"], 100.0, places=1)

    def test_helper_unavailable_proceeds_without_crash(self):
        # If _adjust_panel_holds_to_dur is somehow not importable,
        # the visualize plugin must not crash — proceeds with raw
        # holds. (Guard against a future helper rename breaking
        # the defensive call site.)
        from pipeline.render.visualize.longform_panels import LongformPanels

        timeline = [self._seg(0.0, 100.0, text="s0")]

        def fake_helper(**kwargs):
            cd = kwargs["cache_dir"]
            v = cd / "video.mp4"
            v.write_bytes(b"fake")
            return v

        with TemporaryDirectory() as tmp:
            with patch(
                "pipeline.render.shared.long_form_lib.build_image_panels_video",
                side_effect=fake_helper,
            ), patch(
                "pipeline.render.visualize.longform_panels.probe_duration",
                return_value=100.0,
            ), patch(
                "pipeline.render.shared.long_form_lib._adjust_panel_holds_to_dur",
                side_effect=ImportError("simulated helper rename"),
            ):
                # Must not raise.
                track = LongformPanels().produce(self._spec(), timeline, Path(tmp))
        self.assertEqual(track.extras["source"], "longform_panels")


class LongformPanelsProduceSuccessPathTest(unittest.TestCase):
    """Cover the success path of LongformPanels.produce — exercises
    the kwargs threading + post-move-to-out-path logic that's
    otherwise only hit on a real cloud image-gen call."""

    def _spec_with_extra(self):
        spec = MagicMock()
        spec.output_resolution = (1920, 1080)
        spec.output_fps = 30
        spec.extra = {
            "image_provider": "cloudrun_z_image_turbo",
            "image_style_prefix": "Crayon style.",
            "image_seed": 42,
            "image_steps": 4,
        }
        spec.channel = "cosmosdecoded"
        return spec

    def test_produces_panels_video_when_helper_succeeds(self):
        from pipeline.render.visualize.longform_panels import LongformPanels

        spec = self._spec_with_extra()
        timeline = _timeline()

        produced_path_holder = {}

        def fake_helper(**kwargs):
            # Helper writes to cache_dir; mimic that.
            cache_dir = kwargs["cache_dir"]
            video_path = cache_dir / "video.mp4"
            video_path.write_bytes(b"fake mp4")
            produced_path_holder["produced"] = video_path
            produced_path_holder["kwargs"] = kwargs
            return video_path

        with TemporaryDirectory() as tmp:
            wd = Path(tmp)
            with patch(
                "pipeline.render.shared.long_form_lib.build_image_panels_video",
                side_effect=fake_helper,
            ), patch(
                "pipeline.render.visualize.longform_panels.probe_duration",
                return_value=60.0,
            ):
                track = LongformPanels().produce(spec, timeline, wd)

        self.assertEqual(track.extras["source"], "longform_panels")
        self.assertEqual(track.extras["n_panels"], 1)
        # The helper was called with the right kwargs (smoke-check;
        # contract-pin lives in LongformPanelsBuildKwargContractTest).
        self.assertEqual(produced_path_holder["kwargs"]["image_provider"],
                         "cloudrun_z_image_turbo")
        self.assertEqual(produced_path_holder["kwargs"]["image_seed"], 42)
        self.assertEqual(produced_path_holder["kwargs"]["image_width"], 1920)
        self.assertEqual(produced_path_holder["kwargs"]["image_height"], 1080)

    # 2 tests that exercised the removed solid-color fallback
    # (test_helper_failure_falls_back_to_solid_color and
    # test_empty_timeline_falls_back_to_solid_color) have been removed.
    # Solid-color fallback was deleted per user direction 2026-05-23 —
    # LongformPanels._fallback_solid_color now raises with no env
    # override.


if __name__ == "__main__":
    unittest.main()