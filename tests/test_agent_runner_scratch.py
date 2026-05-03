"""Verify the agent runner's per-task scratch directory + cleanup contract."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

# Pin scratch root to a tempdir BEFORE importing the runner so its first
# _scratch_root() call uses ours.
_TMP = tempfile.mkdtemp(prefix="ytf-runner-test-")
os.environ["YTFACTORY_SCRATCH_ROOT"] = _TMP

from agent import runner  # noqa: E402
from agent.runner import TaskContext  # noqa: E402
from shared.schema import TaskEnvelope, TaskKind  # noqa: E402


def _envelope(kind: TaskKind = TaskKind.NOOP) -> TaskEnvelope:
    return TaskEnvelope(task_id="t-" + os.urandom(4).hex(), job_id="j", kind=kind)


class ScratchLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._created: list[Path] = []
        self._original_noop = runner._REGISTRY[TaskKind.NOOP]

    def tearDown(self) -> None:
        runner._REGISTRY[TaskKind.NOOP] = self._original_noop

    async def test_scratch_dir_exists_during_run(self) -> None:
        seen: dict[str, Path | bool] = {}

        async def probe(ctx: TaskContext) -> str | None:
            seen["scratch"] = ctx.scratch
            seen["exists_during_run"] = ctx.scratch.exists() and ctx.scratch.is_dir()
            (ctx.scratch / "in_run.txt").write_text("hi")
            return None

        runner.register(TaskKind.NOOP, probe)
        ok, _, err = await runner.run(_envelope())

        self.assertTrue(ok, err)
        self.assertTrue(seen["exists_during_run"])
        # Scratch removed after run.
        self.assertFalse(Path(seen["scratch"]).exists(), f"scratch should be cleaned up, but {seen['scratch']} still exists")

    async def test_scratch_cleaned_when_worker_raises(self) -> None:
        captured: dict[str, Path] = {}

        async def boom(ctx: TaskContext) -> str | None:
            captured["scratch"] = ctx.scratch
            (ctx.scratch / "left_over.txt").write_text("oops")
            raise RuntimeError("worker exploded")

        runner.register(TaskKind.NOOP, boom)
        ok, out, err = await runner.run(_envelope())

        self.assertFalse(ok)
        self.assertIsNone(out)
        assert err is not None
        self.assertIn("RuntimeError", err)
        self.assertIn("worker exploded", err)
        self.assertFalse(captured["scratch"].exists(),
                         f"scratch must be cleaned even when worker raises, but {captured['scratch']} survived")

    async def test_scratch_root_respects_env(self) -> None:
        runner.register(TaskKind.NOOP, self._original_noop)
        envelope = _envelope()

        captured: dict[str, Path] = {}

        async def capture(ctx: TaskContext) -> str | None:
            captured["scratch"] = ctx.scratch
            return "noop://x"

        runner.register(TaskKind.NOOP, capture)
        ok, _, _ = await runner.run(envelope)
        self.assertTrue(ok)
        # scratch should be inside our pinned root.
        self.assertEqual(str(captured["scratch"].parent), os.environ["YTFACTORY_SCRATCH_ROOT"])

    async def test_each_task_gets_isolated_scratch(self) -> None:
        scratches: list[Path] = []

        async def capture(ctx: TaskContext) -> str | None:
            scratches.append(ctx.scratch)
            return None

        runner.register(TaskKind.NOOP, capture)
        await asyncio.gather(runner.run(_envelope()), runner.run(_envelope()), runner.run(_envelope()))
        self.assertEqual(len(set(scratches)), 3, "each task should get its own scratch dir")


if __name__ == "__main__":
    unittest.main()
