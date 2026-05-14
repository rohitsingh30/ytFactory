"""Tests for cloud/render-worker-v2/entrypoint.py:_main_from_firestore.

Pinning the bounded retry on initial Firestore lookup (Tier 0 Batch B,
2026-05-14). Telemetry: TEL-FS-04 = 130 of 266 failed jobs (50%) in
the last 30 days had error="job doc not found" — the worker queried
Firestore faster than the cross-region write from the dispatcher
propagated. Pre-fix: single attempt, no retry. Post-fix: 3 attempts
with 1s backoff.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "render_worker_v2_entrypoint_for_firestore_retry_tests",
        ENTRYPOINT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestFirestoreLookupRetry(unittest.TestCase):
    """Bounded retry on initial Firestore lookup."""

    def setUp(self) -> None:
        self.ep = _load_entrypoint()

    def _mk_snap(self, exists: bool, data: dict | None = None):
        snap = mock.MagicMock()
        snap.exists = exists
        snap.to_dict.return_value = data or {}
        return snap

    def test_retries_3x_then_fails_when_doc_never_appears(self):
        """If the doc never appears, fail with a clear error."""
        ref = mock.MagicMock()
        ref.get.return_value = self._mk_snap(exists=False)
        with mock.patch.object(self.ep, "_job_ref", return_value=ref), \
             mock.patch.object(self.ep, "_update_job") as m_update, \
             mock.patch.object(self.ep.time, "sleep") as m_sleep:
            rc = self.ep._main_from_firestore("missing-job-id")
        self.assertEqual(rc, 1)
        # 3 lookup attempts.
        self.assertEqual(ref.get.call_count, 3)
        # 2 sleeps with the lookup-retry argument 1.0 (between the 3 attempts).
        # Other sleep calls in the worker may exist but we count ours specifically.
        lookup_sleeps = [c for c in m_sleep.call_args_list if c.args == (1.0,)]
        self.assertEqual(len(lookup_sleeps), 2,
                         f"expected 2 sleep(1.0) calls; saw {m_sleep.call_args_list}")
        # Update writes failure with a typed message.
        m_update.assert_called_once()
        kwargs = m_update.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["stage"], "bootstrap")
        self.assertIn("not found after 3 attempts", kwargs["error"])

    def test_succeeds_on_first_attempt_when_doc_exists(self):
        """Happy path: no retries, lookup returns immediately on first try.

        We verify by call_count of the Firestore ref.get() — that's the
        only signal that doesn't depend on what the rest of the worker does
        after the lookup returns.
        """
        ref = mock.MagicMock()
        ref.get.return_value = self._mk_snap(
            exists=True,
            data={"channel": "mystoriesanimated", "topic": "x",
                  "proposal": {"channel": "mystoriesanimated", "topic": "x"}},
        )
        with mock.patch.object(self.ep, "_job_ref", return_value=ref), \
             mock.patch.object(self.ep, "_update_job"), \
             mock.patch.object(self.ep.time, "sleep"), \
             mock.patch.object(self.ep, "_stage_render_real",
                               side_effect=Exception("bail-after-lookup")):
            try:
                self.ep._main_from_firestore("existing-job")
            except Exception:
                pass
        # Single lookup — no retry path was triggered.
        self.assertEqual(ref.get.call_count, 1)

    def test_succeeds_on_second_attempt_after_propagation_delay(self):
        """The race we're fixing: doc not visible at t=0, visible at t=1s."""
        ref = mock.MagicMock()
        ref.get.side_effect = [
            self._mk_snap(exists=False),  # attempt 1: not yet propagated
            self._mk_snap(                # attempt 2: visible
                exists=True,
                data={"channel": "x", "topic": "t", "proposal": {}},
            ),
        ]
        with mock.patch.object(self.ep, "_job_ref", return_value=ref), \
             mock.patch.object(self.ep, "_update_job"), \
             mock.patch.object(self.ep.time, "sleep") as m_sleep, \
             mock.patch.object(self.ep, "_stage_render_real",
                               side_effect=Exception("bail-after-lookup")):
            try:
                self.ep._main_from_firestore("delayed-job")
            except Exception:
                pass
        # Two lookups total — the retry succeeded on attempt 2.
        self.assertEqual(ref.get.call_count, 2)
        # Exactly one sleep(1.0) between attempts 1 and 2 (other sleep calls
        # may exist for unrelated worker logic; we count ours specifically).
        lookup_sleeps = [c for c in m_sleep.call_args_list if c.args == (1.0,)]
        self.assertEqual(len(lookup_sleeps), 1)


if __name__ == "__main__":
    unittest.main()
