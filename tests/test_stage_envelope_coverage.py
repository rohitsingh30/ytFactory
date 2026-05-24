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


if __name__ == "__main__":
    unittest.main()
