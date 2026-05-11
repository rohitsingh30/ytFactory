"""Tests for control/routes/burner_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx

from control.routes.burner_routes import router
from fastapi import FastAPI


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class TestListBurners(unittest.IsolatedAsyncioTestCase):
    async def test_list_empty(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[]):
            with patch("control.routes.burner_routes.catalog.catalog_count", return_value=42):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.get("/api/burner_channels")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["burners"], [])
        self.assertEqual(data["catalog_size"], 42)

    async def test_list_with_burners(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}
        state = {"phase": "liking", "last_action_at": "2026-01-01", "last_action_msg": "ok"}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.read_state",
                       return_value=state):
                with patch("control.routes.burner_routes.burner_engage.is_running",
                           return_value=True):
                    with patch("control.routes.burner_routes.burner_engage.prewarm_states") as prewarm:
                        with patch("control.routes.burner_routes.catalog.catalog_count",
                                   return_value=5):
                            async with httpx.AsyncClient(transport=transport,
                                                         base_url="http://test") as client:
                                r = await client.get("/api/burner_channels")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["burners"]), 1)
        self.assertEqual(data["burners"][0]["phase"], "liking")
        self.assertTrue(data["burners"][0]["running"])
        # The route MUST prewarm the state cache before iterating —
        # without this, each read_state would fan out to its own GCS
        # round trip and the dashboard's 5s poll would tail-latency
        # at >10s on Cloud Run.
        prewarm.assert_called_once_with(["burner1"])

    async def test_list_with_null_state(self) -> None:
        """Burner with no state (never run)."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner2", "profile_known": False}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.read_state",
                       return_value=None):
                with patch("control.routes.burner_routes.burner_engage.is_running",
                           return_value=False):
                    with patch("control.routes.burner_routes.burner_engage.prewarm_states"):
                        with patch("control.routes.burner_routes.catalog.catalog_count",
                                   return_value=0):
                            async with httpx.AsyncClient(transport=transport,
                                                         base_url="http://test") as client:
                                r = await client.get("/api/burner_channels")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["burners"][0]["phase"])


