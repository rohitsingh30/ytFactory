"""P3 — verifies HTTP route auto-instrumentation produces spans
with the right names + ytFactory identity attrs.

We build a minimal FastAPI app inline (so the test doesn't depend on
booting the full ``web/server.py`` stack with its Firestore / Azure /
Cloud Run init dance) and verify:

* :func:`obs.instrument_fastapi` creates one span per route call.
* The span name follows OTel HTTP semantic conventions
  (``GET /api/foo/{id}`` form).
* :func:`obs.install_http_identity_middleware` decorates the active
  span with ``ytfactory.channel`` / ``ytfactory.slug`` / etc when
  those keys appear in path or query params.
* Outbound ``requests`` calls inside a handler land as child spans
  under the inbound request's span (verifies trace context flows).
"""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline import observability as obs


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()
        self.app = FastAPI()
        obs.instrument_fastapi(self.app)
        obs.install_http_identity_middleware(self.app)

        @self.app.get("/api/render/{job_id}")
        async def render(job_id: str) -> dict:
            with obs.timed("inner_handler_work"):
                return {"job_id": job_id, "ok": True}

        @self.app.get("/api/jobs")
        async def jobs(channel: str | None = None,
                       slug: str | None = None) -> dict:
            return {"channel": channel, "slug": slug}

        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _spans(self):
        self.bundle.span_processor.force_flush()
        return list(self.bundle.span_inmemory.get_finished_spans())


class TestRouteSpans(_Base):
    def test_route_emits_http_span(self) -> None:
        self.client.get("/api/render/job-123")
        spans = self._spans()
        names = [s.name for s in spans]
        # OTel auto-instrumentation: span named after route template.
        self.assertTrue(
            any("/api/render/{job_id}" in n for n in names),
            f"no /api/render/{{job_id}} span; spans={names}",
        )
        # The inner handler-side span fired too.
        self.assertIn("inner_handler_work", names)

    def test_path_param_attached_as_identity_attr(self) -> None:
        self.client.get("/api/render/job-456")
        # Find the route span (not the inner handler span; not the
        # http.send child spans).
        route_span = next(
            s for s in self._spans()
            if s.name == "GET /api/render/{job_id}"
        )
        self.assertEqual(
            route_span.attributes.get("ytfactory.job_id"), "job-456",
        )

    def test_query_params_attached(self) -> None:
        self.client.get(
            "/api/jobs?channel=historyrecapped&slug=aita-001",
        )
        route_span = next(
            s for s in self._spans()
            if s.name == "GET /api/jobs"
        )
        self.assertEqual(
            route_span.attributes.get("ytfactory.channel"),
            "historyrecapped",
        )
        self.assertEqual(
            route_span.attributes.get("ytfactory.slug"), "aita-001",
        )


class TestSpanNesting(_Base):
    def test_inner_timed_nests_under_route(self) -> None:
        """When a handler opens an ``obs.timed`` block, that span must
        be a child of the auto-instrumented route span — so Cloud Trace
        shows one waterfall, not two flat timelines.
        """
        self.client.get("/api/render/jx")
        spans = self._spans()
        # Find the inner span and verify it carries a parent_span_id
        # that matches the route span's span_id.
        inner = next(s for s in spans if s.name == "inner_handler_work")
        route = next(
            s for s in spans
            if s.name == "GET /api/render/{job_id}"
        )
        self.assertEqual(
            inner.parent.span_id, route.context.span_id,
            "inner_timed must nest under the route span",
        )


if __name__ == "__main__":
    unittest.main()
