"""Pin the SIGTERM handler that surfaces Cloud Run task-timeout failures
to the dashboard (B3a in 2026-05-23 docket).

Backstory: job a0aac53ea01949178afd50ac2b254ef7 hit the Cloud Run
``--task-timeout=3600`` ceiling and was SIGKILLed mid-compose. The
worker's top-level try/except (entrypoint.py:_main_from_firestore)
catches Python exceptions, but Cloud Run sends SIGTERM 10s before
SIGKILL — and Python's default SIGTERM handler is a hard exit that
bypasses the try/except. Result: Firestore left at
``status="rendering"`` forever, dashboard claims the job is "still
working" 8 hours later, no error surfaced to the user.

Fix: ``_on_sigterm`` writes ``status=failed`` to Firestore during the
10s grace window. Because gRPC reentrancy from a signal handler frame
is unsafe (the gRPC SDK holds locks that the main thread might be
mid-acquire on), the actual write runs on a daemon thread that the
handler joins with an 8s timeout (reserving 2s for log flush).
"""
from __future__ import annotations

import importlib.util
import signal
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "cloud" / "render-worker-v2" / "entrypoint.py"


def _load_entrypoint(name: str):
    spec = importlib.util.spec_from_file_location(name, ENTRYPOINT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SigtermHandlerTest(unittest.TestCase):
    """Pin the SIGTERM-induced silent-death mitigation."""

    def setUp(self):
        # Load a FRESH copy of the module per test so the global
        # ``_SIGTERM_FIRED`` / ``_LIVE_RENDER`` / ``_SIGTERM_INSTALLED``
        # state doesn't leak between tests.
        self.ep = _load_entrypoint(
            f"render_worker_v2_entrypoint_for_sigterm_{id(self)}"
        )

    def test_install_registers_handler_and_sets_job_id(self):
        with patch.object(self.ep, "signal") as fake_signal:
            fake_signal.SIGTERM = signal.SIGTERM
            self.ep._install_sigterm_handler("job-abc")
            fake_signal.signal.assert_called_once()
            args, _ = fake_signal.signal.call_args
            self.assertEqual(args[0], signal.SIGTERM)
            self.assertIs(args[1], self.ep._on_sigterm)
            self.assertTrue(self.ep._SIGTERM_INSTALLED)
            self.assertEqual(self.ep._LIVE_RENDER["job_id"], "job-abc")

    def test_install_is_idempotent(self):
        with patch.object(self.ep, "signal") as fake_signal:
            fake_signal.SIGTERM = signal.SIGTERM
            self.ep._install_sigterm_handler("job-1")
            self.ep._install_sigterm_handler("job-2")  # second call no-op
            self.assertEqual(fake_signal.signal.call_count, 1)
            # job_id still updates so the handler writes the latest job
            self.assertEqual(self.ep._LIVE_RENDER["job_id"], "job-2")

    def test_install_in_worker_thread_does_not_crash(self):
        # signal.signal in a non-main thread raises ValueError; the
        # installer must swallow it (tests + embedded use).
        with patch.object(self.ep, "signal") as fake_signal:
            fake_signal.SIGTERM = signal.SIGTERM
            fake_signal.signal.side_effect = ValueError(
                "signal only works in main thread"
            )
            # Should not raise.
            self.ep._install_sigterm_handler("job-xyz")
            self.assertFalse(self.ep._SIGTERM_INSTALLED)

    def test_update_job_snapshots_stage_and_timeline_for_handler(self):
        self.ep._LIVE_RENDER["job_id"] = "job-snap"
        captured: list[dict] = []

        class FakeRef:
            def set(self, fields, merge):  # noqa: ARG002
                captured.append(dict(fields))

        with patch.object(self.ep, "_job_ref", return_value=FakeRef()):
            self.ep._update_job(
                "job-snap",
                status="rendering",
                stage="images",
                timeline=[{"stage": "images", "status": "running"}],
            )
        self.assertEqual(self.ep._LIVE_RENDER["stage"], "images")
        self.assertEqual(
            self.ep._LIVE_RENDER["timeline"],
            [{"stage": "images", "status": "running"}],
        )

    def test_update_job_does_not_snapshot_for_other_jobs(self):
        # Multi-tenancy: progress writes for an UNRELATED job shouldn't
        # corrupt the SIGTERM state.
        self.ep._LIVE_RENDER["job_id"] = "job-real"
        self.ep._LIVE_RENDER["stage"] = "tts"

        class FakeRef:
            def set(self, fields, merge):  # noqa: ARG002
                pass

        with patch.object(self.ep, "_job_ref", return_value=FakeRef()):
            self.ep._update_job(
                "job-other",
                stage="compose",
                timeline=[{"stage": "compose", "status": "running"}],
            )
        # Untouched
        self.assertEqual(self.ep._LIVE_RENDER["stage"], "tts")

    def test_handler_writes_failed_with_timeline_and_stage(self):
        self.ep._LIVE_RENDER["job_id"] = "job-dead"
        self.ep._LIVE_RENDER["stage"] = "compose"
        self.ep._LIVE_RENDER["timeline"] = [
            {"stage": "compose", "status": "running", "msg": "seg 17/60"},
        ]

        writes: list[dict] = []

        def fake_update(job_id, **fields):
            writes.append({"job_id": job_id, **fields})

        with patch.object(self.ep, "_update_job", side_effect=fake_update), \
             patch.object(self.ep, "sys") as fake_sys:
            self.ep._on_sigterm(signal.SIGTERM, None)
            fake_sys.exit.assert_called_once_with(143)

        self.assertEqual(len(writes), 1)
        w = writes[0]
        self.assertEqual(w["job_id"], "job-dead")
        self.assertEqual(w["status"], "failed")
        self.assertEqual(w["stage"], "compose")
        self.assertIn("SIGTERM", w["error"])
        self.assertIn("task-timeout", w["error"])
        self.assertEqual(
            w["timeline"],
            [{"stage": "compose", "status": "running", "msg": "seg 17/60"}],
        )

    def test_handler_is_idempotent_within_a_run(self):
        # Multiple SIGTERMs in rapid succession (defensive) — only one
        # Firestore write.
        self.ep._LIVE_RENDER["job_id"] = "job-dupe"
        self.ep._LIVE_RENDER["stage"] = "images"

        writes: list[dict] = []

        def fake_update(job_id, **fields):
            writes.append({"job_id": job_id, **fields})

        with patch.object(self.ep, "_update_job", side_effect=fake_update), \
             patch.object(self.ep, "sys"):
            self.ep._on_sigterm(signal.SIGTERM, None)
            self.ep._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(len(writes), 1)

    def test_handler_no_op_when_job_id_unknown(self):
        # SIGTERM arrived before _install_sigterm_handler set job_id —
        # nothing to write but must not crash.
        self.ep._LIVE_RENDER["job_id"] = None

        with patch.object(self.ep, "_update_job") as fake_update, \
             patch.object(self.ep, "sys") as fake_sys:
            self.ep._on_sigterm(signal.SIGTERM, None)
            fake_update.assert_not_called()
            fake_sys.exit.assert_called_once_with(143)

    def test_handler_swallows_firestore_failure_and_still_exits(self):
        # If the Firestore write itself fails, the handler must still
        # call sys.exit so the worker doesn't hang past the SIGKILL grace.
        self.ep._LIVE_RENDER["job_id"] = "job-firestoredown"
        self.ep._LIVE_RENDER["stage"] = "rewrite"

        def boom(*_a, **_k):
            raise RuntimeError("Firestore is down")

        with patch.object(self.ep, "_update_job", side_effect=boom), \
             patch.object(self.ep, "sys") as fake_sys:
            self.ep._on_sigterm(signal.SIGTERM, None)
            fake_sys.exit.assert_called_once_with(143)

    def test_handler_completes_within_grace_window(self):
        # Pin the 10s grace contract. If the Firestore write hangs
        # forever, the handler MUST exit within ~8s rather than block
        # past SIGKILL.
        self.ep._LIVE_RENDER["job_id"] = "job-slow"
        self.ep._LIVE_RENDER["stage"] = "tts"

        def slow(*_a, **_k):
            time.sleep(30)  # would block past Cloud Run's 10s grace

        with patch.object(self.ep, "_update_job", side_effect=slow), \
             patch.object(self.ep, "sys") as fake_sys:
            t0 = time.monotonic()
            self.ep._on_sigterm(signal.SIGTERM, None)
            elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 9.5,
            f"handler took {elapsed:.1f}s — must exit within 8s join "
            f"+ overhead so SIGKILL doesn't catch us still blocking.",
        )
        fake_sys.exit.assert_called_once_with(143)


if __name__ == "__main__":
    unittest.main()
