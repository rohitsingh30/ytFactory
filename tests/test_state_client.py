"""Tests for pipeline/state_client.py — CLI + library both wire to /api/state.

We mock urllib.request.urlopen so the client thinks it's hitting the
real website. The fake server returns canned responses keyed by
(method, path).
"""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from pipeline.utils import state_client


class _FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        if isinstance(self._payload, (dict, list)):
            return json.dumps(self._payload).encode("utf-8")
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeServer:
    """Records every request the client made + returns canned data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.responses: dict[tuple[str, str], object] = {}

    def __call__(self, req, timeout=None):  # noqa: ARG002
        method = req.get_method()
        url = req.full_url
        body = req.data
        self.calls.append((method, url, body))
        key = (method, url.split("?")[0].split("/api/state/")[-1])
        # Match any path suffix — tests register by suffix.
        for (m, p), resp in self.responses.items():
            if m == method and url.endswith(p):
                if isinstance(resp, Exception):
                    raise resp
                return _FakeResponse(resp)
        # Unmatched → 404 mimicking the real route's behavior.
        import urllib.error
        raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b"not found"))


class StateClientLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _FakeServer()
        self._patch = patch("pipeline.utils.state_client.urllib.request.urlopen", self.server)
        self._patch.start()
        # Skip auth-header injection — _auth_headers returns {} for
        # localhost. The default WEBSITE_URL is the cloud one, so we
        # also patch out the gcloud token shell-out for hermetic tests.
        self._auth_patch = patch(
            "pipeline.utils.state_client._auth_headers",
            return_value={},
        )
        self._auth_patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._auth_patch.stop()

    def test_get_round_trips_json(self) -> None:
        self.server.responses[("GET", "/api/state/sportsrecapped/narrations/x")] = {
            "slug": "x", "duration_s": 55,
        }
        out = state_client.get("sportsrecapped", "narrations", "x")
        self.assertEqual(out["duration_s"], 55)
        method, url, body = self.server.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/state/sportsrecapped/narrations/x"))
        self.assertIsNone(body)

    def test_put_sends_json_body(self) -> None:
        self.server.responses[("PUT", "/api/state/historyrecapped/narrations/pompeii")] = {
            "ok": True, "uri": "gs://bucket/historyrecapped/narrations/pompeii.json",
        }
        out = state_client.put(
            "historyrecapped", "narrations", "pompeii",
            {"slug": "pompeii", "year": 79},
        )
        self.assertTrue(out["ok"])
        method, url, body = self.server.calls[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(json.loads(body), {"slug": "pompeii", "year": 79})

    def test_delete_returns_ok(self) -> None:
        self.server.responses[("DELETE", "/api/state/x/narrations/y")] = {"ok": True}
        out = state_client.delete("x", "narrations", "y")
        self.assertTrue(out["ok"])

    def test_list_returns_slug_array(self) -> None:
        self.server.responses[("GET", "/api/state/cosmosdecoded/narrations")] = {
            "channel": "cosmosdecoded",
            "kind": "narrations",
            "slugs": ["alpha", "mike", "zulu"],
        }
        out = state_client.list_slugs("cosmosdecoded", "narrations")
        self.assertEqual(out, ["alpha", "mike", "zulu"])

    def test_get_404_raises_state_not_found(self) -> None:
        # No response registered → fake server returns 404.
        with self.assertRaises(state_client.StateNotFoundError):
            state_client.get("x", "narrations", "doesnt-exist")


class StateClientCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _FakeServer()
        self._patch = patch("pipeline.utils.state_client.urllib.request.urlopen", self.server)
        self._patch.start()
        self._auth_patch = patch(
            "pipeline.utils.state_client._auth_headers",
            return_value={},
        )
        self._auth_patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._auth_patch.stop()

    def test_cli_get_prints_json_to_stdout(self) -> None:
        self.server.responses[("GET", "/api/state/x/narrations/y")] = {"hello": "world"}
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = state_client.main(["get", "x", "narrations", "y"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), {"hello": "world"})

    def test_cli_put_reads_stdin(self) -> None:
        self.server.responses[("PUT", "/api/state/x/narrations/y")] = {
            "ok": True, "uri": "gs://b/x/narrations/y.json",
        }
        with patch("sys.stdin", io.StringIO('{"a": 1}')):
            rc = state_client.main(["put", "x", "narrations", "y"])
        self.assertEqual(rc, 0)
        # Body the server saw should round-trip cleanly.
        _method, _url, body = self.server.calls[0]
        self.assertEqual(json.loads(body), {"a": 1})

    def test_cli_put_invalid_json_exits_2(self) -> None:
        with patch("sys.stdin", io.StringIO("not json")):
            rc = state_client.main(["put", "x", "narrations", "y"])
        self.assertEqual(rc, 2)

    def test_cli_get_missing_exits_4(self) -> None:
        # No response registered → server raises 404 → CLI prints "not found" and returns 4.
        rc = state_client.main(["get", "x", "narrations", "missing"])
        self.assertEqual(rc, 4)

    def test_cli_list_one_per_line(self) -> None:
        self.server.responses[("GET", "/api/state/x/narrations")] = {
            "slugs": ["a", "b"],
        }
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = state_client.main(["list", "x", "narrations"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