class TestGetCatalog(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_returns_rows(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        rows = [{"video_id": "abc", "title": "Test"}]
        with patch("control.routes.burner_routes.catalog.list_catalog_dicts",
                   return_value=rows):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/burner_channels/catalog")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["videos"][0]["video_id"], "abc")


class TestStartEngage(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_slug_404(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/unknown/engage")
        self.assertEqual(r.status_code, 404)

    async def test_no_profile_known_409(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": False}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 409)

    async def test_already_running(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}
        state = {"phase": "liking"}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.is_running",
                       return_value=True):
                with patch("control.routes.burner_routes.burner_engage.read_state",
                           return_value=state):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.post("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["started"])
        self.assertEqual(data["reason"], "already_running")

    async def test_spawn_new_process(self) -> None:
        """Laptop dev path (no K_SERVICE): still spawns a local subprocess."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_fp = MagicMock()

        # Force the laptop-dev branch: K_SERVICE absent.
        env_no_kservice = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        with patch.dict("os.environ", env_no_kservice, clear=True), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   return_value=False), \
             patch("control.routes.burner_routes.subprocess.Popen",
                   return_value=mock_proc), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=mock_fp):
            mock_fp.__enter__ = MagicMock(return_value=mock_fp)
            mock_fp.__exit__ = MagicMock(return_value=False)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                r = await client.post("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["started"])
        self.assertEqual(data["pid"], 12345)

    async def test_cloud_enqueues_task(self) -> None:
        """Cloud path (K_SERVICE set): enqueues a BURNER_ENGAGE task,
        does NOT spawn subprocess (Chrome can't run on Cloud Run)."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}

        captured: dict = {}

        class _FakeQueue:
            def enqueue(self, task) -> None:
                captured["task"] = task

        with patch.dict("os.environ", {"K_SERVICE": "ytfactory-web"}), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   return_value=False), \
             patch("control.routes.burner_routes.burner_engage.clear_stop_sentinel"), \
             patch("control.routes.burner_routes.subprocess.Popen") as popen, \
             patch("control.core.queue.get_queue", return_value=_FakeQueue()):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                r = await client.post("/api/burner_channels/burner1/engage")

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["started"])
        self.assertIn("task_id", data)
        self.assertTrue(data["agent_required"])
        # Critical: never spawn subprocess on Cloud Run.
        popen.assert_not_called()
        # And the enqueued task carries the slug as payload.
        from control.core.schema import TaskKind  # noqa: PLC0415
        self.assertEqual(captured["task"].kind, TaskKind.BURNER_ENGAGE)
        self.assertEqual(captured["task"].payload, {"slug": "burner1", "mode": "like_subscribe_view"})

    async def test_cloud_enqueues_task_with_mode(self) -> None:
        """Cloud path threads explicit mode through to the queued task."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}

        captured: dict = {}

        class _FakeQueue:
            def enqueue(self, task) -> None:
                captured["task"] = task

        with patch.dict("os.environ", {"K_SERVICE": "ytfactory-web"}), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   return_value=False), \
             patch("control.routes.burner_routes.burner_engage.clear_stop_sentinel"), \
             patch("control.routes.burner_routes.subprocess.Popen"), \
             patch("control.core.queue.get_queue", return_value=_FakeQueue()):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                r = await client.post(
                    "/api/burner_channels/burner1/engage",
                    json={"mode": "subscribe_only"},
                )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "subscribe_only")
        self.assertEqual(captured["task"].payload["mode"], "subscribe_only")

    async def test_cloud_rejects_unknown_mode(self) -> None:
        """Unknown mode → 400, no task enqueued."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}

        with patch.dict("os.environ", {"K_SERVICE": "ytfactory-web"}), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   return_value=False):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                r = await client.post(
                    "/api/burner_channels/burner1/engage",
                    json={"mode": "destroy_world"},
                )

        self.assertEqual(r.status_code, 400)
        self.assertIn("destroy_world", r.json()["detail"])


class TestPollEngage(unittest.IsolatedAsyncioTestCase):
    async def test_no_state_404(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.read_state",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 404)

    async def test_with_state(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        state = {"phase": "liking", "last_action_at": "2026-01-01", "last_action_msg": "ok"}
        with patch("control.routes.burner_routes.burner_engage.read_state",
                   return_value=state):
            with patch("control.routes.burner_routes.burner_engage.is_running",
                       return_value=True):
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://test") as client:
                    r = await client.get("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["phase"], "liking")
        self.assertTrue(data["running"])


class TestStopEngage(unittest.IsolatedAsyncioTestCase):
    async def test_stop_engage(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.request_stop") as mock_stop:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/burner1/engage/stop")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["stop_requested"])
        self.assertEqual(data["slug"], "burner1")
        mock_stop.assert_called_once_with("burner1")


class TestSubscribeAllBurners(unittest.IsolatedAsyncioTestCase):
    async def test_no_burners(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        env = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        with patch.dict("os.environ", env, clear=True), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/subscribe_all_burners")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enqueued_count"], 0)
        self.assertEqual(data["skipped_count"], 0)
        self.assertIn("hint", data)
        self.assertIn("no burners registered", data["hint"])

    async def test_skips_no_profile_and_running(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burners = [
            {"slug": "noprof", "profile_known": False},
            {"slug": "active", "profile_known": True},
        ]

        def _is_running(slug: str, **kw) -> bool:  # noqa: ARG001
            return slug == "active"

        env = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        with patch.dict("os.environ", env, clear=True), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=burners), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   side_effect=_is_running):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/subscribe_all_burners")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enqueued_count"], 0)
        self.assertEqual(data["skipped_count"], 2)
        reasons = sorted(s["reason"] for s in data["skipped"])
        self.assertEqual(reasons, ["already_running", "no_profile_mapping"])

    async def test_cloud_enqueues_one_subscribe_only_task_per_eligible(self) -> None:
        """Each eligible burner gets its own BURNER_ENGAGE task with mode=subscribe_only."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burners = [
            {"slug": "a", "profile_known": True},
            {"slug": "b", "profile_known": True},
            {"slug": "c", "profile_known": False},  # skipped
        ]
        captured: list = []

        class _FakeQueue:
            def enqueue(self, task) -> None:
                captured.append(task)

        with patch.dict("os.environ", {"K_SERVICE": "ytfactory-web"}), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=burners), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   return_value=False), \
             patch("control.routes.burner_routes.burner_engage.clear_stop_sentinel"), \
             patch("control.core.queue.get_queue", return_value=_FakeQueue()):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/subscribe_all_burners")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enqueued_count"], 2)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(sorted(e["slug"] for e in data["enqueued"]), ["a", "b"])
        from control.core.schema import TaskKind  # noqa: PLC0415
        for task in captured:
            self.assertEqual(task.kind, TaskKind.BURNER_ENGAGE)
            self.assertEqual(task.payload["mode"], "subscribe_only")

    async def test_laptop_dev_spawns_subprocess_per_eligible(self) -> None:
        """Laptop dev path: one detached Popen per eligible burner."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burners = [
            {"slug": "a", "profile_known": True},
            {"slug": "b", "profile_known": True},
        ]
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_fp = MagicMock()
        mock_fp.__enter__ = MagicMock(return_value=mock_fp)
        mock_fp.__exit__ = MagicMock(return_value=False)

        env_no_kservice = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        with patch.dict("os.environ", env_no_kservice, clear=True), \
             patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=burners), \
             patch("control.routes.burner_routes.burner_engage.is_running",
                   return_value=False), \
             patch("control.routes.burner_routes.subprocess.Popen",
                   return_value=mock_proc) as popen, \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=mock_fp):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/subscribe_all_burners")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enqueued_count"], 2)
        self.assertEqual(popen.call_count, 2)
        # Every spawn must be `subscribe_only` mode.
        for call in popen.call_args_list:
            cmd = call.args[0]
            self.assertIn("--mode", cmd)
            self.assertEqual(cmd[cmd.index("--mode") + 1], "subscribe_only")


