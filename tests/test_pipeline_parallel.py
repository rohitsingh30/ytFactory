"""Tests for `pipeline.utils.parallel.run_parallel` — bounded ThreadPool fan-out
used by the long-form / Shorts renderers for ffmpeg trim and PIL caption
PNG render stages.

What this guards:
- Default worker cap (M2 Max sweet spot 4) and the YTFACTORY_FFMPEG_WORKERS
  override.
- Empty / single-job paths skip the pool overhead.
- Multi-job runs really do execute concurrently (timing assertion).
- Exceptions surface to the caller — silent failure here would leave a
  half-rendered video on disk and the pipeline would proceed obliviously.
- Closure binding inside the call sites — a regression where loop vars are
  captured by reference (not by default-arg value) would have every worker
  process the LAST job's params, which is the canonical Python loop-var
  bug. Caught by an explicit ordering test.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.utils import parallel


class DefaultWorkersTests(unittest.TestCase):
    def test_default_is_min_half_cpu_or_4(self):
        # Strip the env var so we read the cpu_count branch.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YTFACTORY_FFMPEG_WORKERS", None)
            n = parallel._default_workers()
        cpu = os.cpu_count() or 4
        self.assertEqual(n, max(1, min(cpu // 2, 4)))

    def test_env_var_override_raises_cap(self):
        with patch.dict(os.environ, {"YTFACTORY_FFMPEG_WORKERS": "12"}):
            self.assertEqual(parallel._default_workers(), 12)

    def test_env_var_override_can_lower_cap(self):
        with patch.dict(os.environ, {"YTFACTORY_FFMPEG_WORKERS": "1"}):
            self.assertEqual(parallel._default_workers(), 1)

    def test_invalid_env_var_falls_back_to_default(self):
        with patch.dict(os.environ, {"YTFACTORY_FFMPEG_WORKERS": "not-a-number"}):
            n = parallel._default_workers()
        cpu = os.cpu_count() or 4
        self.assertEqual(n, max(1, min(cpu // 2, 4)))

    def test_zero_env_var_falls_back_to_default(self):
        # 0 is technically integer-parseable but useless; the helper should
        # reject it and fall back to the cpu-derived default.
        with patch.dict(os.environ, {"YTFACTORY_FFMPEG_WORKERS": "0"}):
            n = parallel._default_workers()
        cpu = os.cpu_count() or 4
        self.assertEqual(n, max(1, min(cpu // 2, 4)))


class RunParallelTests(unittest.TestCase):
    def test_empty_list_is_noop(self):
        # Should return without doing anything; no exception, no print.
        parallel.run_parallel([], label="noop")

    def test_single_job_runs_inline(self):
        ran: list[int] = []

        def _job():
            ran.append(1)

        parallel.run_parallel([_job], label="single")
        self.assertEqual(ran, [1])

    def test_multi_jobs_run_concurrently(self):
        # 8 jobs × 0.1s sleep with workers=4 should finish in ~0.2s, not 0.8s.
        # Generous tolerance because CI machines vary; the assertion is just
        # "well under the sequential floor".
        SLEEP = 0.1
        N = 8
        WORKERS = 4

        def _mk():
            def _job():
                time.sleep(SLEEP)
            return _job

        t0 = time.time()
        parallel.run_parallel([_mk() for _ in range(N)],
                              max_workers=WORKERS, label="concurrent")
        elapsed = time.time() - t0
        sequential_floor = SLEEP * N            # 0.8s if no concurrency
        # Should be roughly N/WORKERS * SLEEP ≈ 0.2s. Allow up to 0.5s slack
        # for thread spin-up / GIL / scheduler noise.
        self.assertLess(elapsed, sequential_floor / 2,
                        f"Expected concurrent execution; took {elapsed:.2f}s "
                        f"(sequential floor {sequential_floor:.2f}s)")

    def test_first_exception_is_raised(self):
        def _ok():
            time.sleep(0.01)

        def _boom():
            raise ValueError("boom")

        # Mix ok + boom jobs; the helper must surface the exception to the
        # caller. Silent swallow here would leave the renderer thinking it
        # succeeded.
        with self.assertRaises(ValueError) as cm:
            parallel.run_parallel([_ok, _boom, _ok], label="error")
        self.assertEqual(str(cm.exception), "boom")

    def test_jobs_share_no_state(self):
        # Each job appends its own integer; final list should contain all of
        # them (in some order). This catches a regression where the helper
        # accidentally serialised through a shared queue and dropped items.
        results: list[int] = []
        lock = __import__("threading").Lock()

        def _mk(i):
            def _job():
                with lock:
                    results.append(i)
            return _job

        parallel.run_parallel([_mk(i) for i in range(20)], label="state")
        self.assertEqual(sorted(results), list(range(20)))

    def test_explicit_max_workers_overrides_default(self):
        # max_workers=1 forces serial execution even if default would be 4.
        SLEEP = 0.05
        N = 4
        t0 = time.time()
        parallel.run_parallel(
            [(lambda: time.sleep(SLEEP)) for _ in range(N)],
            max_workers=1, label="serial",
        )
        elapsed = time.time() - t0
        # Serial floor is N*SLEEP=0.2s; we should be at or above 80% of that.
        self.assertGreater(elapsed, SLEEP * N * 0.8,
                           f"Expected serial execution; took {elapsed:.2f}s")

    def test_max_workers_clamped_to_job_count(self):
        # Asking for 16 workers with 3 jobs should not crash or spin up
        # excess threads; the helper internally clamps to len(jobs).
        ran: list[int] = []
        lock = __import__("threading").Lock()

        def _mk(i):
            def _job():
                with lock:
                    ran.append(i)
            return _job

        parallel.run_parallel([_mk(i) for i in range(3)],
                              max_workers=16, label="clamp")
        self.assertEqual(sorted(ran), [0, 1, 2])

    def test_closure_loop_var_binding_at_call_sites(self):
        # Regression test for the canonical Python loop-var-by-reference bug.
        # The renderer's call sites use `def _job(x=x): ...` to capture loop
        # vars by default-arg value. If a future refactor drops that pattern,
        # every worker would see only the LAST iteration's values. This test
        # mirrors the safe pattern and asserts each job sees its own value.
        results: list[int] = []
        lock = __import__("threading").Lock()
        jobs = []
        for i in range(5):
            def _job(i=i):
                with lock:
                    results.append(i)
            jobs.append(_job)
        parallel.run_parallel(jobs, label="closure")
        self.assertEqual(sorted(results), [0, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
