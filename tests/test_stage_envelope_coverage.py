"""Regression tests for stage envelope coverage (Fix #7, 2026-05-24).

Background — see
``data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.bugs.md``
(CLASS-OF-BUG #D) and
``.claude/skills/diagnose-render/learnings/845bdb0df20e4ba3885ca33c7749e74d.md``
(Finding 4).

The 845bdb0d long-form render emitted exactly ONE ``stage.start`` and
ONE ``stage.end`` event — both for ``stage=upload``. The other six
pipeline stages (rewrite / cast / images / tts / asr / compose)
completed but emitted no envelope events because:

* The long-form dispatch path in ``_main_from_firestore`` collapses
  rewrite/tts/asr/images/compose into one ``_video.render_long_form``
  call. The ``@stage_envelope``-decorated handlers
  (``_stage_rewrite_real``, ``_stage_render_real``, etc.) are never
  invoked for long-form jobs because the ``lf_done`` short-circuit at
  the bottom of the dispatch loop skips every stage except ``upload``.

The fix emits ``stage.start`` / ``stage.end`` (or ``stage.failed``)
events from the long-form bypass section for every pipeline stage,
matching the metadata shape the worker's existing ``@stage_envelope``
decorator produces.

This test verifies the contract directly: the helper functions added
in 2026-05-24's entrypoint (``_stage_envelope_events`` context manager
on the dispatch side and the inline ``_lf_emit_start`` / ``_lf_emit_end``
closures in the long-form branch) emit the right shape.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "render_worker_v2_entrypoint_for_stage_envelope_tests",
        ENTRYPOINT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Stages that MUST emit a ``stage.start`` + ``stage.end`` event for
# every long-form render. ``cast`` and ``asr`` (when skipped) emit
# their pair with ``skipped=True`` metadata.
LONG_FORM_ENVELOPED_STAGES = (
    "rewrite",
    "cast",      # skipped — long-form has no cast stage
    "tts",
    "asr",       # skipped on the default authored-alignment path
    "images",
    "compose",
    "upload",
)

# F33 (2026-05-24): the SHORT-form dispatch path collapses the same
# substages into one ``_stage_render_real`` call. Pre-fix that call
# carried ``@stage_envelope("compose", ...)`` — operators reading the
# event stream saw exactly ONE ``stage.start`` / ``stage.end`` pair
# (labelled "compose") for 4 substages of work. Now: the outer
# envelope is renamed to ``render_substages`` and the dispatch loop's
# ``_compose_progress`` closure emits per-substage envelopes from the
# progress signal. Final cleanup emits a degenerate start+end pair
# (``skipped=True``) for any substage we never observed.
SHORT_FORM_ENVELOPED_SUBSTAGES = (
    "tts",
    "asr",
    "images",
    "compose",
)


class StageEnvelopeHelperShape(unittest.TestCase):
    """The dispatch-loop helper ``_stage_envelope_events`` must emit
    ``stage.start`` + ``stage.end`` (or ``stage.failed``) with the
    same metadata shape the worker's ``@stage_envelope`` decorator
    produces. This pins the contract so a regression (rename, dropped
    field) shows up immediately.
    """

    def setUp(self) -> None:
        # Patch ``_safe_track_event`` so we capture the emit args without
        # bouncing through OTel.
        self.entrypoint = _load_entrypoint()
        self.captured: list[dict] = []

        original = self.entrypoint._safe_track_event

        def _capture(event: str, **kwargs) -> None:
            self.captured.append({"event": event, **kwargs})

        self._orig = original
        self.entrypoint._safe_track_event = _capture

    def tearDown(self) -> None:
        self.entrypoint._safe_track_event = self._orig

    def test_helper_emits_start_then_end_on_success(self) -> None:
        with self.entrypoint._stage_envelope_events(
            "rewrite",
            job={
                "_slug": "test-slug",
                "proposal": {"channel": "mystoriesanimated",
                             "format": "nosleep"},
                "mode": "real",
            },
        ):
            pass
        evts = [e["event"] for e in self.captured]
        self.assertEqual(
            evts, ["stage.start", "stage.end"],
            f"helper must emit start→end on success; got {evts}",
        )
        start, end = self.captured
        self.assertEqual(start["metadata"]["stage"], "rewrite")
        self.assertEqual(start["metadata"]["slug"], "test-slug")
        self.assertEqual(start["metadata"]["channel"], "mystoriesanimated")
        self.assertEqual(start["metadata"]["variant"], "nosleep")
        self.assertEqual(end["metadata"]["stage"], "rewrite")
        # Duration must be populated on stage.end.
        self.assertIn("duration_ms", end)
        self.assertIsNotNone(end["duration_ms"])

    def test_helper_emits_start_then_failed_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.entrypoint._stage_envelope_events(
                "compose", job={"_slug": "boom"},
            ):
                raise RuntimeError("kaboom")
        evts = [e["event"] for e in self.captured]
        self.assertEqual(
            evts, ["stage.start", "stage.failed"],
            f"helper must emit start→failed on exception; got {evts}",
        )
        failed = self.captured[-1]
        self.assertEqual(failed["metadata"]["stage"], "compose")
        self.assertEqual(failed["metadata"]["error_type"], "RuntimeError")
        self.assertIn("kaboom", failed["metadata"]["error"])
        self.assertIn("traceback", failed["metadata"])
        self.assertFalse(failed.get("success", True))


class LongFormEnvelopeContract(unittest.TestCase):
    """The long-form bypass section of ``_main_from_firestore`` must
    emit envelope events for every stage in :data:`LONG_FORM_ENVELOPED_STAGES`.

    We don't run the full ``_main_from_firestore`` here (it requires
    Firestore + GCS + a real renderer subprocess). Instead, we
    static-analyse the entrypoint source to confirm the emit sites
    exist for every required stage. This catches the class of
    regression where someone refactors the long-form branch and drops
    the envelope emission for a substage.
    """

    def setUp(self) -> None:
        self.entrypoint_source = ENTRYPOINT_PATH.read_text(encoding="utf-8")

    def test_long_form_branch_emits_start_for_every_stage(self) -> None:
        # Each enveloped stage must have at least one ``_lf_emit_start("<stage>"``
        # call inside the long-form branch — or the equivalent
        # decorated ``@stage_envelope("<stage>", ...)`` for the
        # upload stage which still runs through the dispatch loop.
        # We use a regex that tolerates the multi-line call formatting
        # produced by black/ruff (e.g. ``_lf_emit_start(\n    "asr",`` —
        # the ``"asr"`` doesn't appear on the same line as the function
        # name when the call has multiple kwargs).
        import re
        missing: list[str] = []
        for stage in LONG_FORM_ENVELOPED_STAGES:
            if stage == "upload":
                # upload still goes through the decorated handler.
                if f'@stage_envelope("{stage}"' not in self.entrypoint_source:
                    missing.append(stage)
            else:
                pat = re.compile(
                    rf'_lf_emit_start\(\s*"{re.escape(stage)}"',
                    re.DOTALL,
                )
                if not pat.search(self.entrypoint_source):
                    missing.append(stage)
        self.assertEqual(
            missing, [],
            f"Long-form envelope coverage gap: no stage.start emit "
            f"site found for {missing}. See Fix #7 (2026-05-24) in "
            f"cloud/render-worker-v2/entrypoint.py — every stage in "
            f"LONG_FORM_ENVELOPED_STAGES must call _lf_emit_start "
            f"(or be decorated with @stage_envelope for upload).",
        )

    def test_long_form_branch_emits_end_for_every_stage(self) -> None:
        import re
        missing: list[str] = []
        for stage in LONG_FORM_ENVELOPED_STAGES:
            if stage == "upload":
                continue  # decorated; emits via the decorator itself
            pat = re.compile(
                rf'_lf_emit_end\(\s*"{re.escape(stage)}"',
                re.DOTALL,
            )
            if not pat.search(self.entrypoint_source):
                missing.append(stage)
        self.assertEqual(
            missing, [],
            f"Long-form envelope coverage gap: no stage.end emit "
            f"site found for {missing}. See Fix #7 (2026-05-24).",
        )

    def test_long_form_branch_emits_failed_on_render_exception(self) -> None:
        # The fix's try/except around ``_video.render_long_form`` must
        # emit ``stage.failed`` (via ``_lf_emit_end(..., success=False)``)
        # for the in-flight substages — otherwise a long-form render
        # that dies mid-pipeline ships no failure signal in events.
        self.assertIn(
            "success=False", self.entrypoint_source,
            "Long-form branch must emit stage.failed (success=False) "
            "on exception. See Fix #7 (2026-05-24).",
        )
        self.assertIn(
            "_lf_emit_end(\"tts\", success=False",
            self.entrypoint_source,
            "Long-form tts failure path must emit stage.failed via "
            "_lf_emit_end. See Fix #7 (2026-05-24).",
        )


class ShortFormEnvelopeContract(unittest.TestCase):
    """F33 (2026-05-24) — the short-form dispatch must emit per-substage
    envelope events, not a single umbrella "compose" pair.

    Pre-fix ``_stage_render_real`` carried ``@stage_envelope("compose",
    ...)``; a short-form render produced ONE ``stage.start`` and ONE
    ``stage.end`` for 4 substages. The fix:

    1. Renames the decorator to ``@stage_envelope("render_substages",
       ...)`` — an honest umbrella name (not "compose", which is one
       of the 4 substages).
    2. In the dispatch loop's ``_compose_progress`` closure, emit
       ``stage.start`` the first time we see a substage, and
       ``stage.end`` for every prior substage we transition off.
    3. Final cleanup loop emits ``stage.end`` for in-flight pills and
       a degenerate ``stage.start + stage.end`` (``skipped=True``) for
       substages we never observed.
    4. Exception path emits ``stage.failed`` for whichever substages
       were in flight.

    We can't run the dispatch loop without Firestore + a real
    renderer, so we static-analyse for the contract markers.
    """

    def setUp(self) -> None:
        self.entrypoint_source = ENTRYPOINT_PATH.read_text(encoding="utf-8")

    def test_outer_decorator_renamed_from_compose(self) -> None:
        """The pre-fix ``@stage_envelope("compose", ...)`` lied — it
        was actually wrapping all 4 substages. Must be renamed to
        ``render_substages``."""
        self.assertIn(
            '@stage_envelope("render_substages",',
            self.entrypoint_source,
            "F33 regression: short-form outer decorator must be "
            '@stage_envelope("render_substages", ...) — not "compose".',
        )

    def test_short_form_emits_per_substage_envelopes(self) -> None:
        """Each of tts/asr/images/compose must have a ``_sf_emit_start``
        call site in the short-form dispatch block."""
        import re
        # We use a permissive regex: the closure either takes the
        # substage name as a string literal first arg, or builds it
        # in a loop. Both patterns count.
        loop_pattern = re.compile(
            r"for\s+\w+\s+in\s+_RENDERER_SUBSTAGES.*?_sf_emit_start",
            re.DOTALL,
        )
        has_loop_emit = bool(loop_pattern.search(self.entrypoint_source))

        has_direct_emit = "_sf_emit_start(stage" in self.entrypoint_source

        self.assertTrue(
            has_loop_emit or has_direct_emit,
            "F33 regression: short-form dispatch must emit "
            "_sf_emit_start for each substage (either in a "
            "_RENDERER_SUBSTAGES loop or via the progress callback).",
        )

    def test_short_form_emits_failed_on_render_exception(self) -> None:
        """The try/except around ``_stage_render_real`` must emit
        ``stage.failed`` (success=False) for in-flight substages."""
        # Check that the short-form dispatch wraps the call in try/except
        # and emits success=False via _sf_emit_end.
        self.assertIn(
            "_sf_emit_end(sub, success=False",
            self.entrypoint_source,
            "F33 regression: short-form exception handler must emit "
            "stage.failed (success=False) for in-flight substages.",
        )
        # And the wrapping try/except must exist.
        self.assertIn(
            "try:\n                    _stage_render_real(",
            self.entrypoint_source,
            "F33 regression: short-form dispatch must wrap "
            "_stage_render_real in try/except to catch BaseException.",
        )

    def test_short_form_emits_skipped_pair_for_never_observed_substages(self) -> None:
        """Final cleanup must emit a degenerate ``stage.start`` +
        ``stage.end`` with ``skipped=True`` for substages we never saw
        — so the event stream carries a complete substage set even
        when the progress signal was lossy."""
        # The skip-reason string is the explicit marker the fix uses.
        self.assertIn(
            '"no progress signal observed"',
            self.entrypoint_source,
            "F33 regression: cleanup loop must emit start+end with "
            "skipped=True for never-observed substages. See "
            "_compose_progress final cleanup loop.",
        )


class ArtifactKindEmitContract(unittest.TestCase):
    """F32 (2026-05-24) — every kind in KNOWN_KINDS either has an
    emit site or is documented as ``future`` in docs/render_telemetry.md.

    The five kinds promised by the docs but missing emit sites pre-fix:
    prompts_raw, asr_alignment, timeline, music, compose. Of those:

    * prompts_raw → now emitted from _author_prompts_for_engine
    * compose    → now emitted from _stage_render_real post-mp4
    * asr_alignment, timeline, music → documented as ``future`` in
      docs/render_telemetry.md § 3
    """

    def setUp(self) -> None:
        self.entrypoint_source = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        self.docs_source = (
            REPO_ROOT / "docs" / "render_telemetry.md"
        ).read_text(encoding="utf-8")

    def test_prompts_raw_emit_site_exists(self) -> None:
        self.assertIn(
            'kind="prompts_raw"',
            self.entrypoint_source,
            "F32 regression: prompts_raw artifact has no emit site. "
            "See _author_prompts_for_engine.",
        )

    def test_compose_artifact_emit_site_exists(self) -> None:
        self.assertIn(
            'kind="compose"',
            self.entrypoint_source,
            "F32 regression: compose artifact has no emit site. "
            "See _stage_render_real post-mp4 block.",
        )

    def test_unemitted_kinds_documented_as_future(self) -> None:
        """asr_alignment / timeline / music are reserved in
        KNOWN_KINDS but have no emit site. docs/render_telemetry.md
        must label them as future / planned so an operator doesn't
        ``gsutil cat`` expecting a file."""
        self.assertIn(
            "Future / planned kinds",
            self.docs_source,
            "F32 regression: docs/render_telemetry.md must have a "
            "'Future / planned kinds' section listing the unemitted "
            "registry kinds.",
        )
        for kind in ("asr_alignment", "timeline", "music"):
            self.assertIn(
                f"`{kind}`",
                self.docs_source,
                f"F32 regression: kind '{kind}' not documented in "
                f"docs/render_telemetry.md",
            )


if __name__ == "__main__":
    unittest.main()