class TestCreateBulk(unittest.IsolatedAsyncioTestCase):
    async def test_bad_count_400(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/burner_channels/create_bulk", json={"count": "not-an-int"}
            )
        self.assertEqual(r.status_code, 400)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/burner_channels/create_bulk", json={"count": 0})
        self.assertEqual(r.status_code, 400)

    async def test_default_count_caps_at_hard_max(self) -> None:
        """Asking for 500 → capped to BULK_CREATE_HARD_MAX with cap_applied=True."""
        from control.routes.burner_routes import BULK_CREATE_HARD_MAX  # noqa: PLC0415
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        captured: list = []

        class _FakeQueue:
            def enqueue(self, task) -> None:
                captured.append(task)

        with patch.dict("os.environ", {"K_SERVICE": "ytfactory-web"}), \
             patch("control.core.queue.get_queue", return_value=_FakeQueue()):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/api/burner_channels/create_bulk", json={"count": 500},
                )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["cap_applied"])
        self.assertEqual(data["enqueued_count"], BULK_CREATE_HARD_MAX)
        self.assertEqual(len(captured), BULK_CREATE_HARD_MAX)
        from control.core.schema import TaskKind  # noqa: PLC0415
        self.assertTrue(all(t.kind == TaskKind.CREATE_BURNER for t in captured))

    async def test_cloud_enqueues_create_burner_tasks(self) -> None:
        """Default count, cloud path: 50 CREATE_BURNER tasks; payload carries email/oauth."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        captured: list = []

        class _FakeQueue:
            def enqueue(self, task) -> None:
                captured.append(task)

        with patch.dict("os.environ", {"K_SERVICE": "ytfactory-web"}), \
             patch("control.core.queue.get_queue", return_value=_FakeQueue()):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/api/burner_channels/create_bulk",
                    json={"count": 3, "email": "rs54@gmail.com", "oauth": False},
                )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enqueued_count"], 3)
        self.assertFalse(data["cap_applied"])
        self.assertEqual(len(captured), 3)
        from control.core.schema import TaskKind  # noqa: PLC0415
        for task in captured:
            self.assertEqual(task.kind, TaskKind.CREATE_BURNER)
            self.assertEqual(task.payload, {"oauth": False, "email": "rs54@gmail.com"})

    async def test_laptop_dev_spawns_create_burner_subprocesses(self) -> None:
        """Laptop dev path: subprocess.Popen × count."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_proc = MagicMock()
        mock_proc.pid = 7777
        mock_fp = MagicMock()
        mock_fp.__enter__ = MagicMock(return_value=mock_fp)
        mock_fp.__exit__ = MagicMock(return_value=False)
        env_no_kservice = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        with patch.dict("os.environ", env_no_kservice, clear=True), \
             patch("control.routes.burner_routes.subprocess.Popen",
                   return_value=mock_proc) as popen, \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=mock_fp):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/api/burner_channels/create_bulk", json={"count": 4},
                )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enqueued_count"], 4)
        self.assertEqual(popen.call_count, 4)
        # Every spawn should be the create_burner_channel CLI.
        for call in popen.call_args_list:
            cmd = call.args[0]
            self.assertIn("pipeline.cross_engage.create_burner_channel", cmd)


if __name__ == "__main__":
    unittest.main()
