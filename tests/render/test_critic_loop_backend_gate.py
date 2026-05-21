"""Tests for the backend gate on ``_run_critic_loop`` in both
:mod:`pipeline.render.short_engine` and
:mod:`pipeline.render.long_engine`.

Pins the contract: critic only runs when
``spec.extra["_critic_backend"] == "laptop"`` -- on cloud, the engine
returns early so we never invoke the claude CLI from Cloud Run (where
it doesn't exist).

Cloud renders are graded post-hoc by
:mod:`pipeline.critique.cloud_poller`.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock


def _mk_spec(extra: dict | None) -> mock.MagicMock:
    spec = mock.MagicMock()
    spec.extra = extra
    spec.channel = "testchannel"
    return spec


class _Base:
    """Shared test bodies across short_engine + long_engine. The two
    engines have parallel ``_run_critic_loop`` impls; same gate."""

    engine_mod = None  # set by subclasses
    runner_name = None  # name of the run function

    def _runner(self):
        return getattr(self.engine_mod, self.runner_name)

    def test_skips_when_backend_is_cloud(self):
        """No ``_critic_backend`` set -> skip (default = cloud)."""
        spec = _mk_spec(extra={})
        with mock.patch.object(self.engine_mod, "_logger") as m_log:
            self._runner()(spec, Path("/tmp/short.mp4"), Path("/tmp/work"))
        # First log call mentions "skipping"
        msg = m_log.info.call_args_list[0].args[0]
        self.assertIn("skipping", msg.lower())

    def test_skips_when_extra_is_none(self):
        spec = _mk_spec(extra=None)
        with mock.patch.object(self.engine_mod, "_logger") as m_log:
            self._runner()(spec, Path("/tmp/short.mp4"), Path("/tmp/work"))
        msg = m_log.info.call_args_list[0].args[0]
        self.assertIn("skipping", msg.lower())

    def test_runs_critic_when_backend_is_laptop(self):
        """Backend=laptop -> imports + calls critique_short."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            mp4 = work / "short.mp4"
            mp4.write_bytes(b"x")
            spec = _mk_spec(extra={"_critic_backend": "laptop",
                                   "slug": "myslug"})
            with mock.patch(
                "pipeline.llm.critic.critique_short",
                return_value={"verdict": "SHIP", "score": 8},
            ) as m_crit:
                self._runner()(spec, mp4, work)
            m_crit.assert_called_once()
            kwargs = m_crit.call_args.kwargs
            self.assertEqual(kwargs["slug"], "myslug")
            self.assertEqual(kwargs["mp4_path"], mp4)
            self.assertTrue(kwargs["out_dir"].exists())

    def test_critic_raise_is_swallowed(self):
        """Critic exception must not crash the render."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            mp4 = work / "short.mp4"
            mp4.write_bytes(b"x")
            spec = _mk_spec(extra={"_critic_backend": "laptop"})
            with mock.patch(
                "pipeline.llm.critic.critique_short",
                side_effect=RuntimeError("vision quota"),
            ):
                # Should NOT raise.
                self._runner()(spec, mp4, work)

    def test_slug_fallback_to_mp4_stem(self):
        """No explicit slug -> use mp4 filename stem."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            mp4 = work / "myrender.mp4"
            mp4.write_bytes(b"x")
            spec = _mk_spec(extra={"_critic_backend": "laptop"})
            with mock.patch(
                "pipeline.llm.critic.critique_short",
                return_value={"verdict": "SHIP", "score": 9},
            ) as m_crit:
                self._runner()(spec, mp4, work)
            self.assertEqual(m_crit.call_args.kwargs["slug"], "myrender")


class TestShortEngineCriticGate(_Base, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pipeline.render import short_engine
        cls.engine_mod = short_engine
        cls.runner_name = "_run_critic_loop"


class TestLongEngineCriticGate(_Base, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pipeline.render import long_engine
        cls.engine_mod = long_engine
        cls.runner_name = "_run_critic_loop"


if __name__ == "__main__":
    unittest.main()
